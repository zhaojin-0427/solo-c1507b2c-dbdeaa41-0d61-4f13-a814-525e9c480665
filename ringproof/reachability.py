"""Lead-head 可达图：以 lead head 为状态、call（plain/bob/single/自定义）为
动作的有向图分析。

调用方提交一个不可变方法版本、起始 lead head、call 定义（call 在 lead end
处替换方法末尾若干 change）、1～20 个目标排列、最大 lead 数与状态上限；
系统逐层 BFS 生成有向图，每个节点对每个可用动作产生一条边，边记录动作、
前后 lead head 与该 lead 的 change 数。分析结果包括：

- 各目标的可达性、最短动作序列、同长度备选数与按 lead 数/call 数/动作
  字典序排列的候选路线；
- 强连通分量（迭代 Tarjan，含单点分量）；
- 无法返回起点的区域（在图中无法沿边回到起点的节点）；
- 截断原因（达到 max_leads 或状态上限 max_states）；
- 路线真值：逐 row 校核完整 lead 序列，重复 row 追溯双方 method、lead、
  change 与 call；
- 禁用 lead head：路线不得经过（起点本身被禁用时拒绝创建）。

整个过程对同一输入完全确定。
"""
from __future__ import annotations

import collections
import heapq
from dataclasses import dataclass

from .engine import apply_change
from .notation import row_to_string

MAX_TARGETS = 20


