"""all-the-work 覆盖分析：覆盖方案版本管理、证明矩阵与枚举过滤/排序。"""
import pytest
from fastapi.testclient import TestClient

from ringproof.coverage import analyze_coverage, coverage_summary
from ringproof.main import create_app

PLAIN_BOB_MINOR = "x16x16x16x16x16x12"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def method_id(client):
    r = client.post(
        "/methods",
        json={"id": "pb-minor", "name": "Plain Bob Minor", "stage": 6, "notation": PLAIN_BOB_MINOR},
    )
    assert r.status_code == 201
    return r.json()["id"]


def _atw_scheme(client, method_id="pb-minor", bells=(2, 3, 4, 5, 6), sid="atw"):
    """标准 all-the-work：每个工作钟在方法下敲全部 place-bell 类别（1..6）各 1 次。"""
    cells = [
        {"bell": b, "method": method_id, "place_bell": pb, "min_leads": 1}
        for b in bells
        for pb in range(1, 7)
    ]
    r = client.post(
        "/coverage-schemes",
        json={"id": sid, "name": "ATW", "stage": 6, "working_bells": list(bells),
              "methods": [{"id": method_id}], "cells": cells},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _plain_touch(client, repeat=5, **over):
    payload = {
        "method_id": "pb-minor",
        "calls": {"bob": {"notation": "14", "replace": 1}},
        "sequence": [{"leads": [{"call": None}], "repeat": repeat}],
    }
    payload.update(over)
    r = client.post("/touches", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------- 覆盖计算单元测试 ----------------

def test_analyze_coverage_bell2_place_bells(method_id):
    """钟 2 在 Plain Bob Minor plain course 的 place-bell 序列为 2,4,6,5,3。"""
    from ringproof.engine import build_lead, prove_rows
    from ringproof.notation import expand_notation
    tokens, changes = expand_notation(PLAIN_BOB_MINOR, 6)
    leads = [
        build_lead(tokens, changes, None, {}, method_id="pb-minor", method_version=1)
        for _ in range(5)
    ]
    proof = prove_rows(6, "Plain Bob Minor", leads, tuple(range(1, 7)))
    spec = {
        "stage": 6,
        "working_bells": [2],
        "methods": [{"id": "pb-minor", "version": 1, "name": "Plain Bob Minor"}],
        "cells": [
            {"bell": 2, "method": "pb-minor", "method_version": 1,
             "place_bell": pb, "min_leads": 1}
            for pb in range(1, 7)
        ],
    }
    a = analyze_coverage(spec, proof)
    by_pb = {c["place_bell"]: c for c in a["cells"]}
    assert {pb for pb, c in by_pb.items() if c["count"] == 1} == {2, 3, 4, 5, 6}
    assert by_pb[2]["first_lead"] == 1
    assert by_pb[6]["first_lead"] == 3
    assert by_pb[1]["count"] == 0 and by_pb[1]["first_lead"] is None
    assert by_pb[1]["deficit"] == 1
    assert not a["full_coverage"]
    assert a["completion"] == pytest.approx(5 / 6)
    assert [m["place_bell"] for m in a["missing_cells"]] == [1]
    # 完整矩阵不包含未出现的 1 号类别
    assert {m["place_bell"] for m in a["matrix"]} == {2, 3, 4, 5, 6}
    assert all(m["required"] for m in a["matrix"])
    summary = coverage_summary(a)
    assert summary["completion"] == pytest.approx(5 / 6)
    assert summary["missing_cells"] == 1


def test_analyze_coverage_balance_is_range_of_bell_completions(method_id):
    """均衡度 = 各工作钟完成率的极差（max-min）。"""
    from ringproof.engine import build_lead, prove_rows
    from ringproof.notation import expand_notation
    tokens, changes = expand_notation(PLAIN_BOB_MINOR, 6)
    leads = [
        build_lead(tokens, changes, None, {}, method_id="pb-minor", method_version=1)
        for _ in range(5)
    ]
    proof = prove_rows(6, "Plain Bob Minor", leads, tuple(range(1, 7)))
    # 钟 2 要求 5 个非 treble 类别（plain course 全覆盖 → 1.0）；
    # 钟 3 额外要求 place bell 1（从不出现，min 1 → 完成率 5/6）
    cells = [
        {"bell": 2, "method": "pb-minor", "method_version": 1,
         "place_bell": pb, "min_leads": 1}
        for pb in (2, 3, 4, 5, 6)
    ] + [
        {"bell": 3, "method": "pb-minor", "method_version": 1,
         "place_bell": pb, "min_leads": 1}
        for pb in range(1, 7)
    ]
    spec = {
        "stage": 6,
        "working_bells": [2, 3],
        "methods": [{"id": "pb-minor", "version": 1, "name": "Plain Bob Minor"}],
        "cells": cells,
    }
    a = analyze_coverage(spec, proof)
    by_bell = {b["bell"]: b["completion"] for b in a["bells"]}
    assert by_bell[2] == 1.0
    assert by_bell[3] == pytest.approx(5 / 6)
    assert a["balance"] == pytest.approx(1 / 6, abs=1e-5)
    assert not a["full_coverage"]


# ---------------- 覆盖方案：创建与不可变版本 ----------------

def test_create_coverage_scheme_expands_and_lists_versions(client, method_id):
    s = _atw_scheme(client, bells=(2,))
    assert s["version"] == 1
    assert s["working_bells"] == [2]
    # place_bell 留空 → 展开为全部 1..6 类别
    assert sorted(c["place_bell"] for c in s["cells"]) == [1, 2, 3, 4, 5, 6]
    assert all(c["method"] == "pb-minor" and c["method_version"] == 1 for c in s["cells"])
    assert {m["id"] for m in s["methods"]} == {"pb-minor"}

    # 同 id 再提交 → 版本递增；v1 保持不变
    s2 = _atw_scheme(client, bells=(2,), sid="atw")
    assert s2["version"] == 2
    again = client.get(f"/coverage-schemes/{s['id']}/versions/1").json()
    assert again == s
    versions = client.get("/coverage-schemes/atw").json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert any(x["id"] == "atw" for x in client.get("/coverage-schemes").json()["coverage_schemes"])


def test_coverage_scheme_validation_errors(client, method_id):
    def post(body):
        return client.post("/coverage-schemes", json=body)

    base = {"name": "s", "stage": 6, "working_bells": [2], "methods": [{"id": "pb-minor"}],
            "cells": [{"bell": 2, "method": "pb-minor", "min_leads": 1}]}
    # 重复格（展开后 place-bell 类别相交）
    dup = dict(base, cells=[
        {"bell": 2, "method": "pb-minor", "min_leads": 1},
        {"bell": 2, "method": "pb-minor", "place_bell": 3, "min_leads": 2},
    ])
    r = post(dup)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "DUPLICATE_CELL"
    # 相同显式格重复
    dup2 = dict(base, cells=[
        {"bell": 2, "method": "pb-minor", "place_bell": 4, "min_leads": 1},
        {"bell": 2, "method": "pb-minor", "place_bell": 4, "min_leads": 2},
    ])
    r = post(dup2)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "DUPLICATE_CELL"
    # 越界工作钟（Pydantic 422）
    r = post(dict(base, working_bells=[7]))
    assert r.status_code == 422
    # 重复工作钟
    r = post(dict(base, working_bells=[2, 2]))
    assert r.status_code == 422
    # 覆盖格引用不在 working_bells 的钟
    r = post(dict(base, working_bells=[3]))
    assert r.status_code == 422
    # 覆盖格引用 methods 列表外的方法
    r = post(dict(base, cells=[{"bell": 2, "method": "other", "min_leads": 1}]))
    assert r.status_code == 422
    # methods 列表重复 id
    r = post(dict(base, methods=[{"id": "pb-minor"}, {"id": "pb-minor"}]))
    assert r.status_code == 422
    # 方法不存在 → 404
    r = post(dict(base, methods=[{"id": "ghost"}], cells=[
        {"bell": 2, "method": "ghost", "min_leads": 1}]))
    assert r.status_code == 404


def test_coverage_scheme_stage_mismatch_rejected(client, method_id):
    client.post("/methods",
                json={"id": "pb-major", "name": "Plain Bob Major", "stage": 8,
                      "notation": "x18x18x18x18x18x18x18x12"})
    r = client.post("/coverage-schemes", json={
        "name": "mix", "stage": 6, "working_bells": [2],
        "methods": [{"id": "pb-major"}],
        "cells": [{"bell": 2, "method": "pb-major", "min_leads": 1}],
    })
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COVERAGE_STAGE_MISMATCH"


def test_coverage_scheme_freezes_method_version(client, method_id):
    """方法版本留空时冻结为当前最新；之后新增方法版本不影响已存方案。"""
    client.post("/methods",
                json={"id": "pb-minor", "name": "Plain Bob Minor v2", "stage": 6,
                      "notation": PLAIN_BOB_MINOR})
    s = _atw_scheme(client, bells=(2,))
    assert s["methods"][0]["version"] == 2
    assert s["cells"][0]["method_version"] == 2


# ---------------- 证明：覆盖矩阵 ----------------

def test_prove_with_coverage_full_course_all_bells(client, method_id):
    s = _atw_scheme(client)
    touch = _plain_touch(client)
    r = client.post(f"/touches/{touch['id']}/versions/1/prove", json={"coverage": {"id": s["id"]}})
    assert r.status_code == 200 and r.headers["X-Proof-Cache"] == "miss"
    cov = r.json()["coverage"]
    assert cov["scheme"]["id"] == "atw" and cov["scheme"]["version"] == 1
    assert cov["counted_leads"] == 5 and cov["unmatched_methods"] == []
    # 5 口工作钟在 plain course 内各敲 5 个不同 place bell（每口缺一个类别）
    for b in cov["bells"]:
        assert b["required_leads"] == 6
        assert b["observed_leads"] == 5
        assert b["completion"] == pytest.approx(5 / 6)
        assert b["cells_total"] == 6 and b["cells_satisfied"] == 5
    assert not cov["full_coverage"]
    # 缺口/首次末次：钟 2 敲 place bell 2 仅在 lead 1，place bell 3 仅在 lead 5
    c2 = {c["place_bell"]: c for c in cov["cells"] if c["bell"] == 2}
    assert c2[2]["first_lead"] == 1 and c2[2]["last_lead"] == 1 and c2[2]["gaps"] == []
    assert c2[3]["first_lead"] == 5
    assert c2[1]["first_lead"] is None and c2[1]["deficit"] == 1
    assert cov["completion"] == pytest.approx(25 / 30)
    assert cov["balance"] == 0.0  # 各工作钟完成率相同 → 均衡度极差为 0


def test_prove_coverage_cache_hit_and_consistency(client, method_id):
    s = _atw_scheme(client)
    touch = _plain_touch(client)
    url = f"/touches/{touch['id']}/versions/1/prove"
    p1 = client.post(url, json={"coverage": {"id": s["id"]}})
    p2 = client.post(url, json={"coverage": {"id": s["id"]}})
    assert p2.headers["X-Proof-Cache"] == "hit"
    assert p1.json()["coverage"] == p2.json()["coverage"]
    # 不带覆盖维度的证明缓存互不干扰
    p3 = client.post(url)
    assert "coverage" not in p3.json()


def test_prove_coverage_frozen_scheme_version(client, method_id):
    """引用时版本留空则随 touch 冻结：之后新增方案版本，重复证明仍用冻结版本。"""
    s1 = _atw_scheme(client, bells=(2,))
    touch = _plain_touch(client)
    url = f"/touches/{touch['id']}/versions/1/prove"
    p1 = client.post(url, json={"coverage": {"id": "atw"}})
    assert p1.json()["coverage"]["scheme"]["version"] == 1
    # 新建同 id 版本 2（只有钟 2 的 place bell 2 一格）
    r = client.post("/coverage-schemes", json={
        "id": "atw", "name": "ATW v2", "stage": 6, "working_bells": [2],
        "methods": [{"id": "pb-minor"}],
        "cells": [{"bell": 2, "method": "pb-minor", "place_bell": 2, "min_leads": 1}],
    })
    assert r.status_code == 201
    p2 = client.post(url, json={"coverage": {"id": "atw"}})
    assert p2.headers["X-Proof-Cache"] == "hit"
    assert p2.json()["coverage"]["scheme"]["version"] == 1
    # 显式引用 v2 则使用新版本（独立缓存）
    p3 = client.post(url, json={"coverage": {"id": "atw", "version": 2}})
    assert p3.json()["coverage"]["scheme"]["version"] == 2
    assert p3.headers["X-Proof-Cache"] == "miss"


def test_prove_coverage_calls_change_lead_heads(client, method_id):
    """call 改变后续 lead head 后，place bell 按实际位置计数。

    6 个 lead 中第 2 个为 bob：钟 3 的 place-bell 序列由全 plain 的
    3,2,4,6,5,3 变为 3,2,2,4,6,5（第 3 个 lead 起的 lead head 被 bob 改变）。"""
    r = client.post("/coverage-schemes", json={
        "name": "b3", "stage": 6, "working_bells": [3],
        "methods": [{"id": "pb-minor"}],
        "cells": [
            {"bell": 3, "method": "pb-minor", "place_bell": pb, "min_leads": 1}
            for pb in range(1, 7)
        ],
    })
    sid = r.json()["id"]
    bob_touch = _plain_touch(
        client,
        sequence=[{"leads": [
            {"call": None}, {"call": "bob"}, {"call": None},
            {"call": None}, {"call": None}, {"call": None},
        ]}],
    )
    cov = client.post(
        f"/touches/{bob_touch['id']}/versions/1/prove", json={"coverage": {"id": sid}}
    ).json()["coverage"]
    by_pb = {c["place_bell"]: c["count"] for c in cov["cells"] if c["bell"] == 3}
    # 钟 3：place bell 2 敲了两次（lead 2 与 lead 3），其余 4,6,5 各一次
    assert by_pb == {1: 0, 2: 2, 3: 1, 4: 1, 5: 1, 6: 1}
    pb2 = next(c for c in cov["cells"] if c["bell"] == 3 and c["place_bell"] == 2)
    assert pb2["first_lead"] == 2 and pb2["last_lead"] == 3 and pb2["gaps"] == [1]

    # 对照：同长度全 plain touch 的分布为 3,2,4,6,5,3（place bell 3 出现两次）
    plain_touch = _plain_touch(client, repeat=6)
    plain_cov = client.post(
        f"/touches/{plain_touch['id']}/versions/1/prove", json={"coverage": {"id": sid}}
    ).json()["coverage"]
    plain_by_pb = {
        c["place_bell"]: c["count"] for c in plain_cov["cells"] if c["bell"] == 3
    }
    assert plain_by_pb == {1: 0, 2: 1, 3: 2, 4: 1, 5: 1, 6: 1}


def test_prove_coverage_multi_method_unmatched_leads(client, method_id):
    client.post("/methods",
                json={"id": "cam", "name": "Cambridge S Minor", "stage": 6,
                      "notation": "&x3x4x2x3x4x5,+2"})
    s = _atw_scheme(client, bells=(2,))
    r = client.post("/touches", json={
        "methods": [{"id": "pb-minor"}, {"id": "cam"}],
        "sequence": [{"leads": [
            {"method": "pb-minor"}, {"method": "cam"},
            {"method": "pb-minor"}, {"method": "pb-minor"}, {"method": "pb-minor"},
        ]}],
    })
    assert r.status_code == 201
    touch = r.json()
    cov = client.post(
        f"/touches/{touch['id']}/versions/1/prove", json={"coverage": {"id": s["id"]}}
    ).json()["coverage"]
    assert cov["counted_leads"] == 4
    assert cov["total_leads"] == 5
    unmatched = cov["unmatched_methods"]
    assert len(unmatched) == 1
    assert unmatched[0]["method"]["id"] == "cam"
    assert unmatched[0]["method"]["name"] == "Cambridge S Minor"
    assert unmatched[0]["leads"] == [2]
    # 矩阵只含方案方法
    assert {m["method"]["id"] for m in cov["matrix"]} == {"pb-minor"}


def test_prove_coverage_min_leads_deficits_and_gaps(client, method_id):
    r = client.post("/coverage-schemes", json={
        "name": "rep", "stage": 6, "working_bells": [2],
        "methods": [{"id": "pb-minor"}],
        "cells": [{"bell": 2, "method": "pb-minor", "place_bell": 2, "min_leads": 2}],
    })
    sid = r.json()["id"]
    # 7 个 plain lead：钟 2 的 place bell 2 出现在 lead 1 与 lead 6（间隔 5）
    touch = _plain_touch(client, repeat=7)
    cov = client.post(
        f"/touches/{touch['id']}/versions/1/prove", json={"coverage": {"id": sid}}
    ).json()["coverage"]
    cell = cov["cells"][0]
    assert cell["count"] == 2 and cell["satisfied"]
    assert cell["first_lead"] == 1 and cell["last_lead"] == 6
    assert cell["gaps"] == [5] and cell["longest_gap"] == 5
    assert cov["deficits"] == [] and cov["missing_cells"] == []
    assert cov["full_coverage"]
    # 方案只要求 place bell 2，但矩阵含全部观测类别（extra_observations 汇总非要求格）
    matrix_pbs = {m["place_bell"] for m in cov["matrix"] if m["bell"] == 2}
    assert matrix_pbs == {2, 3, 4, 5, 6}
    required = {m["place_bell"] for m in cov["matrix"] if m["required"]}
    assert required == {2}
    # 非要求类别（3/4/5/6 各 1 次，每 5 lead 一轮）合计 5 个 lead 的额外观测
    assert cov["extra_observations"] == 5
    # min_leads=3 时未达标：缺口 deficit=1，列入 deficits 但不属于 missing_cells
    r3 = client.post("/coverage-schemes", json={
        "name": "rep3", "stage": 6, "working_bells": [2],
        "methods": [{"id": "pb-minor"}],
        "cells": [{"bell": 2, "method": "pb-minor", "place_bell": 2, "min_leads": 3}],
    })
    sid3 = r3.json()["id"]
    cov3 = client.post(
        f"/touches/{touch['id']}/versions/1/prove", json={"coverage": {"id": sid3}}
    ).json()["coverage"]
    assert cov3["cells"][0]["deficit"] == 1
    assert len(cov3["deficits"]) == 1 and cov3["missing_cells"] == []
    assert not cov3["full_coverage"]


def test_prove_coverage_errors(client, method_id):
    s = _atw_scheme(client)
    touch = _plain_touch(client)
    # 方案不存在 → 404
    r = client.post(f"/touches/{touch['id']}/versions/1/prove",
                    json={"coverage": {"id": "ghost"}})
    assert r.status_code == 404
    # 方案版本不存在 → 404
    r = client.post(f"/touches/{touch['id']}/versions/1/prove",
                    json={"coverage": {"id": s["id"], "version": 9}})
    assert r.status_code == 404
    # 钟数不一致 → 422
    client.post("/methods",
                json={"id": "pb-major", "name": "PB Major", "stage": 8,
                      "notation": "x18x18x18x18x18x18x18x12"})
    r = client.post("/coverage-schemes", json={
        "name": "eight", "stage": 8, "working_bells": [2],
        "methods": [{"id": "pb-major"}],
        "cells": [{"bell": 2, "method": "pb-major", "min_leads": 1}],
    })
    major_scheme = r.json()["id"]
    r = client.post(f"/touches/{touch['id']}/versions/1/prove",
                    json={"coverage": {"id": major_scheme}})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COVERAGE_STAGE_MISMATCH"


# ---------------- 枚举：过滤与排序 ----------------

def _two_slot_touch(client):
    return _plain_touch(
        client,
        sequence=[{"leads": [
            {"call": None}, {"choice": ["plain", "bob"]}, {"call": None},
            {"choice": ["plain", "bob"]}, {"call": None},
        ]}],
    )


def test_enumerate_coverage_attached_and_sorted(client, method_id):
    r = client.post("/coverage-schemes", json={
        "name": "pb4x2", "stage": 6, "working_bells": [2],
        "methods": [{"id": "pb-minor"}],
        "cells": [{"bell": 2, "method": "pb-minor", "place_bell": 4, "min_leads": 2}],
    })
    sid = r.json()["id"]
    touch = _two_slot_touch(client)
    data = client.post(
        f"/touches/{touch['id']}/versions/1/enumerate", json={"coverage": {"id": sid}}
    ).json()
    assert data["filtered_by_coverage"] == 0
    assert data["sorted_by"][:2] == ["coverage_completion", "coverage_balance"]
    completions = [v["coverage"]["completion"] for v in data["variants"]]
    # 完成率降序：两个 1.0（第 4 lead 为 bob）在前，两个 0.5 在后
    assert completions == sorted(completions, reverse=True)
    assert completions == [1.0, 1.0, 0.5, 0.5]
    # 同完成率内按 num_calls 升序：仅第 4 lead bob（1 call）排在双 bob（2 calls）前
    assert [v["num_calls"] for v in data["variants"][:2]] == [1, 2]
    assert data["variants"][0]["calls"][3] == "bob"
    # 响应附带冻结的方案版本与门槛参数
    assert data["coverage"]["id"] == sid
    assert data["require_full_coverage"] is False and data["min_completion"] is None


def test_enumerate_require_full_coverage_filters(client, method_id):
    r = client.post("/coverage-schemes", json={
        "name": "pb4x2", "stage": 6, "working_bells": [2],
        "methods": [{"id": "pb-minor"}],
        "cells": [{"bell": 2, "method": "pb-minor", "place_bell": 4, "min_leads": 2}],
    })
    sid = r.json()["id"]
    touch = _two_slot_touch(client)
    data = client.post(
        f"/touches/{touch['id']}/versions/1/enumerate",
        json={"coverage": {"id": sid}, "require_full_coverage": True},
    ).json()
    assert data["total_variants"] == 2
    assert data["filtered_by_coverage"] == 2
    assert all(v["coverage"]["full_coverage"] for v in data["variants"])
    # 通过的变体均在第 4 lead 使用 bob
    assert all(v["calls"][3] == "bob" for v in data["variants"])


def test_enumerate_min_completion_filters(client, method_id):
    r = client.post("/coverage-schemes", json={
        "name": "pb4x2", "stage": 6, "working_bells": [2],
        "methods": [{"id": "pb-minor"}],
        "cells": [{"bell": 2, "method": "pb-minor", "place_bell": 4, "min_leads": 2}],
    })
    sid = r.json()["id"]
    touch = _two_slot_touch(client)
    data = client.post(
        f"/touches/{touch['id']}/versions/1/enumerate",
        json={"coverage": {"id": sid}, "min_completion": 0.75},
    ).json()
    assert data["total_variants"] == 2
    assert all(v["coverage"]["completion"] >= 0.75 for v in data["variants"])
    # 边界值：0.5 不过滤任何变体（completion==0.5 达标）
    data_all = client.post(
        f"/touches/{touch['id']}/versions/1/enumerate",
        json={"coverage": {"id": sid}, "min_completion": 0.5},
    ).json()
    assert data_all["total_variants"] == 4


def test_enumerate_coverage_validator_requires_ref(client, method_id):
    touch = _two_slot_touch(client)
    r = client.post(
        f"/touches/{touch['id']}/versions/1/enumerate",
        json={"require_full_coverage": True},
    )
    assert r.status_code == 422
    r = client.post(
        f"/touches/{touch['id']}/versions/1/enumerate",
        json={"min_completion": 0.5},
    )
    assert r.status_code == 422
    # min_completion 越界
    r = client.post(
        f"/touches/{touch['id']}/versions/1/enumerate",
        json={"coverage": {"id": "x"}, "min_completion": 1.5},
    )
    assert r.status_code == 422


def test_enumerate_coverage_deterministic(client, method_id):
    """重复枚举结果一致（覆盖方案版本随 touch 冻结）。"""
    r = client.post("/coverage-schemes", json={
        "name": "pb4x2", "stage": 6, "working_bells": [2],
        "methods": [{"id": "pb-minor"}],
        "cells": [{"bell": 2, "method": "pb-minor", "place_bell": 4, "min_leads": 2}],
    })
    sid = r.json()["id"]
    touch = _two_slot_touch(client)
    body = {"coverage": {"id": sid}, "require_full_coverage": False, "min_completion": 0.5}
    d1 = client.post(f"/touches/{touch['id']}/versions/1/enumerate", json=body).json()
    d2 = client.post(f"/touches/{touch['id']}/versions/1/enumerate", json=body).json()
    assert d1["variants"] == d2["variants"]
    assert d1["sorted_by"] == d2["sorted_by"]
