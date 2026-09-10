"""多方法拼接编排测试：创建校验、逐 row 来源、拼接统计、配额/转换剪枝、
枚举排序与截断、版本冻结一致性。"""
import pytest
from fastapi.testclient import TestClient

from ringproof.engine import build_lead, prove_rows
from ringproof.feasibility import quota_feasible
from ringproof.main import create_app
from ringproof.notation import expand_notation

PB_MINOR = "x16x16x16x16x16x12"
ALT_MINOR = "x16x14x16x14x16x12"
ROUNDS6 = (1, 2, 3, 4, 5, 6)


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def methods(client):
    for mid, name, notation in [
        ("pb-minor", "Plain Bob Minor", PB_MINOR),
        ("alt-minor", "Alt Bob Minor", ALT_MINOR),
    ]:
        r = client.post(
            "/methods",
            json={"id": mid, "name": name, "stage": 6, "notation": notation},
        )
        assert r.status_code == 201
    return ("pb-minor", "alt-minor")


def _multi_payload(**over):
    payload = {
        "methods": [{"id": "pb-minor"}, {"id": "alt-minor"}],
        "sequence": [
            {
                "leads": [
                    {"method": "pb-minor"},
                    {"method": "pb-minor"},
                    {"method": "alt-minor"},
                    {"method": "pb-minor"},
                ]
            }
        ],
    }
    payload.update(over)
    return payload


def _variant_key(v):
    return (
        v["truth"] != "true",
        not v["rounds_return"],
        v["num_splices"],
        v["balance"],
        v["num_calls"],
        v["total_changes"],
        v["methods"],
        v["calls"],
    )


# ---------------- 创建校验 ----------------

def test_create_multi_method_touch(client, methods):
    r = client.post("/touches", json=_multi_payload())
    assert r.status_code == 201
    touch = r.json()
    assert [m["id"] for m in touch["methods"]] == ["pb-minor", "alt-minor"]
    assert all(m["version"] == 1 for m in touch["methods"])
    assert touch["method"]["id"] == "pb-minor"  # 首选方法
    assert [l["method"] for l in touch["leads"]] == [
        "pb-minor", "pb-minor", "alt-minor", "pb-minor",
    ]


