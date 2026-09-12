"""呼叫位置 API 端到端测试：位置方案、composition、编译、版本冻结与确定性。"""
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


@pytest.fixture()
def scheme(client, method_id):
    r = client.post(
        "/position-schemes",
        json={
            "id": "tenor-6",
            "name": "tenor positions",
            "method_id": "pb-minor",
            "observer": 6,
            "positions": {"Home": 6, "Wrong": 4, "Middle": 2},
            "home_symbol": "Home",
        },
    )
    assert r.status_code == 201
    return r.json()


def _composition(cid="comp", tokens=None, **over):
    payload = {
        "id": cid,
        "scheme": {"id": "tenor-6"},
        "calls": {
            "bob": {"notation": "14", "replace": 1},
            "single": {"notation": "1234", "replace": 1},
        },
        "parts": [{"tokens": tokens or [{"symbol": "Wrong", "call": "bob", "plain_leads": 1}]}],
    }
    payload.update(over)
    return payload


# ---------------- 位置方案 ----------------

def test_position_scheme_created_immutable(client, scheme):
    assert scheme["version"] == 1
    assert scheme["observer"] == 6
    assert scheme["positions"] == {"Home": 6, "Wrong": 4, "Middle": 2}
    assert scheme["method"] == {"id": "pb-minor", "version": 1}

    r2 = client.post(
        "/position-schemes",
        json={"id": "tenor-6", "name": "tenor positions", "method_id": "pb-minor",
              "observer": 6, "positions": {"Home": 6, "Wrong": 4, "Middle": 2}},
    )
    assert r2.json()["version"] == 2
    assert r2.json()["input_hash"] == scheme["input_hash"]
    again = client.get("/position-schemes/tenor-6/versions/1").json()
    assert again == scheme


def test_position_scheme_errors(client, method_id):
    # 观察钟越界
    r = client.post("/position-schemes", json={
        "name": "bad", "method_id": "pb-minor", "observer": 7,
        "positions": {"Home": 6},
    })
    assert r.status_code == 422 and r.json()["detail"]["code"] == "BELL_OUT_OF_RANGE"
    # 位置越界
    r = client.post("/position-schemes", json={
        "name": "bad", "method_id": "pb-minor", "observer": 6,
        "positions": {"Home": 7},
    })
    assert r.status_code == 422 and r.json()["detail"]["code"] == "POSITION_OUT_OF_RANGE"
    # home 符号未映射
    r = client.post("/position-schemes", json={
        "name": "bad", "method_id": "pb-minor", "observer": 6,
        "positions": {"Wrong": 4},
    })
    assert r.status_code == 422 and r.json()["detail"]["code"] == "HOME_SYMBOL_MISSING"
    # 方法不存在
    r = client.post("/position-schemes", json={
        "name": "bad", "method_id": "ghost", "observer": 6, "positions": {"Home": 6},
    })
    assert r.status_code == 404
    # call 专属映射位置越界
    r = client.post("/position-schemes", json={
        "name": "bad", "method_id": "pb-minor", "observer": 6,
        "positions": {"Home": 6},
        "call_positions": [{"call": "bob", "positions": {"X": 9}}],
    })
    assert r.status_code == 422 and r.json()["detail"]["code"] == "POSITION_OUT_OF_RANGE"
    # 非法 call 名
    r = client.post("/position-schemes", json={
        "name": "bad", "method_id": "pb-minor", "observer": 6,
        "positions": {"Home": 6},
        "call_positions": [{"call": "plain", "positions": {"X": 2}}],
    })
    assert r.status_code == 422 and r.json()["detail"]["code"] == "INVALID_CALL_NAME"


def test_position_scheme_call_specific_mapping(client, method_id):
    r = client.post("/position-schemes", json={
        "id": "mixed", "name": "mixed", "method_id": "pb-minor", "observer": 6,
        "positions": {"Home": 6, "Wrong": 4},
        "call_positions": [{"call": "single", "positions": {"Wrong": 4}}],
    })
    assert r.status_code == 201
    got = client.get("/position-schemes/mixed/versions/1").json()
    assert got["call_positions"] == {"single": {"Wrong": 4}}


# ---------------- composition ----------------

def test_composition_created_and_frozen(client, scheme):
    r = client.post("/compositions", json=_composition())
    assert r.status_code == 201
    body = r.json()
    assert body["version"] == 1
    assert body["plain_course_length"] == 5
    assert body["scheme"]["version"] == 1
    assert body["calls"]["bob"]["normalized"] == "14"
    # token 规范化
    tok = body["parts"][0]["tokens"][0]
    assert tok["symbol"] == "Wrong" and tok["plain_leads"] == 1
    # 重复提交同内容 → 新版本同哈希
    r2 = client.post("/compositions", json=_composition())
    assert r2.json()["version"] == 2 and r2.json()["input_hash"] == body["input_hash"]
    assert client.get("/compositions/comp/versions/1").json() == body


