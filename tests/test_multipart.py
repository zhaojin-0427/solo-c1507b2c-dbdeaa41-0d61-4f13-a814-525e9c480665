"""multipart composition 校核：置换展开、part end/循环/提前回归/闭合、
part 内及跨 part 重复定位、候选区段枚举剪枝、版本冻结与缓存。"""
import pytest
from fastapi.testclient import TestClient

from ringproof.main import create_app
from ringproof.multipart import (
    analyze_segment,
    bell_permutation,
    enumerate_segments,
    perm_cycles,
    perm_order,
    permute_row,
)
from ringproof.notation import expand_notation
from ringproof.engine import build_lead, prove_rows

PB_MINOR = "x16x16x16x16x16x12"
ROUNDS6 = (1, 2, 3, 4, 5, 6)


# ---------------- 置换运算 ----------------

def test_bell_permutation_and_cycles():
    # Plain Bob Minor 一个 plain lead：123456 → 135264
    images = bell_permutation(ROUNDS6, (1, 3, 5, 2, 6, 4))
    assert images == (1, 3, 5, 2, 6, 4)
    cycles, fixed = perm_cycles(images)
    assert cycles == [[2, 3, 5, 6, 4]]
    assert fixed == [1]
    assert perm_order(images) == 5
    # 反复作用回到起点
    row = ROUNDS6
    heads = []
    for _ in range(5):
        row = permute_row(images, row)
        heads.append(row)
    assert heads[-1] == ROUNDS6
    assert heads[0] == (1, 3, 5, 2, 6, 4)


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


def test_analyze_segment_five_part_plain_course():
    rows, events, lead_ends = _pb_rows(5)
    a = analyze_segment(
        stage=6, rows=rows, events=events, lead_end_indices=lead_ends,
        start_change=0, end_change=12, expected_parts=5, fixed_bells=[1],
    )
    assert a["permutation"]["cycles"] == [[2, 3, 5, 6, 4]]
    assert a["permutation"]["order"] == 5
    assert a["permutation"]["fixed_bells"] == [1]
    assert [p["row"] for p in a["part_ends"]] == [
        "135264", "156342", "164523", "142635", "123456",
    ]
    assert a["part_ends"][-1]["is_start_row"] and a["part_ends"][-1]["is_rounds"]
    assert a["early_returns"] == []
    assert a["closes_as_expected"] and a["comes_round"] and a["rounds_return"]
    assert a["fixed_bells_satisfied"]
    assert a["truth"] == "true" and a["first_conflict"] is None
    assert a["total_changes"] == 60 and a["total_rows"] == 61
    assert a["segment"]["start_lead"] == 1 and a["segment"]["end_lead"] == 1


def test_analyze_segment_early_return_and_repeat():
    rows, events, lead_ends = _pb_rows(5)
    a = analyze_segment(
        stage=6, rows=rows, events=events, lead_end_indices=lead_ends,
        start_change=0, end_change=12, expected_parts=10, fixed_bells=[],
    )
    # 轨道长度 5：第 5 个 part end 提前回到起点
    assert a["early_returns"] == [5]
    assert a["closes_as_expected"]  # 10 是 5 的倍数，仍按预期闭合
    assert a["truth"] == "false"
    fc = a["first_conflict"]
    assert fc["row"] == "123456"
    assert fc["first"]["type"] == "start" and fc["first"]["part"] == 1
    assert fc["second"]["part"] == 5 and fc["second"]["change_in_part"] == 12
    assert fc["second"]["touch_change"] == 12 and fc["second"]["lead"] == 1


def test_analyze_segment_orbit_not_closing_points_out_bells():
    rows, events, lead_ends = _pb_rows(5)
    a = analyze_segment(
        stage=6, rows=rows, events=events, lead_end_indices=lead_ends,
        start_change=0, end_change=12, expected_parts=3, fixed_bells=[],
    )
    assert not a["closes_as_expected"] and not a["comes_round"]
    assert [p["row"] for p in a["part_ends"]] == ["135264", "156342", "164523"]
    bad = {b["bell"]: b for b in a["inconsistent_bells"]}
    assert sorted(bad) == [2, 3, 4, 5, 6]  # 只有钟 1 按预期归位
    assert bad[2]["image"] == 6 and bad[2]["start_position"] == 2
    assert bad[2]["end_position"] == 5


