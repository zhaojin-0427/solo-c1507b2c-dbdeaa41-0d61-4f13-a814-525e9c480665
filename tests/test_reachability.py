"""Lead-head 可达图：plain/bob/single/自定义 call 的 lead head 转移、BFS
建图与截断、最短动作序列/同长度备选数/候选排序、强连通分量、无法返回起点
的区域、禁用 lead head、逐 row 真值校核、为真路线搜索与版本化 API。"""
import pytest
from fastapi.testclient import TestClient

from ringproof.main import create_app
from ringproof.notation import expand_notation
from ringproof.reachability import (
    ReachabilityError,
    analyze_reachability,
    build_graph,
    build_variants,
    cannot_return_region,
    check_route_truth,
    enumerate_shortest_routes,
    search_true_route,
    shortest_structure,
    strongly_connected_components,
)

PB_MINOR = "x16x16x16x16x16x12"
ROUNDS6 = (1, 2, 3, 4, 5, 6)
COURSE_HEADS = [
    (1, 2, 3, 4, 5, 6),
    (1, 3, 5, 2, 6, 4),
    (1, 5, 6, 3, 4, 2),
    (1, 6, 4, 5, 2, 3),
    (1, 4, 2, 6, 3, 5),
]


def _variants(calls=None):
    tokens, places = expand_notation(PB_MINOR, 6)
    defs = {}
    for name, notation, replace in calls or []:
        ct, cc = expand_notation(notation, 6)
        defs[name] = {"name": name, "replace": replace, "tokens": ct, "changes": cc}
    return build_variants(tokens, places, defs)


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        r = c.post(
            "/methods",
            json={"id": "pb", "name": "Plain Bob Minor", "stage": 6, "notation": PB_MINOR},
        )
        assert r.status_code == 201
        yield c


def _create(client, **over):
    payload = {
        "id": "lg",
        "method": {"id": "pb"},
        "calls": {
            "bob": {"notation": "14", "replace": 1},
            "single": {"notation": "1234", "replace": 1},
        },
        "targets": ["135264", "156342", "123456"],
        "max_leads": 20,
    }
    payload.update(over)
    return client.post("/lead-graph-analyses", json=payload)


# ---------------- 纯模块：建图 ----------------


def test_build_variants_order_and_changes():
    v = _variants([("zebra", "14", 1), ("bob", "14", 1)])
    assert [x.action for x in v] == ["plain", "bob", "zebra"]
    assert v[0].call is None
    assert all(x.changes == 12 for x in v)
    assert v[1].tokens[-1] == "14" and v[2].tokens[-1] == "14"


def test_build_variants_replace_out_of_range():
    with pytest.raises(ReachabilityError) as exc:
        _variants([("bob", "14", 13)])
    assert exc.value.code == "CALL_REPLACE_RANGE"


def test_plain_course_graph_is_one_scc():
    variants = _variants()
    g = build_graph(ROUNDS6, variants, 20, 10000, set())
    # 只允许 plain 时：5 个 course head，各一条 plain 边串成环
    assert len(g["nodes"]) == 5
    assert g["nodes"] == COURSE_HEADS
    assert len(g["edges"]) == 5
    assert {e["action"] for e in g["edges"]} == {"plain"}
    assert g["truncated"] is False
    comps = strongly_connected_components(g["nodes"], g["adj"], g["edges"])
    assert comps == [[0, 1, 2, 3, 4]]
    assert cannot_return_region(0, g) == []


def test_calls_expand_graph_to_all_lead_heads():
    variants = _variants([("bob", "14", 1), ("single", "1234", 1)])
    g = build_graph(ROUNDS6, variants, 20, 10000, set())
    # 有 call 后可离开 5 个 course head：Minor 全部 120 个偶排列 lead head
    assert len(g["nodes"]) == 120
    assert len(g["edges"]) == 360  # 120 节点 × 3 动作
    assert g["truncated"] is False and g["blocked"] == 0


def test_forbidden_head_blocks_transitions():
    variants = _variants([("bob", "14", 1)])
    forbidden = {COURSE_HEADS[1]}
    g = build_graph(ROUNDS6, variants, 20, 10000, forbidden)
    assert COURSE_HEADS[1] not in g["nodes"]
    assert g["blocked"] >= 1
    assert g["forbidden"] == ["135264"]
    # 没有任何边指向被禁用的 head
    for e in g["edges"]:
        assert g["nodes"][e["to"]] != COURSE_HEADS[1]


