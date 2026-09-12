"""方法相假图谱：course head 枚举、plain course 展开与闭合、内部真值、
共享 row 矩阵/首次冲突/连通分组、创建拒绝、截断、筛选与版本一致性。"""
import pytest
from fastapi.testclient import TestClient

from ringproof.falseness import (
    FalsenessError,
    analyze_falseness,
    enumerate_course_heads,
    expand_plain_course,
)
from ringproof.main import create_app
from ringproof.notation import expand_notation

PB_MINOR = "x16x16x16x16x16x12"
CAMBRIDGE = "&x3x4x2x3x4x5,+2"
FALSE_4 = "x12x"  # 4 口钟方法：plain course 内部有重复 row
ALT_4 = "x14x14x14x12"
ROUNDS6 = (1, 2, 3, 4, 5, 6)


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def methods(client):
    for mid, name, stage, notation in [
        ("pb-minor", "Plain Bob Minor", 6, PB_MINOR),
        ("cambridge-minor", "Cambridge Surprise Minor", 6, CAMBRIDGE),
        ("false-4", "False Four", 4, FALSE_4),
        ("alt-4", "Alt Four", 4, ALT_4),
    ]:
        r = client.post(
            "/methods",
            json={"id": mid, "name": name, "stage": stage, "notation": notation},
        )
        assert r.status_code == 201
    return client


# ---------------- course head 枚举 ----------------

def test_enumerate_course_heads_fixes_other_bells():
    heads, total = enumerate_course_heads(ROUNDS6, [5, 6])
    assert heads == [ROUNDS6, (1, 2, 3, 4, 6, 5)]
    assert total == 2
    # 非可变钟位保持参考排列中的钟
    heads, total = enumerate_course_heads(ROUNDS6, [2, 3, 4])
    assert total == 6 and len(heads) == 6
    assert heads[0] == ROUNDS6  # 参考 course head 排在第一
    assert all(h[0] == 1 and h[4:] == (5, 6) for h in heads)
    assert len(set(heads)) == 6


def test_enumerate_course_heads_truncates_deterministically():
    heads, total = enumerate_course_heads(ROUNDS6, [2, 3, 4], max_course_heads=4)
    assert total == 6 and len(heads) == 4
    assert heads == [
        (1, 2, 3, 4, 5, 6),
        (1, 2, 4, 3, 5, 6),
        (1, 3, 2, 4, 5, 6),
        (1, 3, 4, 2, 5, 6),
    ]
    # 参考排列中的可变钟取值顺序决定枚举顺序
    heads2, _ = enumerate_course_heads((1, 3, 2, 4, 5, 6), [2, 3], max_course_heads=2)
    assert heads2 == [(1, 3, 2, 4, 5, 6), (1, 2, 3, 4, 5, 6)]


# ---------------- plain course 展开 ----------------

def test_expand_plain_course_closes_true():
    _, changes = expand_notation(PB_MINOR, 6)
    c = expand_plain_course(changes, ROUNDS6, 200)
    assert c["leads"] == 5 and c["changes"] == 60
    assert c["truth"] == "true" and c["first_repeat"] is None
    assert c["rows"][0] == (ROUNDS6, 0, 0, 0)
    assert c["rows"][-1][0] == ROUNDS6  # 终点回到 course head 属正常闭合


def test_expand_plain_course_internal_repeat():
    c = expand_plain_course(
        [frozenset(), frozenset({1, 2}), frozenset()], (1, 2, 3, 4), 100
    )
    assert c["leads"] == 2 and c["changes"] == 6
    assert c["truth"] == "false"
    fr = c["first_repeat"]
    assert fr["row"] == (2, 1, 3, 4) and fr["changes"] == [2, 4]
    assert fr["first"] == {"lead": 1, "change_in_lead": 2, "change": 2}
    assert fr["second"] == {"lead": 2, "change_in_lead": 1, "change": 4}


def test_expand_plain_course_not_closed_raises():
    _, changes = expand_notation(PB_MINOR, 6)
    with pytest.raises(FalsenessError) as exc:
        expand_plain_course(changes, ROUNDS6, 2)
    assert exc.value.code == "COURSE_NOT_CLOSED"


# ---------------- 共享 row 分析 ----------------

def _pb_ctx():
    _, changes = expand_notation(PB_MINOR, 6)
    return [{"id": "pb", "version": 1, "name": "PB", "changes": changes}]


