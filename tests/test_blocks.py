"""可复用 block 拼装：区段转相对置换、创建校验（钟数/边界/区段自身为假）、
组合搜索的用量/衔接/长度/末行约束、可达性/剩余长度/行交集剪枝、跨 block
重复冲突的两侧来源、排序、版本冻结与搜索缓存。"""
import pytest
from fastapi.testclient import TestClient

from ringproof.blocks import BlockError, apply_pos_perm, make_block, search_blocks
from ringproof.engine import apply_change, build_lead, prove_rows
from ringproof.main import create_app
from ringproof.multipart import permute_row
from ringproof.notation import expand_notation

PB_MINOR = "x16x16x16x16x16x12"
ROUNDS6 = (1, 2, 3, 4, 5, 6)


def _pb_rows(n_leads, calls_seq=None):
    toks, chs = expand_notation(PB_MINOR, 6)
    bt, bc = expand_notation("14", 6)
    call_defs = {"bob": {"notation": "14", "tokens": bt, "changes": bc, "replace": 1}}
    calls_seq = calls_seq or [None] * n_leads
    leads = [
        build_lead(toks, chs, c, call_defs, method_id="pb", method_version=1, method_name="PB")
        for c in calls_seq
    ]
    result = prove_rows(6, "PB", leads, ROUNDS6)
    rows = [ROUNDS6] + [tuple(int(c) for c in ev["row"]) for ev in result["events"]]
    lead_ends = [12 * (i + 1) for i in range(n_leads)]
    return rows, result["events"], lead_ends


def _make(block_id, rows, events, lead_ends, start, end,
          min_uses=0, max_uses=None, touch_id="t1"):
    return make_block(
        stage=6, block_id=block_id, touch_id=touch_id, touch_version=1,
        rows=rows, events=events, lead_end_indices=lead_ends,
        start_change=start, end_change=end, min_uses=min_uses, max_uses=max_uses,
    )


def _plain_lead_block(block_id="A", touch_id="t1", **kw):
    rows, events, lead_ends = _pb_rows(5)
    return _make(block_id, rows, events, lead_ends, 0, 12, touch_id=touch_id, **kw)


def _search(blocks, **kw):
    args = dict(
        blocks=blocks, start_row=ROUNDS6, target=None, min_changes=0,
        max_changes=60, allowed=None, forbidden=set(),
        max_results=50, max_search=20000,
    )
    args.update(kw)
    return search_blocks(**args)


# ---------------- block 构建：相对起点的钟置换 ----------------

def test_make_block_relative_permutation_and_reexpansion():
    rows, events, lead_ends = _pb_rows(5)
    b = _make("A", rows, events, lead_ends, 0, 12)
    assert b["images"] == (1, 3, 5, 2, 6, 4)
    assert b["length"] == 12 and b["calls"] == 0
    assert b["methods"] == ["pb"]
    assert b["start_lead"] == 1 and b["end_lead"] == 1
    # 相对起点的钟置换：末 row 为起点的 φ 像
    assert permute_row(b["images"], ROUNDS6) == (1, 3, 5, 2, 6, 4)
    # 净位置置换：从不同 lead head 展开，末 row 第 i 位为 lead head 第 C[i] 位的钟
    assert apply_pos_perm(b["pos_perm"], ROUNDS6) == (1, 3, 5, 2, 6, 4)
    assert apply_pos_perm(b["pos_perm"], (1, 3, 5, 2, 6, 4)) == (1, 5, 6, 3, 4, 2)
    # 逐 change 展开与位置置换一致
    cur = (1, 3, 5, 2, 6, 4)
    for places, _prov in b["changes"]:
        cur = apply_change(cur, places)
    assert cur == (1, 5, 6, 3, 4, 2)
    # 逐 change 来源（touch 事件）
    prov = b["changes"][11][1]
    assert prov["touch_change"] == 12 and prov["lead"] == 1
    assert prov["method_id"] == "pb" and prov["call"] is None
    # 逆位置置换：末 row 可还原起点
    assert apply_pos_perm(b["inv_pos_perm"], (1, 3, 5, 2, 6, 4)) == ROUNDS6


def test_make_block_calls_and_methods():
    rows, events, lead_ends = _pb_rows(5, ["bob", None, None, None, None])
    b = _make("Q", rows, events, lead_ends, 0, 12)
    assert b["calls"] == 1
    assert b["changes"][11][1]["call"] == "bob"
    assert b["changes"][11][1]["source"] == "call:bob"


