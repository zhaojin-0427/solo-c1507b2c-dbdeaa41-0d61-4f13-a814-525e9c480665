"""Round block 等价分析：lead 边界循环移位 + 可选反向展开下的 touch 等价归并。

round block 是从 rounds 出发又回到 rounds 的闭合 touch。同一 round block
可以从任一 lead 边界重开：把该边界的起行重标为 rounds（对全部 row 施加
唯一的钟置换 ψ_s 使 ψ_s(r_s) = rounds），得到同一循环序列的另一种展开；
允许反向展开时，倒序序列同样作为候选。全部允许的（移位, 方向）候选中
字典序最小的规范化序列即**规范指纹**，指纹相同的 touch 判为等价并归为
一组，组内按 (touch id, version) 字典序稳定选出代表项。

成员相对代表项的关系由三要素刻画：

- 方向：两者展开方向相同为 forward，相反为 reversed；
- 移位：成员的起行（rounds）对齐到代表项 touch 中的 change 序号
  （落在 lead end 时同时给出 lead 序号）；
- 钟号映射：代表项钟号 → 成员钟号的置换（images 表示）。

不等价的两项比较其规范指纹序列，给出最早分歧 row 及双方在原 touch 中的
method / lead / call 来源。全部计算对同一输入完全确定。
"""
from __future__ import annotations

from bisect import bisect_right
from math import lcm

from .multipart import bell_permutation, perm_cycles, permute_row
from .notation import row_to_string
from .storage import canonical_hash

# 单个 touch 的 change 数上限（防止过大的展开占用过多资源）
MAX_TOUCH_CHANGES = 20_000
# 单个 touch 的移位候选 × 序列长度（移位单元）上限
MAX_SHIFT_CELLS = 4_000_000
# 一次分析中全部 touch 的移位单元总量上限
MAX_TOTAL_CELLS = 20_000_000


