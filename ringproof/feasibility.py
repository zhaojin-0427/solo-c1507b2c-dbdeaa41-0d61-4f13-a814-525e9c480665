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
