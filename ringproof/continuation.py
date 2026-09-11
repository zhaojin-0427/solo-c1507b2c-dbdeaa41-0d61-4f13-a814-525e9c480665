"""前缀续接（continuation）搜索。

在校核通过的 row 前缀之后，搜索由若干完整 lead（方法 × call）组成的
续接尾段：尾段不得与前缀 row、也不得与尾段自身 row 重复（到达目标 row
的最后一 row 除外），并满足 call 数上限、方法配额与相邻方法转换规则
（含前缀末 lead 方法 → 尾段首 lead 方法的边界转换）。

搜索为固定分支顺序的深度优先枚举（方法按声明顺序、call 按 plain 优先、
再按名称排序），因此对同一前缀与约束结果完全确定、可缓存。命中目标的
方案按：尾段长度（lead 数，再 change 数）→ call 数 → 拼接数 →
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
    """一个续接方案的紧凑表示：逐 lead 的 (方法 id, call) 序列。"""

    choices: list[tuple[str, str | None]]
    calls_used: int
    splices: int
    num_changes: int
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
    music_rules: list[dict] | None = None,
    music_flags: tuple[bool, bool] = (False, False),
    min_music_score: int | None = None,
    min_music_hits: int | None = None,
    prefix_changes: int = 0,
) -> dict:
    """在前缀之后搜索续接尾段。结果对同一输入完全确定。"""
    methods = ctx["method_ids"]
    call_opts = _call_options(ctx)
    prefix_len = len(prefix_methods)

    # (方法, call, 起始 row) → 该 lead 逐 row（lead 展开只依赖方法/call）
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

    pruned_quota = 0
    pruned_transition = 0
    pruned_repeat = 0
    filtered_calls = 0
    filtered_music = 0
    checked = 0
    truncated = False
    truncation_reason: str | None = None
    found: list[TailSpec] = []

    # 前缀当前排列已等于目标：空尾段本身就是一个方案（仍须满足尾段配额）
    def _zero_spec() -> TailSpec:
        return TailSpec(
            choices=[],
            calls_used=0,
            splices=0,
            num_changes=0,
            quota_remaining=_quota_remaining(quotas, {}),
        )

    if start_row == target and all(
        (q.get("min") is None or q.get("min") == 0) for q in quotas.values()
    ):
        zero = _zero_spec()
        if music_rules is None:
            found.append(zero)
        else:
            scored = score_rows(
                [row_to_string(start_row)], [], music_rules, *music_flags
            )
            zero.music_score = scored["total_score"]
            zero.music_hits = scored["total_hits"]
            if (
                min_music_score is not None
                and scored["total_score"] < min_music_score
            ) or (
                min_music_hits is not None
                and scored["total_hits"] < min_music_hits
            ):
                filtered_music += 1
            else:
                found.append(zero)

    deepest = {
        "leads": 0,
        "row": row_to_string(start_row),
        "choice_path": [],
    }

    def quota_possible(counts: dict[str, int], k: int) -> bool:
        """已用 k 个 lead 时配额未超 max 且 min 在剩余槽位内仍可达。"""
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

    def sort_key(spec: TailSpec):
        return (
            len(spec.choices),
            spec.num_changes,
            spec.calls_used,
            spec.splices,
            -spec.music_score,
            [(m, c or "") for m, c in spec.choices],
        )

    def collect(spec: TailSpec) -> bool:
        """评分、门槛过滤并维护最优 max_results 个方案；返回是否保留。"""
        nonlocal filtered_music
        if music_rules is not None:
            rows, events = materialize(spec.choices)
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

    def materialize(choices):
        """重放尾段，返回 (逐 change row 字符串, 全局编号 events)。"""
        rows: list[str] = []
        events: list[dict] = []
        cur = start_row
        for off, (method_id, call) in enumerate(choices):
            lead_no = prefix_len + off + 1
            lead = _build_lead(ctx, method_id, call)
            for pos, (token, places) in enumerate(
                zip(lead.tokens, lead.changes), 1
            ):
                cur = apply_change(cur, places)
                events.append(
                    {
                        "change": prefix_changes + len(rows) + 1,
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
                        "row": row_to_string(cur),
                    }
                )
                rows.append(row_to_string(cur))
        return rows, events

    def dfs(
        row: tuple[int, ...],
        last_method: str | None,
        boundary_last: str | None,
        counts: dict[str, int],
        used: frozenset[tuple[int, ...]],
        path: list[tuple[str, str | None]],
        calls_used: int,
        splices: int,
        num_changes: int,
    ) -> None:
        """``boundary_last`` 非 None 表示当前处于前缀边界（即将放尾段首 lead），
        其值为前缀末 lead 方法（空前缀时为 None，边界不约束）。"""
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

        if k > 0 and row == target:
            if quotas_final(counts):
                collect(
                    TailSpec(
                        choices=list(path),
                        calls_used=calls_used,
                        splices=splices,
                        num_changes=num_changes,
                        quota_remaining=_quota_remaining(quotas, counts),
                    )
                )
            return  # 到达目标即终止该路径

        if k >= max_leads:
            return
        if not quota_possible(counts, k):
            pruned_quota += 1
            return

        for method_id in methods:
            # 相邻方法转换：尾段内部看 last_method；尾段首 lead 看前缀末 lead
            prev_method = boundary_last if boundary_last is not None else last_method
            if prev_method is not None:
                ok, _ = transition_ok(
                    prev_method, method_id, allowed, forbidden, strict_allowed=True
                )
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

                # 重复校核：尾段 row 不得撞前缀或尾段自身；仅末 row 到目标豁免
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
                if prev_method is not None and prev_method != method_id:
                    ns += 1

                new_path = path + [(method_id, call)]
                new_used = used | frozenset(add_rows) | {end_row}

                if end_row == target:
                    if quotas_final(new_counts):
                        collect(
                            TailSpec(
                                choices=new_path,
                                calls_used=calls_used + call_inc,
                                splices=ns,
                                num_changes=num_changes + nchanges,
                                quota_remaining=_quota_remaining(quotas, new_counts),
                            )
                        )
                    continue

                if not quota_possible(new_counts, k + 1):
                    pruned_quota += 1
                    continue
                dfs(
                    end_row,
                    method_id,
                    None,
                    new_counts,
                    new_used,
                    new_path,
                    calls_used + call_inc,
                    ns,
                    num_changes + nchanges,
                )

    dfs(
        start_row,
        None,
        prefix_methods[-1] if prefix_methods else None,
        {},
        frozenset(seen_rows),
        [],
        0,
        0,
        0,
    )

    results = []
    for spec in found:
        rows, events = materialize(spec.choices)
        method_counts: dict[str, int] = {}
        for m, _ in spec.choices:
            method_counts[m] = method_counts.get(m, 0) + 1
        splice_leads = set()
        chain = (prefix_methods + [m for m, _ in spec.choices])
        for i in range(1, len(chain)):
            if chain[i] != chain[i - 1]:
                splice_leads.add(i + 1)
        for ev in events:
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
                "num_leads": len(spec.choices),
                "num_changes": spec.num_changes,
                "num_calls": spec.calls_used,
                "num_splices": spec.splices,
                "method_counts": method_counts,
                "quota_remaining": spec.quota_remaining,
                "reached_target": True,
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
        "pruned_by_quota": pruned_quota,
        "pruned_by_transition": pruned_transition,
        "pruned_by_repeat": pruned_repeat,
        "filtered_by_max_calls": filtered_calls,
        "filtered_by_music": filtered_music,
        "deepest_progress": deepest,
        "results": results,
    }