def test_truncation_max_leads_and_max_states():
    variants = _variants()
    g = build_graph(ROUNDS6, variants, 1, 10000, set())
    assert g["truncated"] is True and g["limit"] == "max_leads"
    assert len(g["nodes"]) == 2
    g2 = build_graph(ROUNDS6, _variants([("bob", "14", 1)]), 20, 3, set())
    assert g2["truncated"] is True and g2["limit"] == "max_states"
    assert len(g2["nodes"]) == 3


# ---------------- 最短序列、备选数、排序 ----------------


def test_shortest_structure_plain_course():
    variants = _variants([("bob", "14", 1), ("single", "1234", 1)])
    g = build_graph(ROUNDS6, variants, 20, 10000, set())
    # 沿 5 个 course head 的 plain course：距离 1..4，首选动作全 plain
    for dist, head in enumerate(COURSE_HEADS[1:], 1):
        s = shortest_structure(0, g["index"][head], g)
        assert s["distance"] == dist
        assert s["actions"] == ["plain"] * dist
        assert s["count"] == 1
    s0 = shortest_structure(0, 0, g)
    assert s0["distance"] == 0 and s0["actions"] == [] and s0["count"] == 1
    # bob lead 从 rounds 到达另一个 head
    bob_end = (1, 2, 3, 5, 6, 4)
    sb = shortest_structure(0, g["index"][bob_end], g)
    assert sb["distance"] == 1 and sb["actions"] == ["bob"]


def test_alternative_count_sums_parent_paths():
    # 只允许 plain：5 个 course head 成环，每节点唯一前驱，备选数恒为 1
    variants = _variants()
    g = build_graph(ROUNDS6, variants, 20, 10000, set())
    for head in COURSE_HEADS[1:]:
        s = shortest_structure(0, g["index"][head], g)
        assert s["count"] == 1

    # 合成 0→{1,2}→3 的两层菱形图：到 3 有 2 条等长序列；
    # 0→1 有两条不同动作（平行边）时到 3 为 3 条
    def diamond_graph(parallel_from_0=False):
        nodes = [0, 1, 2, 3]
        edges = [
            {"from": 0, "to": 1, "action": "plain", "call": None,
             "action_index": 0, "changes": 1},
            {"from": 0, "to": 2, "action": "bob", "call": "bob",
             "action_index": 1, "changes": 1},
            {"from": 1, "to": 3, "action": "plain", "call": None,
             "action_index": 0, "changes": 1},
            {"from": 2, "to": 3, "action": "plain", "call": None,
             "action_index": 0, "changes": 1},
        ]
        if parallel_from_0:
            edges.append({"from": 0, "to": 1, "action": "zebra", "call": "zebra",
                          "action_index": 2, "changes": 1})
        adj = [[] for _ in nodes]
        for ei, e in enumerate(edges):
            adj[e["from"]].append(ei)
        return {"nodes": nodes, "index": {i: i for i in nodes}, "edges": edges,
                "adj": adj, "truncated": False}

    s = shortest_structure(0, 3, diamond_graph())
    assert s["distance"] == 2 and s["count"] == 2
    # 首选序列 call 更少：plain→plain（经节点 1）
    assert s["actions"] == ["plain", "plain"]

    s2 = shortest_structure(0, 3, diamond_graph(parallel_from_0=True))
    assert s2["distance"] == 2 and s2["count"] == 3
    # 经节点 1 的两条 (plain, plain) 与 (zebra, plain) 中，call 少者优先
    assert s2["actions"] == ["plain", "plain"]


def test_enumeration_order_prefers_plain_then_calls():
    variants = _variants([("bob", "14", 1), ("single", "1234", 1)])
    g = build_graph(ROUNDS6, variants, 20, 10000, set())
    # 距离 1 的目标只有一条 plain 路线
    s = shortest_structure(0, 1, g)
    routes, _ = enumerate_shortest_routes(0, 1, s, g, variants, 10)
    assert [r["actions"] for r in routes] == [["plain"]]
    # 每条路线记录 call、前后 lead head 与 change 数
    lead = routes[0]["leads"][0]
    assert lead["call"] is None and lead["changes"] == 12
    assert lead["lead_head_before"] == "123456"
    assert lead["lead_head_after"] == "135264"