class RoundBlockError(ValueError):
    """round block 等价分析请求非法。``code`` 为机器可读错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------- 规范化与循环移位 ----------------

def normalization_images(row: tuple[int, ...]) -> tuple[int, ...]:
    """把起行重标为 rounds 的钟置换 images：images[b-1] = 钟 b 在 row 中的位置（1 起）。"""
    return bell_permutation(row, tuple(range(1, len(row) + 1)))


def rotated_rows(
    rows: list[tuple[int, ...]], shift: int, reverse: bool = False
) -> list[tuple[int, ...]]:
    """从 change 序号 shift 处重开循环序列（含首尾闭合 row，共 n+1 个）。"""
    n = len(rows) - 1
    if reverse:
        return [rows[(shift - i) % n] for i in range(n + 1)]
    return [rows[(shift + i) % n] for i in range(n + 1)]


def normalize_rows(
    rows: list[tuple[int, ...]], shift: int, reverse: bool = False
) -> list[tuple[int, ...]]:
    """从 shift 重开序列并把起行重标为 rounds 的规范化序列。"""
    images = normalization_images(rows[shift])
    return [permute_row(images, r) for r in rotated_rows(rows, shift, reverse)]


def canonical_fingerprint(
    rows: list[tuple[int, ...]], shifts: list[int], allow_reverse: bool
) -> tuple[list[tuple[int, ...]], int, str]:
    """全部 (移位, 方向) 候选中字典序最小的规范化序列。

    返回 (fingerprint, best_shift, best_direction)；候选按移位升序、
    forward 先于 reversed 枚举，并列时首个最小者胜出（确定性）。
    """
    n = len(rows) - 1
    best: list[tuple[int, ...]] | None = None
    best_shift = shifts[0]
    best_dir = "forward"
    for s in shifts:
        images = normalization_images(rows[s])
        for rev in ((False, True) if allow_reverse else (False,)):
            cur: list[tuple[int, ...]] = []
            take = best is None
            abandoned = False
            for i in range(n + 1):
                idx = (s - i) % n if rev else (s + i) % n
                r = permute_row(images, rows[idx])
                cur.append(r)
                if not take:
                    if r < best[i]:
                        take = True
                    elif r > best[i]:
                        abandoned = True
                        break
            if not abandoned and take:
                best = cur
                best_shift = s
                best_dir = "reversed" if rev else "forward"
    return best, best_shift, best_dir


def fingerprint_hash(stage: int, sequence: list[tuple[int, ...]]) -> str:
    """规范指纹序列的内容哈希。"""
    return canonical_hash({"stage": stage, "rows": [row_to_string(r) for r in sequence]})


# ---------------- 成员对齐 ----------------

def fingerprint_position(change: int, shift: int, direction: str, n: int) -> int:
    """原 touch 中 change 序号在规范指纹序列中的位置。"""
    if direction == "reversed":
        return (shift - change) % n
    return (change - shift) % n


def original_change(position: int, shift: int, direction: str, n: int) -> int:
    """规范指纹序列位置对应的原 touch change 序号。"""
    if direction == "reversed":
        return (shift - position) % n
    return (shift + position) % n


def alignment(member: dict, representative: dict, n: int):
    """成员相对代表项的对齐。

    两个 transform 均为 ``{"shift": s, "direction": d}``。返回
    ``(direction, shift_change, align)``：direction 为 forward/reversed；
    shift_change 为成员起行（change 0）对齐到代表项 touch 的 change 序号；
    ``align(j)`` 给出成员 change j 对应的代表项 change 序号。
    """
    sm, dm = member["shift"], member["direction"]
    sp, dp = representative["shift"], representative["direction"]
    direction = "forward" if dm == dp else "reversed"

    def align(j: int) -> int:
        pos = fingerprint_position(j, sm, dm, n)
        return original_change(pos, sp, dp, n)

    return direction, align(0), align


def bell_mapping_images(
    member_open_row: tuple[int, ...], representative_open_row: tuple[int, ...]
) -> tuple[int, ...]:
    """代表项钟号 → 成员钟号的置换 images。

    两个起行重标后都为 rounds：代表项起行第 i 位的钟 b 与成员起行
    第 i 位的钟相对应（归一化 ψ 的复合 ψ_member⁻¹ ∘ ψ_representative）。
    """
    images = [0] * len(member_open_row)
    for pos, b in enumerate(representative_open_row):
        images[b - 1] = member_open_row[pos]
    return tuple(images)


def perm_payload(images: tuple[int, ...]) -> dict:
    """钟号映射置换的展示结构（images / 循环 / 固定钟 / 阶）。"""
    cycles, fixed = perm_cycles(images)
    order = 1
    for c in cycles:
        order = lcm(order, len(c))
    return {"images": list(images), "cycles": cycles, "fixed_bells": fixed, "order": order}


# ---------------- lead / change 定位 ----------------

def lead_of_change(lead_end_indices: list[int], change: int) -> int | None:
    """产生指定 change 序号 row 的 lead 序号（1 起）；change 0 为起始 row，返回 None。"""
    if change <= 0:
        return None
    return bisect_right(lead_end_indices, change - 1) + 1


def shift_lead(lead_end_indices: list[int], change: int) -> int | None:
    """移位点对应的 lead 序号：0 表示原始起行；非 lead end 返回 None。"""
    if change == 0:
        return 0
    i = bisect_right(lead_end_indices, change)
    if i >= 1 and lead_end_indices[i - 1] == change:
        return i
    return None


# ---------------- 分歧与来源 ----------------

def first_divergence(
    seq_a: list[tuple[int, ...]], seq_b: list[tuple[int, ...]]
) -> int | None:
    """两个规范化序列的最早分歧位置；一个为另一个前缀时返回较短序列的长度。"""
    n = min(len(seq_a), len(seq_b))
    for i in range(n):
        if seq_a[i] != seq_b[i]:
            return i
    if len(seq_a) != len(seq_b):
        return n
    return None


def row_provenance(
    rows: list[tuple[int, ...]], events: list[dict], change: int
) -> dict:
    """原 touch 中某个 change 序号 row 的来源（method / lead / call）。"""
    if change == 0:
        return {"type": "start", "change": 0, "row": row_to_string(rows[0])}
    ev = events[change - 1]
    return {
        "type": "change",
        "change": ev["change"],
        "lead": ev["lead"],
        "change_in_lead": ev["change_in_lead"],
        "method": ev["method"],
        "method_id": ev["method_id"],
        "method_version": ev["method_version"],
        "call": ev["call"],
        "notation": ev["notation"],
        "source": ev["source"],
        "row": ev["row"],
    }
