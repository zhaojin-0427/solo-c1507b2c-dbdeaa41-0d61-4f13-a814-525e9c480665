"""multipart composition 校核：由单 part 区段的置换展开整首 composition。

调用方从不可变 touch 选取首尾落在 lead end 的连续区段作为一个 part；
系统求出区段起止 row 之间的**钟置换** φ——φ(钟) = 在区段末 row 中接替
该钟位置的钟。change 作用于位置、φ 作用于钟，两者可交换，因此同一串
change 从 φ(start) 出发产生的逐 row 恰为 part 1 对应 row 的 φ 像：
part k 的逐 row 是 part 1 逐 row 的 φ^(k-1) 像，各 part 无须重复提交
row，系统按置换展开整首 composition。

校核内容：

- 各 part end 排列、φ 的循环分解与阶（轨道长度）；
- 提前回到起点：part end 在预期 part 数之前回到区段首 row 的位置；
- 轨道不按预期闭合（φ^expected ≠ 恒等）时不一致的钟位；
- 必须保持原位的钟（fixed bells）是否被 φ 固定；
- part 内及跨 part 的重复 row：冲突给出 row 与双方的 part、change、
  method、call 来源（追溯 touch 中对应 change 的事件）。

枚举接口在 touch 的全部 lead-end 区段中搜索整首为真的 multipart
composition：先按 fixed bells 与轨道长度（φ 的阶须落在 part 数范围内，
此时 part 数被唯一确定为阶）剪枝，再做行交集扫描；结果沿用 touch 中的
位置顺序（区段起始 change、结束 change）。整个过程对同一输入完全确定。
"""
from __future__ import annotations

from bisect import bisect_right
from math import lcm

from .notation import row_to_string

# 展开后整首 composition 的 change 数上限（防止过大的展开占用过多资源）
MAX_COMPOSITION_CHANGES = 100_000