def test_make_block_closure_allowed():
    # 末 row 回到区段起点属正常闭合（plain course 60 change 回 rounds）
    rows, events, lead_ends = _pb_rows(5)
    b = _make("C", rows, events, lead_ends, 0, 60)
    assert b["images"] == ROUNDS6  # 恒等置换
    assert b["length"] == 60


def test_make_block_boundary_errors():
    rows, events, lead_ends = _pb_rows(5)
    with pytest.raises(BlockError) as ei:
        _make("A", rows, events, lead_ends, 0, 13)
    assert ei.value.code == "NOT_LEAD_END"
    with pytest.raises(BlockError) as ei:
        _make("A", rows, events, lead_ends, 5, 12)
    assert ei.value.code == "NOT_LEAD_END"
    with pytest.raises(BlockError) as ei:
        _make("A", rows, events, lead_ends, 0, 61)
    assert ei.value.code == "CHANGE_OUT_OF_RANGE"


def test_make_block_self_false_rejected():
    # [plain, bob, bob, bob]：row 135264 在 change 12 与 48 重复
    rows, events, lead_ends = _pb_rows(4, [None, "bob", "bob", "bob"])
    with pytest.raises(BlockError) as ei:
        _make("A", rows, events, lead_ends, 0, 48)
    assert ei.value.code == "BLOCK_NOT_TRUE"
    assert "135264" in ei.value.message


def _rows_for(notation, n_leads, stage=4):
    """任意方法/钟数的逐 row 展开（无 call），供异起点与重复用例。"""
    toks, chs = expand_notation(notation, stage)
    start = tuple(range(1, stage + 1))
    leads = [
        build_lead(toks, chs, None, {}, method_id="m", method_version=1, method_name="M")
        for _ in range(n_leads)
    ]
    result = prove_rows(stage, "M", leads, start)
    rows = [start] + [tuple(int(c) for c in ev["row"]) for ev in result["events"]]
    lead_ends = [len(toks) * (i + 1) for i in range(n_leads)]
    return rows, result["events"], lead_ends


# ---------------- 组合搜索 ----------------

def test_search_single_block_to_target():
    s = _search([_plain_lead_block()], target=ROUNDS6, max_changes=60)
    assert s["total_results"] == 1 and s["candidates"] == 1
    r = s["results"][0]
    assert r["sequence"] == ["A"] * 5
    assert r["total_changes"] == 60 and r["num_blocks"] == 5 and r["num_calls"] == 0
    assert r["final_row"] == "123456" and r["target_reached"] is True
    assert r["blocks_used"] == {"A": 5}
    assert [seg["end_row"] for seg in r["segments"]] == [
        "135264", "156342", "164523", "142635", "123456",
    ]
    assert [seg["seq"] for seg in r["segments"]] == [1, 2, 3, 4, 5]
    assert s["checked"] == 6 and s["first_conflict"] is None
    assert not s["truncated"]


def test_search_without_target_collects_boundaries_and_reports_conflict():
    s = _search([_plain_lead_block()], max_changes=60)
    assert [r["sequence"] for r in s["results"]] == [
        ["A"], ["A", "A"], ["A"] * 3, ["A"] * 4,
    ]
    assert [r["total_changes"] for r in s["results"]] == [12, 24, 36, 48]
    assert all(r["target_reached"] is None for r in s["results"])
    assert s["checked"] == 5 and s["pruned_by_rows"] == 1
    # 冲突：A^5 末 row 回到起点 rounds（无目标时不豁免），列出两侧来源
    fc = s["first_conflict"]
    assert fc["row"] == "123456"
    assert fc["first"] == {"type": "start", "change": 0}
    second = fc["second"]
    assert second["block"] == "A" and second["block_index"] == 5
    assert second["change"] == 60 and second["change_in_block"] == 12
    assert second["touch"] == {"id": "t1", "version": 1}
    assert second["touch_change"] == 12 and second["lead"] == 1
    assert second["method_id"] == "pb" and second["call"] is None


def test_search_usage_limit_pruning():
    s = _search([_plain_lead_block(max_uses=2)], max_changes=60)
    assert [r["sequence"] for r in s["results"]] == [["A"], ["A", "A"]]
    assert s["pruned_by_usage"] == 1