def test_enumeration_order_by_call_then_action_name():
    # 菱形图：到节点 3 的两条序列 [plain,bob] 与 [bob,plain] 同为 1 个 call，
    # 全局顺序按 call 数再按**动作名称**字典序（bob < plain）→ bob,plain 在前
    n0, n1, n2, n3 = (1, 2), (2, 1), (1, 3), (3, 1)
    g = {
        "nodes": [n0, n1, n2, n3],
        "edges": [
            {"from": 0, "to": 1, "action": "plain", "call": None,
             "action_index": 0, "changes": 1},
            {"from": 0, "to": 2, "action": "bob", "call": "bob",
             "action_index": 1, "changes": 1},
            {"from": 1, "to": 3, "action": "bob", "call": "bob",
             "action_index": 1, "changes": 1},
            {"from": 2, "to": 3, "action": "plain", "call": None,
             "action_index": 0, "changes": 1},
        ],
        "adj": [[0, 1], [2], [3], []],
        "truncated": False,
    }
    s = shortest_structure(0, 3, g)
    assert s["count"] == 2 and s["actions"] == ["bob", "plain"]
    variants = _variants([("bob", "14", 1)])
    routes, truncated = enumerate_shortest_routes(0, 3, s, g, variants, 10)
    assert truncated is False
    assert [r["actions"] for r in routes] == [
        ["bob", "plain"], ["plain", "bob"],
    ]
    assert [r["num_calls"] for r in routes] == [1, 1]
    routes1, _ = enumerate_shortest_routes(0, 3, s, g, variants, 1)
    assert [r["actions"] for r in routes1] == [["bob", "plain"]]


def test_unreachable_target():
    variants = _variants()
    g = build_graph(ROUNDS6, variants, 20, 10000, set())
    # 不属于 5 个 course head 的完整排列不可达
    other = (2, 1, 3, 4, 5, 6)
    assert other not in g["index"]
    res = analyze_reachability(
        {"id": "pb", "version": 1}, ROUNDS6, variants, [other], set(),
        20, 10000, 10, 1000,
    )
    t = res["targets"][0]
    assert t["reachable"] is False and t["distance"] is None
    assert t["alternative_count"] == 0 and t["preferred_route"] is None
    assert t["routes"] == [] and t["true_route"] is None


# ---------------- 真值校核 ----------------


def test_check_route_truth_plain_course_true():
    variants = _variants()
    by_action = {v.action: v for v in variants}
    truth = check_route_truth(ROUNDS6, ["plain"] * 5, by_action, "pb")
    assert truth["truth"] == "true" and truth["first_repeat"] is None


def test_check_route_truth_flags_repeat_with_provenance():
    variants = _variants()
    by_action = {v.action: v for v in variants}
    # 6 个 plain lead：第 5 lead 末已回到起点，第 6 lead 起重复
    truth = check_route_truth(ROUNDS6, ["plain"] * 6, by_action, "pb")
    assert truth["truth"] == "false"
    rep = truth["first_repeat"]
    assert rep["row"] == "123456"
    assert rep["first"]["lead"] == 0 and rep["first"]["change"] == 0
    assert rep["second"]["lead"] == 5 and rep["second"]["change_in_lead"] == 12
    assert rep["first"]["method"] == "pb" and rep["first"]["call"] is None
    assert rep["second"]["call"] is None


def test_come_round_to_start_is_true():
    variants = _variants([("bob", "14", 1)])
    by_action = {v.action: v for v in variants}
    # 标准 3-bob touch（60 change）回到 rounds
    truth = check_route_truth(ROUNDS6, ["bob", "bob", "bob"], by_action, "pb")
    assert truth["truth"] == "true"


# ---------------- 为真路线搜索 ----------------


def test_search_true_route_rounds_via_bobs():
    variants = _variants([("bob", "14", 1), ("single", "1234", 1)])
    g = build_graph(ROUNDS6, variants, 20, 10000, set())
    s = shortest_structure(0, 0, g)
    res = search_true_route(0, 0, s, g, variants, 20, 50000)
    assert res["route"] is not None
    route = res["route"]
    assert route["actions"] == ["bob", "bob", "bob"]
    assert route["num_leads"] == 3 and route["num_calls"] == 3
    assert route["total_changes"] == 36


