"""音乐模式评分与选优测试：方案 CRUD 与校验、证明评分、版本冻结、
枚举音乐门槛过滤与排序。"""
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
def touch_id(client):
    client.post(
        "/methods",
        json={"id": "pb-minor", "name": "Plain Bob Minor", "stage": 6, "notation": PLAIN_BOB_MINOR},
    )
    r = client.post(
        "/touches",
        json={
            "id": "pc",
            "method_id": "pb-minor",
            "calls": {"bob": {"notation": "14", "replace": 1}},
            "sequence": [{"leads": [{"call": None}], "repeat": 5}],
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


def _scheme_payload(**over):
    payload = {
        "id": "music-6",
        "name": "六钟音乐方案",
        "stage": 6,
        "rules": [
            {"type": "row", "name": "queens-ish", "points": 10, "row": "135264"},
            {"type": "run", "name": "front-12", "points": 3, "bells": [1, 2], "position": "front"},
            {"type": "run", "name": "back-56", "points": 2, "bells": [5, 6], "position": "back"},
            {
                "type": "positions",
                "name": "5-front-6-back",
                "points": 1,
                "allow_overlap": True,
                "positions": [
                    {"bell": 5, "position": 1},
                    {"bell": 6, "position": 6},
                ],
            },
        ],
    }
    payload.update(over)
    return payload


def _create_scheme(client, **over):
    r = client.post("/music-schemes", json=_scheme_payload(**over))
    assert r.status_code == 201, r.json()
    return r.json()


# ---------------- 方案 CRUD 与不可变性 ----------------

def test_create_scheme_and_immutable_versions(client):
    v1 = _create_scheme(client)
    assert v1["version"] == 1
    assert v1["stage"] == 6
    assert [r["name"] for r in v1["rules"]] == [
        "queens-ish", "front-12", "back-56", "5-front-6-back",
    ]
    assert v1["rules"][0]["row"] == "135264"
    assert v1["rules"][1]["bells"] == [1, 2] and v1["rules"][1]["position"] == "front"
    assert v1["score_start_row"] is False and v1["score_final_rounds"] is False

    # 同一 id 再提交 → 新版本；内容相同则哈希相同；v1 保持不变
    v2 = _create_scheme(client)
    assert v2["version"] == 2
    assert v2["input_hash"] == v1["input_hash"]
    assert client.get("/music-schemes/music-6/versions/1").json() == v1

    versions = client.get("/music-schemes/music-6").json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    listed = client.get("/music-schemes").json()["music_schemes"]
    assert [m["id"] for m in listed] == ["music-6", "music-6"]
    assert client.get("/music-schemes/ghost/versions/1").status_code == 404
    assert client.get("/music-schemes/ghost").status_code == 404


def test_scheme_auto_id_and_flags(client):
    r = client.post(
        "/music-schemes",
        json=_scheme_payload(id=None, score_start_row=True, score_final_rounds=True),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["id"] and body["score_start_row"] is True
    assert body["score_final_rounds"] is True


# ---------------- 方案校验：非法规则不得保存 ----------------

def test_scheme_rejects_bad_rows_and_bells(client):
    cases = [
        # 不完整排列（重复钟号）
        ({"type": "row", "name": "r", "row": "112345"}, "ROW_NOT_PERMUTATION"),
        # 排列长度与钟数不符
        ({"type": "row", "name": "r", "row": "12345"}, "ROW_LENGTH_MISMATCH"),
        # 越界钟号（run / positions；row 中的越界钟号使排列不完整）
        ({"type": "run", "name": "r", "bells": [6, 7], "position": "back"}, "BELL_OUT_OF_RANGE"),
        ({"type": "positions", "name": "r", "positions": [{"bell": 7, "position": 1}]}, "BELL_OUT_OF_RANGE"),
        ({"type": "row", "name": "r", "row": "123457"}, "ROW_NOT_PERMUTATION"),
        # 位置与钟数不符
        ({"type": "positions", "name": "r", "positions": [{"bell": 1, "position": 7}]}, "POSITION_OUT_OF_RANGE"),
        # 非连续钟组
        ({"type": "run", "name": "r", "bells": [1, 3], "position": "front"}, "RUN_NOT_CONSECUTIVE"),
        ({"type": "run", "name": "r", "bells": [4, 5, 3], "position": "front"}, "RUN_NOT_CONSECUTIVE"),
        # 重复钟号 / 重复位置对 / 重名规则
        ({"type": "run", "name": "r", "bells": [4, 4], "position": "front"}, "DUPLICATE_BELL"),
        (
            {"type": "positions", "name": "r",
             "positions": [{"bell": 5, "position": 1}, {"bell": 5, "position": 1}]},
            "DUPLICATE_POSITION",
        ),
    ]
    for rule, code in cases:
        r = client.post("/music-schemes", json=_scheme_payload(id=None, rules=[rule]))
        assert r.status_code == 422, (rule, r.json())
        assert r.json()["detail"]["code"] == code

    # 重名规则
    r = client.post(
        "/music-schemes",
        json=_scheme_payload(
            id=None,
            rules=[
                {"type": "row", "name": "dup", "row": "135264"},
                {"type": "row", "name": "dup", "row": "123456"},
            ],
        ),
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "DUPLICATE_RULE_NAME"
    # 钟数越界
    r = client.post("/music-schemes", json=_scheme_payload(id=None, stage=3))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "STAGE_OUT_OF_RANGE"
    # 未通过校验的方案不落库
    assert client.get("/music-schemes").json()["music_schemes"] == []


def test_scheme_accepts_descending_run_and_negative_points(client):
    r = client.post(
        "/music-schemes",
        json=_scheme_payload(
            id=None,
            rules=[
                {"type": "run", "name": "back-654", "points": -2,
                 "bells": [6, 5, 4], "position": "back", "max_per_row": 1},
            ],
        ),
    )
    assert r.status_code == 201
    rule = r.json()["rules"][0]
    assert rule["bells"] == [6, 5, 4] and rule["points"] == -2
    assert rule["max_per_row"] == 1


# ---------------- 证明时的音乐评分 ----------------

def test_prove_with_music_scores_and_provenance(client, touch_id):
    _create_scheme(client)
    p = client.post(f"/touches/{touch_id}/versions/1/prove", json={"music": {"id": "music-6"}})
    assert p.status_code == 200
    assert p.headers["X-Proof-Cache"] == "miss"
    music = p.json()["music"]
    assert music["scheme"] == {
        "id": "music-6", "version": 1, "name": "六钟音乐方案",
        "input_hash": music["scheme"]["input_hash"],
    }
    # 起始 row 与末尾 rounds 不计分：61 个 row 中计 59 个
    assert music["rows_scored"] == 59
    hits = {r["name"]: r["hits"] for r in music["rules"]}
    assert hits == {"queens-ish": 1, "front-12": 1, "back-56": 3, "5-front-6-back": 19}
    assert music["total_hits"] == 24
    assert music["total_score"] == 10 + 3 + 3 * 2 + 19

    # 首次计分 row：change 7 的 563412（5 在首位），追溯到 lead/method/call
    first = music["first_scoring_row"]
    assert first["row"] == "563412" and first["score"] == 1
    assert first["change"] == 7 and first["lead"] == 1
    assert first["method"] == "Plain Bob Minor" and first["method_id"] == "pb-minor"
    assert first["call"] is None and first["source"] == "method"
    # 最高分 row：change 12 的 135264（精确 row 规则 10 分）
    best = music["highest_scoring_row"]
    assert best["row"] == "135264" and best["score"] == 10
    assert best["change"] == 12 and best["lead"] == 1 and best["change_in_lead"] == 12

    # 同一版本重复证明：缓存命中且结果一致
    p2 = client.post(f"/touches/{touch_id}/versions/1/prove", json={"music": {"id": "music-6"}})
    assert p2.headers["X-Proof-Cache"] == "hit"
    assert p2.json() == p.json()
    # 不带方案的证明走独立缓存，结果不含 music
    p3 = client.post(f"/touches/{touch_id}/versions/1/prove")
    assert "music" not in p3.json()


def test_prove_music_start_row_and_final_rounds_flags(client, touch_id):
    # rounds 精确 row 规则：默认不计起始与末尾 rounds → 0 命中
    _create_scheme(client, id="rounds-off", rules=[{"type": "row", "name": "rounds", "points": 7, "row": "123456"}])
    p = client.post(f"/touches/{touch_id}/versions/1/prove", json={"music": {"id": "rounds-off"}})
    music = p.json()["music"]
    assert music["total_score"] == 0 and music["total_hits"] == 0
    assert music["first_scoring_row"] is None and music["highest_scoring_row"] is None

    # 开启两个开关：起始 row（0 号）与末尾 rounds（60 号）各命中一次
    _create_scheme(
        client, id="rounds-on", score_start_row=True, score_final_rounds=True,
        rules=[{"type": "row", "name": "rounds", "points": 7, "row": "123456"}],
    )
    p2 = client.post(f"/touches/{touch_id}/versions/1/prove", json={"music": {"id": "rounds-on"}})
    music2 = p2.json()["music"]
    assert music2["total_score"] == 14 and music2["total_hits"] == 2
    assert music2["rows_scored"] == 61
    assert music2["first_scoring_row"]["change"] == 0
    assert music2["first_scoring_row"]["type"] == "start"
    # 起始与末尾 rounds 同分（7 分），平分取最早者；末尾 rounds 计分由 total_hits=2 佐证
    assert music2["highest_scoring_row"]["score"] == 7
    assert music2["highest_scoring_row"]["change"] == 0


def test_prove_music_overlap_and_row_cap(client, touch_id):
    # 不允许叠加：两个位置对同时满足时只计 1 次；max_per_row 再限制计分次数
    _create_scheme(
        client, id="cap",
        rules=[
            {"type": "positions", "name": "no-overlap", "points": 5,
             "positions": [{"bell": 1, "position": 1}, {"bell": 2, "position": 2}]},
            {"type": "positions", "name": "overlap-capped", "points": 2,
             "allow_overlap": True, "max_per_row": 1,
             "positions": [{"bell": 1, "position": 1}, {"bell": 2, "position": 2}]},
        ],
        score_start_row=True,
    )
    p = client.post(f"/touches/{touch_id}/versions/1/prove", json={"music": {"id": "cap"}})
    rules = {r["name"]: r for r in p.json()["music"]["rules"]}
    # 起始 row 123456 同时满足两对：no-overlap 计 1 次（5 分），
    # overlap-capped 本可叠加 2 次但被 max_per_row=1 限制为 1 次（2 分）
    assert rules["no-overlap"]["hits"] >= 1
    assert rules["overlap-capped"]["hits"] == rules["no-overlap"]["hits"]
    assert rules["no-overlap"]["score"] == rules["no-overlap"]["hits"] * 5
    assert rules["overlap-capped"]["score"] == rules["overlap-capped"]["hits"] * 2


def test_prove_music_errors(client, touch_id):
    # 方案不存在 / 版本不存在
    r = client.post(f"/touches/{touch_id}/versions/1/prove", json={"music": {"id": "ghost"}})
    assert r.status_code == 404
    _create_scheme(client)
    r2 = client.post(
        f"/touches/{touch_id}/versions/1/prove",
        json={"music": {"id": "music-6", "version": 99}},
    )
    assert r2.status_code == 404
    # 钟数不符：8 钟方案用于 6 钟 touch
    _create_scheme(client, id="music-8", stage=8, rules=[
        {"type": "run", "name": "back-78", "bells": [7, 8], "position": "back"}
    ])
    r3 = client.post(f"/touches/{touch_id}/versions/1/prove", json={"music": {"id": "music-8"}})
    assert r3.status_code == 422
    assert r3.json()["detail"]["code"] == "MUSIC_STAGE_MISMATCH"


def test_scheme_version_frozen_with_touch(client, touch_id):
    _create_scheme(client)  # v1：queens-ish 10 分
    p1 = client.post(f"/touches/{touch_id}/versions/1/prove", json={"music": {"id": "music-6"}})
    assert p1.json()["music"]["scheme"]["version"] == 1
    assert p1.json()["music"]["total_score"] == 38

    # 方案之后产生新版本（分值不同），不影响已冻结的 touch 版本
    _create_scheme(client, rules=[{"type": "row", "name": "queens-ish", "points": 100, "row": "135264"}])
    p2 = client.post(f"/touches/{touch_id}/versions/1/prove", json={"music": {"id": "music-6"}})
    assert p2.headers["X-Proof-Cache"] == "hit"
    assert p2.json() == p1.json()
    assert p2.json()["music"]["scheme"]["version"] == 1

    # 显式引用 v2 则按 v2 评分
    p3 = client.post(
        f"/touches/{touch_id}/versions/1/prove",
        json={"music": {"id": "music-6", "version": 2}},
    )
    assert p3.json()["music"]["scheme"]["version"] == 2
    assert p3.json()["music"]["total_score"] == 100


# ---------------- 枚举：音乐门槛过滤与排序 ----------------

@pytest.fixture()
def choice_touch_id(client):
    client.post(
        "/methods",
        json={"id": "pb-minor", "name": "Plain Bob Minor", "stage": 6, "notation": PLAIN_BOB_MINOR},
    )
    r = client.post(
        "/touches",
        json={
            "id": "choice-touch",
            "method_id": "pb-minor",
            "calls": {"bob": {"notation": "14", "replace": 1}},
            "sequence": [{"leads": [{"choice": ["plain", "bob"]}], "repeat": 4}],
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


def test_enumerate_with_music_sorts_and_reports(client, choice_touch_id):
    _create_scheme(client)
    e = client.post(
        f"/touches/{choice_touch_id}/versions/1/enumerate",
        json={"music": {"id": "music-6"}},
    )
    assert e.status_code == 200
    body = e.json()
    assert body["music"]["id"] == "music-6"
    assert body["music"]["version"] == 1
    assert body["sorted_by"] == ["truth", "rounds_return", "music_score", "num_calls", "total_changes"]
    assert body["filtered_by_music"] == 0
    assert body["total_variants"] == 16
    # 每个变体都带音乐分，且按 真值 → rounds → 音乐分（降序）→ 既有项 稳定排序
    keys = [
        (
            v["truth"] != "true",
            not v["rounds_return"],
            -v["music_score"],
            v["num_calls"],
            v["total_changes"],
        )
        for v in body["variants"]
    ]
    assert keys == sorted(keys)
    assert all("music_score" in v and "music_hits" in v for v in body["variants"])
    # 重放一致
    e2 = client.post(
        f"/touches/{choice_touch_id}/versions/1/enumerate",
        json={"music": {"id": "music-6"}},
    ).json()
    assert e2 == body


def test_enumerate_music_thresholds_filter_and_count(client, choice_touch_id):
    _create_scheme(client)
    full = client.post(
        f"/touches/{choice_touch_id}/versions/1/enumerate",
        json={"music": {"id": "music-6"}},
    ).json()
    top_score = max(v["music_score"] for v in full["variants"])
    above = [v for v in full["variants"] if v["music_score"] >= top_score]

    e = client.post(
        f"/touches/{choice_touch_id}/versions/1/enumerate",
        json={"music": {"id": "music-6"}, "min_music_score": top_score},
    ).json()
    assert e["filtered_by_music"] == 16 - len(above)
    assert e["total_variants"] == len(above)
    assert all(v["music_score"] >= top_score for v in e["variants"])
    assert e["min_music_score"] == top_score

    # 规则命中数门槛
    top_hits = max(v["music_hits"] for v in full["variants"])
    keep = [v for v in full["variants"] if v["music_hits"] >= top_hits]
    e2 = client.post(
        f"/touches/{choice_touch_id}/versions/1/enumerate",
        json={"music": {"id": "music-6"}, "min_music_hits": top_hits},
    ).json()
    assert e2["filtered_by_music"] == 16 - len(keep)
    assert all(v["music_hits"] >= top_hits for v in e2["variants"])

    # 高不可攀的门槛：全部被过滤
    e3 = client.post(
        f"/touches/{choice_touch_id}/versions/1/enumerate",
        json={"music": {"id": "music-6"}, "min_music_score": 10**6},
    ).json()
    assert e3["total_variants"] == 0
    assert e3["filtered_by_music"] == 16


def test_enumerate_music_threshold_requires_scheme(client, choice_touch_id):
    r = client.post(
        f"/touches/{choice_touch_id}/versions/1/enumerate",
        json={"min_music_score": 1},
    )
    assert r.status_code == 422
    r2 = client.post(
        f"/touches/{choice_touch_id}/versions/1/enumerate",
        json={"min_music_hits": 1},
    )
    assert r2.status_code == 422


def test_enumerate_music_frozen_and_multi_method_sort(client, choice_touch_id):
    _create_scheme(client)
    # 首次枚举冻结 v1；方案升级后重复枚举结果不变
    e1 = client.post(
        f"/touches/{choice_touch_id}/versions/1/enumerate",
        json={"music": {"id": "music-6"}},
    ).json()
    _create_scheme(client, rules=[{"type": "row", "name": "queens-ish", "points": 100, "row": "135264"}])
    e2 = client.post(
        f"/touches/{choice_touch_id}/versions/1/enumerate",
        json={"music": {"id": "music-6"}},
    ).json()
    assert e2 == e1
    assert e2["music"]["version"] == 1

    # 多方法 touch：音乐排序键置于拼接数之前
    client.post(
        "/methods",
        json={"id": "alt-minor", "name": "Alt Bob Minor", "stage": 6, "notation": "x16x14x16x14x16x12"},
    )
    r = client.post(
        "/touches",
        json={
            "id": "multi",
            "methods": [{"id": "pb-minor"}, {"id": "alt-minor"}],
            "sequence": [{"leads": [{"method_choice": ["pb-minor", "alt-minor"]}], "repeat": 3}],
        },
    )
    assert r.status_code == 201
    e3 = client.post(
        "/touches/multi/versions/1/enumerate", json={"music": {"id": "music-6"}}
    ).json()
    assert e3["sorted_by"] == [
        "truth", "rounds_return", "music_score",
        "num_splices", "balance", "num_calls", "total_changes",
    ]
    keys = [
        (
            v["truth"] != "true",
            not v["rounds_return"],
            -v["music_score"],
            v["num_splices"],
            v["balance"],
            v["num_calls"],
            v["total_changes"],
        )
        for v in e3["variants"]
    ]
    assert keys == sorted(keys)