def test_analyze_shared_rows_conflict_and_groups():
    heads = [ROUNDS6, (1, 3, 5, 2, 6, 4), (1, 2, 3, 4, 6, 5)]
    a = analyze_falseness(_pb_ctx(), heads, 200)
    # 123456 与 135264 是同一 course 的两个 lead head：共享全部 60 个 row；
    # 123465 属于另一 course，与二者均无共享
    assert a["matrix"] == {
        "size": 3,
        "entries": [{"a": 0, "b": 1, "shared_rows": 60}],
    }
    assert [c["shared_with"] for c in a["courses"]] == [1, 1, 0]
    assert all(c["truth"] == "true" and c["leads"] == 5 for c in a["courses"])
    conflict = a["first_conflict"]
    assert conflict["row"] == "123456" and conflict["shared_rows"] == 60
    assert conflict["a"] == {
        "index": 0, "method": "pb", "method_version": 1,
        "course_head": "123456", "lead": 0, "change_in_lead": 0, "change": 0,
    }
    assert conflict["b"] == {
        "index": 1, "method": "pb", "method_version": 1,
        "course_head": "135264", "lead": 4, "change_in_lead": 12, "change": 48,
    }
    assert a["groups"] == [
        {"id": 1, "size": 2, "courses": [0, 1]},
        {"id": 2, "size": 1, "courses": [2]},
    ]
    assert a["checks"] == {
        "courses": 3, "pairs": 3, "shared_pairs": 1, "distinct_rows": 120,
    }


def test_analyze_course_not_closed_reports_method_and_head():
    with pytest.raises(FalsenessError) as exc:
        analyze_falseness(_pb_ctx(), [ROUNDS6], 2)
    assert exc.value.code == "COURSE_NOT_CLOSED"
    assert "pb" in exc.value.message and "123456" in exc.value.message


# ---------------- API：创建与读取 ----------------

def _create(client, **over):
    payload = {
        "id": "fa",
        "methods": [{"id": "pb-minor"}, {"id": "cambridge-minor"}],
        "mutable_bells": [5, 6],
    }
    payload.update(over)
    return client.post("/falseness-analyses", json=payload)


def test_create_and_get_analysis(methods):
    r = _create(methods)
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == "fa" and body["version"] == 1
    assert body["stage"] == 6
    assert body["reference_course_head"] == "123456"
    assert body["mutable_bells"] == [5, 6]
    assert body["fixed_bells"] == [1, 2, 3, 4]
    assert body["course_heads"] == ["123456", "123465"]
    assert body["total_course_heads"] == 2
    assert body["truncated"] is False and body["truncation_reason"] is None
    assert [m["id"] for m in body["methods"]] == ["pb-minor", "cambridge-minor"]
    assert all(m["version"] == 1 for m in body["methods"])  # 版本冻结

    # 各 course 闭合长度与内部真值
    courses = body["courses"]
    assert len(courses) == 4
    assert [(c["method"], c["course_head"]) for c in courses] == [
        ("pb-minor", "123456"), ("pb-minor", "123465"),
        ("cambridge-minor", "123456"), ("cambridge-minor", "123465"),
    ]
    assert [c["leads"] for c in courses] == [5, 5, 5, 5]
    assert [c["changes"] for c in courses] == [60, 60, 120, 120]
    assert all(c["truth"] == "true" for c in courses)
    assert [c["shared_with"] for c in courses] == [2, 2, 3, 3]

    # 相假矩阵：method×course head 组合间的共享 row 数
    assert body["matrix"]["size"] == 4
    assert body["matrix"]["entries"] == [
        {"a": 0, "b": 2, "shared_rows": 20},
        {"a": 0, "b": 3, "shared_rows": 8},
        {"a": 1, "b": 2, "shared_rows": 8},
        {"a": 1, "b": 3, "shared_rows": 20},
        {"a": 2, "b": 3, "shared_rows": 24},
    ]
    # 首次冲突追溯双方的 method、course head、lead 与 change
    conflict = body["first_conflict"]
    assert conflict["row"] == "123456" and conflict["shared_rows"] == 20
    assert conflict["a"]["method"] == "pb-minor"
    assert conflict["a"]["course_head"] == "123456"
    assert (conflict["a"]["lead"], conflict["a"]["change"]) == (0, 0)
    assert conflict["b"]["method"] == "cambridge-minor"
    assert conflict["b"]["course_head"] == "123456"
    assert (conflict["b"]["lead"], conflict["b"]["change"]) == (0, 0)
    # 连通分组：全部组合经共享 row 关系连成一组
    assert body["groups"] == [{"id": 1, "size": 4, "courses": [0, 1, 2, 3]}]
    # 截断状态、检查数量与输入哈希
    assert body["checks"] == {
        "courses": 4, "pairs": 6, "shared_pairs": 5, "distinct_rows": 288,
    }
    assert body["input_hash"]
    assert body["dependencies"]["ringproof_version"]

    # 重复查询保持一致
    g1 = methods.get("/falseness-analyses/fa/versions/1")
    g2 = methods.get("/falseness-analyses/fa/versions/1")
    assert g1.status_code == g2.status_code == 200
    assert g1.json() == g2.json() == body


def test_create_freezes_method_versions(methods):
    r = _create(methods, methods=[{"id": "pb-minor"}])
    assert r.status_code == 201
    assert r.json()["methods"][0]["version"] == 1
    # 方法更新后，分析版本仍冻结在 v1
    methods.post(
        "/methods",
        json={"id": "pb-minor", "name": "PB2", "stage": 6, "notation": PB_MINOR},
    )
    g = methods.get("/falseness-analyses/fa/versions/1")
    assert g.json()["methods"][0]["version"] == 1
    assert g.json()["input_hash"] == r.json()["input_hash"]


