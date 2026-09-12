"""呼叫位置写法（calling positions）编译为 touch。

位置方案（position scheme）按方法版本冻结，给定一口观察钟与 Home/Wrong/
Middle 等位置符号在 call 结束（lead end）后的观察钟位置；composition 由可
复用的 part 组成，每个 token 记录位置符号与 bob、single 或自定义 call，并
可限制此前经过的 plain lead 数。编译器从指定 course head 逐 lead 推演：

- 一个 part 从某个 course head 开始；token 未限定 plain lead 数时，在一个
  plain course（至多 course_length 个 lead）内逐一试探：先铺 0..L-1 个
  plain lead 再接 call lead，观察钟在 call 结束后的位置唯一命中目标符号；
- token 给定 ``plain_leads``（call 前恰好经过的 plain lead 数）时只校验该
  lead；给定 ``max_plain_leads`` 时只在 0..max 内枚举；
- 位置在限额内不可达 → POSITION_UNREACHABLE；限额内多个 lead 都能命中 →
  AMBIGUOUS_POSITION（返回候选 lead）；
- 每个 part 的全部 token 解析后，补 plain lead 至观察钟第一次回到 home：
  course_length 个 lead 内回不到 home → PART_MISMATCH；下一个 part 从该
  course head 继续；
- 出错时返回出错 token、候选 lead 与当前排列，不落任何版本。

“course” 由观察钟回到 home 位置界定（未必等于方法的 plain course lead
数），call 会改变其后的 lead head 与回 home 所需的 plain lead 数。

编译器只做位置解析与逐 lead 推演；生成的 ExpandedLead 序列交给
``engine.prove_rows`` 完成序列证明。整个过程对同一输入完全确定。
"""
from __future__ import annotations

from dataclasses import dataclass

from .engine import ExpandedLead, apply_change, build_lead
from .notation import row_to_string