def test_analyze_segment_fixed_bells_violation():
    rows, events, lead_ends = _pb_rows(5)
    a = analyze_segment(
        stage=6, rows=rows, events=events, lead_end_indices=lead_ends,
        start_change=0, end_change=12, expected_parts=5, fixed_bells=[1, 2],
    )
    checks = {c["bell"]: c for c in a["fixed_bell_checks"]}
    assert checks[1]["ok"] and checks[1]["image"] == 1
    assert not checks[2]["ok"] and checks[2]["image"] == 3
    assert not a["fixed_bells_satisfied"]


def test_analyze_segment_within_part_repeat_with_call_provenance():
    # [plain, bob, bob, bob]：row 135264 在 change 12 与 48 重复（part 内）
    rows, events, lead_ends = _pb_rows(4, [None, "bob", "bob", "bob"])
    a = analyze_segment(
        stage=6, rows=rows, events=events, lead_end_indices=lead_ends,
        start_change=0, end_change=48, expected_parts=1, fixed_bells=[],
    )
    assert a["truth"] == "false"
    fc = a["first_conflict"]
    assert fc["row"] == "135264"
    assert fc["first"]["part"] == 1 and fc["second"]["part"] == 1
    assert fc["first"]["touch_change"] == 12 and fc["first"]["call"] is None
    assert fc["second"]["touch_change"] == 48
    assert fc["second"]["call"] == "bob" and fc["second"]["notation"] == "14"
    assert fc["second"]["source"] == "call:bob"
    assert fc["second"]["lead"] == 4 and fc["second"]["change_in_lead"] == 12


def test_analyze_segment_cross_part_repeat():
    rows, events, lead_ends = _pb_rows(10)
    a = analyze_segment(
        stage=6, rows=rows, events=events, lead_end_indices=lead_ends,
        start_change=0, end_change=24, expected_parts=5, fixed_bells=[],
    )
    assert a["truth"] == "false"
    fc = a["first_conflict"]
    assert fc["row"] == "123456"
    assert fc["first"]["type"] == "start" and fc["first"]["part"] == 1
    assert fc["second"]["part"] == 3 and fc["second"]["change"] == 60
    assert fc["second"]["touch_change"] == 12 and fc["second"]["lead"] == 1


# ---------------- 候选区段枚举 ----------------

def test_enumerate_segments_pruning_and_order():
    rows, _, lead_ends = _pb_rows(5)
    e = enumerate_segments(
        stage=6, rows=rows, lead_end_indices=lead_ends, fixed_bells=[],
        min_parts=2, max_parts=64, max_results=50, max_search=20000,
    )
    assert e["candidates"] == 15 and e["checked"] == 15
    assert e["pruned_by_orbit"] == 1  # (0,60)：恒等置换，阶 1
    assert e["pruned_by_rows"] == 9
    assert e["pruned_by_fixed_bells"] == 0
    assert not e["truncated"]
    # 只保留整首为真的结果，沿用 touch 位置顺序
    segs = [(r["segment"]["start_change"], r["segment"]["end_change"]) for r in e["results"]]
    assert segs == [(0, 12), (12, 24), (24, 36), (36, 48), (48, 60)]
    assert all(r["parts"] == 5 and r["comes_round"] for r in e["results"])
    assert e["results"][0]["rounds_return"]  # 从 rounds 出发的 5-part 回到 rounds
    assert not e["results"][1]["rounds_return"]
    assert e["results"][0]["permutation"]["cycles"] == [[2, 3, 5, 6, 4]]


def test_enumerate_segments_fixed_bells_and_parts_range():
    rows, _, lead_ends = _pb_rows(5)
    e = enumerate_segments(
        stage=6, rows=rows, lead_end_indices=lead_ends, fixed_bells=[2],
        min_parts=2, max_parts=64, max_results=50, max_search=20000,
    )
    assert e["total_results"] == 0
    assert e["pruned_by_fixed_bells"] == 14 and e["pruned_by_orbit"] == 1
    assert e["truncation_reason"] is not None  # 穷尽未发现

    e4 = enumerate_segments(
        stage=6, rows=rows, lead_end_indices=lead_ends, fixed_bells=[],
        min_parts=4, max_parts=4, max_results=50, max_search=20000,
    )
    assert e4["total_results"] == 0 and e4["pruned_by_orbit"] == 15


