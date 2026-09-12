"""方法相假图谱：以 course head 为独立分析对象的方法间相假分析。

调用方选择 1～8 个同钟数方法的不可变版本与一个参考 course head，指定
2～6 口可变钟；其余钟位固定，枚举可变钟在参考 course head 所占位置上的
全部排列（不超过 720 个）作为 course head 集合。每个 (方法, course head)
组合独立展开 plain course：反复施加方法的 plain lead 直到 lead head 回到
course head（上限 max_leads 个 lead，达到上限仍未闭合则拒绝创建）。

分析结果：各 course 的闭合长度（lead 数）与内部真值；组合间共享 row 数
构成的相假矩阵；首次冲突追溯双方的 method、course head、lead 与 change；
按共享 row 关系求连通分组。整个过程对同一输入完全确定。
"""
from __future__ import annotations

from itertools import islice, permutations
from math import factorial

from .engine import apply_change
from .notation import row_to_string

MAX_COURSE_HEADS = 720  # 6! — 可变钟（≤6 口）排列数上限
MAX_LEADS = 1000  # 单个 plain course 的 lead 上限


class FalsenessError(ValueError):
    """相假分析请求非法。``code`` 为机器可读错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def enumerate_course_heads(
    reference: tuple[int, ...],
    mutable_bells: list[int],
    max_course_heads: int = MAX_COURSE_HEADS,
) -> tuple[list[tuple[int, ...]], int]:
    """固定非可变钟，枚举可变钟在参考 course head 所占位置上的全部排列。

    顺序确定：第一个为参考 course head 本身，其余按可变钟在参考排列中的
    取值顺序做字典序排列。返回 (course_heads, total_possible)；枚举数量被
    max_course_heads 截断时只取前 max_course_heads 个（total_possible 仍
    给出全部可能数）。
    """
    mutable = set(mutable_bells)
    positions = [i for i, b in enumerate(reference) if b in mutable]
    values = [reference[i] for i in positions]
    total = factorial(len(values))
    heads: list[tuple[int, ...]] = []
    for perm in islice(permutations(values), max_course_heads):
        row = list(reference)
        for i, v in zip(positions, perm):
            row[i] = v
        heads.append(tuple(row))
    return heads, total


def expand_plain_course(
    changes: list[frozenset[int]],
    course_head: tuple[int, ...],
    max_leads: int,
) -> dict:
    """从 course_head 起反复施加 plain lead，直到 lead head 回到 course_head。

    返回 ``{"leads", "changes", "rows", "truth", "first_repeat"}``；rows
    逐项为 (row, lead, change_in_lead, change)，起始 course head 记为
    (…, 0, 0, 0)。达到 max_leads 仍未闭合抛
    FalsenessError(COURSE_NOT_CLOSED)。
    """
    rows: list[tuple[tuple[int, ...], int, int, int]] = [(course_head, 0, 0, 0)]
    cur = course_head
    change_no = 0
    closed_at: int | None = None
    for lead_no in range(1, max_leads + 1):
        for pos, places in enumerate(changes, 1):
            cur = apply_change(cur, places)
            change_no += 1
            rows.append((cur, lead_no, pos, change_no))
        if cur == course_head:
            closed_at = lead_no
            break
    if closed_at is None:
        raise FalsenessError(
            "COURSE_NOT_CLOSED",
            f"plain course 在 {max_leads} 个 lead 内未回到 course head",
        )

    # 内部真值：终点回到 course head 属正常闭合，不算重复
    seen: dict[tuple[int, ...], int] = {}
    first_repeat = None
    last = len(rows) - 1
    for i, (row, lead, pos, ch) in enumerate(rows):
        j = seen.get(row)
        if j is not None:
            if i == last and row == course_head:
                continue
            first_repeat = {
                "row": row,
                "changes": [rows[j][3], ch],
                "first": {
                    "lead": rows[j][1],
                    "change_in_lead": rows[j][2],
                    "change": rows[j][3],
                },
                "second": {"lead": lead, "change_in_lead": pos, "change": ch},
            }
            break
        seen[row] = i
    return {
        "leads": closed_at,
        "changes": change_no,
        "rows": rows,
        "truth": "true" if first_repeat is None else "false",
        "first_repeat": first_repeat,
    }


def analyze_falseness(
    methods: list[dict],
    course_heads: list[tuple[int, ...]],
    max_leads: int,
) -> dict:
    """对全部 method×course head 组合展开 plain course 并计算共享 row 关系。

    ``methods`` 逐项形如 ``{"id", "version", "name", "changes"}``（changes
    为一个 plain lead 的 places 序列）。组合顺序确定：方法按声明顺序为
    外层、course head 按枚举顺序为内层。返回 JSON 可序列化的分析结果
    （row 一律用字符串）。
    """
    courses: list[dict] = []
    occurrences: list[dict[tuple[int, ...], tuple[int, int, int]]] = []
    for m in methods:
        for head in course_heads:
            try:
                course = expand_plain_course(m["changes"], head, max_leads)
            except FalsenessError as e:
                raise FalsenessError(
                    e.code,
                    f"方法 {m['id']!r} 自 course head {row_to_string(head)} "
                    f"展开：{e.message}",
                ) from e
            index = len(courses)
            first_occ: dict[tuple[int, ...], tuple[int, int, int]] = {}
            for row, lead, pos, ch in course["rows"]:
                if row not in first_occ:
                    first_occ[row] = (lead, pos, ch)
            occurrences.append(first_occ)
            courses.append(
                {
                    "index": index,
                    "method": m["id"],
                    "method_version": m["version"],
                    "method_name": m["name"],
                    "course_head": row_to_string(head),
                    "leads": course["leads"],
                    "changes": course["changes"],
                    "truth": course["truth"],
                    "first_repeat": _repeat_payload(course["first_repeat"]),
                }
            )

    # 共享 row：row → 各组合中的首次出现来源
    owners: dict[tuple[int, ...], list[tuple[int, tuple[int, int, int]]]] = {}
    for idx, occ in enumerate(occurrences):
        for row, src in occ.items():
            owners.setdefault(row, []).append((idx, src))

    shared: dict[tuple[int, int], int] = {}
    for lst in owners.values():
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                a, b = lst[i][0], lst[j][0]
                key = (a, b) if a < b else (b, a)
                shared[key] = shared.get(key, 0) + 1

    degree = [0] * len(courses)
    for a, b in shared:
        degree[a] += 1
        degree[b] += 1
    for c, d in zip(courses, degree):
        c["shared_with"] = d

    entries = [
        {"a": a, "b": b, "shared_rows": n}
        for (a, b), n in sorted(shared.items())
    ]

    first_conflict = None
    if shared:
        a, b = min(shared)
        common = set(occurrences[a]) & set(occurrences[b])
        row = min(common, key=lambda r: (occurrences[a][r][2], occurrences[b][r][2]))
        first_conflict = {
            "row": row_to_string(row),
            "shared_rows": shared[(a, b)],
            "a": _side_payload(a, courses[a], occurrences[a][row]),
            "b": _side_payload(b, courses[b], occurrences[b][row]),
        }

    n = len(courses)
    return {
        "courses": courses,
        "matrix": {"size": n, "entries": entries},
        "first_conflict": first_conflict,
        "groups": _connected_groups(n, shared),
        "checks": {
            "courses": n,
            "pairs": n * (n - 1) // 2,
            "shared_pairs": len(shared),
            "distinct_rows": len(owners),
        },
    }


def _repeat_payload(first_repeat: dict | None) -> dict | None:
    """内部首次重复的来源（row 转字符串）；无重复为 None。"""
    if first_repeat is None:
        return None
    return {
        "row": row_to_string(first_repeat["row"]),
        "changes": first_repeat["changes"],
        "first": first_repeat["first"],
        "second": first_repeat["second"],
    }


def _side_payload(
    index: int, course: dict, src: tuple[int, int, int]
) -> dict:
    """冲突一侧的来源：组合、method、course head 与 row 首次出现的 lead/change。"""
    lead, pos, ch = src
    return {
        "index": index,
        "method": course["method"],
        "method_version": course["method_version"],
        "course_head": course["course_head"],
        "lead": lead,
        "change_in_lead": pos,
        "change": ch,
    }


def _connected_groups(n: int, shared: dict[tuple[int, int], int]) -> list[dict]:
    """按共享 row 关系求连通分组（并查集，根取最小编号保证确定性）。

    每个组合恰好属于一个分组；无共享 row 的组合自成一组。"""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in shared:
        ra, rb = find(a), find(b)
        if ra != rb:
            if ra < rb:
                parent[rb] = ra
            else:
                parent[ra] = rb
    members: dict[int, list[int]] = {}
    for i in range(n):
        members.setdefault(find(i), []).append(i)
    return [
        {"id": k, "size": len(mems), "courses": mems}
        for k, mems in enumerate((members[root] for root in sorted(members)), 1)
    ]