def test_search_length_window():
    s = _search([_plain_lead_block()], min_changes=36, max_changes=48)
    assert [r["total_changes"] for r in s["results"]] == [36, 48]
    assert s["pruned_by_length"] == 1  # 48 之后放不下任何 block


def test_search_reachability_pruning():
    # 目标 rounds 但预算只够 2 个 block：第 1 个 block 后即不可达
    s = _search([_plain_lead_block()], target=ROUNDS6, max_changes=24)
    assert s["total_results"] == 0
    assert s["pruned_by_reachability"] == 1
    assert s["checked"] == 2
    assert s["truncation_reason"] is not None


def test_search_two_blocks_order_and_checked():
    a = _plain_lead_block("A", touch_id="t1")
    b = _plain_lead_block("B", touch_id="t2")
    s = _search([a, b], max_changes=24)
    assert [r["sequence"] for r in s["results"]] == [
        ["A"], ["B"], ["A", "A"], ["A", "B"], ["B", "A"], ["B", "B"],
    ]
    assert s["checked"] == 7 and s["pruned_by_length"] == 4
    assert s["first_conflict"] is None


def test_search_transition_rules():
    a = _plain_lead_block("A", touch_id="t1")
    b = _plain_lead_block("B", touch_id="t2")
    # 黑名单：A→B 被禁止
    s = _search([a, b], max_changes=24, forbidden={("A", "B")})
    assert [r["sequence"] for r in s["results"]] == [
        ["A"], ["B"], ["A", "A"], ["B", "A"], ["B", "B"],
    ]
    assert s["pruned_by_transition"] == 1
    # 白名单：只允许 B→A；同 block 延续始终允许
    s = _search([a, b], max_changes=24, allowed={("B", "A")})
    assert [r["sequence"] for r in s["results"]] == [
        ["A"], ["B"], ["A", "A"], ["B", "A"], ["B", "B"],
    ]
    assert s["pruned_by_transition"] == 1


def test_search_call_count_sorting():
    rows, events, lead_ends = _pb_rows(5, ["bob", None, None, None, None])
    q = _make("Q", rows, events, lead_ends, 0, 12, touch_id="t2")
    p = _plain_lead_block("P")
    s = _search([p, q], max_changes=12)
    assert [r["sequence"] for r in s["results"]] == [["P"], ["Q"]]
    assert [r["num_calls"] for r in s["results"]] == [0, 1]


def test_search_min_uses_required():
    a = _plain_lead_block("A", min_uses=2)
    b = _plain_lead_block("B", touch_id="t2")
    s = _search([a, b], max_changes=36)
    # 候选须含 A ≥ 2 次；排序：change 数 → block 数 → 字典序
    assert [r["sequence"] for r in s["results"]] == [
        ["A", "A"],
        ["A", "A", "A"],
        ["A", "A", "B"],
        ["A", "B", "A"],
        ["B", "A", "A"],
    ]
    assert all(r["blocks_used"].get("A", 0) >= 2 for r in s["results"])


def test_search_truncated_by_max_search():
    s = _search([_plain_lead_block()], max_changes=60, max_search=3)
    assert s["truncated"] and s["checked"] == 3
    assert "max_search" in s["truncation_reason"]


def test_search_max_results_keeps_best():
    s = _search([_plain_lead_block()], max_changes=60, max_results=2)
    assert s["candidates"] == 4 and s["total_results"] == 2
    assert [r["total_changes"] for r in s["results"]] == [12, 24]


def test_search_deterministic_repeat():
    a = _plain_lead_block("A", touch_id="t1")
    b = _plain_lead_block("B", touch_id="t2")
    s1 = _search([a, b], max_changes=36)
    s2 = _search([a, b], max_changes=36)
    assert s1 == s2