def test_stage_mismatch_rejected(client, methods):
    r8 = client.post(
        "/methods",
        json={"id": "pb-major", "name": "PB Major", "stage": 8, "notation": "x18x18x18x18x18x18x18x12"},
    )
    assert r8.status_code == 201
    r = client.post(
        "/touches",
        json=_multi_payload(methods=[{"id": "pb-minor"}, {"id": "pb-major"}]),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "STAGE_MISMATCH"


def test_missing_method_version_rejected(client, methods):
    r = client.post(
        "/touches",
        json=_multi_payload(methods=[{"id": "pb-minor", "version": 99}, {"id": "alt-minor"}]),
    )
    assert r.status_code == 404
    assert "v99" in r.json()["detail"]
    r2 = client.post(
        "/touches",
        json=_multi_payload(methods=[{"id": "ghost"}, {"id": "alt-minor"}]),
    )
    assert r2.status_code == 404


def test_unknown_method_in_lead_rejected(client, methods):
    r = client.post(
        "/touches",
        json=_multi_payload(sequence=[{"leads": [{"method": "ghost"}]}]),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UNKNOWN_METHOD"
    r2 = client.post(
        "/touches",
        json=_multi_payload(sequence=[{"leads": [{"method_choice": ["pb-minor", "ghost"]}]}]),
    )
    assert r2.status_code == 422
    assert r2.json()["detail"]["code"] == "UNKNOWN_METHOD"


def test_unknown_transition_rejected(client, methods):
    r = client.post(
        "/touches",
        json=_multi_payload(allowed_transitions=[["pb-minor", "ghost"]]),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UNKNOWN_TRANSITION"
    r2 = client.post(
        "/touches",
        json=_multi_payload(forbidden_transitions=[["ghost", "alt-minor"]]),
    )
    assert r2.status_code == 422
    assert r2.json()["detail"]["code"] == "UNKNOWN_TRANSITION"


def test_unsatisfiable_quotas_rejected(client, methods):
    # 1) min 总和超过 lead 数
    r = client.post(
        "/touches",
        json=_multi_payload(method_quotas={"pb-minor": {"min": 5}}),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UNSATISFIABLE_QUOTA"
    # 2) 固定 lead 数超过 max（4 个 lead 中 3 个固定 pb-minor）
    r2 = client.post(
        "/touches",
        json=_multi_payload(method_quotas={"pb-minor": {"max": 2}}),
    )
    assert r2.status_code == 422
    assert r2.json()["detail"]["code"] == "UNSATISFIABLE_QUOTA"
    # 3) min 超过可用槽位（没有 lead 能用 alt-minor）
    r3 = client.post(
        "/touches",
        json=_multi_payload(
            sequence=[{"leads": [{"method": "pb-minor"}, {"method": "pb-minor"}]}],
            method_quotas={"alt-minor": {"min": 1}},
        ),
    )
    assert r3.status_code == 422
    assert r3.json()["detail"]["code"] == "UNSATISFIABLE_QUOTA"


def test_fixed_transition_violation_rejected(client, methods):
    r = client.post(
        "/touches",
        json=_multi_payload(forbidden_transitions=[["pb-minor", "alt-minor"]]),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "TRANSITION_VIOLATION"
    # 白名单不含 pb→alt 同样拒绝
    r2 = client.post(
        "/touches",
        json=_multi_payload(allowed_transitions=[["alt-minor", "pb-minor"]]),
    )
    assert r2.status_code == 422
    assert r2.json()["detail"]["code"] == "TRANSITION_VIOLATION"


def test_touch_create_schema_guards(client, methods):
    # methods 与 method_id 互斥
    r = client.post(
        "/touches",
        json=_multi_payload(method_id="pb-minor"),
    )
    assert r.status_code == 422
    # 重复方法 id
    r2 = client.post(
        "/touches",
        json=_multi_payload(methods=[{"id": "pb-minor"}, {"id": "pb-minor"}]),
    )
    assert r2.status_code == 422
    # 配额 min > max
    r3 = client.post(
        "/touches",
        json=_multi_payload(method_quotas={"pb-minor": {"min": 3, "max": 1}}),
    )
    assert r3.status_code == 422
    # 配额引用未声明方法
    r4 = client.post(
        "/touches",
        json=_multi_payload(method_quotas={"ghost": {"min": 1}}),
    )
    assert r4.status_code == 422
    assert r4.json()["detail"]["code"] == "UNKNOWN_METHOD"


# ---------------- 证明：逐 row 来源与拼接统计 ----------------

def test_prove_splice_provenance_and_stats(client, methods):
    r = client.post("/touches", json=_multi_payload())
    tid = r.json()["id"]
    proof = client.post(f"/touches/{tid}/versions/1/prove").json()

    assert proof["total_changes"] == 48
    assert proof["lead_methods"] == ["pb-minor", "pb-minor", "alt-minor", "pb-minor"]
    assert proof["splices"] == [
        {"lead": 3, "from_method": "pb-minor", "to_method": "alt-minor"},
        {"lead": 4, "from_method": "alt-minor", "to_method": "pb-minor"},
    ]
    assert proof["splice_count"] == 2
    # 连续段：pb(1-2) → alt(3) → pb(4)
    assert [(r_["method_id"], r_["start_lead"], r_["end_lead"]) for r_ in proof["runs"]] == [
        ("pb-minor", 1, 2),
        ("alt-minor", 3, 3),
        ("pb-minor", 4, 4),
    ]
    assert proof["runs"][0]["changes"] == 24
    # 各方法用量
    assert proof["method_usage"]["pb-minor"]["leads"] == 3
    assert proof["method_usage"]["pb-minor"]["changes"] == 36
    assert proof["method_usage"]["alt-minor"]["leads"] == 1
    assert proof["method_usage"]["alt-minor"]["version"] == 1
    # 逐 row 来源：方法 id/版本 + 拼接标记
    ev = proof["events"]
    assert ev[0]["method_id"] == "pb-minor" and ev[0]["splice"] is False
    assert ev[24]["method_id"] == "alt-minor" and ev[24]["method_version"] == 1
    assert ev[24]["splice"] is True and ev[35]["splice"] is True
    assert ev[36]["method_id"] == "pb-minor" and ev[36]["splice"] is True
    # 冻结的方法版本清单
    assert [m["id"] for m in proof["methods"]] == ["pb-minor", "alt-minor"]
    assert proof["quotas_satisfied"] is True
    assert proof["transitions_satisfied"] is True


def test_prove_reports_quota_status(client, methods):
    r = client.post(
        "/touches",
        json=_multi_payload(
            method_quotas={"pb-minor": {"min": 2, "max": 3}, "alt-minor": {"max": 2}}
        ),
    )
    assert r.status_code == 201
    tid = r.json()["id"]
    proof = client.post(f"/touches/{tid}/versions/1/prove").json()
    qs = proof["quota_status"]
    assert qs["pb-minor"] == {"leads": 3, "min": 2, "max": 3, "satisfied": True}
    assert qs["alt-minor"] == {"leads": 1, "min": None, "max": 2, "satisfied": True}
    assert proof["quotas_satisfied"] is True


def test_rows_endpoint_marks_method_and_splice(client, methods):
    r = client.post("/touches", json=_multi_payload())
    tid = r.json()["id"]
    rows = client.get(f"/touches/{tid}/versions/1/rows", params={"limit": 100}).json()
    assert rows["total_changes"] == 48
    assert len(rows["splices"]) == 2
    assert [m["id"] for m in rows["methods"]] == ["pb-minor", "alt-minor"]
    by_lead = {}
    for row in rows["rows"]:
        by_lead.setdefault(row["lead"], row)
    assert by_lead[1]["method_id"] == "pb-minor" and by_lead[1]["splice"] is False
    assert by_lead[3]["method_id"] == "alt-minor" and by_lead[3]["splice"] is True
    assert by_lead[4]["method_id"] == "pb-minor" and by_lead[4]["splice"] is True


def test_method_choice_blocks_prove(client, methods):
    r = client.post(
        "/touches",
        json=_multi_payload(sequence=[{"leads": [{"method_choice": ["pb-minor", "alt-minor"]}]}]),
    )
    assert r.status_code == 201
    tid = r.json()["id"]
    rp = client.post(f"/touches/{tid}/versions/1/prove")
    assert rp.status_code == 422
    assert rp.json()["detail"]["code"] == "UNRESOLVED_CHOICE"


# ---------------- 枚举：组合、剪枝、排序、截断 ----------------

def test_enumerate_method_and_call_combos(client, methods):
    seq = [
        {
            "leads": [
                {"method_choice": ["pb-minor", "alt-minor"], "choice": ["plain", "bob"]},
                {"method": "pb-minor", "call": None},
            ]
        }
    ]
    r = client.post(
        "/touches",
        json=_multi_payload(
            sequence=seq, calls={"bob": {"notation": "14", "replace": 1}}
        ),
    )
    assert r.status_code == 201
    tid = r.json()["id"]
    e = client.post(f"/touches/{tid}/versions/1/enumerate", json={})
    assert e.status_code == 200
    body = e.json()
    assert body["total_combos"] == 4  # 2 方法 × 2 call
    assert body["checked"] == 4 and body["truncated"] is False
    assert body["total_variants"] == 4
    v0 = body["variants"][0]
    assert "method_assignment" in v0 and "assignment" in v0
    combos = {(tuple(v["methods"]), tuple(v["calls"])) for v in body["variants"]}
    assert len(combos) == 4  # 四种组合都出现


def test_enumerate_prunes_by_quota(client, methods):
    seq = [{"leads": [{"method_choice": ["pb-minor", "alt-minor"]}], "repeat": 4}]
    r = client.post(
        "/touches",
        json=_multi_payload(
            sequence=seq, method_quotas={"pb-minor": {"min": 2, "max": 2}}
        ),
    )
    assert r.status_code == 201
    tid = r.json()["id"]
    e = client.post(f"/touches/{tid}/versions/1/enumerate", json={}).json()
    assert e["total_combos"] == 16
    assert e["total_variants"] == 6  # C(4,2)
    assert e["pruned_by_quota"] == 10
    assert all(v["method_counts"]["pb-minor"] == 2 for v in e["variants"])
    keys = [_variant_key(v) for v in e["variants"]]
    assert keys == sorted(keys)


def test_enumerate_prunes_by_transition(client, methods):
    seq = [{"leads": [{"method_choice": ["pb-minor", "alt-minor"]}], "repeat": 2}]
    # 黑名单：禁止 pb→alt
    r = client.post(
        "/touches",
        json=_multi_payload(sequence=seq, forbidden_transitions=[["pb-minor", "alt-minor"]]),
    )
    tid = r.json()["id"]
    e = client.post(f"/touches/{tid}/versions/1/enumerate", json={}).json()
    assert e["total_combos"] == 4
    assert e["pruned_by_transition"] == 1
    assert e["total_variants"] == 3
    assert [tuple(v["methods"]) for v in e["variants"] if v["num_splices"] > 0] == [
        ("alt-minor", "pb-minor")
    ]
    # 白名单：只允许 pb→alt（同方法延续始终允许）
    r2 = client.post(
        "/touches",
        json=_multi_payload(sequence=seq, allowed_transitions=[["pb-minor", "alt-minor"]]),
    )
    tid2 = r2.json()["id"]
    e2 = client.post(f"/touches/{tid2}/versions/1/enumerate", json={}).json()
    assert e2["pruned_by_transition"] == 1
    assert e2["total_variants"] == 3
    spliced = [v for v in e2["variants"] if v["num_splices"] > 0]
    assert [tuple(v["methods"]) for v in spliced] == [("pb-minor", "alt-minor")]


def test_enumerate_sort_prefers_true_rounds_fewer_splices(client, methods):
    # 5 个 lead 全部候选两种方法：全 pb-minor 的 plain course 为真且回到 rounds
    seq = [{"leads": [{"method_choice": ["pb-minor", "alt-minor"]}], "repeat": 5}]
    r = client.post("/touches", json=_multi_payload(sequence=seq))
    tid = r.json()["id"]
    e = client.post(f"/touches/{tid}/versions/1/enumerate", json={}).json()
    assert e["total_combos"] == 32
    assert e["sorted_by"] == [
        "truth", "rounds_return", "num_splices", "balance", "num_calls", "total_changes",
    ]
    keys = [_variant_key(v) for v in e["variants"]]
    assert keys == sorted(keys)
    best = e["variants"][0]
    assert best["truth"] == "true" and best["rounds_return"] is True
    assert best["num_splices"] == 0
    # 全 pb-minor 的 plain course 确实在结果中且为真、回到 rounds
    plain = [v for v in e["variants"] if v["methods"] == ["pb-minor"] * 5]
    assert len(plain) == 1
    assert plain[0]["truth"] == "true" and plain[0]["rounds_return"] is True
    assert plain[0]["total_changes"] == 60


def test_enumerate_truncation_reports_checked_and_reason(client, methods):
    seq = [{"leads": [{"choice": ["plain", "bob"]}], "repeat": 10}]
    r = client.post(
        "/touches",
        json=_multi_payload(sequence=seq, calls={"bob": {"notation": "14", "replace": 1}}),
    )
    tid = r.json()["id"]
    e = client.post(
        f"/touches/{tid}/versions/1/enumerate",
        json={"max_variants": 4096, "max_search": 50},
    ).json()
    assert e["total_combos"] == 1024
    assert e["truncated"] is True
    assert e["checked"] == 50
    assert "max_search" in e["truncation_reason"]
    assert e["total_variants"] <= 50
    # 不截断时 checked 等于组合数
    e2 = client.post(
        f"/touches/{tid}/versions/1/enumerate", json={"max_variants": 4096}
    ).json()
    assert e2["truncated"] is False
    assert e2["checked"] == 1024
    assert e2["truncation_reason"] is None


def test_enumerate_deterministic_replay(client, methods):
    seq = [{"leads": [{"method_choice": ["pb-minor", "alt-minor"]}], "repeat": 4}]
    r = client.post("/touches", json=_multi_payload(sequence=seq))
    tid = r.json()["id"]
    e1 = client.post(f"/touches/{tid}/versions/1/enumerate", json={}).json()
    e2 = client.post(f"/touches/{tid}/versions/1/enumerate", json={}).json()
    assert e1 == e2


# ---------------- 版本冻结与一致性 ----------------

def test_touch_freezes_method_versions(client, methods):
    r = client.post("/touches", json=_multi_payload())
    tid = r.json()["id"]
    # 方法之后产生新版本，不影响已冻结的 touch
    client.post(
        "/methods",
        json={"id": "pb-minor", "name": "Plain Bob Minor v2", "stage": 6, "notation": PB_MINOR},
    )
    p1 = client.post(f"/touches/{tid}/versions/1/prove")
    assert p1.headers["X-Proof-Cache"] == "miss"
    proof = p1.json()
    assert proof["methods"][0]["version"] == 1
    assert proof["methods"][0]["name"] == "Plain Bob Minor"
    # 重复证明：缓存命中且结果一致
    p2 = client.post(f"/touches/{tid}/versions/1/prove")
    assert p2.headers["X-Proof-Cache"] == "hit"
    assert p2.json() == proof
    # touch 详情仍指向冻结版本
    detail = client.get(f"/touches/{tid}/versions/1").json()
    assert [m["version"] for m in detail["methods"]] == [1, 1]


# ---------------- 引擎与可行性 ----------------

def test_engine_splice_stats_and_quota_status():
    toks_a, ch_a = expand_notation(PB_MINOR, 6)
    toks_b, ch_b = expand_notation(ALT_MINOR, 6)
    leads = [
        build_lead(toks_a, ch_a, None, {}, method_id="a", method_version=1, method_name="A"),
        build_lead(toks_b, ch_b, None, {}, method_id="b", method_version=2, method_name="B"),
        build_lead(toks_b, ch_b, None, {}, method_id="b", method_version=2, method_name="B"),
    ]
    r = prove_rows(
        6,
        "A",
        leads,
        ROUNDS6,
        method_quotas={"a": {"min": 1, "max": 2}, "b": {"min": None, "max": 3}},
        forbidden_transitions={("b", "a")},
    )
    assert r["splice_count"] == 1
    assert r["splices"] == [{"lead": 2, "from_method": "a", "to_method": "b"}]
    assert [(run["method_id"], run["leads"]) for run in r["runs"]] == [("a", 1), ("b", 2)]
    assert r["method_usage"]["a"]["leads"] == 1
    assert r["method_usage"]["b"]["changes"] == 24
    assert r["quota_status"]["a"] == {"leads": 1, "min": 1, "max": 2, "satisfied": True}
    assert r["quotas_satisfied"] is True
    assert r["transitions_satisfied"] is True  # a→b 不在禁止列表
    assert r["events"][0]["method_id"] == "a" and r["events"][0]["splice"] is False
    assert r["events"][12]["method_id"] == "b" and r["events"][12]["splice"] is True


def test_engine_transition_violation_and_quota_breach_reported():
    toks_a, ch_a = expand_notation(PB_MINOR, 6)
    toks_b, ch_b = expand_notation(ALT_MINOR, 6)
    leads = [
        build_lead(toks_a, ch_a, None, {}, method_id="a", method_version=1, method_name="A"),
        build_lead(toks_b, ch_b, None, {}, method_id="b", method_version=1, method_name="B"),
    ]
    r = prove_rows(
        6,
        "A",
        leads,
        ROUNDS6,
        method_quotas={"a": {"min": 2, "max": None}},
        forbidden_transitions={("a", "b")},
    )
    assert r["quotas_satisfied"] is False
    assert r["quota_status"]["a"]["satisfied"] is False
    assert r["transitions_satisfied"] is False
    assert r["transition_violations"] == [
        {"lead": 2, "from_method": "a", "to_method": "b", "rule": "forbidden"}
    ]


def test_quota_feasible_exact_check():
    assert quota_feasible(2, [{"a"}, {"a"}], {"a": {"min": 2, "max": 2}}, ["a"])
    assert not quota_feasible(2, [{"a"}, {"a"}], {"a": {"min": 3, "max": None}}, ["a"])
    assert not quota_feasible(2, [{"a"}, {"a"}], {"a": {"min": None, "max": 1}}, ["a"])
    assert quota_feasible(4, [{"a", "b"}] * 4, {"a": {"min": 2, "max": 2}}, ["a", "b"])
    assert not quota_feasible(
        4,
        [{"a", "b"}] * 4,
        {"a": {"min": 3, "max": 3}, "b": {"min": 3, "max": 3}},
        ["a", "b"],
    )
    assert not quota_feasible(2, [{"b"}, {"b"}], {"a": {"min": 1, "max": None}}, ["a", "b"])
    # 候选集制约：b 只能出现在第 1 个 lead，b min=2 不可行
    assert not quota_feasible(
        3, [{"a", "b"}, {"a"}, {"a"}], {"b": {"min": 2, "max": None}}, ["a", "b"]
    )
    assert quota_feasible(
        3, [{"a", "b"}, {"a"}, {"a"}], {"b": {"min": 1, "max": 1}}, ["a", "b"]
    )
