"""Row 生成、touch 展开与序列证明。

证明内容：
- 排列完整性：每个 row 都是 1..stage 的完整排列；
- 记号一致性：每个 row 都能由上一 row 施加对应 change 的 places 重新推出；
- rounds 回归：记录所有回到 rounds 的 change 位置；
- 重复检测：定位首次重复 row 的两个 change，并给出 method / lead / call 来源；
- 多方法拼接：逐 row 标明方法 id/版本，统计各方法用量、拼接次数与连续段，
  并校核各方法配额（min/max）与相邻转换规则（allow/forbid）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .notation import row_to_string


class TouchError(ValueError):
    """touch 结构非法。``code`` 为机器可读错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def apply_change(row: tuple[int, ...], places: frozenset[int]) -> tuple[int, ...]:
    """对 row 施加一个 change：place 位置不动，其余位置按相邻对互换。"""
    out = list(row)
    i, stage = 0, len(row)
    while i < stage:
        if (i + 1) in places:
            i += 1
        else:
            out[i], out[i + 1] = out[i + 1], out[i]
            i += 2
    return tuple(out)


@dataclass
class ExpandedLead:
    """一个 lead 展开后的 change 序列；call 为 None 表示 plain lead。

    ``method_id``/``method_version``/``method_name`` 标识该 lead 实际使用的方法
    （多方法拼接时逐 lead 可能不同）。
    """

    call: str | None
    tokens: list[str]
    changes: list[frozenset[int]]
    method_id: str | None = None
    method_version: int | None = None
    method_name: str | None = None


def build_lead(
    method_tokens: list[str],
    method_changes: list[frozenset[int]],
    call_name: str | None,
    call_defs: dict[str, dict],
    method_id: str | None = None,
    method_version: int | None = None,
    method_name: str | None = None,
) -> ExpandedLead:
    """生成一个 lead；call 在 lead end 处替换方法末尾 ``replace`` 个 change。"""
    if call_name is None or call_name == "plain":
        return ExpandedLead(
            None, list(method_tokens), list(method_changes),
            method_id, method_version, method_name,
        )
    if call_name not in call_defs:
        raise TouchError("UNKNOWN_CALL", f"未定义的 call: {call_name!r}")
    spec = call_defs[call_name]
    k = spec["replace"]
    if not 1 <= k <= len(method_tokens):
        raise TouchError(
            "CALL_REPLACE_RANGE",
            f"call {call_name!r} 替换 {k} 个 change，超出 lead 长度 {len(method_tokens)}",
        )
    return ExpandedLead(
        call_name,
        list(method_tokens[:-k]) + list(spec["tokens"]),
        list(method_changes[:-k]) + list(spec["changes"]),
        method_id,
        method_version,
        method_name,
    )


def adjacent_places(prev: tuple[int, ...], row: tuple[int, ...]) -> frozenset[int] | None:
    """若 row 由 prev 经过一次合法 change（每个非 place 位置与相邻位置互换）
    得到，返回该 change 的 places 集合；否则返回 None。"""
    n = len(prev)
    if len(row) != n:
        return None
    places: list[int] = []
    i = 0
    while i < n:
        if prev[i] == row[i]:
            places.append(i + 1)
            i += 1
        elif i + 1 < n and prev[i] == row[i + 1] and prev[i + 1] == row[i]:
            i += 2
        else:
            return None
    return frozenset(places)


def transition_ok(
    from_id: str,
    to_id: str,
    allowed: set[tuple[str, str]] | None,
    forbidden: set[tuple[str, str]],
    strict_allowed: bool = False,
) -> tuple[bool, str | None]:
    """检查相邻 lead 的方法转换是否合法。同方法延续默认始终允许。

    - 黑名单优先：显式列入 forbidden 的转换（含同方法）一律拒绝；
    - 白名单非空时，跨方法转换须在白名单内；
    - ``strict_allowed`` 为真时（前缀续接搜索），同方法延续也须显式列入
      白名单，即白名单是转换的完整枚举。
    """
    if (from_id, to_id) in forbidden:
        return False, "forbidden"
    if not strict_allowed and from_id == to_id:
        return True, None
    if allowed is not None and (from_id, to_id) not in allowed:
        return False, "not_in_allowed"
    return True, None


def _provenance(row_index: int, events: list[dict]) -> dict:
    """某个 row（以其 change 序号标识）的来源。"""
    if row_index == 0:
        return {"type": "start", "change": 0}
    ev = events[row_index - 1]
    return {
        "type": "change",
        "change": ev["change"],
        "method": ev["method"],
        "method_id": ev["method_id"],
        "method_version": ev["method_version"],
        "lead": ev["lead"],
        "change_in_lead": ev["change_in_lead"],
        "notation": ev["notation"],
        "source": ev["source"],
        "call": ev["call"],
    }