def test_search_from_different_lead_head():
    # 异起点：block 为单个全交叉 change，从 3124 展开应到达 1342
    rows, events, lead_ends = _rows_for("x", 2)
    x = make_block(
        stage=4, block_id="X", touch_id="t", touch_version=1,
        rows=rows, events=events, lead_end_indices=lead_ends,
        start_change=0, end_change=1, min_uses=0, max_uses=None,
    )
    assert x["pos_perm"] == (1, 0, 3, 2)
    # 有目标：逐 change 展开可达 1342，可达性剪枝不得误杀
    s = search_blocks(
        blocks=[x], start_row=(3, 1, 2, 4), target=(1, 3, 4, 2),
        min_changes=0, max_changes=1, allowed=None, forbidden=set(),
        max_results=50, max_search=1000,
    )
    assert s["total_results"] == 1
    r = s["results"][0]
    assert r["final_row"] == "1342" and r["target_reached"] is True
    # 分段末行与组合末行一致（统一按净位置置换计算）
    assert r["segments"][0]["start_row"] == "3124"
    assert r["segments"][0]["end_row"] == "1342"
    # 无目标：final_row 与 segments 末行同样一致
    s2 = search_blocks(
        blocks=[x], start_row=(3, 1, 2, 4), target=None,
        min_changes=0, max_changes=1, allowed=None, forbidden=set(),
        max_results=50, max_search=1000,
    )
    r2 = s2["results"][0]
    assert r2["final_row"] == "1342"
    assert r2["segments"][0]["end_row"] == r2["final_row"]


def test_search_target_cannot_bypass_repeat():
    # A、B 展开为 1234、2143、2134、2143：2143 已重复，目标末行不豁免
    rows_a, events_a, le_a = _rows_for("x12", 1)  # 1234 → 2143 → 2134
    rows_b, events_b, le_b = _rows_for("12", 1)   # 1234 → 1243
    a = make_block(
        stage=4, block_id="A", touch_id="t1", touch_version=1,
        rows=rows_a, events=events_a, lead_end_indices=le_a,
        start_change=0, end_change=2, min_uses=0, max_uses=None,
    )
    b = make_block(
        stage=4, block_id="B", touch_id="t2", touch_version=1,
        rows=rows_b, events=events_b, lead_end_indices=le_b,
        start_change=0, end_change=1, min_uses=0, max_uses=None,
    )
    s = search_blocks(
        blocks=[a, b], start_row=(1, 2, 3, 4), target=(2, 1, 4, 3),
        min_changes=0, max_changes=3, allowed=None, forbidden=set(),
        max_results=50, max_search=1000,
    )
    # [A, B] 末行 2143 重复出现，须排除；[B, A]（1234、1243、2134、2143）为真
    assert [r["sequence"] for r in s["results"]] == [["B", "A"]]
    assert s["pruned_by_rows"] >= 1
    # 冲突对应两次出现的来源：A 的第 1 个 change 与 B 的第 1 个 change
    fc = s["first_conflict"]
    assert fc["row"] == "2143"
    assert fc["first"]["block"] == "A" and fc["first"]["block_index"] == 1
    assert fc["first"]["change_in_block"] == 1 and fc["first"]["change"] == 1
    assert fc["first"]["touch"] == {"id": "t1", "version": 1}
    assert fc["first"]["touch_change"] == 1 and fc["first"]["call"] is None
    assert fc["second"]["block"] == "B" and fc["second"]["block_index"] == 2
    assert fc["second"]["change_in_block"] == 1 and fc["second"]["change"] == 3
    assert fc["second"]["touch"] == {"id": "t2", "version": 1}
    assert fc["second"]["touch_change"] == 1 and fc["second"]["call"] is None


# ---------------- API ----------------

@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def touches(client):
    client.post(
        "/methods",
        json={"id": "pb-minor", "name": "Plain Bob Minor", "stage": 6, "notation": PB_MINOR},
    )
    r = client.post(
        "/touches",
        json={"id": "pc", "method_id": "pb-minor",
              "calls": {"bob": {"notation": "14", "replace": 1},
                        "single": {"notation": "1234", "replace": 1}},
              "sequence": [{"leads": [{"call": None}], "repeat": 5}]},
    )
    assert r.status_code == 201
    r = client.post(
        "/touches",
        json={"id": "pc2", "method_id": "pb-minor",
              "calls": {"bob": {"notation": "14", "replace": 1}},
              "sequence": [{"leads": [{"call": "bob"}, {"call": None}], "repeat": 3}]},
    )
    assert r.status_code == 201
    return client


def _composition_payload(**over):
    payload = {
        "id": "bc",
        "blocks": [
            {"id": "A", "touch": {"id": "pc"}, "start_change": 0, "end_change": 12},
        ],
        "max_changes": 60,
        "target_row": "123456",
    }
    payload.update(over)
    return payload


