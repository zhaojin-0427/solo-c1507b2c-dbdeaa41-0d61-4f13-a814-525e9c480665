"""Row 生成、touch 展开与序列证明。

证明内容：
- 排列完整性：每个 row 都是 1..stage 的完整排列；
- 记号一致性：每个 row 都能由上一 row 施加对应 change 的 places 重新推出；
- rounds 回归：记录所有回到 rounds 的 change 位置；
- 重复检测：定位首次重复 row 的两个 change，并给出 method / lead / call 来源。
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
    """一个 lead 展开后的 change 序列；call 为 None 表示 plain lead。"""

    call: str | None
    tokens: list[str]
    changes: list[frozenset[int]]


def build_lead(
    method_tokens: list[str],
    method_changes: list[frozenset[int]],
    call_name: str | None,
    call_defs: dict[str, dict],
) -> ExpandedLead:
    """生成一个 lead；call 在 lead end 处替换方法末尾 ``replace`` 个 change。"""
    if call_name is None or call_name == "plain":
        return ExpandedLead(None, list(method_tokens), list(method_changes))
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
    )


def _provenance(row_index: int, events: list[dict], method_name: str) -> dict:
    """某个 row（以其 change 序号标识）的来源。"""
    if row_index == 0:
        return {"type": "start", "change": 0}
    ev = events[row_index - 1]
    return {
        "type": "change",
        "change": ev["change"],
        "method": method_name,
        "lead": ev["lead"],
        "change_in_lead": ev["change_in_lead"],
        "notation": ev["notation"],
        "source": ev["source"],
    }


def prove_rows(
    stage: int,
    method_name: str,
    leads: list[ExpandedLead],
    start_row: tuple[int, ...],
    max_calls: int | None = None,
) -> dict:
    """展开全部 lead 生成逐 row 序列并完成证明。结果对同一输入完全确定。"""
    rounds = tuple(range(1, stage + 1))
    rows: list[tuple[int, ...]] = [start_row]
    events: list[dict] = []
    lead_end_indices: list[int] = []

    for lead_no, lead in enumerate(leads, 1):
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
                    "first": _provenance(j, events, method_name),
                    "second": _provenance(i, events, method_name),
                }
                break
        else:
            seen[r] = i

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
        "events": events,
    }