class ComposeError(ValueError):
    """呼叫位置写法无法编译。``code`` 为机器可读错误码。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


DEFAULT_COURSE_LIMIT = 100  # 未给 course_length 时，界定 plain course 的 lead 上限


@dataclass
class CompiledToken:
    """一个 token 解析出的位置匹配结果。"""

    part: int
    token: int
    symbol: str
    call: str | None  # None 表示 plain
    plain_leads_before: int  # 该 call lead 之前经过的 plain lead 数
    lead_index: int  # 该 call 在本 part 内的 lead 序号（1 起）
    observer_position: int


@dataclass
class LeadRecord:
    """生成 touch 中单个 lead 的完整标注。"""

    lead: int
    part: int
    part_repeat: int
    part_name: str | None
    token: int | None  # None 表示自动补的 plain lead（token 之间或 part 尾部）
    call: str | None
    symbol: str | None
    lead_head_before: str
    lead_head_after: str
    observer_position: int


def _lead_end_row(start: tuple[int, ...], lead: ExpandedLead) -> tuple[int, ...]:
    """施加一个 lead，返回 lead end 排列。"""
    cur = start
    for places in lead.changes:
        cur = apply_change(cur, places)
    return cur


def _build(method_ctx: dict, call: str | None) -> ExpandedLead:
    return build_lead(
        method_ctx["tokens"],
        method_ctx["changes"],
        call,
        method_ctx["call_defs"],
        method_id=method_ctx.get("method_id"),
        method_version=method_ctx.get("method_version"),
        method_name=method_ctx.get("method_name"),
    )


def _observer_pos(row: tuple[int, ...], observer: int) -> int:
    return row.index(observer) + 1


def plain_course_length(
    method_ctx: dict,
    start: tuple[int, ...],
    observer: int,
    home_position: int,
    *,
    limit: int = DEFAULT_COURSE_LIMIT,
) -> int | None:
    """从 start 起只敲 plain lead，求观察钟第一次回到 home 位置所用的 lead 数。

    ``limit`` 个 lead 内回不到 home 则返回 None。"""
    plain = _build(method_ctx, None)
    cur = start
    for i in range(1, limit + 1):
        cur = _lead_end_row(cur, plain)
        if _observer_pos(cur, observer) == home_position:
            return i
    return None


def _target_position(scheme: dict, symbol: str, call_name: str | None) -> int:
    """查位置方案：call 专属映射优先，缺省回落 default；未定义即不可达。"""
    positions = scheme["positions"]
    per_call = scheme["call_positions"].get(call_name or "")
    if per_call is not None and symbol in per_call:
        return per_call[symbol]
    if symbol in positions:
        return positions[symbol]
    raise ComposeError(
        "SYMBOL_NOT_MAPPED",
        f"位置符号 {symbol!r} 在方案中没有映射"
        + ("" if call_name is None else f"（call {call_name!r} 亦无专属映射）"),
        {"symbol": symbol, "call": call_name},
    )


def _probe_leads(
    method_ctx: dict,
    start: tuple[int, ...],
    observer: int,
    call_name: str | None,
    gaps: list[int],
) -> list[dict]:
    """对每个候选试探：先敲 gap 个 plain lead 再接 call lead，返回各候选的
    lead end 与观察钟位置。各候选互不影响（都从 start 推演）。"""
    out: list[dict] = []
    for gap in gaps:
        row_at = start
        for _ in range(gap):
            row_at = _lead_end_row(row_at, _build(method_ctx, None))
        end_row = _lead_end_row(row_at, _build(method_ctx, call_name))
        out.append(
            {
                "gap": gap,
                "call": call_name,
                "end_row": end_row,
                "observer_position": _observer_pos(end_row, observer),
            }
        )
    return out


def _candidate(entry: dict, part_lead: int) -> dict:
    """把一次 lead 试探整理为候选 lead 明细。"""
    gap = entry["gap"]
    return {
        "lead_index": part_lead + gap + 1,
        "plain_leads_before": gap,
        "call": entry["call"] or "plain",
        "lead_head_after": row_to_string(entry["end_row"]),
        "observer_position": entry["observer_position"],
    }


def compile_composition(
    *,
    scheme: dict,
    method_ctx: dict,
    parts: list[dict],
    start_row: tuple[int, ...],
    course_length: int | None = None,
) -> dict:
    """把呼叫位置 composition 编译为逐 lead 的 ExpandedLead 序列。

    ``scheme`` 形如 ``{"observer", "home_symbol", "positions": {symbol: pos},
    "call_positions": {call_name: {symbol: pos}}}``；``parts`` 为展开重复后
    的 part 列表，每个 part 形如 ``{"name": str|None, "repeat_index": int,
    "tokens": [{"symbol", "call", "plain_leads", "max_plain_leads"}]}``。

    ``course_length`` 为搜索/收尾窗口（lead 数）；缺省时由起始 course head
    起观察钟只敲 plain lead 第一次回到 home 自动界定，界定不出来则抛
    ComposeError(PLAIN_COURSE_NOT_BOUND)。

    成功返回 ``{"leads", "records", "compiled_tokens", "course_heads",
    "course_lengths", "course_length", "final_row"}``；失败抛 ComposeError，
    details 携带出错 token、候选 lead 与当前排列。
    """
    observer = scheme["observer"]
    home_position = scheme["positions"][scheme["home_symbol"]]
    if course_length is None:
        course_length = plain_course_length(
            method_ctx, start_row, observer, home_position, limit=DEFAULT_COURSE_LIMIT
        )
        if course_length is None:
            raise ComposeError(
                "PLAIN_COURSE_NOT_BOUND",
                f"观察钟 {observer} 号钟在 {DEFAULT_COURSE_LIMIT} 个 plain lead 内未回到 home"
                f"（{scheme['home_symbol']}，第 {home_position} 位），无法界定 plain course；"
                f"请在编译请求中显式给出 course_length",
                {"current_row": row_to_string(start_row)},
            )
    course_limit = course_length

    leads: list[ExpandedLead] = []
    records: list[LeadRecord] = []
    compiled_tokens: list[CompiledToken] = []
    course_heads: list[str] = [row_to_string(start_row)]
    course_lengths: list[int] = []

    cur = start_row
    global_lead = 0
    leads_before_part = 0

    for part_idx, part in enumerate(parts, 1):
        part_name = part.get("name")
        part_lead = 0  # 本 part 已经敲的 lead 数（仅用于候选序号展示）
        last_token_info: dict | None = None  # 本 part 最后一个已解析 token（收尾失败时定位用）

        def _append_lead(call: str | None, symbol: str | None, token_no: int | None):
            nonlocal cur, global_lead, part_lead
            lead_head = cur
            lead = _build(method_ctx, call)
            end_row = _lead_end_row(lead_head, lead)
            global_lead += 1
            part_lead += 1
            leads.append(lead)
            records.append(
                LeadRecord(
                    lead=global_lead,
                    part=part_idx,
                    part_repeat=part.get("repeat_index", 1),
                    part_name=part_name,
                    token=token_no,
                    call=call,
                    symbol=symbol,
                    lead_head_before=row_to_string(lead_head),
                    lead_head_after=row_to_string(end_row),
                    observer_position=_observer_pos(end_row, observer),
                )
            )
            cur = end_row
            return end_row

        for token_idx, tok in enumerate(part["tokens"], 1):
            symbol = tok["symbol"]
            call_name = tok.get("call")
            target = _target_position(scheme, symbol, call_name)

            # 候选从当前行（part 首行或上一个 call 之后）起算：先铺 gap 个
            # plain lead 再接 call lead；plain_leads 给出精确 gap，
            # max_plain_leads 限定 0..max，缺省时枚举一个 course 窗口。
            exact = tok.get("plain_leads")
            max_before = tok.get("max_plain_leads")
            if exact is not None:
                gaps = [exact]
            elif max_before is not None:
                gaps = list(range(0, min(max_before, course_limit - 1) + 1))
            else:
                gaps = list(range(0, course_limit))

            candidates = _probe_leads(method_ctx, cur, observer, call_name, gaps)
            matched = [e for e in candidates if e["observer_position"] == target]

            token_info = {
                "part": part_idx,
                "token": token_idx,
                "symbol": symbol,
                "call": call_name or "plain",
                "plain_leads": exact,
                "max_plain_leads": max_before,
                "target_position": target,
            }
            last_token_info = token_info
            candidate_payload = [_candidate(e, part_lead) for e in candidates]

            if not matched:
                raise ComposeError(
                    "POSITION_UNREACHABLE",
                    f"第 {part_idx} part 第 {token_idx} 个 token"
                    f"（{symbol} / {call_name or 'plain'}）：限额内没有 lead 能使"
                    f"观察钟 {observer} 号钟在 call 结束后位于第 {target} 位",
                    {
                        "token": token_info,
                        "candidate_leads": candidate_payload,
                        "current_row": row_to_string(cur),
                    },
                )
            if len(matched) > 1:
                raise ComposeError(
                    "AMBIGUOUS_POSITION",
                    f"第 {part_idx} part 第 {token_idx} 个 token"
                    f"（{symbol} / {call_name or 'plain'}）：限额内有 "
                    f"{len(matched)} 个 lead 都能命中第 {target} 位，匹配不唯一；"
                    f"请用 plain_leads / max_plain_leads 收紧范围",
                    {
                        "token": token_info,
                        "matching_leads": [_candidate(e, part_lead) for e in matched],
                        "candidate_leads": candidate_payload,
                        "current_row": row_to_string(cur),
                    },
                )

            chosen = matched[0]
            # 提交：先补 gap 个 plain lead，再敲 call lead
            for _ in range(chosen["gap"]):
                _append_lead(None, None, None)
            _append_lead(call_name, symbol, token_idx)
            compiled_tokens.append(
                CompiledToken(
                    part=part_idx,
                    token=token_idx,
                    symbol=symbol,
                    call=call_name,
                    plain_leads_before=chosen["gap"],
                    lead_index=part_lead,
                    observer_position=chosen["observer_position"],
                )
            )

        # part 收尾：补 plain lead 至观察钟第一次回到 home（course 窗口内）
        plain_lead = _build(method_ctx, None)
        scan_row = cur
        tail_rows: list[tuple[int, ...]] = []
        for _ in range(course_limit):
            scan_row = _lead_end_row(scan_row, plain_lead)
            tail_rows.append(scan_row)
        home_idx = next(
            (i for i, r in enumerate(tail_rows) if _observer_pos(r, observer) == home_position),
            None,
        )
        if home_idx is None:
            raise ComposeError(
                "PART_MISMATCH",
                f"第 {part_idx} part 接不上：其最后一个 call 之后，{course_limit} 个 "
                f"plain lead 内观察钟 {observer} 号钟未能回到 home"
                f"（{scheme['home_symbol']}，第 {home_position} 位）",
                {
                    "part": part_idx,
                    "token": last_token_info,
                    "candidate_leads": [
                        {
                            "lead_index": part_lead + i + 1,
                            "plain_leads_before": i + 1,
                            "call": "plain",
                            "lead_head_after": row_to_string(r),
                            "observer_position": _observer_pos(r, observer),
                        }
                        for i, r in enumerate(tail_rows)
                    ],
                    "current_row": row_to_string(cur),
                },
            )
        for _ in range(home_idx + 1):
            _append_lead(None, None, None)
        course_lengths.append(global_lead - leads_before_part)
        leads_before_part = global_lead
        course_heads.append(row_to_string(cur))

    return {
        "leads": leads,
        "records": records,
        "compiled_tokens": compiled_tokens,
        "course_heads": course_heads,
        "course_lengths": course_lengths,
        "course_length": course_length,
        "final_row": cur,
    }