def test_search_true_route_simple_target():
    variants = _variants()
    g = build_graph(ROUNDS6, variants, 20, 10000, set())
    s = shortest_structure(0, 1, g)
    res = search_true_route(0, 1, s, g, variants, 20, 50000)
    assert res["route"]["actions"] == ["plain"]


def test_search_true_route_prefers_fewer_calls_at_same_leads():
    # 回归：152643 存在 5 lead / 2 call 的真路线时，不得选中 5 lead / 3 call
    variants = _variants([("bob", "14", 1), ("single", "1234", 1)])
    g = build_graph(ROUNDS6, variants, 20, 10000, set())
    target = tuple(int(ch) for ch in "152643")
    idx = g["index"][target]
    s = shortest_structure(0, idx, g)
    res = search_true_route(0, idx, s, g, variants, 20, 200000, "pb")
    route = res["route"]
    assert route is not None
    assert route["num_leads"] == 5 and route["num_calls"] == 2
    assert route["actions"] == ["bob", "plain", "bob", "plain", "plain"]
    # 双保险：返回路线逐 row 校核确为真
    truth = check_route_truth(
        ROUNDS6, route["actions"], {v.action: v for v in variants}, "pb"
    )
    assert truth["truth"] == "true"


def test_search_true_route_lex_order_among_equal_call_counts():
    # 回归：145263 三条 4 lead / 2 call 真路线按动作名字典序取 bob,single,...
    variants = _variants([("bob", "14", 1), ("single", "1234", 1)])
    g = build_graph(ROUNDS6, variants, 20, 10000, set())
    target = tuple(int(ch) for ch in "145263")
    idx = g["index"][target]
    s = shortest_structure(0, idx, g)
    res = search_true_route(0, idx, s, g, variants, 20, 200000, "pb")
    route = res["route"]
    assert route["num_leads"] == 4 and route["num_calls"] == 2
    assert route["actions"] == ["bob", "single", "plain", "plain"]
    # 最短候选同样严格按动作名排序（plain 开头不再排在 bob 开头之前）
    routes, _ = enumerate_shortest_routes(0, idx, s, g, variants, 20)
    true_routes = [r for r in routes if r["num_calls"] == 2]
    assert [r["actions"] for r in true_routes[:3]] == [
        ["bob", "single", "plain", "plain"],
        ["plain", "bob", "plain", "single"],
        ["single", "bob", "plain", "plain"],
    ]


def test_intra_lead_repeat_makes_route_false_and_unsearchable():
    # 方法 x.14.14（4 口钟）：从 1234 走 plain 到 2143，第 1、3 个 change 重复
    from ringproof.notation import expand_notation as _exp

    tokens, places = _exp("x.14.14", 4)
    variants4 = build_variants(tokens, places, {})
    g = build_graph((1, 2, 3, 4), variants4, 10, 10000, set())
    target = (2, 1, 4, 3)
    idx = g["index"][target]
    truth = check_route_truth(
        (1, 2, 3, 4), ["plain"], {v.action: v for v in variants4}, "m4"
    )
    assert truth["truth"] == "false"
    rep = truth["first_repeat"]
    assert rep["row"] == "2143"
    assert (rep["first"]["lead"], rep["first"]["change_in_lead"]) == (1, 1)
    assert (rep["second"]["lead"], rep["second"]["change_in_lead"]) == (1, 3)
    # 为真搜索不得返回该假路线（没有其它为真路线时为 None）
    s = shortest_structure(0, idx, g)
    res = search_true_route(0, idx, s, g, variants4, 10, 50000, "m4")
    assert res["route"] is None


# ---------------- 组装分析 ----------------


