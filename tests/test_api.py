"""API 端到端测试：版本不可变、证明一致性、枚举排序、错误码。"""
import pytest
from fastapi.testclient import TestClient

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


def _touch_payload(**over):
    payload = {
        "method_id": "pb-minor",
        "calls": {
            "bob": {"notation": "14", "replace": 1},
            "single": {"notation": "1234", "replace": 1},
        },
        "sequence": [{"leads": [{"call": None}], "repeat": 5}],
    }
    payload.update(over)
    return payload


# ---------------- 方法 ----------------

def test_create_method_and_immutable_versions(client):
    r1 = client.post(
        "/methods",
        json={"id": "pb-minor", "name": "Plain Bob Minor", "stage": 6, "notation": PLAIN_BOB_MINOR},
    )
    assert r1.status_code == 201
    v1 = r1.json()
    assert v1["version"] == 1
    assert v1["lead_length"] == 12
    assert v1["notation_normalized"] == "x.16.x.16.x.16.x.16.x.16.x.12"
    assert len(v1["changes"]) == 12 and v1["changes"][1]["places"] == [1, 6]

    # 同一 id 再提交 → 新版本；内容相同则输入哈希相同；v1 保持不变
    r2 = client.post(
        "/methods",
        json={"id": "pb-minor", "name": "Plain Bob Minor", "stage": 6, "notation": PLAIN_BOB_MINOR},
    )
    v2 = r2.json()
    assert v2["version"] == 2
    assert v2["input_hash"] == v1["input_hash"]
    again = client.get("/methods/pb-minor/versions/1").json()
    assert again == v1

    versions = client.get("/methods/pb-minor").json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]


def test_method_notation_errors(client):
    cases = [
        ({"name": "bad", "stage": 6, "notation": "x17x16"}, "BELL_OUT_OF_RANGE"),
        ({"name": "bad", "stage": 6, "notation": "x113x16"}, "DUPLICATE_PLACE"),
        ({"name": "bad", "stage": 4, "notation": "13"}, "NOT_ADJACENT"),
        ({"name": "bad", "stage": 3, "notation": "x1"}, "STAGE_OUT_OF_RANGE"),
    ]
    for body, code in cases:
        r = client.post("/methods", json=body)
        assert r.status_code == 422, body
        assert r.json()["detail"]["code"] == code


def test_symmetric_and_repeat_notation_accepted(client):
    r = client.post(
        "/methods",
        json={"name": "Cambridge S Minor", "stage": 6, "notation": "&x3x4x2x3x4x5,+2"},
    )
    assert r.status_code == 201
    assert r.json()["lead_length"] == 24
    r2 = client.post(
        "/methods",
        json={"name": "PB via repeat", "stage": 6, "notation": "(x16)x5 x12"},
    )
    assert r2.status_code == 201
    assert r2.json()["notation_normalized"] == "x.16.x.16.x.16.x.16.x.16.x.12"


# ---------------- touch 与证明 ----------------

def test_touch_prove_plain_course(client, method_id):
    r = client.post("/touches", json=_touch_payload())
    assert r.status_code == 201
    touch = r.json()
    assert touch["version"] == 1
    assert touch["total_leads"] == 5
    assert touch["method"]["name"] == "Plain Bob Minor"
    assert touch["calls"]["bob"]["normalized"] == "14"

    tid = touch["id"]
    p1 = client.post(f"/touches/{tid}/versions/1/prove")
    assert p1.status_code == 200
    assert p1.headers["X-Proof-Cache"] == "miss"
    proof = p1.json()
    assert proof["total_changes"] == 60
    assert proof["permutation_complete"] and proof["notation_consistent"]
    assert proof["rounds_return"] is True
    assert proof["rounds_at"] == [0, 60]
    assert proof["truth"] == "true"
    assert proof["first_repeat"] is None
    assert proof["lead_heads"][0] == "135264"

    # 同一版本重复证明：结果一致，且命中缓存
    p2 = client.post(f"/touches/{tid}/versions/1/prove")
    assert p2.headers["X-Proof-Cache"] == "hit"
    assert p2.json() == proof


def test_prove_locates_first_repeat(client, method_id):
    r = client.post("/touches", json=_touch_payload(sequence=[{"leads": [{"call": None}], "repeat": 6}]))
    tid = r.json()["id"]
    proof = client.post(f"/touches/{tid}/versions/1/prove").json()
    assert proof["truth"] == "false"
    rep = proof["first_repeat"]
    # 60 号 change 回到 rounds，与起始 row（0 号）构成首次重复
    assert rep["changes"] == [0, 60]
    assert rep["row"] == "123456"
    assert rep["first"] == {"type": "start", "change": 0}
    assert rep["second"]["method"] == "Plain Bob Minor"
    assert rep["second"]["lead"] == 5
    assert rep["second"]["source"] == "method"