def test_enumerate_segments_truncation():
    rows, _, lead_ends = _pb_rows(5)
    e = enumerate_segments(
        stage=6, rows=rows, lead_end_indices=lead_ends, fixed_bells=[],
        min_parts=2, max_parts=64, max_results=2, max_search=20000,
    )
    assert e["total_results"] == 2 and e["truncated"]
    assert "max_results" in e["truncation_reason"]

    e2 = enumerate_segments(
        stage=6, rows=rows, lead_end_indices=lead_ends, fixed_bells=[],
        min_parts=2, max_parts=64, max_results=50, max_search=3,
    )
    assert e2["checked"] == 3 and e2["truncated"]
    assert "max_search" in e2["truncation_reason"]


# ---------------- API ----------------

@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def pc_touch(client):
    client.post(
        "/methods",
        json={"id": "pb-minor", "name": "Plain Bob Minor", "stage": 6, "notation": PB_MINOR},
    )
    r = client.post(
        "/touches",
        json={
            "id": "pc",
            "method_id": "pb-minor",
            "calls": {"bob": {"notation": "14", "replace": 1},
                      "single": {"notation": "1234", "replace": 1}},
            "sequence": [{"leads": [{"call": None}], "repeat": 5}],
        },
    )
    assert r.status_code == 201
    return r.json()


def _post_analysis(client, **over):
    payload = {
        "id": "mp",
        "touch": {"id": "pc"},
        "part_start_change": 0,
        "part_end_change": 12,
        "expected_parts": 5,
        "fixed_bells": [1],
    }
    payload.update(over)
    return client.post("/multipart-analyses", json=payload)


def test_api_create_and_get_analysis(client, pc_touch):
    r = _post_analysis(client)
    assert r.status_code == 201
    body = r.json()
    assert body["version"] == 1
    assert body["touch"] == {"id": "pc", "version": 1, "input_hash": body["touch"]["input_hash"]}
    assert body["permutation"]["cycles"] == [[2, 3, 5, 6, 4]]
    assert body["permutation"]["order"] == 5
    assert [p["row"] for p in body["part_ends"]] == [
        "135264", "156342", "164523", "142635", "123456",
    ]
    assert body["early_returns"] == []
    assert body["closes_as_expected"] and body["truth"] == "true"
    assert body["fixed_bells_satisfied"]
    # 依赖冻结：touch、方法、call、ringproof 版本
    deps = body["dependencies"]
    assert deps["touch"]["version"] == 1
    assert deps["methods"] == [
        {"id": "pb-minor", "version": 1, "input_hash": deps["methods"][0]["input_hash"]}
    ]
    assert sorted(deps["calls"]) == ["bob", "single"]
    assert "ringproof_version" in deps

    # GET 读回与创建响应一致；列表与版本列表
    got = client.get("/multipart-analyses/mp/versions/1").json()
    assert got == body
    listing = client.get("/multipart-analyses").json()["multipart_analyses"]
    assert [m["id"] for m in listing] == ["mp"]
    versions = client.get("/multipart-analyses/mp").json()["versions"]
    assert [v["version"] for v in versions] == [1]

    # 404
    assert client.get("/multipart-analyses/ghost/versions/1").status_code == 404
    assert client.get("/multipart-analyses/mp/versions/9").status_code == 404
    assert client.get("/multipart-analyses/ghost").status_code == 404


def test_api_analysis_immutable_and_frozen_touch(client, pc_touch):
    r1 = _post_analysis(client)
    assert r1.status_code == 201
    # touch 出新版本后，分析 v1 仍冻结在 touch v1
    client.post(
        "/touches",
        json={"id": "pc", "method_id": "pb-minor",
              "sequence": [{"leads": [{"call": None}], "repeat": 6}]},
    )
    got = client.get("/multipart-analyses/mp/versions/1").json()
    assert got["touch"]["version"] == 1
    assert got["total_changes"] == 60
    # 显式引用 touch v1 再建分析：输入相同则 input_hash 一致
    r2 = _post_analysis(client, touch={"id": "pc", "version": 1})
    assert r2.status_code == 201
    assert r2.json()["version"] == 2
    assert r2.json()["input_hash"] == r1.json()["input_hash"]
    assert r2.json()["touch"]["version"] == 1


def test_api_analysis_early_return_and_inconsistent_bells(client, pc_touch):
    r = _post_analysis(client, id="mp10", expected_parts=10, fixed_bells=[])
    assert r.status_code == 201
    body = r.json()
    assert body["early_returns"] == [5]
    assert body["closes_as_expected"] and body["truth"] == "false"
    assert body["first_conflict"]["second"]["part"] == 5

    r3 = _post_analysis(client, id="mp3", expected_parts=3, fixed_bells=[])
    body3 = r3.json()
    assert not body3["closes_as_expected"]
    assert sorted(b["bell"] for b in body3["inconsistent_bells"]) == [2, 3, 4, 5, 6]