def test_analyze_reachability_target_payload():
    variants = _variants([("bob", "14", 1), ("single", "1234", 1)])
    res = analyze_reachability(
        {"id": "pb", "version": 1, "name": "PB"},
        ROUNDS6, variants, [COURSE_HEADS[1], ROUNDS6], set(),
        20, 10000, 10, 50000,
    )
    t1, t0 = res["targets"]
    assert t1["reachable"] and t1["distance"] == 1
    assert t1["shortest_sequence"] == ["plain"]
    assert t1["preferred_route"]["truth"] == "true"
    assert t0["reachable"] and t0["distance"] == 0
    assert t0["shortest_sequence"] == []
    # 为真往返路线：3 bobs
    assert t0["true_route"]["actions"] == ["bob", "bob", "bob"]
    # 备选数（回到起点的 5-lead 序列有 3^5 条）
    assert t0["alternative_count"] == 1  # 空序列唯一
    assert len(res["components"]) == 1


def test_analyze_start_forbidden_rejected():
    variants = _variants()
    with pytest.raises(ReachabilityError) as exc:
        analyze_reachability(
            {"id": "pb", "version": 1}, ROUNDS6, variants, [COURSE_HEADS[1]],
            {ROUNDS6}, 20, 10000, 10, 50000,
        )
    assert exc.value.code == "START_FORBIDDEN"


# ---------------- API ----------------


def test_create_lead_graph_api(client):
    r = _create(client)
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["id"] == "lg" and b["version"] == 1 and b["stage"] == 6
    assert b["start_lead_head"] == "123456"
    assert b["actions"] == ["plain", "bob", "single"]
    assert b["graph"]["num_nodes"] == 120 and b["graph"]["num_edges"] == 360
    # 全图强连通（任意 lead head 间都可达）
    assert len(b["components"]) == 1
    assert set(b["components"][0]["lead_heads"]) == set(b["graph"]["nodes"])
    assert b["cannot_return"]["count"] == 0
    assert b["truncated"] is False and b["truncation_reason"] is None
    # 边记录 call、前后排列与 change 数
    edge = b["graph"]["edges"][0]
    assert set(edge) >= {
        "action", "call", "lead_head_before", "lead_head_after", "changes",
    }
    assert edge["changes"] == 12

    targets = {t["target"]: t for t in b["targets"]}
    t = targets["135264"]
    assert t["distance"] == 1 and t["shortest_sequence"] == ["plain"]
    assert t["preferred_route"]["truth"] == "true"
    t0 = targets["123456"]
    assert t0["distance"] == 0 and t0["true_route"]["actions"] == ["bob", "bob", "bob"]

    assert b["input_hash"] and b["dependencies"]["method"]["version"] == 1
    assert b["dependencies"]["ringproof_version"]

    # 重复查询一致
    g1 = client.get("/lead-graph-analyses/lg/versions/1").json()
    g2 = client.get("/lead-graph-analyses/lg/versions/1").json()
    assert g1 == g2 == b


def test_only_true_filter(client):
    r = _create(client)
    assert r.status_code == 201
    g = client.get("/lead-graph-analyses/lg/versions/1?only_true=true").json()
    assert g["filter"] == "only_true"
    for t in g["targets"]:
        assert all(rt["truth"] == "true" for rt in t["routes"])


def test_only_true_does_not_backfill_false_route(client):
    # 方法 x.14.14：plain lead 内部重复（2143 出现在第 1、3 个 change），
    # 最短路线为假且没有为真替代 → only_true 不得回填假路线
    client.post(
        "/methods",
        json={"id": "m4", "name": "Repeat Four", "stage": 4, "notation": "x.14.14"},
    )
    r = client.post(
        "/lead-graph-analyses",
        json={"id": "lg4", "method": {"id": "m4"}, "targets": ["2143"]},
    )
    assert r.status_code == 201
    b = r.json()["targets"][0]
    assert b["preferred_route"]["truth"] == "false"
    assert b["true_route"] is None
    g = client.get(
        "/lead-graph-analyses/lg4/versions/1?only_true=true"
    ).json()["targets"][0]
    assert g["routes"] == []
    assert g["preferred_route"] is None
    assert g["true_route"] is None


def test_true_route_strict_ordering_api(client):
    # 回归：true_route 严格按 lead 数、call 数、动作名字典序
    r = _create(client, id="ord", targets=["152643", "145263"])
    b = r.json()
    by_target = {t["target"]: t for t in b["targets"]}
    tr = by_target["152643"]["true_route"]
    assert (tr["num_leads"], tr["num_calls"], tr["actions"]) == (
        5, 2, ["bob", "plain", "bob", "plain", "plain"],
    )
    tr2 = by_target["145263"]["true_route"]
    assert (tr2["num_leads"], tr2["num_calls"], tr2["actions"]) == (
        4, 2, ["bob", "single", "plain", "plain"],
    )