def test_touch_with_bob_call_provenance(client, method_id):
    seq = [{"leads": [{"call": None}, {"call": None}, {"call": "bob"}]}]
    r = client.post("/touches", json=_touch_payload(sequence=seq))
    tid = r.json()["id"]
    proof = client.post(f"/touches/{tid}/versions/1/prove").json()
    ev = proof["events"][35]  # 第 3 个 lead 的最后一个 change
    assert ev["lead"] == 3 and ev["change_in_lead"] == 12
    assert ev["notation"] == "14" and ev["source"] == "call:bob"
    assert proof["calls_used"] == 1


def test_rows_endpoint_per_row_provenance(client, method_id):
    r = client.post("/touches", json=_touch_payload())
    tid = r.json()["id"]
    rows = client.get(f"/touches/{tid}/versions/1/rows", params={"limit": 1000}).json()
    assert rows["total_changes"] == 60
    assert len(rows["rows"]) == 60
    first = rows["rows"][0]
    assert first == {
        "change": 1, "lead": 1, "change_in_lead": 1, "notation": "x",
        "places": [], "source": "method", "call": None, "row": "214365",
        "method": "Plain Bob Minor", "method_id": "pb-minor",
        "method_version": 1, "splice": False,
    }
    page = client.get(f"/touches/{tid}/versions/1/rows", params={"offset": 58, "limit": 5}).json()
    assert len(page["rows"]) == 2 and page["rows"][-1]["row"] == "123456"


def test_touch_version_immutable_and_hash_stable(client, method_id):
    r1 = client.post("/touches", json=_touch_payload(id="my-touch"))
    r2 = client.post("/touches", json=_touch_payload(id="my-touch"))
    t1, t2 = r1.json(), r2.json()
    assert (t1["version"], t2["version"]) == (1, 2)
    assert t1["input_hash"] == t2["input_hash"]  # 内容寻址
    assert client.get("/touches/my-touch/versions/1").json() == t1


def test_max_calls_limit_reported(client, method_id):
    seq = [{"leads": [{"call": "bob"}], "repeat": 3}]
    r = client.post("/touches", json=_touch_payload(sequence=seq, max_calls=2))
    tid = r.json()["id"]
    proof = client.post(f"/touches/{tid}/versions/1/prove").json()
    assert proof["calls_used"] == 3
    assert proof["exceeds_max_calls"] is True


# ---------------- 枚举 ----------------

def test_enumerate_variants_sorted(client, method_id):
    seq = [{"leads": [{"choice": ["plain", "bob"]}], "repeat": 4}]
    r = client.post("/touches", json=_touch_payload(sequence=seq))
    tid = r.json()["id"]

    # choice 未决时 prove 拒绝
    rp = client.post(f"/touches/{tid}/versions/1/prove")
    assert rp.status_code == 422
    assert rp.json()["detail"]["code"] == "UNRESOLVED_CHOICE"

    e = client.post(f"/touches/{tid}/versions/1/enumerate", json={}).json()
    assert e["total_variants"] == 16
    keys = [(v["num_calls"], v["total_changes"], not v["rounds_return"]) for v in e["variants"]]
    assert keys == sorted(keys)  # 按改动数、总 change 数、rounds 回归排序
    assert e["variants"][0]["num_calls"] == 0
    assert e["variants"][0]["calls"] == ["plain"] * 4

    # 限制替换次数：≤1 个 bob → C(4,0)+C(4,1) = 5 个变体
    e2 = client.post(f"/touches/{tid}/versions/1/enumerate", json={"max_calls": 1}).json()
    assert e2["total_variants"] == 5
    assert e2["filtered_by_max_calls"] == 11
    assert all(v["num_calls"] <= 1 for v in e2["variants"])

    # 枚举结果确定：重放一致
    e3 = client.post(f"/touches/{tid}/versions/1/enumerate", json={}).json()
    assert e3 == e


def test_enumerate_variant_cap(client, method_id):
    seq = [{"leads": [{"choice": ["plain", "bob", "single"]}], "repeat": 6}]
    r = client.post("/touches", json=_touch_payload(sequence=seq))
    tid = r.json()["id"]
    resp = client.post(f"/touches/{tid}/versions/1/enumerate", json={"max_variants": 100})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "TOO_MANY_VARIANTS"


# ---------------- 其他错误 ----------------

def test_unknown_call_and_bad_start_row(client, method_id):
    r = client.post("/touches", json=_touch_payload(sequence=[{"leads": [{"call": "nope"}]}]))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "UNKNOWN_CALL"

    r2 = client.post("/touches", json=_touch_payload(start_row="112345"))
    assert r2.status_code == 422
    assert r2.json()["detail"]["code"] == "ROW_NOT_PERMUTATION"


def test_not_found(client):
    assert client.get("/methods/ghost/versions/1").status_code == 404
    assert client.get("/touches/ghost/versions/1").status_code == 404
    assert client.post("/touches/ghost/versions/1/prove").status_code == 404
    r = client.post("/touches", json=_touch_payload(method_id="ghost"))
    assert r.status_code == 404


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"