def test_api_analysis_validation_errors(client, pc_touch):
    # touch 不存在
    assert _post_analysis(client, touch={"id": "ghost"}).status_code == 404
    assert _post_analysis(client, touch={"id": "pc", "version": 9}).status_code == 404
    # 区段首尾不在 lead end
    r = _post_analysis(client, part_end_change=13)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "NOT_LEAD_END"
    r = _post_analysis(client, part_start_change=5, part_end_change=12)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "NOT_LEAD_END"
    # 超出 touch 范围
    r = _post_analysis(client, part_end_change=61)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "CHANGE_OUT_OF_RANGE"
    # fixed_bells 越界
    r = _post_analysis(client, fixed_bells=[7])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "BELL_OUT_OF_RANGE"
    # 展开规模超限（20 个 lead 的 touch + 500 个 part）
    client.post(
        "/touches",
        json={"id": "pc20", "method_id": "pb-minor",
              "sequence": [{"leads": [{"call": None}], "repeat": 20}]},
    )
    r = _post_analysis(client, touch={"id": "pc20"}, part_end_change=240, expected_parts=500)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "TOO_MANY_CHANGES"
    # pydantic 校验：start >= end、fixed_bells 重复、expected_parts 越界
    assert _post_analysis(client, part_start_change=12, part_end_change=12).status_code == 422
    assert _post_analysis(client, fixed_bells=[1, 1]).status_code == 422
    assert _post_analysis(client, expected_parts=0).status_code == 422


def test_api_analysis_unresolved_choice_rejected(client, pc_touch):
    client.post(
        "/touches",
        json={"id": "choice-touch", "method_id": "pb-minor",
              "calls": {"bob": {"notation": "14", "replace": 1}},
              "sequence": [{"leads": [{"choice": ["plain", "bob"]}, {"call": None}]}]},
    )
    r = _post_analysis(client, touch={"id": "choice-touch"}, part_end_change=12)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "UNRESOLVED_CHOICE"


def test_api_enumerate(client, pc_touch):
    assert _post_analysis(client, fixed_bells=[]).status_code == 201
    r = client.post("/multipart-analyses/mp/versions/1/enumerate", json={})
    assert r.status_code == 200 and r.headers["X-Multipart-Cache"] == "miss"
    body = r.json()
    assert body["candidates"] == 15 and body["checked"] == 15
    assert body["sorted_by"] == ["start_change", "end_change"]
    assert body["parts_range"] == [2, 64]
    assert body["pruned_by_orbit"] == 1 and body["pruned_by_rows"] == 9
    segs = [(x["segment"]["start_change"], x["segment"]["end_change"]) for x in body["results"]]
    assert segs == [(0, 12), (12, 24), (24, 36), (36, 48), (48, 60)]
    assert all(x["parts"] == 5 for x in body["results"])
    assert body["dependencies"]["touch"]["version"] == 1

    # 相同请求重复枚举：缓存命中且结果一致
    r2 = client.post("/multipart-analyses/mp/versions/1/enumerate", json={})
    assert r2.headers["X-Multipart-Cache"] == "hit"
    assert r2.json() == body

    # 限定 part 数：阶不为 4 的候选全部被轨道剪枝
    r4 = client.post("/multipart-analyses/mp/versions/1/enumerate", json={"parts": 4})
    assert r4.json()["total_results"] == 0
    assert r4.json()["pruned_by_orbit"] == 15
    assert r4.json()["parts_range"] == [4, 4]

    # 结果上限截断
    r5 = client.post("/multipart-analyses/mp/versions/1/enumerate", json={"max_results": 2})
    assert r5.json()["total_results"] == 2 and r5.json()["truncated"]

    # 分析不存在
    assert client.post("/multipart-analyses/ghost/versions/1/enumerate", json={}).status_code == 404


def test_api_enumerate_inherits_fixed_bells(client, pc_touch):
    assert _post_analysis(client, id="mpf", fixed_bells=[2]).status_code == 201
    r = client.post("/multipart-analyses/mpf/versions/1/enumerate", json={})
    body = r.json()
    assert body["fixed_bells"] == [2]
    assert body["total_results"] == 0
    assert body["pruned_by_fixed_bells"] == 14
    assert body["pruned_by_orbit"] == 1