def test_frozen_method_version(client):
    r = _create(client)
    h1 = r.json()["input_hash"]
    # 方法新版本不影响已冻结分析
    client.post(
        "/methods",
        json={"id": "pb", "name": "PB2", "stage": 6, "notation": PB_MINOR},
    )
    g = client.get("/lead-graph-analyses/lg/versions/1").json()
    assert g["method"]["version"] == 1 and g["input_hash"] == h1


def test_rejects_bad_permutations(client):
    r = _create(client, targets=["12345"])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "ROW_LENGTH_MISMATCH"
    r = _create(client, targets=["112345"])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "ROW_NOT_PERMUTATION"
    r = _create(client, start_lead_head="12345")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "ROW_LENGTH_MISMATCH"
    r = _create(client, forbidden_lead_heads=["12345"])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "ROW_LENGTH_MISMATCH"
    assert client.get("/lead-graph-analyses/lg").status_code == 404  # 未落库


def test_rejects_replace_out_of_range(client):
    r = _create(client, calls={"bob": {"notation": "14", "replace": 13}})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "CALL_REPLACE_RANGE"


def test_rejects_start_in_forbidden(client):
    r = _create(client, start_lead_head="135264", forbidden_lead_heads=["135264"])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "START_FORBIDDEN"


def test_rejects_duplicate_targets_and_reserved_call(client):
    r = _create(client, targets=["135264", "135264"])
    assert r.status_code == 422
    r = _create(client, calls={"plain": {"notation": "14", "replace": 1}})
    assert r.status_code == 422


def test_unknown_method_404(client):
    r = _create(client, method={"id": "nope"})
    assert r.status_code == 404
    r = _create(client, method={"id": "pb", "version": 9})
    assert r.status_code == 404


def test_forbidden_head_avoidance(client):
    r = _create(
        client, id="lgf", targets=["156342"], forbidden_lead_heads=["135264"],
    )
    assert r.status_code == 201
    b = r.json()
    assert b["graph"]["blocked_transitions"] >= 1
    assert "135264" not in b["graph"]["nodes"]
    t = b["targets"][0]
    # bob lead 可绕过 135264 到达 156342：所有候选路线都不得经过禁用 head
    assert t["reachable"] is True
    for rt in t["routes"]:
        heads = [l["lead_head_before"] for l in rt["leads"]]
        heads.append(rt["leads"][-1]["lead_head_after"])
        assert "135264" not in heads
    assert t["true_route"] is not None
    heads = [l["lead_head_before"] for l in t["true_route"]["leads"]]
    heads.append(t["true_route"]["leads"][-1]["lead_head_after"])
    assert "135264" not in heads


def test_truncation_reported(client):
    r = _create(client, id="lg1", targets=["156342"], max_leads=1)
    assert r.status_code == 201
    b = r.json()
    assert b["truncated"] is True and b["truncation_limit"] == "max_leads"
    assert "max_leads=1" in b["truncation_reason"]

    r = _create(client, id="lg3", targets=["156342"], max_states=3)
    b = r.json()
    assert b["truncated"] is True and b["truncation_limit"] == "max_states"
    assert b["graph"]["num_nodes"] == 3
    assert "max_states=3" in b["truncation_reason"]


def test_versions_and_listing(client):
    assert _create(client).status_code == 201
    assert _create(client, targets=["164523"]).status_code == 201
    assert client.get("/lead-graph-analyses/lg/versions/2").status_code == 200
    lst = client.get("/lead-graph-analyses").json()["lead_graph_analyses"]
    assert [(x["id"], x["version"]) for x in lst] == [("lg", 1), ("lg", 2)]
    vers = client.get("/lead-graph-analyses/lg").json()["versions"]
    assert [v["version"] for v in vers] == [1, 2]
    assert client.get("/lead-graph-analyses/nope/versions/1").status_code == 404


def test_target_bell_count_mismatch_is_row_length(client):
    # 目标钟数不符（4 位目标 vs 6 口方法）按长度不符拒绝
    r = _create(client, targets=["1234"])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "ROW_LENGTH_MISMATCH"