def test_api_create_and_get(client, touches):
    r = client.post("/block-compositions", json=_composition_payload())
    assert r.status_code == 201
    body = r.json()
    assert body["version"] == 1 and body["stage"] == 6
    assert body["start_row"] == "123456" and body["target_row"] == "123456"
    assert body["min_changes"] == 0 and body["max_changes"] == 60
    b = body["blocks"][0]
    assert b["touch"] == {"id": "pc", "version": 1}
    assert b["permutation"]["images"] == [1, 3, 5, 2, 6, 4]
    assert b["permutation"]["cycles"] == [[2, 3, 5, 6, 4]]
    assert b["permutation"]["order"] == 5
    assert b["changes"] == 12 and b["calls"] == 0 and b["leads"] == 1
    assert b["min_uses"] == 0 and b["max_uses"] is None
    # 依赖冻结：touch、方法、call、ringproof 版本
    deps = body["dependencies"]
    assert deps["touches"] == [
        {"id": "pc", "version": 1, "input_hash": deps["touches"][0]["input_hash"]}
    ]
    assert [m["id"] for m in deps["methods"]] == ["pb-minor"]
    assert sorted(c["name"] for c in deps["calls"]) == ["bob", "single"]
    assert "ringproof_version" in deps

    # GET 读回与创建响应一致；列表与版本列表
    got = client.get("/block-compositions/bc/versions/1").json()
    assert got == body
    listing = client.get("/block-compositions").json()["block_compositions"]
    assert [x["id"] for x in listing] == ["bc"]
    versions = client.get("/block-compositions/bc").json()["versions"]
    assert [v["version"] for v in versions] == [1]

    # 404
    assert client.get("/block-compositions/ghost/versions/1").status_code == 404
    assert client.get("/block-compositions/bc/versions/9").status_code == 404
    assert client.get("/block-compositions/ghost").status_code == 404


def test_api_composition_immutable_and_frozen_touch(client, touches):
    r1 = client.post("/block-compositions", json=_composition_payload())
    assert r1.status_code == 201
    # touch 出新版本后，composition v1 仍冻结在 touch v1
    client.post(
        "/touches",
        json={"id": "pc", "method_id": "pb-minor",
              "sequence": [{"leads": [{"call": None}], "repeat": 6}]},
    )
    got = client.get("/block-compositions/bc/versions/1").json()
    assert got["blocks"][0]["touch"]["version"] == 1
    # 显式引用 touch v1 再建 composition：输入相同则 input_hash 一致
    r2 = client.post(
        "/block-compositions",
        json=_composition_payload(
            blocks=[{"id": "A", "touch": {"id": "pc", "version": 1},
                     "start_change": 0, "end_change": 12}]
        ),
    )
    assert r2.status_code == 201
    assert r2.json()["version"] == 2
    assert r2.json()["input_hash"] == r1.json()["input_hash"]