def test_create_rejects_duplicate_mutable_bells(methods):
    r = _create(methods, mutable_bells=[5, 5])
    assert r.status_code == 422
    # 未落库
    assert methods.get("/falseness-analyses/fa").status_code == 404


def test_create_rejects_duplicate_methods(methods):
    r = _create(methods, methods=[{"id": "pb-minor"}, {"id": "pb-minor"}])
    assert r.status_code == 422


def test_create_rejects_cross_stage_methods(methods):
    r = _create(methods, methods=[{"id": "pb-minor"}, {"id": "alt-4"}])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "STAGE_MISMATCH"


def test_create_rejects_mutable_bell_out_of_range(methods):
    r = _create(methods, mutable_bells=[6, 7])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "BELL_OUT_OF_RANGE"


def test_create_rejects_bad_course_head(methods):
    r = _create(methods, course_head="112345")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "ROW_NOT_PERMUTATION"
    r = _create(methods, course_head="12345")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "ROW_LENGTH_MISMATCH"


def test_create_rejects_unknown_method(methods):
    r = _create(methods, methods=[{"id": "nope"}])
    assert r.status_code == 404
    r = _create(methods, methods=[{"id": "pb-minor", "version": 9}])
    assert r.status_code == 404


def test_create_rejects_course_not_closed(methods):
    r = _create(methods, max_leads=2)  # PB/Cambridge 的 plain course 需 5 个 lead
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "COURSE_NOT_CLOSED"
    assert "pb-minor" in detail["message"] and "123456" in detail["message"]
    assert methods.get("/falseness-analyses/fa").status_code == 404


def test_create_truncated_course_heads(methods):
    r = _create(methods, mutable_bells=[2, 3, 4], max_course_heads=4)
    assert r.status_code == 201
    body = r.json()
    assert body["truncated"] is True
    assert body["total_course_heads"] == 6
    assert len(body["course_heads"]) == 4
    assert "max_course_heads=4" in body["truncation_reason"]
    assert body["checks"]["courses"] == 8  # 2 方法 × 4 course head


def test_internal_false_course_reported(methods):
    r = _create(
        methods,
        id="fa4",
        methods=[{"id": "false-4"}],
        mutable_bells=[3, 4],
        course_head="1234",
    )
    assert r.status_code == 201
    body = r.json()
    assert body["course_heads"] == ["1234", "1243"]
    for c in body["courses"]:
        assert c["truth"] == "false" and c["leads"] == 2
    assert body["courses"][0]["first_repeat"]["row"] == "2134"
    assert body["courses"][1]["first_repeat"]["row"] == "2143"
    # 两个 course 的 row 集合完全相同：相互共享 4 个 row
    assert body["matrix"]["entries"] == [{"a": 0, "b": 1, "shared_rows": 4}]


# ---------------- 筛选与版本 ----------------

def test_filter_only_true_disjoint(methods):
    # 单方法：两个 course 互不相交且均为真 → 全部保留
    r = _create(methods, id="solo", methods=[{"id": "pb-minor"}])
    assert r.status_code == 201
    g = methods.get("/falseness-analyses/solo/versions/1?only_true_disjoint=true")
    body = g.json()
    assert body["filter"] == "true_disjoint"
    assert body["total_courses"] == 2 and body["returned_courses"] == 2
    assert [c["course_head"] for c in body["courses"]] == ["123456", "123465"]
    # matrix/groups 仍为完整分析
    assert body["matrix"]["size"] == 2 and len(body["groups"]) == 2

    # 双方法：每个组合都与其他组合共享 row → 全部滤除
    r = _create(methods, id="dual")
    assert r.status_code == 201
    g = methods.get("/falseness-analyses/dual/versions/1?only_true_disjoint=true")
    body = g.json()
    assert body["total_courses"] == 4 and body["returned_courses"] == 0
    assert body["courses"] == []

    # 内部为假的 course 被滤除
    r = _create(
        methods, id="false1", methods=[{"id": "false-4"}],
        mutable_bells=[3, 4], course_head="1234",
    )
    assert r.status_code == 201
    g = methods.get("/falseness-analyses/false1/versions/1?only_true_disjoint=true")
    assert g.json()["returned_courses"] == 0


def test_immutable_versions_and_listing(methods):
    r1 = _create(methods)
    assert r1.status_code == 201
    r2 = _create(methods, mutable_bells=[4, 5])
    assert r2.status_code == 201 and r2.json()["version"] == 2
    # v1 保持不变
    g1 = methods.get("/falseness-analyses/fa/versions/1")
    assert g1.json() == r1.json()
    # 列表与版本列表
    lst = methods.get("/falseness-analyses").json()["falseness_analyses"]
    assert [(x["id"], x["version"]) for x in lst] == [("fa", 1), ("fa", 2)]
    versions = methods.get("/falseness-analyses/fa").json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]


def test_get_404s(methods):
    assert methods.get("/falseness-analyses/nope").status_code == 404
    assert methods.get("/falseness-analyses/nope/versions/1").status_code == 404
    r = _create(methods)
    assert r.status_code == 201
    assert methods.get("/falseness-analyses/fa/versions/9").status_code == 404
