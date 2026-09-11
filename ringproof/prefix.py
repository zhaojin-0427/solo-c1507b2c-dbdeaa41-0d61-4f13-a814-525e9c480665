"""部分 touch（row 前缀）的重放与校核。

调用方提交已经敲出的 row 序列，并按 lead 标注每个 lead 的方法与 call；
也可引用某个不可变 touch 的指定 change（该 change 须位于 lead end）。
重放时逐 change 检查：

- 排列完整性：每个提交 row 都是 1..stage 的完整排列；
- 相邻换位：相邻 row 须仅由相邻位置互换得到（推得实际 places）；
- 记号一致性：实际 places 与标注 lead（方法 + call）推出的 places 相同；
- 重复 row：定位首个重复（末尾回到 rounds 属正常回归，不算重复）。

结果冻结当前排列、已出现 row、lead 边界、方法/调用版本与 ringproof 版本，
供 /continuation 续接搜索使用；同一输入重放结果完全确定。
"""
from __future__ import annotations

from .engine import adjacent_places
from .notation import (
    NotationError,
    bell_from_char,
    row_to_string,
    validate_stage,
)

ROUNDS = None  # 占位，实际 rounds 由 stage 构造


def parse_submitted_row(text: str, stage: int) -> tuple[int, ...] | None:
    """解析调用方敲出的 row；非法（长度不符/越界字符/非完整排列）时返回 None。"""
    s = text.strip().upper()
    if len(s) != stage:
        return None
    try:
        row = tuple(bell_from_char(c) for c in s)
    except NotationError:
        return None
    if sorted(row) != list(range(1, stage + 1)):
        return None
    return row


