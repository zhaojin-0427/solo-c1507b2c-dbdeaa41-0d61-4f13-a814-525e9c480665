"""可复用 block 拼装：从不可变 touch 截取 lead-end 区段作为可复用 block。

调用方从多个不可变 touch 截取首尾落在 lead end 的连续区段作为 block，
为每个 block 设置使用次数（min/max），并配置相邻衔接规则（允许/禁止的
block 转换）、总 change 数范围与目标末行。区段在保存时转为**相对起点的
位置置换** C（末 row 第 i 位的钟来自区段起点第 C[i] 位）；change 作用于
位置、与起点无关，因此同一 block 可从任意 lead head 展开：从任意 row R
出发逐 change 施加，末 row 第 i 位即 R 第 C[i] 位的钟。规范化摘要同时
给出相对起点的钟置换 φ（φ(起点第 i 位的钟) = 末 row 第 i 位的钟）。

创建时的拒绝条件（不落库）：

- 钟数不一致：各 block 引用的 touch 钟数须相同；
- 边界非法：区段首尾须落在 lead end（起始 change 0 为 touch 起始 row）
  且不超出 touch 范围；
- 区段自身为假：区段从自身起点展开即含重复 row（末 row 回到区段起点
  属正常闭合，不算重复）。

搜索（深度优先，block 按声明顺序分支）按展开后的逐 row 检查跨 block
重复，只返回满足用量、衔接、长度与末行要求的组合；先按**端点可达性**
（当前 row 到目标的最少 block 数，按位置置换自目标反向 BFS 的安全下界）、
**剩余长度**（补足最少用量所需与可达 change 范围）与**行交集**剪枝，再按
目标达成 → 总 change 数 → block 数 → call 数（→ 确定性字典序）稳定
排序。行交集冲突记录首次冲突的两侧来源（block、touch、change、method、
call）。到达目标即终止该路径；仅当目标即起点时，组合末 row 回到起点属
正常闭合（come-round），不算重复——其余任何重复（含目标末行撞上路径中
已出现的 row）一律按冲突剪枝。搜索对同一输入完全确定。
"""
from __future__ import annotations

from .engine import apply_change, transition_ok
from .multipart import (
    bell_permutation,
    perm_cycles,
    perm_order,
    segment_leads,
)
from .notation import row_to_string

# 端点可达性反向 BFS 的深度与行表上限（超出则停用该剪枝，不影响正确性）
MAX_REACH_DEPTH = 1024
MAX_REACH_ROWS = 1_000_000


