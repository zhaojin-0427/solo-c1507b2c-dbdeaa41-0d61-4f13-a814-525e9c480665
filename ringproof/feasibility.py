"""方法配额可行性的精确判定（最大流 + 下界变换）。

问题：N 个 lead 各自有候选方法集合（固定方法为单例），每个方法有
[min, max] 使用次数约束。判定是否存在一种 lead→method 分配使所有
配额同时满足。建模为带下界的可行流：

- S → 方法 m：容量 [min_m, max_m]
- 方法 m → lead i：容量 1（i 的候选含 m 时）
- lead i → T：容量 1
- T → S：容量 [N, N]（强制每个 lead 都被分配）

下界通过需求数组变换为标准最大流后判定。
"""
from __future__ import annotations

from collections import deque

from .engine import transition_ok


class _Dinic:
    def __init__(self, n: int):
        self.n = n
        self.adj: list[list[int]] = [[] for _ in range(n)]
        self.to: list[int] = []
        self.cap: list[int] = []

    def add_edge(self, u: int, v: int, c: int) -> None:
        self.adj[u].append(len(self.to))
        self.to.append(v)
        self.cap.append(c)
        self.adj[v].append(len(self.to))
        self.to.append(u)
        self.cap.append(0)

    def maxflow(self, s: int, t: int) -> int:
        flow = 0
        while True:
            level = [-1] * self.n
            level[s] = 0
            q = deque([s])
            while q:
                u = q.popleft()
                for eid in self.adj[u]:
                    v = self.to[eid]
                    if self.cap[eid] > 0 and level[v] < 0:
                        level[v] = level[u] + 1
                        q.append(v)
            if level[t] < 0:
                return flow
            it = [0] * self.n

            def dfs(u: int, f: int) -> int:
                if u == t:
                    return f
                while it[u] < len(self.adj[u]):
                    eid = self.adj[u][it[u]]
                    v = self.to[eid]
                    if self.cap[eid] > 0 and level[v] == level[u] + 1:
                        d = dfs(v, min(f, self.cap[eid]))
                        if d > 0:
                            self.cap[eid] -= d
                            self.cap[eid ^ 1] += d
                            return d
                    it[u] += 1
                return 0

            while True:
                f = dfs(s, 1 << 60)
                if f == 0:
                    break
                flow += f


def quota_feasible(
    num_leads: int,
    candidates: list[set[str]],
    quotas: dict[str, dict],
    method_ids: list[str],
) -> bool:
    """判定是否存在 lead→method 分配使各方法用量落在 [min, max] 内。

    ``candidates[i]`` 为第 i 个 lead 的候选方法集合；``quotas`` 形如
    ``{method_id: {"min": m, "max": M}}``（min/max 可为 None）。
    """
    n = num_leads
    mids = list(method_ids)
    midx = {m: j for j, m in enumerate(mids)}
    # 节点：0=S, 1=T, 2..2+M-1=方法, 之后 lead, 最后 SS/TT（下界变换的超源汇）
    m_count = len(mids)
    S, T = 0, 1
    SS = 2 + m_count + n
    TT = SS + 1
    din = _Dinic(TT + 1)
    demand = [0] * (TT + 1)

    def add_bounded(u: int, v: int, lo: int, hi: int) -> bool:
        if hi < lo:
            return False
        if hi > lo:
            din.add_edge(u, v, hi - lo)
        demand[u] -= lo
        demand[v] += lo
        return True

    ok = True
    for j, m in enumerate(mids):
        q = quotas.get(m) or {}
        lo = q.get("min") or 0
        hi = q.get("max")
        ok &= add_bounded(S, 2 + j, lo, n if hi is None else hi)
    for i, cands in enumerate(candidates):
        lead_node = 2 + m_count + i
        for m in cands:
            j = midx.get(m)
            if j is not None:
                din.add_edge(2 + j, lead_node, 1)
        ok &= add_bounded(lead_node, T, 0, 1)
    ok &= add_bounded(T, S, n, n)
    if not ok:
        return False

    total_pos = 0
    for v in range(TT + 1):
        d = demand[v]
        if d > 0:
            din.add_edge(SS, v, d)
            total_pos += d
        elif d < 0:
            din.add_edge(v, TT, -d)
    return din.maxflow(SS, TT) == total_pos


def joint_assignment_feasible(
    candidates: list[set[str]],
    quotas: dict[str, dict],
    allowed: set[tuple[str, str]] | None,
    forbidden: set[tuple[str, str]],
    method_ids: list[str],
    state_budget: int = 1_000_000,
) -> bool | None:
    """判定是否存在同时满足方法配额与相邻转换规则的 lead→method 分配。

    返回 True/False 表示确定可行/不可行；返回 None 表示状态数超出
    ``state_budget``、无法确定（调用方须按不可行拒绝，fail-closed，
    避免未校验的 touch 落库）。

    调用前需保证配额单独可行（``quota_feasible`` 为真）。DP 状态为
    ``(上一方法, 各受限方法用量的截断计数)``：有 max 的方法精确计数
    （超限即剪枝），仅有 min 的方法在 min 处饱和计数。
    """
    n = len(candidates)
    if n == 0:
        return True
    if allowed is None and not forbidden:
        return True  # 无转换约束：配额可行即联合可行

    constrained = [
        m
        for m in method_ids
        if (quotas.get(m) or {}).get("min") is not None
        or (quotas.get(m) or {}).get("max") is not None
    ]
    caps = []
    for m in constrained:
        q = quotas[m]
        hi, lo = q.get("max"), q.get("min")
        caps.append(hi if hi is not None else (lo or 0))
    cidx = {m: i for i, m in enumerate(constrained)}

    def bump(counts: tuple[int, ...], m: str) -> tuple[int, ...] | None:
        i = cidx.get(m)
        if i is None:
            return counts
        c = counts[i]
        if c >= caps[i]:
            if quotas[m].get("max") is not None:
                return None  # 超过 max，剪枝
            return counts  # 无 max：在 min 处饱和
        return counts[:i] + (c + 1,) + counts[i + 1 :]

    zero = tuple(0 for _ in constrained)
    frontier: set[tuple[str, tuple[int, ...]]] = set()
    for m in candidates[0]:
        c = bump(zero, m)
        if c is not None:
            frontier.add((m, c))
    states_seen = len(frontier)
    for pos in range(1, n):
        nxt: set[tuple[str, tuple[int, ...]]] = set()
        for last, counts in frontier:
            for m in candidates[pos]:
                if not transition_ok(last, m, allowed, forbidden)[0]:
                    continue
                c = bump(counts, m)
                if c is not None:
                    nxt.add((m, c))
        states_seen += len(nxt)
        if not nxt:
            return False
        if states_seen > state_budget:
            return None  # 预算耗尽：结果未知，由调用方 fail-closed 拒绝
        frontier = nxt
    # 终点：所有 min 达标（max 已在扩展时剪枝）
    for _, counts in frontier:
        if all(
            quotas[m].get("min") is None or counts[i] >= quotas[m]["min"]
            for i, m in enumerate(constrained)
        ):
            return True
    return False