def prove_rows(
    stage: int,
    method_name: str,
    leads: list[ExpandedLead],
    start_row: tuple[int, ...],
    max_calls: int | None = None,
    method_quotas: dict[str, dict] | None = None,
    allowed_transitions: set[tuple[str, str]] | None = None,
    forbidden_transitions: set[tuple[str, str]] | None = None,
) -> dict:
    """展开全部 lead 生成逐 row 序列并完成证明。结果对同一输入完全确定。

    ``method_quotas`` 形如 ``{method_id: {"min": m, "max": M}}``；
    转换规则仅约束相邻 lead 间的方法变化（同方法延续始终允许）。
    """
    rounds = tuple(range(1, stage + 1))
    rows: list[tuple[int, ...]] = [start_row]
    events: list[dict] = []
    lead_end_indices: list[int] = []
    lead_methods: list[str] = []  # 每个 lead 实际使用的方法标识（id 或名称）

    def _lead_key(lead: ExpandedLead) -> str:
        return lead.method_id if lead.method_id is not None else (
            lead.method_name or method_name
        )

    for lead_no, lead in enumerate(leads, 1):
        mid = _lead_key(lead)
        lead_methods.append(mid)
        mname = lead.method_name or method_name
        for pos, (token, places) in enumerate(zip(lead.tokens, lead.changes), 1):
            new_row = apply_change(rows[-1], places)
            rows.append(new_row)
            events.append(
                {
                    "change": len(rows) - 1,
                    "lead": lead_no,
                    "change_in_lead": pos,
                    "notation": token,
                    "places": sorted(places),
                    "source": "method" if lead.call is None else f"call:{lead.call}",
                    "call": lead.call,
                    "method": mname,
                    "method_id": lead.method_id,
                    "method_version": lead.method_version,
                    "row": row_to_string(new_row),
                }
            )
        lead_end_indices.append(len(rows) - 1)

    # 1) 排列完整性
    expected = list(rounds)
    permutation_complete = all(sorted(r) == expected for r in rows)

    # 2) 记号一致性：独立重放每个 change
    notation_consistent = True
    for i, ev in enumerate(events, 1):
        if apply_change(rows[i - 1], frozenset(ev["places"])) != rows[i]:
            notation_consistent = False
            break

    # 3) rounds 回归
    rounds_at = [i for i, r in enumerate(rows) if r == rounds]

    # 4) 首次重复 row 定位（终点回到起始 rounds 属于正常回归，不算重复）
    seen = {rows[0]: 0}
    first_repeat: dict | None = None
    n = len(rows) - 1
    for i in range(1, len(rows)):
        r = rows[i]
        if r in seen:
            j = seen[r]
            final_rounds = i == n and r == rounds and rows[0] == rounds
            if not final_rounds:
                first_repeat = {
                    "row": row_to_string(r),
                    "changes": [j, i],
                    "first": _provenance(j, events),
                    "second": _provenance(i, events),
                }
                break
        else:
            seen[r] = i

    # 5) 拼接：相邻 lead 方法变化处；连续段为同方法的最大连续 lead 序列
    splices: list[dict] = []
    runs: list[dict] = []
    splice_leads: set[int] = set()
    for k in range(1, len(leads)):
        if lead_methods[k] != lead_methods[k - 1]:
            splices.append(
                {"lead": k + 1, "from_method": lead_methods[k - 1], "to_method": lead_methods[k]}
            )
            splice_leads.add(k + 1)
    if leads:
        start = 1
        for k in range(2, len(leads) + 2):
            if k == len(leads) + 1 or lead_methods[k - 1] != lead_methods[start - 1]:
                seg = leads[start - 1 : k - 1]
                runs.append(
                    {
                        "method_id": lead_methods[start - 1],
                        "start_lead": start,
                        "end_lead": k - 1,
                        "leads": len(seg),
                        "changes": sum(len(l.tokens) for l in seg),
                    }
                )
                start = k
    for ev in events:
        ev["splice"] = ev["lead"] in splice_leads

    # 6) 各方法用量（按 lead 与 change 计）
    method_usage: dict[str, dict] = {}
    for lead, mid in zip(leads, lead_methods):
        u = method_usage.setdefault(
            mid,
            {
                "name": lead.method_name or method_name,
                "version": lead.method_version,
                "leads": 0,
                "changes": 0,
            },
        )
        u["leads"] += 1
        u["changes"] += len(lead.tokens)

    # 7) 配额校核
    quotas = method_quotas or {}
    quota_status: dict[str, dict] = {}
    for mid, q in quotas.items():
        used = method_usage.get(mid, {}).get("leads", 0)
        lo, hi = q.get("min"), q.get("max")
        quota_status[mid] = {
            "leads": used,
            "min": lo,
            "max": hi,
            "satisfied": (lo is None or used >= lo) and (hi is None or used <= hi),
        }
    quotas_satisfied = all(s["satisfied"] for s in quota_status.values())

    # 8) 相邻转换校核
    forbidden = forbidden_transitions or set()
    transition_violations: list[dict] = []
    for k in range(1, len(leads)):
        ok, reason = transition_ok(
            lead_methods[k - 1], lead_methods[k], allowed_transitions, forbidden
        )
        if not ok:
            transition_violations.append(
                {
                    "lead": k + 1,
                    "from_method": lead_methods[k - 1],
                    "to_method": lead_methods[k],
                    "rule": reason,
                }
            )

    calls_used = sum(1 for lead in leads if lead.call)
    return {
        "stage": stage,
        "start_row": row_to_string(start_row),
        "total_changes": n,
        "total_leads": len(leads),
        "permutation_complete": permutation_complete,
        "notation_consistent": notation_consistent,
        "rounds": row_to_string(rounds),
        "rounds_return": rows[-1] == rounds,
        "rounds_at": rounds_at,
        "truth": "true" if first_repeat is None else "false",
        "first_repeat": first_repeat,
        "calls_used": calls_used,
        "max_calls": max_calls,
        "exceeds_max_calls": max_calls is not None and calls_used > max_calls,
        "lead_heads": [row_to_string(rows[i]) for i in lead_end_indices],
        "lead_methods": lead_methods,
        "splices": splices,
        "splice_count": len(splices),
        "runs": runs,
        "method_usage": method_usage,
        "quota_status": quota_status,
        "quotas_satisfied": quotas_satisfied,
        "transition_violations": transition_violations,
        "transitions_satisfied": not transition_violations,
        "events": events,
    }