class ReachabilityError(ValueError):
    """可达图请求非法。``code`` 为机器可读错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------- 数据类型 ----------------

# call 上下文：name / 是否 plain / 替换数 / 替换后该 lead 的 (tokens, places)
CallCtx = dict


@dataclass(frozen=True)
class LeadVariant:
    """一个动作（call）展开后的完整 lead。"""

    action: str
    call: str | None  # None 表示 plain
    tokens: tuple[str, ...]
    places: tuple[frozenset[int], ...]

    @property
    def changes(self) -> int:
        return len(self.tokens)


def build_variants(
    method_tokens: list[str],
    method_places: list[frozenset[int]],
    call_defs: dict[str, CallCtx],
) -> list[LeadVariant]:
    """生成全部可用动作：plain 恒在首位，其余 call 按名称字典序。

    ``call_defs`` 逐项形如 ``{"name", "replace", "tokens", "changes"}``
    （changes 为 call 记号展开后的 places 列表）。替换数越界抛
    ReachabilityError(CALL_REPLACE_RANGE)。
    """
    variants = [
        LeadVariant("plain", None, tuple(method_tokens), tuple(method_places))
    ]
    for name in sorted(call_defs):
        spec = call_defs[name]
        k = spec["replace"]
        if not 1 <= k <= len(method_tokens):
            raise ReachabilityError(
                "CALL_REPLACE_RANGE",
                f"call {name!r} 替换 {k} 个 change，超出 lead 长度 "
                f"{len(method_tokens)}",
            )
        tokens = tuple(method_tokens[:-k]) + tuple(spec["tokens"])
        places = tuple(method_places[:-k]) + tuple(spec["changes"])
        variants.append(LeadVariant(name, name, tokens, places))
    return variants


def lead_rows(
    head: tuple[int, ...], variant: LeadVariant
) -> list[tuple[int, ...]]:
    """从 lead head 施加完整 lead 的 places，返回不含 head 本身的逐 change row。"""
    rows: list[tuple[int, ...]] = []
    cur = head
    for places in variant.places:
        cur = apply_change(cur, places)
        rows.append(cur)
    return rows


def lead_end(head: tuple[int, ...], variant: LeadVariant) -> tuple[int, ...]:
    return lead_rows(head, variant)[-1]


# ---------------- BFS 建图 ----------------


def build_graph(
    start: tuple[int, ...],
    variants: list[LeadVariant],
    max_leads: int,
    max_states: int,
    forbidden: set[tuple[int, ...]],
) -> dict:
    """逐层 BFS 生成 lead-head 可达有向图。

    返回：
    - ``nodes`` — 去重节点（lead head 元组），按发现顺序；
    - ``index`` — row → 节点下标；
    - ``depth`` — 自起点的最短 lead 数；
    - ``edges`` — ``{"action", "call", "from", "to", "changes"}`` 列表，
      按 (from 下标, 动作顺序) 排序；
    - ``adj`` — 每个节点的出边下标；
    - ``truncated`` / ``truncation_reason`` / ``limit``；
    - ``blocked`` — 边的终点落在禁用 lead head 的次数；
    - ``forbidden`` — 禁用 lead head 字符串列表（输入回显）。
    """
    nodes: list[tuple[int, ...]] = [start]
    index: dict[tuple[int, ...], int] = {start: 0}
    depth = {0: 0}
    frontier = [0]
    edges: list[dict] = []
    blocked = 0
    truncated = False
    reason: str | None = None
    limit = None
    cap_hit_level: int | None = None

    for level in range(1, max_leads + 1):
        if not frontier:
            break
        # 上一层恰好填满状态上限：本层不再展开
        if len(nodes) >= max_states:
            truncated = True
            limit = "max_states"
            reason = (
                f"可达 lead head 达到状态上限 max_states={max_states}，"
                f"在第 {level} 个 lead 处截断（更深的状态未展开）"
            )
            break
        next_frontier_set: set[tuple[int, ...]] = set()
        for node_idx in frontier:
            head = nodes[node_idx]
            for ai, variant in enumerate(variants):
                dest = lead_end(head, variant)
                if dest in forbidden:
                    blocked += 1
                    continue
                j = index.get(dest)
                if j is None:
                    # 达到状态上限：不再收录新节点
                    if len(nodes) >= max_states:
                        if not truncated:
                            truncated = True
                            limit = "max_states"
                            reason = (
                                f"可达 lead head 达到状态上限 max_states={max_states}，"
                                f"在第 {level} 个 lead 处截断（更深的状态未展开）"
                            )
                        continue
                    j = len(nodes)
                    index[dest] = j
                    nodes.append(dest)
                    depth[j] = level
                    next_frontier_set.add(dest)
                edges.append(
                    {
                        "from": node_idx,
                        "to": j,
                        "action": variant.action,
                        "call": variant.call,
                        "action_index": ai,
                        "changes": variant.changes,
                    }
                )
        if truncated:
            break
        frontier = [index[r] for r in sorted(next_frontier_set, key=row_to_string)]

    if not truncated and frontier:
        # 最后一层的新节点已收录但尚未展开即达 lead 上限
        truncated = True
        limit = "max_leads"
        reason = (
            f"达到最大 lead 数 max_leads={max_leads}：第 {max_leads} 个 lead 之后的"
            f"状态未继续展开（图中节点均在 {max_leads} 个 lead 内可达）"
        )

    edges.sort(key=lambda e: (e["from"], e["action_index"]))
    adj: list[list[int]] = [[] for _ in nodes]
    for ei, e in enumerate(edges):
        adj[e["from"]].append(ei)

    return {
        "nodes": nodes,
        "index": index,
        "depth": depth,
        "edges": edges,
        "adj": adj,
        "truncated": truncated,
        "truncation_reason": reason,
        "limit": limit,
        "blocked": blocked,
        "forbidden": sorted(row_to_string(r) for r in forbidden),
    }


# ---------------- 强连通分量与无回区域 ----------------


def strongly_connected_components(
    nodes: list[tuple[int, ...]], adj: list[list[int]], edges: list[dict]
) -> list[list[int]]:
    """迭代版 Tarjan 求强连通分量（含单点/无自环分量），结果确定性排序。"""
    n = len(nodes)
    index = [-1] * n
    low = [0] * n
    on_stack = [False] * n
    stack: list[int] = []
    counter = 0
    comps: list[list[int]] = []

    for root in range(n):
        if index[root] != -1:
            continue
        work: list[tuple[int, int]] = [(root, 0)]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        while work:
            v, pi = work[-1]
            if pi < len(adj[v]):
                e = edges[adj[v][pi]]
                work[-1] = (v, pi + 1)
                w = e["to"]
                if index[w] == -1:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack[w] = True
                    work.append((w, 0))
                elif on_stack[w] and index[w] < low[v]:
                    low[v] = index[w]
            else:
                if low[v] == index[v]:
                    comp: list[int] = []
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        comp.append(w)
                        if w == v:
                            break
                    comps.append(sorted(comp))
                work.pop()
                if work:
                    p = work[-1][0]
                    if low[v] < low[p]:
                        low[p] = low[v]

    # 确定性：按分量内最小行字典序
    comps.sort(key=lambda comp: row_to_string(nodes[comp[0]]))
    return comps


def cannot_return_region(start_idx: int, graph: dict) -> list[int]:
    """在图中无法沿有向边回到起点的节点（反向 DFS 自起点不可达者）。"""
    edges, adj = graph["edges"], graph["adj"]
    rev: list[list[int]] = [[] for _ in graph["nodes"]]
    for ei, e in enumerate(edges):
        rev[e["to"]].append(ei)
    seen = {start_idx}
    stack = [start_idx]
    while stack:
        v = stack.pop()
        for ei in rev[v]:
            w = edges[ei]["from"]
            if w not in seen:
                seen.add(w)
                stack.append(w)
    return [i for i in range(len(graph["nodes"])) if i not in seen]


# ---------------- 最短动作序列与同长度备选数 ----------------


def shortest_structure(start_idx: int, target_idx: int, graph: dict) -> dict:
    """逐层 BFS：距离、同长度动作序列数、首选父边。

    动作序列按 (call 数, 动作名称字典序) 比较：call 数少者优先；同 call 数
    按完整动作名序列的字符串字典序（"bob" < "plain" < "single"）。首选序列
    为同长度中 call 数最少、动作字典序最小者。序列数为精确整数（大整数）。
    """
    edges, adj = graph["edges"], graph["adj"]
    dist = {start_idx: 0}
    counts = {start_idx: 1}
    best_parent: dict[int, dict] = {}
    best_key: dict[int, tuple] = {start_idx: (0, ())}
    frontier = [start_idx]
    found = False
    level = 0
    while frontier and not found:
        level += 1
        nxt: dict[int, list[dict]] = {}
        for v in frontier:
            for ei in adj[v]:
                e = edges[ei]
                w = e["to"]
                if w in dist:
                    continue  # 同层边/回边：不参与更短层
                nxt.setdefault(w, []).append(e)
                if w == target_idx:
                    found = True
        new_frontier: list[int] = []
        for w, incoming in nxt.items():
            dist[w] = level
            counts[w] = sum(counts[e["from"]] for e in incoming)
            # 首选父边：父节点首选序列追加本边动作后，(call 数, 动作名字典序) 最优
            best = min(
                incoming,
                key=lambda e: _extend_key(best_key[e["from"]], e["action"]),
            )
            best_parent[w] = {"edge": best, "parent": best["from"]}
            best_key[w] = _extend_key(best_key[best["from"]], best["action"])
            new_frontier.append(w)
        frontier = new_frontier

    if target_idx not in dist:
        return {"distance": None, "count": 0, "actions": None, "dist": dist}

    actions = _rebuild_actions(target_idx, best_parent, edges)
    return {
        "distance": dist[target_idx],
        "count": counts[target_idx],
        "actions": actions,
        "dist": dist,
    }


def _extend_key(key: tuple, action: str) -> tuple:
    """父节点首选键 (call 数, 动作名序列) 追加一条动作后的键。"""
    calls, seq = key
    return (calls + (0 if action == "plain" else 1), seq + (action,))


def _rebuild_actions(node: int, best_parent: dict, edges: list[dict]) -> list[str]:
    edge_path: list[dict] = []
    cur = node
    while cur in best_parent:
        e = best_parent[cur]["edge"]
        edge_path.append(e)
        cur = best_parent[cur]["parent"]
    edge_path.reverse()
    return [e["action"] for e in edge_path]


def enumerate_shortest_routes(
    start_idx: int,
    target_idx: int,
    shortest: dict,
    graph: dict,
    variants: list[LeadVariant],
    limit: int,
    pop_budget: int = 50000,
) -> tuple[list[dict], bool]:
    """枚举至多 limit 条最短动作序列，按 (call 数, 动作名字典序) 全局有序。

    在「只走 dist+1 的最短边」的 DAG 上用最小堆枚举：先反向标出能到达
    目标的节点，再以完整序列 (call 数, 动作名元组) 为堆键扩展，每次弹出
    目标节点即得到下一最优路线。返回 (路线列表, 是否因预算截断)。
    """
    edges, adj = graph["edges"], graph["adj"]
    dist = shortest["dist"]

    pred: dict[int, list[dict]] = {i: [] for i in dist}
    for e in edges:
        a, b = e["from"], e["to"]
        if a in dist and b in dist and dist[b] == dist[a] + 1:
            pred[b].append(e)
    can_reach = {target_idx}
    stack = [target_idx]
    while stack:
        b = stack.pop()
        for e in pred[b]:
            a = e["from"]
            if a not in can_reach:
                can_reach.add(a)
                stack.append(a)
    if start_idx not in can_reach:
        return [], False

    results: list[dict] = []
    counter = 0
    # (call 数, 动作名序列, 唯一序号, 当前节点, 边路径)
    heap: list[tuple] = [(0, (), counter, start_idx, [])]
    pops = 0
    truncated = False
    while heap and len(results) < limit:
        if pops >= pop_budget:
            truncated = True
            break
        calls, seq, _tie, v, path = heapq.heappop(heap)
        pops += 1
        if v == target_idx:
            results.append(_route_payload(path, graph, variants))
            continue
        for ei in adj[v]:
            e = edges[ei]
            w = e["to"]
            if w not in can_reach or dist.get(w) != dist[v] + 1:
                continue
            counter += 1
            action = e["action"]
            heapq.heappush(
                heap,
                (
                    calls + (0 if action == "plain" else 1),
                    seq + (action,),
                    counter,
                    w,
                    path + [e],
                ),
            )
    return results, truncated


def _route_payload(
    edge_path: list[dict], graph: dict, variants: list[LeadVariant]
) -> dict:
    nodes = graph["nodes"]
    leads = []
    for k, e in enumerate(edge_path, 1):
        leads.append(
            {
                "lead": k,
                "call": e["call"],
                "action": e["action"],
                "lead_head_before": row_to_string(nodes[e["from"]]),
                "lead_head_after": row_to_string(nodes[e["to"]]),
                "changes": e["changes"],
            }
        )
    return {
        "actions": [e["action"] for e in edge_path],
        "num_leads": len(edge_path),
        "num_calls": sum(1 for e in edge_path if e["call"] is not None),
        "total_changes": sum(e["changes"] for e in edge_path),
        "leads": leads,
    }


# ---------------- 逐 row 真值校核 ----------------


def check_route_truth(
    start: tuple[int, ...],
    actions: list[str],
    variants_by_action: dict[str, LeadVariant],
    method_id: str,
) -> dict:
    """逐 row 展开动作序列并校核真值（lead head 与 lead 内全部 row）。

    除整条路线最后一个 change 的 row 回到起点（正常 come-round）外，任何
    重复 row（含同一 lead 内部的重复）均为假。重复时给出双方来源：method、
    lead（0 表示起点）、change_in_lead、全局 change 与该 lead 的 call。
    """
    seen: dict[tuple[int, ...], dict] = {
        start: {
            "method": method_id,
            "lead": 0,
            "change_in_lead": 0,
            "change": 0,
            "call": None,
        }
    }
    cur = start
    change_no = 0
    for lead_no, action in enumerate(actions, 1):
        variant = variants_by_action[action]
        intra_src: dict[tuple[int, ...], dict] = {}
        for pos, places in enumerate(variant.places, 1):
            cur = apply_change(cur, places)
            change_no += 1
            src = {
                "method": method_id,
                "lead": lead_no,
                "change_in_lead": pos,
                "change": change_no,
                "call": variant.call,
            }
            if cur in seen:
                first = seen[cur]
            elif cur in intra_src:
                first = intra_src[cur]
            else:
                first = None
            if first is not None:
                is_come_round = (
                    cur == start
                    and lead_no == len(actions)
                    and pos == len(variant.places)
                )
                if not is_come_round:
                    return {
                        "truth": "false",
                        "row": row_to_string(cur),
                        "first_repeat": {
                            "row": row_to_string(cur),
                            "first": first,
                            "second": src,
                        },
                    }
            intra_src[cur] = src
            seen[cur] = src
    return {"truth": "true", "first_repeat": None}


# ---------------- 反向距离与为真路线搜索（A*） ----------------


def _reverse_distances(target_idx: int, graph: dict) -> dict[int, int]:
    """自目标沿反向边的最少边（lead）数；图截断时仅覆盖反向可达部分。"""
    edges = graph["edges"]
    rev: dict[int, list[int]] = {i: [] for i in range(len(graph["nodes"]))}
    for e in edges:
        rev[e["to"]].append(e["from"])
    dist = {target_idx: 0}
    frontier = [target_idx]
    d = 0
    while frontier:
        d += 1
        nxt: list[int] = []
        for v in frontier:
            for w in rev[v]:
                if w not in dist:
                    dist[w] = d
                    nxt.append(w)
        frontier = nxt
    return dist


def _reverse_call_distances(target_idx: int, graph: dict) -> dict[int, int]:
    """自目标沿反向边的最少 **call**（非 plain）边数（0-1 BFS，可采纳下界）。"""
    n = len(graph["nodes"])
    rev: dict[int, list[tuple[int, int]]] = {i: [] for i in range(n)}
    for e in graph["edges"]:
        cost = 0 if e["call"] is None else 1
        rev[e["to"]].append((e["from"], cost))
    dist = {target_idx: 0}
    dq = collections.deque([target_idx])
    while dq:
        v = dq.popleft()
        for w, cost in rev[v]:
            nd = dist[v] + cost
            if w not in dist or nd < dist[w]:
                dist[w] = nd
                if cost:
                    dq.append(w)
                else:
                    dq.appendleft(w)
    return dist


def _extend_lead(
    cur: tuple[int, ...],
    variant: LeadVariant,
    end_node: int,
    target_idx: int,
    is_final: bool,
    start_row: tuple[int, ...],
    path_rows: frozenset,
) -> tuple[bool, list[tuple[int, ...]], tuple[int, ...]]:
    """逐 change 检查一个 lead 的全部 row。

    lead 内部不得重复；除「整路线最后一个 lead 的末 row 回到起点」外，任何
    row 不得与已有路径（含 lead 中途）重复。返回 (是否为真, 本 lead 新增
    row, 末 row)。
    """
    rows = lead_rows(cur, variant)
    intra: set[tuple[int, ...]] = set()
    new_rows: list[tuple[int, ...]] = []
    for pos, row in enumerate(rows):
        last_change = pos == len(rows) - 1
        come_round = (
            is_final and last_change and end_node == target_idx and row == start_row
        )
        if row in intra:
            return False, [], rows[-1]
        if row in path_rows and not come_round:
            return False, [], rows[-1]
        intra.add(row)
        if row not in path_rows:
            new_rows.append(row)
    return True, new_rows, rows[-1]


def search_true_route(
    start_idx: int,
    target_idx: int,
    shortest: dict,
    graph: dict,
    variants: list[LeadVariant],
    max_leads: int,
    budget: int,
    method_id: str = "",
) -> dict:
    """搜索到达目标的**为真**路线，严格按 (lead 数, call 数, 动作名字典序)
    取第一条。

    外层按 lead 数迭代加深（自最短距离起）；固定 lead 数内做 A*：下界为
    自当前节点到目标的反向最少 call 边数（0-1 BFS，可采纳），堆键为
    (已用 call 数 + 下界, 动作名序列)。逐 lead 检查**全部 row**（含 lead
    内部重复与 lead 中途撞上路径 row），唯一允许的重复是整路线最后一个
    change 回到起点。``budget`` 为 lead 扩展上限。
    """
    edges, adj, nodes = graph["edges"], graph["adj"], graph["nodes"]
    variants_by_action = {v.action: v for v in variants}
    rev_leads = _reverse_distances(target_idx, graph)
    rev_calls = _reverse_call_distances(target_idx, graph)
    start_row = nodes[start_idx]
    pushed = 0
    truncated = False

    lower = shortest["distance"]
    if lower is None:
        return {"route": None, "truncated": False, "edges_checked": pushed}
    lower_loop = max(lower, 1 if target_idx == start_idx else 0)

    for limit in range(lower_loop, max_leads + 1):
        # (已用 call 数 + call 下界, 动作名序列, 唯一序号, 节点, 深度,
        #  当前末 row, 已出现 row, 边路径)
        counter = 0
        heap = [
            (rev_calls.get(start_idx, 10 ** 9), (), counter,
             start_idx, 0, start_row, frozenset({start_row}), [])
        ]
        while heap:
            _f, seq, _tie, v, depth, cur, path_rows, path = heapq.heappop(heap)
            if depth == limit:
                if v == target_idx and depth > 0:
                    payload = _route_payload(path, graph, variants)
                    truth = check_route_truth(
                        start_row, payload["actions"], variants_by_action,
                        method_id,
                    )
                    if truth["truth"] == "true":
                        return {
                            "route": payload,
                            "truncated": truncated,
                            "edges_checked": pushed,
                            "search_leads": limit,
                        }
                continue
            remaining_after = limit - depth - 1
            for ei in adj[v]:
                if pushed >= budget:
                    truncated = True
                    break
                e = edges[ei]
                w = e["to"]
                rd = rev_leads.get(w)
                if rd is None or rd > remaining_after:
                    continue
                variant = variants_by_action[e["action"]]
                is_final = depth + 1 == limit
                ok, new_rows, end_row = _extend_lead(
                    cur, variant, w, target_idx, is_final,
                    start_row, path_rows,
                )
                if not ok:
                    continue
                pushed += 1
                calls_used = sum(1 for x in path if x["call"] is not None)
                calls_used += 0 if e["call"] is None else 1
                action = e["action"]
                counter += 1
                heapq.heappush(
                    heap,
                    (
                        calls_used + rev_calls.get(w, 10 ** 9),
                        seq + (action,),
                        counter,
                        w,
                        depth + 1,
                        end_row,
                        path_rows.union(new_rows),
                        path + [e],
                    ),
                )
            if truncated:
                break
        if truncated:
            break

    return {
        "route": None,
        "truncated": truncated,
        "edges_checked": pushed,
    }


# ---------------- 组装分析 ----------------


def analyze_reachability(
    method: dict,
    start: tuple[int, ...],
    variants: list[LeadVariant],
    targets: list[tuple[int, ...]],
    forbidden: set[tuple[int, ...]],
    max_leads: int,
    max_states: int,
    max_routes: int,
    true_search_budget: int,
) -> dict:
    """建图并对每个目标产出可达性、最短序列、备选数、候选路线真值与
    （预算内的）为真路线。``method`` 形如 ``{"id", "version", "name"}``。"""
    if start in forbidden:
        raise ReachabilityError(
            "START_FORBIDDEN",
            f"起始 lead head {row_to_string(start)} 位于禁用 lead head 中",
        )

    graph = build_graph(start, variants, max_leads, max_states, forbidden)
    comps = strongly_connected_components(graph["nodes"], graph["adj"], graph["edges"])
    no_return = cannot_return_region(0, graph)
    variants_by_action = {v.action: v for v in variants}
    method_id = method["id"]

    target_payloads = []
    for target in targets:
        t_idx = graph["index"].get(target)
        entry: dict = {
            "target": row_to_string(target),
            "reachable": t_idx is not None,
        }
        if t_idx is None:
            entry.update(
                {
                    "distance": None,
                    "shortest_sequence": None,
                    "alternative_count": 0,
                    "preferred_route": None,
                    "routes": [],
                    "routes_returned": 0,
                    "routes_truncated": False,
                    "true_route": None,
                    "true_route_truncated": False,
                    "true_search_edges_checked": 0,
                }
            )
        else:
            shortest = shortest_structure(0, t_idx, graph)
            routes, routes_truncated = enumerate_shortest_routes(
                0, t_idx, shortest, graph, variants, max_routes
            )
            for route in routes:
                truth = check_route_truth(
                    start, route["actions"], variants_by_action, method_id
                )
                route["truth"] = truth["truth"]
                route["first_repeat"] = truth["first_repeat"]
            true_search = search_true_route(
                0, t_idx, shortest, graph, variants, max_leads,
                true_search_budget, method_id,
            )
            # 首选路线为排序后的第一条最短路线（可能为假，供调用方对照）
            preferred = routes[0] if routes else None
            entry.update(
                {
                    "distance": shortest["distance"],
                    "shortest_sequence": shortest["actions"],
                    "alternative_count": shortest["count"],
                    "preferred_route": preferred,
                    "routes": routes,
                    "routes_returned": len(routes),
                    "routes_truncated": routes_truncated
                    or shortest["count"] > len(routes),
                    "true_route": true_search["route"],
                    "true_route_truncated": true_search["truncated"],
                    "true_search_edges_checked": true_search["edges_checked"],
                }
            )
        target_payloads.append(entry)

    return {
        "graph": graph,
        "components": comps,
        "no_return": no_return,
        "targets": target_payloads,
    }