def test_api_create_validation_errors(client, touches):
    # touch 不存在
    assert client.post("/block-compositions", json=_composition_payload(
        blocks=[{"id": "A", "touch": {"id": "ghost"}, "start_change": 0, "end_change": 12}],
    )).status_code == 404
    assert client.post("/block-compositions", json=_composition_payload(
        blocks=[{"id": "A", "touch": {"id": "pc", "version": 9}, "start_change": 0, "end_change": 12}],
    )).status_code == 404
    # 边界非法：不在 lead end / 超出范围
    r = client.post("/block-compositions", json=_composition_payload(
        blocks=[{"id": "A", "touch": {"id": "pc"}, "start_change": 0, "end_change": 13}],
    ))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "NOT_LEAD_END"
    r = client.post("/block-compositions", json=_composition_payload(
        blocks=[{"id": "A", "touch": {"id": "pc"}, "start_change": 5, "end_change": 12}],
    ))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "NOT_LEAD_END"
    r = client.post("/block-compositions", json=_composition_payload(
        blocks=[{"id": "A", "touch": {"id": "pc"}, "start_change": 0, "end_change": 61}],
    ))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "CHANGE_OUT_OF_RANGE"
    # 区段自身为假
    client.post(
        "/touches",
        json={"id": "false-touch", "method_id": "pb-minor",
              "calls": {"bob": {"notation": "14", "replace": 1}},
              "sequence": [{"leads": [{"call": None}, {"call": "bob"},
                                      {"call": "bob"}, {"call": "bob"}]}]},
    )
    r = client.post("/block-compositions", json=_composition_payload(
        blocks=[{"id": "A", "touch": {"id": "false-touch"}, "start_change": 0, "end_change": 48}],
    ))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "BLOCK_NOT_TRUE"
    # 钟数不一致
    client.post("/methods", json={"id": "pb-major", "name": "PB Major", "stage": 8,
                                  "notation": "x18x18x18x18x18x18x18x12"})
    client.post("/touches", json={"id": "pc8", "method_id": "pb-major",
                                  "sequence": [{"leads": [{"call": None}], "repeat": 3}]})
    r = client.post("/block-compositions", json=_composition_payload(blocks=[
        {"id": "A", "touch": {"id": "pc"}, "start_change": 0, "end_change": 12},
        {"id": "B", "touch": {"id": "pc8"}, "start_change": 0, "end_change": 16},
    ]))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "STAGE_MISMATCH"
    # 衔接规则引用未声明 block
    r = client.post("/block-compositions", json=_composition_payload(
        forbidden_transitions=[["A", "ghost"]],
    ))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "UNKNOWN_BLOCK"
    # 最少用量所需 change 超限
    r = client.post("/block-compositions", json=_composition_payload(
        blocks=[{"id": "A", "touch": {"id": "pc"}, "start_change": 0, "end_change": 12,
                 "min_uses": 6}],
    ))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "UNSATISFIABLE_LENGTH"
    # 目标末行非法
    r = client.post("/block-compositions", json=_composition_payload(target_row="123"))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "ROW_LENGTH_MISMATCH"
    # pydantic：start>=end、min>max uses、重复 block id、min_changes>max_changes、空 blocks
    assert client.post("/block-compositions", json=_composition_payload(
        blocks=[{"id": "A", "touch": {"id": "pc"}, "start_change": 12, "end_change": 12}],
    )).status_code == 422
    assert client.post("/block-compositions", json=_composition_payload(
        blocks=[{"id": "A", "touch": {"id": "pc"}, "start_change": 0, "end_change": 12,
                 "min_uses": 3, "max_uses": 2}],
    )).status_code == 422
    assert client.post("/block-compositions", json=_composition_payload(blocks=[
        {"id": "A", "touch": {"id": "pc"}, "start_change": 0, "end_change": 12},
        {"id": "A", "touch": {"id": "pc"}, "start_change": 12, "end_change": 24},
    ])).status_code == 422
    assert client.post("/block-compositions", json=_composition_payload(
        min_changes=61, max_changes=60,
    )).status_code == 422
    assert client.post("/block-compositions", json=_composition_payload(
        blocks=[],
    )).status_code == 422


def test_api_create_unresolved_choice_rejected(client, touches):
    client.post(
        "/touches",
        json={"id": "choice-touch", "method_id": "pb-minor",
              "calls": {"bob": {"notation": "14", "replace": 1}},
              "sequence": [{"leads": [{"choice": ["plain", "bob"]}, {"call": None}]}]},
    )
    r = client.post("/block-compositions", json=_composition_payload(
        blocks=[{"id": "A", "touch": {"id": "choice-touch"}, "start_change": 0, "end_change": 12}],
    ))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "UNRESOLVED_CHOICE"


def test_api_search_and_cache(client, touches):
    assert client.post("/block-compositions", json=_composition_payload()).status_code == 201
    r = client.post("/block-compositions/bc/versions/1/search", json={})
    assert r.status_code == 200 and r.headers["X-Block-Search-Cache"] == "miss"
    body = r.json()
    assert body["sorted_by"] == ["target_reached", "total_changes", "num_blocks", "num_calls"]
    assert body["total_results"] == 1
    res = body["results"][0]
    assert res["sequence"] == ["A"] * 5
    assert res["total_changes"] == 60 and res["num_blocks"] == 5
    assert res["final_row"] == "123456" and res["target_reached"] is True
    assert res["blocks_used"] == {"A": 5}
    assert [seg["end_row"] for seg in res["segments"]][-1] == "123456"
    assert body["dependencies"]["touches"][0]["id"] == "pc"
    assert body["first_conflict"] is None

    # 相同请求重复搜索：缓存命中且结果一致
    r2 = client.post("/block-compositions/bc/versions/1/search", json={})
    assert r2.headers["X-Block-Search-Cache"] == "hit"
    assert r2.json() == body

    # 404
    assert client.post("/block-compositions/ghost/versions/1/search", json={}).status_code == 404