def test_composition_unknown_call_and_symbol(client, scheme):
    r = client.post("/compositions", json=_composition(tokens=[{"symbol": "Wrong", "call": "ghost"}]))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "UNKNOWN_CALL"
    r = client.post("/compositions", json=_composition(tokens=[{"symbol": "Ghost", "call": "bob"}]))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "UNKNOWN_SYMBOL"


def test_composition_validates_bad_call_notation(client, scheme):
    r = client.post("/compositions", json=_composition(
        calls={"bob": {"notation": "17", "replace": 1}},
        tokens=[{"symbol": "Wrong", "call": "bob"}],
    ))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "BELL_OUT_OF_RANGE"


def test_composition_references_missing_scheme(client):
    # 空库（仅有方法、无位置方案）创建 composition → 404
    import tempfile
    from ringproof.main import create_app as ca
    with TestClient(ca(tempfile.mkdtemp() + "/empty.db")) as empty:
        empty.post("/methods", json={"id": "pb-minor", "name": "PB", "stage": 6,
                                     "notation": PLAIN_BOB_MINOR})
        rr = empty.post("/compositions", json=_composition())
        assert rr.status_code == 404


# ---------------- 编译 ----------------

def test_compile_resolves_wrong_and_saves_touch(client, scheme):
    client.post("/compositions", json=_composition())
    r = client.post("/compositions/comp/versions/1/compile", json={})
    assert r.status_code == 200
    j = r.json()
    assert r.headers["X-Compile-Cache"] == "miss"
    # bob 在第 2 个 lead（1 个 plain 之后），bob 后观察钟在第 4 位
    assert j["compiled_tokens"] == [{
        "part": 1, "token": 1, "symbol": "Wrong", "call": "bob",
        "plain_leads": 1, "lead_index_in_part": 2, "position": 4,
    }]
    bob_lead = next(l for l in j["compiled_leads"] if l["call"] == "bob")
    assert bob_lead["lead"] == 2 and bob_lead["position"] == 4
    assert bob_lead["lead_head_before"] == "135264"
    # part 收尾补 1 个 plain lead 至观察钟回 home（共 3 个 lead）
    assert j["course_lengths"] == [3]
    assert j["course_heads"][1] == "154326"
    assert j["final_row"] == "154326"
    # 另存 touch 版本且可独立证明
    t = j["touch"]
    assert t["id"] == "compiled:comp" and t["version"] == 1
    p = client.post(f"/touches/{t['id']}/versions/{t['version']}/prove").json()
    assert p["total_changes"] == 36 and p["truth"] == "true" and p["calls_used"] == 1
    # 逐 lead 标注与 touch lead_heads 一致
    assert [l["lead_head_after"] for l in j["compiled_leads"]] == p["lead_heads"]


def test_compile_deterministic_and_cached(client, scheme):
    client.post("/compositions", json=_composition())
    j1 = client.post("/compositions/comp/versions/1/compile", json={}).json()
    r2 = client.post("/compositions/comp/versions/1/compile", json={})
    assert r2.headers["X-Compile-Cache"] == "hit"
    assert r2.json() == j1


def test_compile_unreachable_position(client, scheme):
    # gap=0 的 bob 后观察钟在第 5 位，不是 Wrong
    client.post("/compositions", json=_composition(
        cid="bad", tokens=[{"symbol": "Wrong", "call": "bob", "plain_leads": 0}]))
    r = client.post("/compositions/bad/versions/1/compile", json={})
    assert r.status_code == 422
    j = r.json()
    assert j["detail"]["code"] == "POSITION_UNREACHABLE"
    assert j["token"]["symbol"] == "Wrong"
    assert j["current_row"] == "123456"
    assert len(j["candidate_leads"]) == 1
    assert j["candidate_leads"][0]["observer_position"] == 5


def test_compile_ambiguous_position(client, scheme):
    # 无 plain_leads 且窗口 10：gap=4 与 gap=9 都命中 Home
    client.post("/compositions", json=_composition(
        cid="amb", tokens=[{"symbol": "Home", "call": "bob"}]))
    r = client.post("/compositions/amb/versions/1/compile", json={"course_length": 10})
    assert r.status_code == 422
    j = r.json()
    assert j["detail"]["code"] == "AMBIGUOUS_POSITION"
    assert [m["plain_leads_before"] for m in j["matching_leads"]] == [4, 9]
    assert len(j["candidate_leads"]) == 10
    assert j["current_row"] == "123456"


def test_compile_part_mismatch(client, scheme):
    # Home bob（gap4）后回 home 需要 5 个 plain lead；course_length=3 时 part 接不上
    client.post("/compositions", json=_composition(
        cid="pm", tokens=[{"symbol": "Home", "call": "bob", "plain_leads": 4}]))
    r = client.post("/compositions/pm/versions/1/compile", json={"course_length": 3})
    assert r.status_code == 422
    j = r.json()
    assert j["detail"]["code"] == "PART_MISMATCH"
    assert j["part"] == 1
    assert len(j["candidate_leads"]) == 3


