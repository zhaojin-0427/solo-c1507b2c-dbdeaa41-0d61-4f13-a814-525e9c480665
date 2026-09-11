"""前缀续接（continuation）搜索。

在校核通过的 row 前缀（可止于 lead 中途）之后搜索续接尾段：

- 若前缀末 lead 只敲了一部分，先按该 lead 的方法与 call **强制敲完剩余
  change**（强制段不参与选择），到达一个 lead end，再扩展完整 lead；
- 每个完整 lead 由（方法 × call）组合产生；尾段 row 不得与前缀 row、
  强制段 row 或尾段自身 row 重复（到达目标 row 的末 row 除外）；
- 满足 call 数上限、方法配额与相邻方法转换规则（含前缀末 lead 方法 →
  尾段首 lead 方法的边界；同方法延续始终允许，白名单只约束跨方法转换）。

搜索为固定分支顺序的深度优先枚举（方法按声明顺序、call 按 plain 优先、
再按名称排序），对同一前缀与约束结果完全确定、可缓存。**每个被访问且
约束满足的 lead end 都会成为一个候选方案**（无论是否到达目标）；候选按
到达目标 → 尾段长度（lead 数，再 change 数）→ call 数 → 拼接数 →
音乐分（高者优先）→ 确定性字典序 稳定排序。无解或预算耗尽时返回最深
进度、已检查状态数与原因。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .engine import ExpandedLead, apply_change, build_lead, transition_ok
from .music import score_rows
from .notation import row_to_string


@dataclass
class TailSpec:
    """一个续接方案：强制段 + 逐完整 lead 的 (方法 id, call) 序列。"""

    choices: list[tuple[str, str | None]]
    calls_used: int
    splices: int
    num_changes: int
    forced_changes: int = 0
    reached_target: bool = False
    quota_remaining: dict[str, dict] = field(default_factory=dict)
    music_score: int = 0
    music_hits: int = 0


def _build_lead(ctx: dict, method_id: str, call: str | None) -> ExpandedLead:
    m = ctx["method_by_id"][method_id]
    return build_lead(
        *ctx["parsed"][method_id],
        call,
        ctx["call_defs"],
        method_id=method_id,
        method_version=m["version"],
        method_name=m["name"],
    )


def _lead_rows(
    start: tuple[int, ...], lead: ExpandedLead
) -> tuple[list[tuple[int, ...]], tuple[int, ...]]:
    """施加一个 lead，返回 (逐 change 产生的 row, lead end row)。"""
    cur = start
    rows: list[tuple[int, ...]] = []
    for places in lead.changes:
        cur = apply_change(cur, places)
        rows.append(cur)
    return rows, cur


def _call_options(ctx: dict) -> list[str | None]:
    """固定分支顺序：plain 优先，其余 call 按名称排序。"""
    return [None] + sorted(ctx["call_defs"])


def _quota_remaining(quotas: dict[str, dict], counts: dict[str, int]) -> dict[str, dict]:
    out = {}
    for mid, q in quotas.items():
        used = counts.get(mid, 0)
        out[mid] = {
            "used": used,
            "min": q.get("min"),
            "max": q.get("max"),
            "remaining_min": None if q.get("min") is None else max(0, q["min"] - used),
            "remaining_max": None if q.get("max") is None else max(0, q["max"] - used),
        }
    return out


def search_continuations(
    ctx: dict,
    start_row: tuple[int, ...],
    seen_rows: set[tuple[int, ...]],
    prefix_methods: list[str],
    *,
    max_leads: int,
    target: tuple[int, ...],
    max_calls: int | None,
    max_results: int,
    max_search: int,
    quotas: dict[str, dict],
    allowed: set[tuple[str, str]] | None,
    forbidden: set[tuple[str, str]],
    partial: dict | None = None,
    music_rules: list[dict] | None = None,
    music_flags: tuple[bool, bool] = (False, False),
    min_music_score: int | None = None,
    min_music_hits: int | None = None,
    prefix_changes: int = 0,
) -> dict:
    """在前缀之后搜索续接尾段。结果对同一输入完全确定。

    ``partial`` 非 None 时形如 ``{"method_id", "method_version", "method_name",
    "call", "skip", "tokens", "changes"}``：前缀在该 lead 内已敲 skip 个
    change，搜索先用 **前缀冻结时的记号**（tokens/changes 已按保存的 call
    定义展开，不受续接请求中同名 call 覆盖影响）强制施加剩余 change。
    """
    methods = ctx["method_ids"]
    call_opts = _call_options(ctx)
    prefix_len = len(prefix_methods)

    outcome_cache: dict[
        tuple[str, str | None, tuple[int, ...]],
        tuple[list[tuple[int, ...]], int],
    ] = {}

    def outcome(method_id: str, call: str | None, row: tuple[int, ...]):
        key = (method_id, call, row)
        cached = outcome_cache.get(key)
        if cached is not None:
            return cached
        lead = _build_lead(ctx, method_id, call)
        produced, end_row = _lead_rows(row, lead)
        res = (produced, len(lead.changes))
        outcome_cache[key] = res
        return res

    # ---------- 强制敲完部分 lead 的剩余 change（沿用前缀冻结记号） ----------
    forced = None
    forced_error: str | None = None
    forced_seen: frozenset[tuple[int, ...]] = frozenset()
    used0 = frozenset(seen_rows)
    if partial is not None:
        mid = partial["method_id"]
        call = partial["call"]
        frozen_lead = ExpandedLead(
            call=call,
            tokens=list(partial["tokens"]),
            changes=[frozenset(p) for p in partial["changes"]],
            method_id=mid,
            method_version=partial.get("method_version"),
            method_name=partial.get("method_name"),
        )
        skip = partial["skip"]
        rest = frozen_lead.changes[skip:]
        cur = start_row
        forced_rows: list[tuple[int, ...]] = []
        for places in rest:
            cur = apply_change(cur, places)
            forced_rows.append(cur)
        forced = {
            "method_id": mid,
            "method_version": frozen_lead.method_version,
            "method_name": frozen_lead.method_name,
            "call": call,
            "skip": skip,
            "lead": frozen_lead,
            "rows": forced_rows,
            "end_row": cur,
            "num_changes": len(rest),
        }
        # 强制段（末 row 到目标豁免）不得引入重复
        end_row = forced_rows[-1] if forced_rows else start_row
        collision = False
        local: list[tuple[int, ...]] = []
        for i, r in enumerate(forced_rows):
            is_end = i == len(forced_rows) - 1
            if r in used0 or r in local:
                if not (is_end and r == target):
                    collision = True
                    break
            local.append(r)
        if collision:
            forced_error = "部分 lead 剩余 change 与前缀或自身重复，无法续接"
        else:
            forced_seen = used0 | frozenset(forced_rows)

    pruned_quota = 0
    pruned_transition = 0
    pruned_repeat = 0
    filtered_calls = 0
    filtered_music = 0
    checked = 0
    truncated = False
    truncation_reason: str | None = None

    found: list[TailSpec] = []

    def sort_key(spec: TailSpec):
        # 到达目标 → 尾段完整 lead 数 → call 数 → 拼接数 → 总 change 数 →
        # 音乐分（高者优先）→ 确定性字典序；forced 段长度在同 lead 数内优先
        return (
            not spec.reached_target,
            len(spec.choices),
            spec.calls_used,
            spec.splices,
            spec.forced_changes,
            spec.num_changes,
            -spec.music_score,
            [(m, c or "") for m, c in spec.choices],
        )

    def materialize(spec: TailSpec):
        """重放 强制段 + 尾段，返回 (逐 change row 字符串, 全局编号 events)。"""
        rows: list[str] = []
        events: list[dict] = []
        cur = start_row
        change_counter = 0

        if forced is not None:
            full_lead = forced["lead"]
            for off, places in enumerate(full_lead.changes[forced["skip"] :]):
                pos = forced["skip"] + off + 1
                cur = apply_change(cur, places)
                change_counter += 1
                token = full_lead.tokens[forced["skip"] + off]
                call = forced["call"]
                events.append(
                    {
                        "change": prefix_changes + change_counter,
                        "lead": prefix_len,
                        "change_in_lead": pos,
                        "notation": token,
                        "places": sorted(places),
                        "source": "method" if call is None else f"call:{call}",
                        "call": call,
                        "method": full_lead.method_name,
                        "method_id": forced["method_id"],
                        "method_version": full_lead.method_version,
                        "splice": False,
                        "forced_remainder": True,
                        "row": row_to_string(cur),
                    }
                )
                rows.append(row_to_string(cur))

        for off2, (method_id, call) in enumerate(spec.choices):
            lead_no = prefix_len + off2 + 1
            lead = _build_lead(ctx, method_id, call)
            for pos, (token, places) in enumerate(
                zip(lead.tokens, lead.changes), 1
            ):
                cur = apply_change(cur, places)
                change_counter += 1
                events.append(
                    {
                        "change": prefix_changes + change_counter,
                        "lead": lead_no,
                        "change_in_lead": pos,
                        "notation": token,
                        "places": sorted(places),
                        "source": "method" if call is None else f"call:{call}",
                        "call": call,
                        "method": lead.method_name,
                        "method_id": method_id,
                        "method_version": lead.method_version,
                        "splice": False,
                        "forced_remainder": False,
                        "row": row_to_string(cur),
                    }
                )
                rows.append(row_to_string(cur))
        return rows, events

    def collect(spec: TailSpec) -> bool:
        """评分、门槛过滤并维护最优 max_results 个候选；返回是否保留。"""
        nonlocal filtered_music
        if music_rules is not None:
            rows, events = materialize(spec)
            scored = score_rows(
                [row_to_string(start_row)] + rows,
                events,
                music_rules,
                *music_flags,
            )
            spec.music_score = scored["total_score"]
            spec.music_hits = scored["total_hits"]
            if (
                min_music_score is not None
                and scored["total_score"] < min_music_score
            ) or (
                min_music_hits is not None
                and scored["total_hits"] < min_music_hits
            ):
                filtered_music += 1
                return False
        found.append(spec)
        found.sort(key=sort_key)
        if len(found) > max_results:
            found.pop()
        return True

    def quota_possible(counts: dict[str, int], k: int) -> bool:
        """已用 k 个完整 lead 时配额未超 max 且 min 在剩余槽位内仍可达。"""
        remaining_slots = max_leads - k
        for mid, q in quotas.items():
            used = counts.get(mid, 0)
            hi, lo = q.get("max"), q.get("min")
            if hi is not None and used > hi:
                return False
            if lo is not None and used + remaining_slots < lo:
                return False
        return True

    def quotas_final(counts: dict[str, int]) -> bool:
        return all(
            (q.get("min") is None or counts.get(mid, 0) >= q["min"])
            and (q.get("max") is None or counts.get(mid, 0) <= q["max"])
            for mid, q in quotas.items()
        )

    deepest = {
        "leads": 0,
        "row": row_to_string(start_row),
        "choice_path": [],
    }

    def emit(counts: dict[str, int], spec: TailSpec) -> None:
        """到目标的候选须满足最终配额（min 与 max）；未到目标候选只需当前
        未超 max——min 不可达只剪枝后续扩展，不隐藏当前合法节点。"""
        over_max = any(
            q.get("max") is not None and counts.get(mid, 0) > q["max"]
            for mid, q in quotas.items()
        )
        if over_max:
            return
        if spec.reached_target and not quotas_final(counts):
            return
        collect(spec)

    def dfs(
        row: tuple[int, ...],
        last_method: str | None,
        counts: dict[str, int],
        used: frozenset[tuple[int, ...]],
        path: list[tuple[str, str | None]],
        calls_used: int,
        splices: int,
        num_changes: int,
        is_forced_end: bool = False,
    ) -> None:
        nonlocal checked, pruned_quota, pruned_transition, pruned_repeat
        nonlocal filtered_calls, truncated, truncation_reason
        if truncated:
            return
        if checked >= max_search:
            truncated = True
            truncation_reason = f"达到搜索上限 max_search={max_search}"
            return

        checked += 1
        k = len(path)
        if k > deepest["leads"]:
            deepest["leads"] = k
            deepest["row"] = row_to_string(row)
            deepest["choice_path"] = [
                {"lead": prefix_len + i + 1, "method": m, "call": c or "plain"}
                for i, (m, c) in enumerate(path)
            ]

        # 每个合法 lead end 都是候选（强制段终点或完整 lead 之后）
        is_candidate = is_forced_end or k > 0
        if is_candidate:
            spec = TailSpec(
                choices=list(path),
                calls_used=calls_used,
                splices=splices,
                num_changes=num_changes,
                forced_changes=(forced["num_changes"] if forced else 0),
                reached_target=(row == target),
                quota_remaining=_quota_remaining(quotas, counts),
            )
            emit(counts, spec)

        # 到达目标即终止该路径（目标已作为候选收集）
        if row == target and (is_forced_end or k > 0):
            return
        if k >= max_leads:
            return
        # min 配额在剩余槽位内已不可达时，不再向下扩展（当前候选仍保留）
        if not quota_possible(counts, k):
            pruned_quota += 1
            return

        for method_id in methods:
            # 相邻方法转换：同方法延续始终允许；白名单只约束跨方法转换
            if last_method is not None:
                ok, _ = transition_ok(last_method, method_id, allowed, forbidden)
                if not ok:
                    pruned_transition += 1
                    continue
            for call in call_opts:
                if truncated:
                    return
                call_inc = 1 if call else 0
                if max_calls is not None and calls_used + call_inc > max_calls:
                    filtered_calls += 1
                    continue
                produced, nchanges = outcome(method_id, call, row)
                end_row = produced[-1]

                bad = False
                add_rows: list[tuple[int, ...]] = []
                for r in produced[:-1]:
                    if r in used or r in add_rows:
                        bad = True
                        break
                    add_rows.append(r)
                if not bad and (end_row in used or end_row in add_rows):
                    if end_row != target:
                        bad = True
                if bad:
                    pruned_repeat += 1
                    continue

                new_counts = dict(counts)
                new_counts[method_id] = new_counts.get(method_id, 0) + 1
                ns = splices
                if last_method is not None and last_method != method_id:
                    ns += 1

                new_path = path + [(method_id, call)]
                new_used = used | frozenset(add_rows) | {end_row}

                if not quota_possible(new_counts, k + 1):
                    pruned_quota += 1
                    continue
                dfs(
                    end_row,
                    method_id,
                    new_counts,
                    new_used,
                    new_path,
                    calls_used + call_inc,
                    ns,
                    num_changes + nchanges,
                )

    if forced_error is None:
        if forced is not None:
            fmid = forced["method_id"]
            base_counts = {fmid: 0}  # 强制敲完的部分 lead 不占尾段配额
            dfs(
                forced["end_row"],
                fmid,
                dict(base_counts),
                forced_seen,
                [],
                0,
                0,
                forced["num_changes"],
                is_forced_end=True,
            )
        else:
            # 前缀当前排列已等于目标：空尾段（0 个完整 lead）也是候选
            if start_row == target and quotas_final({}):
                zero = TailSpec(
                    choices=[],
                    calls_used=0,
                    splices=0,
                    num_changes=0,
                    reached_target=True,
                    quota_remaining=_quota_remaining(quotas, {}),
                )
                collect(zero)
            # 第一个尾段 lead 与前缀末 lead 之间同样做转换判断与拼接计数
            boundary_last = prefix_methods[-1] if prefix_methods else None
            dfs(
                start_row, boundary_last, {}, frozenset(seen_rows),
                [], 0, 0, 0,
            )

    results = []
    for spec in found:
        rows, events = materialize(spec)
        method_counts: dict[str, int] = {}
        for m, _ in spec.choices:
            method_counts[m] = method_counts.get(m, 0) + 1
        splice_leads = set()
        chain = list(prefix_methods) + [m for m, _ in spec.choices]
        for i in range(1, len(chain)):
            if chain[i] != chain[i - 1]:
                splice_leads.add(i + 1)
        for ev in events:
            if not ev.get("forced_remainder"):
                ev["splice"] = ev["lead"] in splice_leads
        results.append(
            {
                "leads": [
                    {
                        "lead": prefix_len + i + 1,
                        "method": m,
                        "method_version": ctx["method_by_id"][m]["version"],
                        "call": c or "plain",
                    }
                    for i, (m, c) in enumerate(spec.choices)
                ],
                "forced_remainder": (
                    {
                        "lead": prefix_len,
                        "method": forced["method_id"],
                        "method_version": ctx["method_by_id"][forced["method_id"]]["version"],
                        "call": forced["call"] or "plain",
                        "remaining_changes": forced["num_changes"],
                        "end_row": row_to_string(forced["end_row"]),
                    }
                    if forced is not None
                    else None
                ),
                "num_leads": len(spec.choices),
                "num_changes": spec.num_changes,
                "num_calls": spec.calls_used,
                "num_splices": spec.splices,
                "method_counts": method_counts,
                "quota_remaining": spec.quota_remaining,
                "reached_target": spec.reached_target,
                "target_row": row_to_string(target),
                "music_score": spec.music_score,
                "music_hits": spec.music_hits,
                "rows": rows,
                "events": events,
            }
        )

    return {
        "checked_states": checked,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "solutions_found": len(results),
        "forced_remainder_impossible": forced_error,
        "pruned_by_quota": pruned_quota,
        "pruned_by_transition": pruned_transition,
        "pruned_by_repeat": pruned_repeat,
        "filtered_by_max_calls": filtered_calls,
        "filtered_by_music": filtered_music,
        "deepest_progress": deepest,
        "results": results,
    }