def test_api_search_multi_touch_blocks(client, touches):
    # A 为 pc 的 plain lead（0 call），B 为 pc2 的 bob lead（1 call）
    assert client.post("/block-compositions", json=_composition_payload(
        blocks=[
            {"id": "A", "touch": {"id": "pc"}, "start_change": 0, "end_change": 12},
            {"id": "B", "touch": {"id": "pc2"}, "start_change": 0, "end_change": 12},
        ],
        target_row=None, max_changes=24,
    )).status_code == 201
    r = client.post("/block-compositions/bc/versions/1/search", json={})
    body = r.json()
    assert [x["sequence"] for x in body["results"]] == [
        ["A"], ["B"], ["A", "A"], ["A", "B"], ["B", "A"], ["B", "B"],
    ]
    calls = {tuple(x["sequence"]): x["num_calls"] for x in body["results"]}
    assert calls[("A",)] == 0 and calls[("B",)] == 1
    assert calls[("A", "B")] == 1 and calls[("B", "B")] == 2
    assert body["blocks"][1]["touch"] == {"id": "pc2", "version": 1}
    assert body["blocks"][1]["calls"] == 1
    assert [t["id"] for t in body["dependencies"]["touches"]] == ["pc", "pc2"]


def test_api_search_same_touch_segments_to_target(client, touches):
    # A、B 为同一 touch 的两个 plain lead 区段：任意 5 个 block 回到 rounds
    assert client.post("/block-compositions", json=_composition_payload(
        blocks=[
            {"id": "A", "touch": {"id": "pc"}, "start_change": 0, "end_change": 12},
            {"id": "B", "touch": {"id": "pc"}, "start_change": 12, "end_change": 24},
        ],
    )).status_code == 201
    r = client.post("/block-compositions/bc/versions/1/search", json={})
    body = r.json()
    assert body["total_results"] == 32 and body["candidates"] == 32
    assert body["checked"] == 63
    assert all(x["final_row"] == "123456" and x["target_reached"] for x in body["results"])
    assert body["results"][0]["sequence"] == ["A"] * 5
    assert body["results"][-1]["sequence"] == ["B"] * 5
    assert body["dependencies"]["touches"] == [
        {"id": "pc", "version": 1, "input_hash": body["dependencies"]["touches"][0]["input_hash"]}
    ]


def test_api_search_no_target_conflict_provenance(client, touches):
    assert client.post("/block-compositions", json=_composition_payload(
        target_row=None,
    )).status_code == 201
    r = client.post("/block-compositions/bc/versions/1/search", json={})
    body = r.json()
    assert [x["sequence"] for x in body["results"]] == [
        ["A"], ["A", "A"], ["A"] * 3, ["A"] * 4,
    ]
    assert body["pruned_by_rows"] == 1
    fc = body["first_conflict"]
    assert fc["row"] == "123456"
    assert fc["first"] == {"type": "start", "change": 0}
    assert fc["second"]["block"] == "A" and fc["second"]["block_index"] == 5
    assert fc["second"]["touch"] == {"id": "pc", "version": 1}
    assert fc["second"]["touch_change"] == 12 and fc["second"]["change"] == 60
    assert fc["second"]["method"] == "Plain Bob Minor"
    assert fc["second"]["method_id"] == "pb-minor"
    assert fc["second"]["call"] is None


def test_api_search_truncation_and_max_results(client, touches):
    assert client.post("/block-compositions", json=_composition_payload(
        target_row=None,
    )).status_code == 201
    r = client.post("/block-compositions/bc/versions/1/search", json={"max_search": 3})
    body = r.json()
    assert body["truncated"] and body["checked"] == 3
    assert "max_search" in body["truncation_reason"]
    # max_results 封顶：保留排序最优
    r2 = client.post("/block-compositions/bc/versions/1/search", json={"max_results": 2})
    body2 = r2.json()
    assert body2["candidates"] == 4 and body2["total_results"] == 2
    assert [x["total_changes"] for x in body2["results"]] == [12, 24]