class BlockError(ValueError):
    """block 拼装请求非法。``code`` 为机器可读错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _inverse_perm(perm: tuple[int, ...]) -> tuple[int, ...]:
    """0 起位置置换的逆：``inv[perm[i]] == i``。"""
    inv = [0] * len(perm)
    for i, p in enumerate(perm):
        inv[p] = i
    return tuple(inv)


def apply_pos_perm(perm: tuple[int, ...], row: tuple[int, ...]) -> tuple[int, ...]:
    """对 row 施加位置置换：``end[i] = row[perm[i]]``。"""
    return tuple(row[p] for p in perm)


# ---------------- block 构建 ----------------

def make_block(
    *,
    stage: int,
    block_id: str,
    touch_id: str,
    touch_version: int,
    rows: list[tuple[int, ...]],
    events: list[dict],
    lead_end_indices: list[int],
    start_change: int,
    end_change: int,
    min_uses: int,
    max_uses: int | None,
) -> dict:
    """把 touch 的 lead-end 区段构建为 block 描述符（含相对起点的钟置换）。

    ``rows`` 为 touch 的逐 row（索引 0 为起始 row），``events`` 为逐 change
    事件（change i 对应 events[i-1]），``lead_end_indices`` 为各 lead end 的
    change 序号。边界非法或区段自身为假时抛 ``BlockError``（不落库）。
    """
    total_changes = len(events)
    lead_ends = set(lead_end_indices)
    if end_change > total_changes:
        raise BlockError(
            "CHANGE_OUT_OF_RANGE",
            f"block {block_id!r} 的 end_change {end_change} 超出 touch 范围"
            f"（1..{total_changes}）",
        )
    if start_change != 0 and start_change not in lead_ends:
        raise BlockError(
            "NOT_LEAD_END",
            f"block {block_id!r} 的 start_change {start_change} 不是 lead end"
            f"（lead end 为 {lead_end_indices}，0 为 touch 起始 row）",
        )
    if end_change not in lead_ends:
        raise BlockError(
            "NOT_LEAD_END",
            f"block {block_id!r} 的 end_change {end_change} 不是 lead end"
            f"（lead end 为 {lead_end_indices}）",
        )

    seg = end_change - start_change
    seg_rows = rows[start_change : end_change + 1]
    # 区段自身须为真：从自身起点展开不含重复 row（末 row 回到起点属正常闭合）
    seen = {seg_rows[0]: 0}
    for j in range(1, seg + 1):
        r = seg_rows[j]
        if r in seen and not (j == seg and r == seg_rows[0]):
            raise BlockError(
                "BLOCK_NOT_TRUE",
                f"block {block_id!r} 的区段自身为假：row {row_to_string(r)} 在区段内"
                f"第 {seen[r]} 个 change（touch change {start_change + seen[r]}）与"
                f"第 {j} 个 change（touch change {start_change + j}）重复",
            )
        seen[r] = j

    changes: list[tuple[frozenset[int], dict]] = []
    call_leads: set[int] = set()
    methods: set[str] = set()
    for j in range(1, seg + 1):
        ev = events[start_change + j - 1]
        changes.append(
            (
                frozenset(ev["places"]),
                {
                    "touch_change": ev["change"],
                    "lead": ev["lead"],
                    "change_in_lead": ev["change_in_lead"],
                    "method": ev["method"],
                    "method_id": ev["method_id"],
                    "method_version": ev["method_version"],
                    "call": ev["call"],
                    "notation": ev["notation"],
                    "source": ev["source"],
                },
            )
        )
        if ev["call"]:
            call_leads.add(ev["lead"])
        methods.add(ev["method_id"] if ev["method_id"] is not None else ev["method"])

    # 区段的净位置置换 C（end[i] = start[C[i]]）：对恒等 row 施加同一串
    # change 即得。block 从任意 lead head R 展开时，末 row 第 i 位为 R 第
    # C[i] 位的钟——展开、可达性剪枝与分段末行统一按 C 计算。
    cur = tuple(range(1, stage + 1))
    for places, _prov in changes:
        cur = apply_change(cur, places)
    pos_perm = tuple(b - 1 for b in cur)

    images = bell_permutation(seg_rows[0], seg_rows[-1])
    start_lead, end_lead = segment_leads(lead_end_indices, start_change, end_change)
    return {
        "id": block_id,
        "touch": {"id": touch_id, "version": touch_version},
        "start_change": start_change,
        "end_change": end_change,
        "length": seg,
        "start_row": seg_rows[0],
        "end_row": seg_rows[-1],
        "images": images,
        "pos_perm": pos_perm,
        "inv_pos_perm": _inverse_perm(pos_perm),
        "calls": len(call_leads),
        "methods": sorted(methods),
        "start_lead": start_lead,
        "end_lead": end_lead,
        "changes": changes,
        "min_uses": min_uses,
        "max_uses": max_uses,
    }


def block_summary(block: dict) -> dict:
    """block 的规范化摘要（JSON 安全，含相对起点的钟置换）。"""
    cycles, fixed = perm_cycles(block["images"])
    return {
        "id": block["id"],
        "touch": dict(block["touch"]),
        "start_change": block["start_change"],
        "end_change": block["end_change"],
        "changes": block["length"],
        "start_row": row_to_string(block["start_row"]),
        "end_row": row_to_string(block["end_row"]),
        "start_lead": block["start_lead"],
        "end_lead": block["end_lead"],
        "leads": block["end_lead"] - block["start_lead"] + 1,
        "calls": block["calls"],
        "methods": list(block["methods"]),
        "min_uses": block["min_uses"],
        "max_uses": block["max_uses"],
        "permutation": {
            "images": list(block["images"]),
            "cycles": cycles,
            "fixed_bells": fixed,
            "order": perm_order(block["images"]),
        },
    }


# ---------------- 端点可达性 ----------------

def _reachability(
    blocks: list[dict], target: tuple[int, ...], max_depth: int
) -> dict | None:
    """各 row 到 target 的最少 block 数（按净位置置换自目标反向 BFS）。

    忽略用量/衔接/禁重，是安全下界：剩余 block 预算小于该步数时必然无法
    到达目标。行表超出预算时返回 None（停用可达性剪枝，不影响正确性）。
    """
    inverses = [b["inv_pos_perm"] for b in blocks]
    dist = {target: 0}
    frontier = [target]
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        nxt: list[tuple[int, ...]] = []
        for r in frontier:
            for inv in inverses:
                src = apply_pos_perm(inv, r)
                if src not in dist:
                    dist[src] = depth
                    nxt.append(src)
        if len(dist) > MAX_REACH_ROWS:
            return None
        frontier = nxt
    return {"dist": dist, "complete": not frontier, "depth": depth}


# ---------------- 组合搜索 ----------------

def search_blocks(
    *,
    blocks: list[dict],
    start_row: tuple[int, ...],
    target: tuple[int, ...] | None,
    min_changes: int,
    max_changes: int,
    allowed: set[tuple[str, str]] | None,
    forbidden: set[tuple[str, str]],
    max_results: int,
    max_search: int,
) -> dict:
    """搜索满足用量、衔接、长度与末行要求的 block 组合。结果对同一输入完全确定。

    深度优先枚举（block 按声明顺序分支），每个非空路径的末端若满足各
    block 最少用量、长度下限与末行要求即成为候选；候选按 目标达成 →
    总 change 数 → block 数 → call 数 → 确定性字典序 稳定排序，保留最优
    ``max_results`` 个。``target`` 为 None 时不限制末行。
    """
    n = len(blocks)
    min_len = min(b["length"] for b in blocks)
    reach = None
    if target is not None:
        reach = _reachability(
            blocks, target, min(max_changes // min_len, MAX_REACH_DEPTH)
        )

    results: list[dict] = []
    candidates = 0
    checked = 0
    pruned_by_reachability = 0
    pruned_by_length = 0
    pruned_by_rows = 0
    pruned_by_usage = 0
    pruned_by_transition = 0
    truncated = False
    truncation_reason: str | None = None
    first_conflict: dict | None = None

    used: dict[tuple[int, ...], dict] = {start_row: {"type": "start", "change": 0}}
    counts = [0] * n
    path: list[int] = []

    def prov_entry(b: dict, j: int, block_index: int, global_change: int) -> dict:
        """某 block 第 j 个 change 在当前组合中的来源（含 touch 事件明细）。"""
        return {
            "type": "change",
            "block_index": block_index,
            "block": b["id"],
            "touch": dict(b["touch"]),
            "change": global_change,
            "change_in_block": j,
            **b["changes"][j - 1][1],
        }

    def sort_key(spec: dict):
        return (
            spec["target_reached"] is False,
            spec["total_changes"],
            spec["num_blocks"],
            spec["num_calls"],
            spec["sequence"],
        )

    def collect(row: tuple[int, ...], changes: int, calls: int) -> None:
        nonlocal candidates
        candidates += 1
        results.append(
            {
                "sequence": [blocks[i]["id"] for i in path],
                "path": tuple(path),
                "num_blocks": len(path),
                "total_changes": changes,
                "num_calls": calls,
                "final_row": row_to_string(row),
                "target_reached": (row == target) if target is not None else None,
            }
        )
        results.sort(key=sort_key)
        if len(results) > max_results:
            results.pop()

    def dfs(row: tuple[int, ...], last_id: str | None, changes: int, calls: int) -> None:
        nonlocal checked, truncated, truncation_reason, first_conflict
        nonlocal pruned_by_reachability, pruned_by_length, pruned_by_rows
        nonlocal pruned_by_usage, pruned_by_transition
        if truncated:
            return
        if checked >= max_search:
            truncated = True
            truncation_reason = f"达到搜索上限 max_search={max_search}"
            return
        checked += 1

        # 候选：至少一个 block，且满足各 block 最少用量、长度下限与末行要求
        if path:
            usage_ok = all(counts[i] >= blocks[i]["min_uses"] for i in range(n))
            if (
                usage_ok
                and changes >= min_changes
                and (target is None or row == target)
            ):
                collect(row, changes, calls)
        # 到达目标即终止（继续延伸的末行必然与此次目标出现重复）
        if path and target is not None and row == target:
            return

        rem_changes = max_changes - changes
        # 剪枝·剩余长度：最短的 block 都放不下
        if rem_changes < min_len:
            pruned_by_length += 1
            return
        # 剪枝·剩余长度：补足各 block 最少用量所需 change 超出预算
        must_add = sum(
            max(0, blocks[i]["min_uses"] - counts[i]) * blocks[i]["length"]
            for i in range(n)
        )
        if must_add > rem_changes:
            pruned_by_length += 1
            return
        # 剪枝·剩余长度：用尽剩余用量也达不到 min_changes
        may_add = 0
        for i in range(n):
            rem_uses = (
                blocks[i]["max_uses"] - counts[i]
                if blocks[i]["max_uses"] is not None
                else rem_changes
            )
            may_add += rem_uses * blocks[i]["length"]
        if changes + min(may_add, rem_changes) < min_changes:
            pruned_by_length += 1
            return
        # 剪枝·端点可达性：剩余 block 预算内无法到达目标
        if reach is not None:
            rem_blocks = rem_changes // min_len
            rem_usage = 0
            for i in range(n):
                rem_usage += (
                    blocks[i]["max_uses"] - counts[i]
                    if blocks[i]["max_uses"] is not None
                    else rem_blocks
                )
            rem_blocks = min(rem_blocks, rem_usage)
            need = reach["dist"].get(row)
            if need is None:
                if reach["complete"] or rem_blocks <= reach["depth"]:
                    pruned_by_reachability += 1
                    return
            elif need > rem_blocks:
                pruned_by_reachability += 1
                return

        for i in range(n):
            if truncated:
                return
            b = blocks[i]
            if b["max_uses"] is not None and counts[i] >= b["max_uses"]:
                pruned_by_usage += 1
                continue
            if last_id is not None:
                ok, _ = transition_ok(last_id, b["id"], allowed, forbidden)
                if not ok:
                    pruned_by_transition += 1
                    continue
            if changes + b["length"] > max_changes:
                pruned_by_length += 1
                continue
            # 行交集：展开 block 的逐 row，与当前路径已用 row 比对。
            # 仅"回到起点"（目标即起点）的 block 末 row 豁免；其余重复——
            # 含目标末行撞上路径中已出现的 row——一律按冲突剪枝。
            cur = row
            added: list[tuple[tuple[int, ...], int]] = []
            conflict: tuple[tuple[int, ...], int] | None = None
            for j in range(1, b["length"] + 1):
                cur = apply_change(cur, b["changes"][j - 1][0])
                if cur in used and not (
                    j == b["length"]
                    and target is not None
                    and cur == target
                    and cur == start_row
                ):
                    conflict = (cur, j)
                    break
                added.append((cur, j))
            if conflict is not None:
                pruned_by_rows += 1
                if first_conflict is None:
                    r, j = conflict
                    first_conflict = {
                        "row": row_to_string(r),
                        "first": used[r],
                        "second": prov_entry(b, j, len(path) + 1, changes + j),
                    }
                continue
            counts[i] += 1
            path.append(i)
            new_rows: list[tuple[int, ...]] = []
            for r, j in added:
                if r not in used:
                    used[r] = prov_entry(b, j, len(path), changes + j)
                    new_rows.append(r)
            dfs(cur, b["id"], changes + b["length"], calls + b["calls"])
            for r in new_rows:
                del used[r]
            path.pop()
            counts[i] -= 1

    dfs(start_row, None, 0, 0)

    if not truncated and not results:
        truncation_reason = "搜索空间穷尽，未发现满足全部约束的组合"

    out_results = []
    for spec in results:
        segments = []
        cur = start_row
        for seq, i in enumerate(spec["path"], 1):
            b = blocks[i]
            end = apply_pos_perm(b["pos_perm"], cur)
            segments.append(
                {
                    "seq": seq,
                    "block": b["id"],
                    "touch": dict(b["touch"]),
                    "start_row": row_to_string(cur),
                    "end_row": row_to_string(end),
                    "changes": b["length"],
                    "calls": b["calls"],
                }
            )
            cur = end
        blocks_used = {}
        for i in range(n):
            c = spec["path"].count(i)
            if c:
                blocks_used[blocks[i]["id"]] = c
        out_results.append(
            {
                "sequence": spec["sequence"],
                "segments": segments,
                "blocks_used": blocks_used,
                "num_blocks": spec["num_blocks"],
                "total_changes": spec["total_changes"],
                "num_calls": spec["num_calls"],
                "final_row": spec["final_row"],
                "target_reached": spec["target_reached"],
            }
        )

    return {
        "checked": checked,
        "candidates": candidates,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "pruned_by_reachability": pruned_by_reachability,
        "pruned_by_length": pruned_by_length,
        "pruned_by_rows": pruned_by_rows,
        "pruned_by_usage": pruned_by_usage,
        "pruned_by_transition": pruned_by_transition,
        "first_conflict": first_conflict,
        "total_results": len(out_results),
        "results": out_results,
    }