def replay_prefix(
    stage: int,
    leads: list[dict],
    submitted_rows: list[str],
    start_row: tuple[int, ...] | None = None,
) -> dict:
    """重放按 lead 标注的 row 前缀。

    ``leads`` 每项形如 ``{"method_id", "method_version", "method_name",
    "call", "tokens", "changes"}``（tokens/changes 为该 lead 实际展开后的
    change 序列，已含 call 替换）；``submitted_rows`` 为不含起始 row 的
    逐 change row，长度须等于各 lead change 数之和（前缀须止于 lead end）。

    返回校核明细与冻结状态（valid 为假时冻结首个非法处的当前排列）。
    """
    validate_stage(stage)
    rounds = tuple(range(1, stage + 1))
    start = start_row or rounds
    expected_changes = sum(len(l["tokens"]) for l in leads)
    length_mismatch = len(submitted_rows) != expected_changes

    events: list[dict] = []
    seen: dict[tuple[int, ...], int] = {start: 0}
    seen_rows: list[str] = [row_to_string(start)]
    lead_end_indices: list[int] = []
    lead_methods: list[str] = []

    first_error: dict | None = None
    current = start
    row_index = 0  # 已成功接受的最后一个 row 的序号
    permutation_ok = sorted(start) == list(rounds)

    def _fail(code: str, message: str, at_change: int, **extra) -> None:
        nonlocal first_error
        if first_error is None:
            first_error = {
                "code": code,
                "message": message,
                "at_change": at_change,
                **extra,
            }

    def _prov(change_no: int, lead_no: int, pos: int, lead: dict, token: str) -> dict:
        return {
            "type": "change",
            "change": change_no,
            "lead": lead_no,
            "change_in_lead": pos,
            "notation": token,
            "method": lead["method_name"],
            "method_id": lead["method_id"],
            "method_version": lead["method_version"],
            "call": lead["call"],
            "source": "method" if lead["call"] is None else f"call:{lead['call']}",
        }

    if not length_mismatch:
        change_no = 0
        stopped = False
        for lead_no, lead in enumerate(leads, 1):
            if stopped:
                break
            mid = lead["method_id"]
            lead_methods.append(mid)
            for pos, (token, expected_places) in enumerate(
                zip(lead["tokens"], lead["changes"]), 1
            ):
                change_no += 1
                raw = submitted_rows[change_no - 1]
                nxt = parse_submitted_row(raw, stage)
                expected_set = frozenset(expected_places)

                event = {
                    "change": change_no,
                    "lead": lead_no,
                    "change_in_lead": pos,
                    "notation": token,
                    "places": sorted(expected_set),
                    "source": "method"
                    if lead["call"] is None
                    else f"call:{lead['call']}",
                    "call": lead["call"],
                    "method": lead["method_name"],
                    "method_id": lead["method_id"],
                    "method_version": lead["method_version"],
                    "row": raw.strip().upper(),
                }

                if nxt is None:
                    event["actual_places"] = None
                    event["ok"] = False
                    events.append(event)
                    _fail(
                        "ROW_NOT_PERMUTATION",
                        f"第 {change_no} 个 change 的 row {raw!r} 不是 1..{stage} 的完整排列",
                        change_no,
                        expected=_prov(change_no, lead_no, pos, lead, token),
                    )
                    stopped = True
                    break

                actual = adjacent_places(current, nxt)
                if actual is None:
                    event["actual_places"] = None
                    event["ok"] = False
                    events.append(event)
                    _fail(
                        "NOT_ADJACENT",
                        f"第 {change_no} 个 change：{row_to_string(current)} → "
                        f"{row_to_string(nxt)} 无法由相邻换位产生",
                        change_no,
                        expected=_prov(change_no, lead_no, pos, lead, token),
                    )
                    stopped = True
                    break
                if actual != expected_set:
                    event["actual_places"] = sorted(actual)
                    event["ok"] = False
                    events.append(event)
                    _fail(
                        "NOTATION_MISMATCH",
                        f"第 {change_no} 个 change 的实际换位 places={sorted(actual)} "
                        f"与标注记号 {token!r} 的 places={sorted(expected_set)} 不符"
                        f"（lead {lead_no}，方法 {lead['method_id']}，"
                        f"call={lead['call'] or 'plain'}）",
                        change_no,
                        actual_places=sorted(actual),
                        expected=_prov(change_no, lead_no, pos, lead, token),
                    )
                    stopped = True
                    break

                # 重复检测：末尾回到 rounds 属正常回归（与 prove 语义一致）
                final_rounds = change_no == expected_changes and nxt == rounds
                if nxt in seen and not final_rounds:
                    event["actual_places"] = sorted(actual)
                    event["ok"] = True
                    events.append(event)
                    current, row_index = nxt, change_no
                    seen_rows.append(row_to_string(nxt))
                    _fail(
                        "REPEATED_ROW",
                        f"第 {change_no} 个 change 的 row {row_to_string(nxt)} "
                        f"已在第 {seen[nxt]} 个 change 出现过",
                        change_no,
                        repeated_row=row_to_string(nxt),
                        first_seen_at=seen[nxt],
                        expected=_prov(change_no, lead_no, pos, lead, token),
                    )
                    stopped = True
                    break

                event["actual_places"] = sorted(actual)
                event["ok"] = True
                events.append(event)
                if nxt not in seen:
                    seen[nxt] = change_no
                seen_rows.append(row_to_string(nxt))
                current, row_index = nxt, change_no
            if not stopped:
                lead_end_indices.append(row_index)

    if length_mismatch and first_error is None:
        first_error = {
            "code": "PREFIX_LENGTH_MISMATCH",
            "message": (
                f"提交 {len(submitted_rows)} 个 row 与标注 lead 的 "
                f"{expected_changes} 个 change 不符（前缀须止于 lead end）"
            ),
            "at_change": min(len(submitted_rows), expected_changes) + 1,
            "submitted": len(submitted_rows),
            "expected": expected_changes,
        }

    splice_leads = {
        k + 1
        for k in range(1, len(lead_methods))
        if lead_methods[k] != lead_methods[k - 1]
    }
    for ev in events:
        ev["splice"] = ev["lead"] in splice_leads

    return {
        "stage": stage,
        "valid": first_error is None and not length_mismatch,
        "first_error": first_error,
        "start_row": row_to_string(start),
        "current_row": row_to_string(current),
        "accepted_changes": row_index,
        "total_changes": expected_changes,
        "length_mismatch": length_mismatch,
        "permutation_complete": permutation_ok and first_error is None,
        "seen_rows": seen_rows,
        "seen_set": seen,
        "lead_methods": lead_methods,
        "lead_end_indices": lead_end_indices,
        "events": events,
    }