def test_api_search_different_head(client):
    """异起点回归：单全交叉 block 从 3124 展开应到达 1342，且分段末行一致。"""
    client.post(
        "/methods",
        json={"id": "mx", "name": "Cross", "stage": 4, "notation": "x"},
    )
    client.post(
        "/touches",
        json={"id": "tx", "method_id": "mx",
              "sequence": [{"leads": [{"call": None}], "repeat": 2}]},
    )
    r = client.post(
        "/block-compositions",
        json={
            "id": "different-head",
            "blocks": [{"id": "X", "touch": {"id": "tx"}, "start_change": 0, "end_change": 1}],
            "start_row": "3124",
            "target_row": "1342",
            "max_changes": 1,
        },
    )
    assert r.status_code == 201
    # 有目标：逐 change 展开可达 1342，可达性剪枝不得漏掉合法组合
    r = client.post("/block-compositions/different-head/versions/1/search", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["total_results"] == 1
    res = body["results"][0]
    assert res["sequence"] == ["X"]
    assert res["final_row"] == "1342" and res["target_reached"] is True
    assert res["segments"][0]["start_row"] == "3124"
    assert res["segments"][0]["end_row"] == "1342"
    # 移除目标限制：final_row 与 segments 末行同样一致
    assert client.post(
        "/block-compositions",
        json={
            "id": "different-head-open",
            "blocks": [{"id": "X", "touch": {"id": "tx"}, "start_change": 0, "end_change": 1}],
            "start_row": "3124",
            "max_changes": 1,
        },
    ).status_code == 201
    body2 = client.post(
        "/block-compositions/different-head-open/versions/1/search", json={}
    ).json()
    res2 = body2["results"][0]
    assert res2["final_row"] == "1342"
    assert res2["segments"][0]["end_row"] == res2["final_row"]


def test_api_search_target_cannot_bypass_repeat(client):
    """目标末行不豁免重复：2143 两次出现的 [A, B] 须排除并给出两侧来源。"""
    client.post(
        "/methods",
        json={"id": "m-x12", "name": "CrossThen12", "stage": 4, "notation": "x12"},
    )
    client.post(
        "/methods",
        json={"id": "m-12", "name": "Only12", "stage": 4, "notation": "12"},
    )
    client.post(
        "/touches",
        json={"id": "ta", "method_id": "m-x12",
              "sequence": [{"leads": [{"call": None}]}]},
    )
    client.post(
        "/touches",
        json={"id": "tb", "method_id": "m-12",
              "sequence": [{"leads": [{"call": None}]}]},
    )
    # A：1234 → 2143 → 2134；B：再接一个 change 到 2143（重复）
    r = client.post(
        "/block-compositions",
        json={
            "id": "repeat-target",
            "blocks": [
                {"id": "A", "touch": {"id": "ta"}, "start_change": 0, "end_change": 2},
                {"id": "B", "touch": {"id": "tb"}, "start_change": 0, "end_change": 1},
            ],
            "start_row": "1234",
            "target_row": "2143",
            "max_changes": 3,
        },
    )
    assert r.status_code == 201
    body = client.post("/block-compositions/repeat-target/versions/1/search", json={}).json()
    seqs = [x["sequence"] for x in body["results"]]
    # [A, B]（1234、2143、2134、2143，2143 重复）须排除；[B, A] 为真保留
    assert ["A", "B"] not in seqs
    assert seqs == [["B", "A"]]
    assert body["pruned_by_rows"] >= 1
    fc = body["first_conflict"]
    assert fc["row"] == "2143"
    assert fc["first"]["block"] == "A" and fc["first"]["block_index"] == 1
    assert fc["first"]["change_in_block"] == 1 and fc["first"]["change"] == 1
    assert fc["first"]["touch"] == {"id": "ta", "version": 1}
    assert fc["first"]["touch_change"] == 1
    assert fc["first"]["method"] == "CrossThen12" and fc["first"]["call"] is None
    assert fc["second"]["block"] == "B" and fc["second"]["block_index"] == 2
    assert fc["second"]["change_in_block"] == 1 and fc["second"]["change"] == 3
    assert fc["second"]["touch"] == {"id": "tb", "version": 1}
    assert fc["second"]["touch_change"] == 1
    assert fc["second"]["method"] == "Only12" and fc["second"]["call"] is None