class MultipartError(ValueError):
    """multipart 校核请求非法。``code`` 为机器可读错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------- 置换运算 ----------------

def bell_permutation(
    start_row: tuple[int, ...], end_row: tuple[int, ...]
) -> tuple[int, ...]:
    """区段起止 row 间的钟置换 φ：start_row 第 i 位的钟 → end_row 第 i 位的钟。

    返回 images 元组，``images[b - 1]`` 即 φ(b)（接替钟 b 原位置的钟）。
    """
    images = [0] * len(start_row)
    for pos, bell in enumerate(start_row):
        images[bell - 1] = end_row[pos]
    return tuple(images)


def permute_row(images: tuple[int, ...], row: tuple[int, ...]) -> tuple[int, ...]:
    """对 row 施加钟置换 φ（逐钟改名）。"""
    return tuple(images[b - 1] for b in row)


def perm_power(images: tuple[int, ...], k: int) -> tuple[int, ...]:
    """φ^k 的 images 表示。"""
    cur = tuple(range(1, len(images) + 1))
    for _ in range(k):
        cur = tuple(images[b - 1] for b in cur)
    return cur


def perm_cycles(images: tuple[int, ...]) -> tuple[list[list[int]], list[int]]:
    """φ 的循环分解：返回 (非平凡循环, 保持不动的钟)。

    每个循环旋转到最小钟开头，循环之间按首元素排序（确定性输出）。
    """
    n = len(images)
    seen = [False] * (n + 1)
    cycles: list[list[int]] = []
    fixed: list[int] = []
    for b in range(1, n + 1):
        if seen[b]:
            continue
        cyc = []
        x = b
        while not seen[x]:
            seen[x] = True
            cyc.append(x)
            x = images[x - 1]
        if len(cyc) == 1:
            fixed.append(cyc[0])
        else:
            m = cyc.index(min(cyc))
            cycles.append(cyc[m:] + cyc[:m])
    cycles.sort(key=lambda c: c[0])
    return cycles, fixed


def perm_order(images: tuple[int, ...]) -> int:
    """φ 的阶（轨道长度）：各循环长度的最小公倍数。"""
    cycles, _ = perm_cycles(images)
    order = 1
    for c in cycles:
        order = lcm(order, len(c))
    return order


def _perm_payload(images: tuple[int, ...]) -> dict:
    cycles, fixed = perm_cycles(images)
    order = 1
    for c in cycles:
        order = lcm(order, len(c))
    return {
        "images": list(images),
        "cycles": cycles,
        "fixed_bells": fixed,
        "order": order,
    }


# ---------------- 逐 row 展开与重复扫描 ----------------

def _first_repeat(
    images: tuple[int, ...],
    part1_rows: list[tuple[int, ...]],
    parts: int,
    start_row: tuple[int, ...],
) -> tuple[tuple[int, ...], int, int] | None:
    """按置换展开整首 composition，返回首个重复 row 的 (row, 首见序号, 再见序号)。

    序号为整首 composition 的全局 change 序号（0 = 起始 row）。part 衔接处
    （上一 part 末 row = 下一 part 首 row）只计一次；末 part 末 row 回到
    起始 row 属正常闭合，不算重复（与引擎的 rounds 回归规则一致）。
    """
    seg = len(part1_rows) - 1
    seen = {start_row: 0}
    cur = part1_rows
    for part in range(1, parts + 1):
        if part > 1:
            cur = [permute_row(images, r) for r in cur]
        base = (part - 1) * seg
        for j in range(1, seg + 1):
            r = cur[j]
            g = base + j
            is_final = part == parts and j == seg
            if r in seen and not (is_final and r == start_row):
                return r, seen[r], g
            seen[r] = g
    return None


def _provenance(
    part: int,
    change_in_part: int,
    change: int,
    touch_change: int,
    events: list[dict],
) -> dict:
    """展开 composition 中某次 row 出现的来源：part、change 及 touch 事件。"""
    ev = events[touch_change - 1]
    return {
        "part": part,
        "change_in_part": change_in_part,
        "change": change,
        "touch_change": touch_change,
        "lead": ev["lead"],
        "change_in_lead": ev["change_in_lead"],
        "method": ev["method"],
        "method_id": ev["method_id"],
        "method_version": ev["method_version"],
        "call": ev["call"],
        "notation": ev["notation"],
        "source": ev["source"],
    }


def _occurrence_of(global_change: int, seg: int, start_change: int, events: list[dict]) -> dict:
    """把整首 composition 的全局 change 序号映射为来源明细。"""
    if global_change == 0:
        return {"type": "start", "part": 1, "change": 0, "change_in_part": 0,
                "touch_change": start_change}
    part = (global_change - 1) // seg + 1
    j = (global_change - 1) % seg + 1
    return _provenance(part, j, global_change, start_change + j, events)


# ---------------- 区段定位 ----------------

def segment_leads(lead_end_indices: list[int], start_change: int, end_change: int) -> tuple[int, int]:
    """区段覆盖的首末 lead 序号（1 起）；start_change 为 0 或 lead end。"""
    start_lead = bisect_right(lead_end_indices, start_change) + 1
    end_lead = bisect_right(lead_end_indices, end_change)
    return start_lead, end_lead


def _segment_payload(
    rows: list[tuple[int, ...]],
    lead_end_indices: list[int],
    start_change: int,
    end_change: int,
) -> dict:
    start_lead, end_lead = segment_leads(lead_end_indices, start_change, end_change)
    return {
        "start_change": start_change,
        "end_change": end_change,
        "start_row": row_to_string(rows[start_change]),
        "end_row": row_to_string(rows[end_change]),
        "start_lead": start_lead,
        "end_lead": end_lead,
        "leads": end_lead - start_lead + 1,
        "changes": end_change - start_change,
    }


# ---------------- 单区段校核 ----------------

def analyze_segment(
    *,
    stage: int,
    rows: list[tuple[int, ...]],
    events: list[dict],
    lead_end_indices: list[int],
    start_change: int,
    end_change: int,
    expected_parts: int,
    fixed_bells: list[int],
) -> dict:
    """校核一个 part 区段按预期 part 数展开后的整首 composition。

    ``rows`` 为 touch 的逐 row（索引 0 为起始 row），``events`` 为逐 change
    事件（change i 对应 events[i-1]），``lead_end_indices`` 为各 lead end 的
    change 序号。结果对同一输入完全确定。
    """
    rounds = tuple(range(1, stage + 1))
    start_row = rows[start_change]
    seg = end_change - start_change
    part1_rows = rows[start_change : end_change + 1]
    images = bell_permutation(start_row, rows[end_change])
    permutation = _perm_payload(images)

    # 必须保持原位的钟：φ 须将其固定
    fixed_checks = [
        {"bell": b, "image": images[b - 1], "ok": images[b - 1] == b}
        for b in fixed_bells
    ]

    # 各 part end：φ^k(start)；提前回到起点的位置；末次是否闭合
    part_ends: list[dict] = []
    early_returns: list[int] = []
    cur = start_row
    for k in range(1, expected_parts + 1):
        cur = permute_row(images, cur)
        part_ends.append(
            {
                "part": k,
                "row": row_to_string(cur),
                "is_start_row": cur == start_row,
                "is_rounds": cur == rounds,
            }
        )
        if cur == start_row and k < expected_parts:
            early_returns.append(k)
    final_row = cur
    closes = final_row == start_row

    # 轨道不按预期闭合时，指出不一致的钟位
    inconsistent: list[dict] = []
    if not closes:
        final_images = perm_power(images, expected_parts)
        for b in range(1, stage + 1):
            if final_images[b - 1] != b:
                inconsistent.append(
                    {
                        "bell": b,
                        "image": final_images[b - 1],
                        "start_position": start_row.index(b) + 1,
                        "end_position": final_row.index(b) + 1,
                    }
                )

    # part 内及跨 part 重复扫描（带双方来源）
    repeat = _first_repeat(images, part1_rows, expected_parts, start_row)
    first_conflict = None
    if repeat is not None:
        row, first_g, second_g = repeat
        first_conflict = {
            "row": row_to_string(row),
            "first": _occurrence_of(first_g, seg, start_change, events),
            "second": _occurrence_of(second_g, seg, start_change, events),
        }

    return {
        "segment": _segment_payload(rows, lead_end_indices, start_change, end_change),
        "expected_parts": expected_parts,
        "fixed_bells": list(fixed_bells),
        "permutation": permutation,
        "fixed_bell_checks": fixed_checks,
        "fixed_bells_satisfied": all(c["ok"] for c in fixed_checks),
        "part_ends": part_ends,
        "early_returns": early_returns,
        "closes_as_expected": closes,
        "inconsistent_bells": inconsistent,
        "truth": "true" if first_conflict is None else "false",
        "first_conflict": first_conflict,
        "start_row": row_to_string(start_row),
        "final_row": row_to_string(final_row),
        "comes_round": closes,
        "rounds_return": final_row == rounds,
        "total_changes": expected_parts * seg,
        "total_rows": expected_parts * seg + 1,
    }


# ---------------- 候选区段枚举 ----------------

def enumerate_segments(
    *,
    stage: int,
    rows: list[tuple[int, ...]],
    lead_end_indices: list[int],
    fixed_bells: list[int],
    min_parts: int,
    max_parts: int,
    max_results: int,
    max_search: int,
) -> dict:
    """在 touch 的全部 lead-end 区段中枚举整首为真的 multipart composition。

    候选按 touch 中的位置顺序（起始 change、结束 change 升序）生成；先按
    fixed bells 与轨道长度（φ 的阶须落在 [min_parts, max_parts]，此时 part
    数被唯一确定为阶）剪枝，再做行交集扫描，只保留整首为真的结果。
    """
    rounds = tuple(range(1, stage + 1))
    starts = [0] + list(lead_end_indices)
    ends = list(lead_end_indices)
    total_candidates = sum(1 for s in starts for e in ends if e > s)

    results: list[dict] = []
    checked = 0
    pruned_fixed = 0
    pruned_orbit = 0
    pruned_rows = 0
    pruned_size = 0
    truncated = False
    truncation_reason = None

    for s in starts:
        for e in ends:
            if e <= s:
                continue
            if checked >= max_search:
                truncated = True
                truncation_reason = f"达到搜索上限 max_search={max_search}"
                break
            checked += 1
            images = bell_permutation(rows[s], rows[e])
            # 剪枝 1：必须保持原位的钟
            if any(images[b - 1] != b for b in fixed_bells):
                pruned_fixed += 1
                continue
            # 剪枝 2：轨道长度（part 数须等于 φ 的阶才能整首闭合且不重复）
            order = perm_order(images)
            if not min_parts <= order <= max_parts:
                pruned_orbit += 1
                continue
            # 剪枝 3：展开规模上限
            seg = e - s
            if order * seg > MAX_COMPOSITION_CHANGES:
                pruned_size += 1
                continue
            # 剪枝 4：行交集扫描（part 内及跨 part 重复）
            part1_rows = rows[s : e + 1]
            if _first_repeat(images, part1_rows, order, rows[s]) is not None:
                pruned_rows += 1
                continue

            # 整首为真：整理结果（part end、循环、闭合信息）
            permutation = _perm_payload(images)
            part_ends = []
            cur = rows[s]
            for k in range(1, order + 1):
                cur = permute_row(images, cur)
                part_ends.append(
                    {
                        "part": k,
                        "row": row_to_string(cur),
                        "is_start_row": cur == rows[s],
                        "is_rounds": cur == rounds,
                    }
                )
            results.append(
                {
                    "segment": _segment_payload(rows, lead_end_indices, s, e),
                    "parts": order,
                    "permutation": permutation,
                    "part_ends": part_ends,
                    "early_returns": [],
                    "start_row": row_to_string(rows[s]),
                    "final_row": row_to_string(cur),
                    "comes_round": True,
                    "rounds_return": cur == rounds,
                    "total_changes": order * seg,
                    "total_rows": order * seg + 1,
                }
            )
            if len(results) >= max_results:
                truncated = True
                truncation_reason = f"达到结果上限 max_results={max_results}"
                break
        if truncated:
            break

    if not truncated and not results:
        truncation_reason = "候选区段已穷尽，未发现整首为真的 multipart composition"

    return {
        "candidates": total_candidates,
        "checked": checked,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "pruned_by_fixed_bells": pruned_fixed,
        "pruned_by_orbit": pruned_orbit,
        "pruned_by_size": pruned_size,
        "pruned_by_rows": pruned_rows,
        "total_results": len(results),
        "results": results,
    }