def test_compile_two_parts_chain_and_rounds(client, scheme):
    # Wrong(gap1) part 重复两次：每 part 3 lead，最终 6 lead(72 changes) 回 rounds
    client.post("/compositions", json=_composition(parts=[{"repeat": 2, "tokens": [
        {"symbol": "Wrong", "call": "bob", "plain_leads": 1}]}]))
    j = client.post("/compositions/comp/versions/1/compile", json={}).json()
    assert j["course_lengths"] == [3, 3]
    assert j["course_heads"] == ["123456", "154326", "123456"]
    assert j["final_row"] == "123456" and j["rounds_return"] is True
    assert j["proof"]["truth"] == "true" and j["proof"]["total_changes"] == 72
    assert [l["part"] for l in j["compiled_leads"] if l["call"]] == [1, 2]


def test_compile_with_singles_and_custom_calls(client, scheme):
    # single gap1 后观察钟也在第 4 位
    client.post("/compositions", json=_composition(
        cid="s", tokens=[{"symbol": "Wrong", "call": "single", "plain_leads": 1}]))
    j = client.post("/compositions/s/versions/1/compile", json={}).json()
    assert j["compiled_tokens"][0]["call"] == "single"
    assert j["compiled_tokens"][0]["position"] == 4
    assert j["proof"]["calls_used"] == 1


def test_compile_repeated_part_names(client, scheme):
    client.post("/compositions", json=_composition(parts=[
        {"name": "A", "tokens": [{"symbol": "Wrong", "call": "bob", "plain_leads": 1}]},
        {"name": "B", "tokens": [{"symbol": "Wrong", "call": "bob", "plain_leads": 1}]},
    ]))
    j = client.post("/compositions/comp/versions/1/compile", json={}).json()
    names = {l["part"]: l["part_name"] for l in j["compiled_leads"]}
    assert names == {1: "A", 2: "B"}


def test_compile_dependencies_frozen(client, scheme):
    client.post("/compositions", json=_composition())
    j = client.post("/compositions/comp/versions/1/compile", json={}).json()
    deps = j["dependencies"]
    assert deps["ringproof_version"]
    assert deps["method"]["id"] == "pb-minor"
    assert deps["position_scheme"]["id"] == "tenor-6"
    assert deps["composition"]["version"] == 1
    assert deps["calls"]["bob"]["replace"] == 1
    # 在方法新版本发布后，冻结版本编译结果不变
    client.post("/methods", json={"id": "pb-minor", "name": "Plain Bob Minor v2",
                                  "stage": 6, "notation": PLAIN_BOB_MINOR})
    again = client.post("/compositions/comp/versions/1/compile", json={})
    assert again.headers["X-Compile-Cache"] == "hit"
    assert again.json() == j


def test_compile_different_course_head_gives_distinct_result(client, scheme):
    client.post("/compositions", json=_composition())
    j1 = client.post("/compositions/comp/versions/1/compile",
                     json={"course_head": "123456"}).json()
    # 从 W-part 的 course head 154326 起：同样 gap1 命中 Wrong，终到 rounds。
    # 非 rounds 起点无法由观察钟自动界定 plain course，须显式 course_length。
    r2 = client.post("/compositions/comp/versions/1/compile",
                     json={"course_head": "154326", "course_length": 5})
    assert r2.status_code == 200, r2.text
    j2 = r2.json()
    assert j1["request_hash"] != j2["request_hash"]
    assert j1["course_head"] == "123456" and j2["course_head"] == "154326"
    assert j2["final_row"] == "123456"
    assert j1["final_row"] != j2["final_row"]
    # 缺省 course_length 且起点观察钟不在 home（普通 lead end 135264）→ 无法自动界定
    r3 = client.post("/compositions/comp/versions/1/compile",
                     json={"course_head": "135264"})
    assert r3.status_code == 422
    assert r3.json()["detail"]["code"] == "PLAIN_COURSE_NOT_BOUND"


def test_compile_save_touch_false(client, scheme):
    client.post("/compositions", json=_composition())
    j = client.post("/compositions/comp/versions/1/compile",
                    json={"save_touch": False}).json()
    assert j["touch"] is None
    # 未落 touch
    assert client.get("/touches/compiled:comp/versions/1").status_code == 404


def test_compile_not_found(client):
    r = client.post("/compositions/ghost/versions/1/compile", json={})
    assert r.status_code == 404


def test_composition_lists_and_versions(client, scheme):
    client.post("/compositions", json=_composition())
    client.post("/position-schemes", json={"id": "p2", "name": "p2", "method_id": "pb-minor",
                                           "observer": 6, "positions": {"Home": 6}})
    assert any(x["id"] == "comp" for x in client.get("/compositions").json()["compositions"])
    versions = client.get("/compositions/comp").json()["versions"]
    assert [v["version"] for v in versions] == [1]
    assert client.get("/compositions/ghost/versions/1").status_code == 404
