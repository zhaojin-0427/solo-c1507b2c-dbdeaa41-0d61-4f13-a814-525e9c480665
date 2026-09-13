"""Round block 等价分析：规范化与循环移位、规范指纹、等价归并、成员相对
代表项的移位/方向/钟号映射、逐 row 对照、最早分歧定位、创建校验
（钟数/闭合/移位点/固定钟）、版本冻结与重复读取一致性。"""
import pytest
from fastapi.testclient import TestClient

from ringproof.engine import build_lead, prove_rows
from ringproof.main import create_app
from ringproof.multipart import permute_row
from ringproof.notation import expand_notation
from ringproof.roundblock import (
    alignment,
    bell_mapping_images,
    canonical_fingerprint,
    first_divergence,
    lead_of_change,
    normalization_images,
    normalize_rows,
    rotated_rows,
    shift_lead,
)

PB_MINOR = "x16x16x16x16x16x12"
PB_MINOR_REV = "12x16x16x16x16x16x"  # PB Minor 的逐 change 倒序
PB_MINIMUS = "x14x14x14x12"
ROUNDS6 = (1, 2, 3, 4, 5, 6)
CALLS = {"bob": {"notation": "14", "replace": 1},
         "single": {"notation": "1234", "replace": 1}}


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


# ---------------- 规范化与循环移位 ----------------

def test_normalization_images_relabels_open_row_to_rounds():
    rows, _, _ = _pb_rows(5)
    images = normalization_images(rows[12])  # 135264 → rounds
    # images[b-1] = 钟 b 在起行 135264 中的位置（1 起）
    assert images == (1, 4, 2, 6, 3, 5)
    assert permute_row(images, rows[12]) == ROUNDS6


def test_rotated_rows_forward_and_reverse():
    rows, _, _ = _pb_rows(2)
    n = len(rows) - 1
    fwd = rotated_rows(rows, 12)
    assert fwd[0] == rows[12] and fwd[-1] == rows[12] and len(fwd) == n + 1
    assert fwd[1] == rows[13] and fwd[-2] == rows[11]
    rev = rotated_rows(rows, 0, reverse=True)
    assert rev == [rows[(0 - i) % n] for i in range(n + 1)]
    assert rev[1] == rows[n - 1] and rev[-1] == rows[0]
    # 末尾 lead end（change n）与起点 0 等价
    assert rotated_rows(rows, n) == rotated_rows(rows, 0)


def test_normalize_rows_periodic_plain_course():
    # plain course 的 change 序列以 lead 为周期：任一 lead 边界重开并归一化
    # 后与原序列完全一致
    rows, _, _ = _pb_rows(5)
    for s in (0, 12, 24, 36, 48):
        assert normalize_rows(rows, s) == rows


def test_canonical_fingerprint_deterministic_tiebreak():
    rows, _, _ = _pb_rows(5)
    seq, shift, direction = canonical_fingerprint(rows, [0, 12, 24, 36, 48], False)
    # 全部候选相同：首个（shift 0, forward）胜出
    assert (shift, direction) == (0, "forward")
    assert seq == rows
    # 反向候选参与时仍确定
    seq2, shift2, dir2 = canonical_fingerprint(rows, [0, 12], True)
    assert (shift2, dir2) in {(0, "forward"), (0, "reversed")}
    assert seq2 == canonical_fingerprint(rows, [0, 12], True)[0]


def test_first_divergence():
    a = [(1, 2), (3, 4), (5, 6)]
    assert first_divergence(a, list(a)) is None
    assert first_divergence(a, [(1, 2), (9, 9), (5, 6)]) == 1
    assert first_divergence(a, [(1, 2), (3, 4)]) == 2  # 前缀：较短序列耗尽
    assert first_divergence([(1, 2)], a) == 1


def test_lead_of_change_and_shift_lead():
    lead_ends = [12, 24, 36]
    assert lead_of_change(lead_ends, 0) is None
    assert lead_of_change(lead_ends, 1) == 1
    assert lead_of_change(lead_ends, 12) == 1
    assert lead_of_change(lead_ends, 13) == 2
    assert lead_of_change(lead_ends, 36) == 3
    assert shift_lead(lead_ends, 0) == 0
    assert shift_lead(lead_ends, 12) == 1
    assert shift_lead(lead_ends, 36) == 3
    assert shift_lead(lead_ends, 13) is None


def test_alignment_and_bell_mapping_consistency():
    # B 为 A 在 change 24 重开并归一化的序列：成员 B 相对代表项 A 的
    # 对齐与钟号映射须逐 row 成立
    rows_a, _, _ = _pb_rows(6, [None, None, "bob", None, None, "bob"])
    n = len(rows_a) - 1
    rows_b = normalize_rows(rows_a, 24)
    # A 的 best transform 视为 (0, forward)，B 的视为 (0, forward)
    direction, delta, align = alignment(
        {"shift": 0, "direction": "forward"},
        {"shift": 0, "direction": "forward"},
        n,
    )
    assert direction == "forward" and delta == 0
    images = bell_mapping_images(rows_b[0], rows_a[0])
    # B 的起行是 A 的 change 24 归一化：rows_b[j] == images(rows_a[(24+j) % n])
    images24 = normalization_images(rows_a[24])
    for j in range(n + 1):
        assert rows_b[j] == permute_row(images24, rows_a[(24 + j) % n])
    # 反向：B 的 change j 对应 A 的 change (n-j)
    direction_r, _, align_r = alignment(
        {"shift": 0, "direction": "reversed"},
        {"shift": 0, "direction": "forward"},
        n,
    )
    assert direction_r == "reversed"
    assert align_r(0) == 0 and align_r(1) == n - 1


# ---------------- API ----------------

@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def methods(client):
    client.post("/methods", json={"id": "pb", "name": "Plain Bob Minor",
                                  "stage": 6, "notation": PB_MINOR})
    client.post("/methods", json={"id": "pb-rev", "name": "Plain Bob Minor Reversed",
                                  "stage": 6, "notation": PB_MINOR_REV})
    client.post("/methods", json={"id": "pb-minimus", "name": "Plain Bob Minimus",
                                  "stage": 4, "notation": PB_MINIMUS})


def _touch(client, tid, calls_seq, method_id="pb", calls=None):
    body = {
        "id": tid,
        "method_id": method_id,
        "sequence": [{"leads": [{"call": c} for c in calls_seq], "repeat": 1}],
    }
    if calls:
        body["calls"] = calls
    r = client.post("/touches", json=body)
    assert r.status_code == 201, r.json()
    return r.json()


@pytest.fixture()
def touches(client, methods):
    # ta 与 tb 互为旋转（tb = ta 从 lead 2 末尾重开）；tc 为 plain course
    _touch(client, "ta", [None, None, "bob", None, None, "bob"], calls=CALLS)
    _touch(client, "tb", ["bob", None, None, "bob", None, None], calls=CALLS)
    _touch(client, "tc", [None] * 5, calls=CALLS)


def _post_analysis(client, **over):
    payload = {"id": "rb", "touches": [{"id": "ta"}, {"id": "tb"}]}
    payload.update(over)
    return client.post("/round-block-analyses", json=payload)


def test_api_create_rotation_equivalent(client, touches):
    r = _post_analysis(client)
    assert r.status_code == 201, r.json()
    body = r.json()
    assert body["version"] == 1 and body["stage"] == 6
    assert body["total_touches"] == 2 and body["total_groups"] == 1
    assert body["equivalent_pairs"] == 1
    # 变换规则独立记录
    assert body["transform_rules"] == {
        "shift_changes": None, "allow_reverse": False, "fixed_bells": [],
    }
    group = body["groups"][0]
    assert group["size"] == 2 and group["representative"] == {"id": "ta", "version": 1}
    by_id = {t["touch"]["id"]: t for t in body["touches"]}
    # 合法移位完整记录：0 + 各 lead end（72 闭合，末尾与 0 等价不计）
    assert [s["change"] for s in by_id["ta"]["allowed_shifts"]] == [0, 12, 24, 36, 48, 60]
    assert [s["lead"] for s in by_id["ta"]["allowed_shifts"]] == [0, 1, 2, 3, 4, 5]
    # 代表项：恒等关系
    rep = by_id["ta"]["relative_to_representative"]
    assert rep["direction"] == "forward" and rep["shift_change"] == 0
    assert rep["bell_mapping"]["images"] == [1, 2, 3, 4, 5, 6]
    assert rep["rows_matched"] == rep["rows_total"] == 73
    # 成员 tb：相对代表项的移位 lead、方向、钟号映射
    mem = by_id["tb"]["relative_to_representative"]
    assert mem["direction"] == "forward"
    assert mem["shift_change"] == 24 and mem["shift_lead"] == 2
    assert mem["bell_mapping"]["images"] != [1, 2, 3, 4, 5, 6]
    assert mem["rows_matched"] == mem["rows_total"] == 73
    # 规范指纹一致
    assert by_id["ta"]["fingerprint"]["hash"] == by_id["tb"]["fingerprint"]["hash"]
    assert by_id["ta"]["fingerprint"]["first_row"] == "123456"
    # 依赖冻结：touch、方法、call、ringproof 版本
    deps = body["dependencies"]
    assert [(t["id"], t["version"]) for t in deps["touches"]] == [("ta", 1), ("tb", 1)]
    assert [(m["id"], m["version"]) for m in deps["methods"]] == [("pb", 1)]
    assert sorted({c["name"] for c in deps["calls"]}) == ["bob", "single"]
    assert {c["touch_id"] for c in deps["calls"]} == {"ta", "tb"}
    assert "ringproof_version" in deps

    # GET 读回与创建响应一致；列表与版本列表
    got = client.get("/round-block-analyses/rb/versions/1").json()
    assert got == body
    listing = client.get("/round-block-analyses").json()["round_block_analyses"]
    assert [a["id"] for a in listing] == ["rb"]
    versions = client.get("/round-block-analyses/rb").json()["versions"]
    assert [v["version"] for v in versions] == [1]
    # 404
    assert client.get("/round-block-analyses/ghost/versions/1").status_code == 404
    assert client.get("/round-block-analyses/rb/versions/9").status_code == 404
    assert client.get("/round-block-analyses/ghost").status_code == 404


def test_api_non_equivalent_groups(client, touches):
    r = _post_analysis(client, touches=[{"id": "ta"}, {"id": "tc"}])
    assert r.status_code == 201
    body = r.json()
    assert body["total_groups"] == 2 and body["equivalent_pairs"] == 0
    assert [g["size"] for g in body["groups"]] == [1, 1]
    by_id = {t["touch"]["id"]: t for t in body["touches"]}
    assert by_id["ta"]["group"] != by_id["tc"]["group"]
    assert by_id["ta"]["fingerprint"]["hash"] != by_id["tc"]["fingerprint"]["hash"]


def test_api_reverse_unfolding(client, methods):
    # 反向方法展开的 plain course 是正向 plain course 的倒序
    _touch(client, "fwd", [None] * 5)
    _touch(client, "rev", [None] * 5, method_id="pb-rev")
    # 不允许反向：两个组
    r = _post_analysis(client, touches=[{"id": "fwd"}, {"id": "rev"}])
    assert r.status_code == 201
    assert r.json()["total_groups"] == 2
    # 允许反向：归并为一组，成员方向 reversed
    r = _post_analysis(client, id="rb-rev",
                       touches=[{"id": "fwd"}, {"id": "rev"}],
                       allow_reverse=True)
    assert r.status_code == 201, r.json()
    body = r.json()
    assert body["total_groups"] == 1 and body["equivalent_pairs"] == 1
    assert body["transform_rules"]["allow_reverse"] is True
    by_id = {t["touch"]["id"]: t for t in body["touches"]}
    rel = by_id["rev"]["relative_to_representative"]
    assert rel["direction"] == "reversed"
    assert rel["rows_matched"] == rel["rows_total"] == 61


def test_api_shift_restriction_changes_equivalence(client, touches):
    # 只允许原始起行（shift 0）：互为旋转的 ta/tb 不再等价
    r = _post_analysis(client, shift_changes=[0])
    assert r.status_code == 201
    body = r.json()
    assert body["total_groups"] == 2
    assert body["transform_rules"]["shift_changes"] == [0]
    by_id = {t["touch"]["id"]: t for t in body["touches"]}
    assert [s["change"] for s in by_id["ta"]["allowed_shifts"]] == [0]
    # 允许 lead 2 末尾重开：恢复等价
    r = _post_analysis(client, id="rb2", shift_changes=[0, 24])
    assert r.status_code == 201
    assert r.json()["total_groups"] == 1


def test_api_fixed_bells(client, methods):
    _touch(client, "pc1", [None] * 5)
    _touch(client, "pc2", [None] * 5)
    # treble 在 plain course 的全部 lead head 都在第 1 位：允许
    r = _post_analysis(client, touches=[{"id": "pc1"}, {"id": "pc2"}], fixed_bells=[1])
    assert r.status_code == 201, r.json()
    assert r.json()["transform_rules"]["fixed_bells"] == [1]
    # 钟 2 只在 rounds 归位：任一非零移位都会改动它 → 拒绝创建
    r = _post_analysis(client, id="rb-bad", touches=[{"id": "pc1"}, {"id": "pc2"}],
                       fixed_bells=[2])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "FIXED_BELL_DISPLACED"
    # 只允许 shift 0（rounds 中钟 2 在第 2 位）：允许
    r = _post_analysis(client, id="rb-ok", touches=[{"id": "pc1"}, {"id": "pc2"}],
                       fixed_bells=[2], shift_changes=[0])
    assert r.status_code == 201
    # 越界固定钟
    r = _post_analysis(client, id="rb-oob", touches=[{"id": "pc1"}, {"id": "pc2"}],
                       fixed_bells=[7])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "BELL_OUT_OF_RANGE"


def test_api_rejections(client, methods):
    _touch(client, "ok1", [None] * 5)
    _touch(client, "ok2", [None] * 5)
    _touch(client, "open", [None, "bob"], calls=CALLS)  # 未闭合
    _touch(client, "minimus", [None] * 3, method_id="pb-minimus")

    # touch 未闭合回到 rounds
    r = _post_analysis(client, touches=[{"id": "ok1"}, {"id": "open"}])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NOT_ROUND_BLOCK"
    # 钟数不一致
    r = _post_analysis(client, touches=[{"id": "ok1"}, {"id": "minimus"}])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "STAGE_MISMATCH"
    # 移位点不在 lead end
    r = _post_analysis(client, touches=[{"id": "ok1"}, {"id": "ok2"}],
                       shift_changes=[0, 5])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NOT_LEAD_END"
    # touch 不存在
    r = _post_analysis(client, touches=[{"id": "ok1"}, {"id": "ghost"}])
    assert r.status_code == 404
    r = _post_analysis(client, touches=[{"id": "ok1"}, {"id": "ok2", "version": 9}])
    assert r.status_code == 404
    # 重复引用同一 touch 版本（pydantic 拒绝）
    r = _post_analysis(client, touches=[{"id": "ok1"}, {"id": "ok1"}])
    assert r.status_code == 422
    # 少于 2 个 touch（pydantic 拒绝）
    r = _post_analysis(client, touches=[{"id": "ok1"}])
    assert r.status_code == 422


def test_api_compare_equivalent(client, touches):
    _post_analysis(client)
    r = client.get("/round-block-analyses/rb/versions/1/compare",
                   params={"touch_a": "ta", "version_a": 1,
                           "touch_b": "tb", "version_b": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["equivalent"] is True
    assert body["transform"]["direction"] == "forward"
    assert body["transform"]["shift_change"] == 24
    assert body["transform"]["shift_lead"] == 2
    assert body["transform"]["bell_mapping"]["images"]
    assert body["rows_matched"] == body["rows_total"] == 73
    # 逐 row 对照：全部匹配，含分页信息
    assert len(body["comparison"]) == 73
    assert all(e["match"] for e in body["comparison"])
    first = body["comparison"][0]
    assert first["change"] == 0 and first["lead"] is None
    assert first["row"] == "123456"
    assert first["aligned"]["change"] == 24 and first["aligned"]["lead"] == 2
    # 分页
    r = client.get("/round-block-analyses/rb/versions/1/compare",
                   params={"touch_a": "ta", "version_a": 1,
                           "touch_b": "tb", "version_b": 1,
                           "offset": 70, "limit": 10})
    page = r.json()
    assert [e["change"] for e in page["comparison"]] == [70, 71, 72]
    # 重复读取一致
    again = client.get("/round-block-analyses/rb/versions/1/compare",
                       params={"touch_a": "ta", "version_a": 1,
                               "touch_b": "tb", "version_b": 1}).json()
    assert again == body


def test_api_compare_non_equivalent_divergence(client, touches):
    _post_analysis(client, touches=[{"id": "ta"}, {"id": "tc"}])
    r = client.get("/round-block-analyses/rb/versions/1/compare",
                   params={"touch_a": "ta", "version_a": 1,
                           "touch_b": "tc", "version_b": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["equivalent"] is False
    div = body["first_divergence"]
    assert div["row_index"] == 12
    # 双方的 method、lead、call 来源
    pa, pb = div["a"]["provenance"], div["b"]["provenance"]
    assert pa["method_id"] == "pb" and pa["lead"] == 3 and pa["call"] == "bob"
    assert pa["source"] == "call:bob"
    assert pb["method_id"] == "pb" and pb["lead"] == 1 and pb["call"] is None
    assert div["a"]["fingerprint_row"] != div["b"]["fingerprint_row"]
    assert not div["a"]["exhausted"] and not div["b"]["exhausted"]
    # 各自携带 best_transform 与指纹
    assert body["a"]["best_transform"]["shift_change"] == 24
    assert body["b"]["fingerprint"]["hash"] != body["a"]["fingerprint"]["hash"]


def test_api_compare_membership_and_404(client, touches):
    _post_analysis(client)
    _touch(client, "outsider", [None] * 5, calls=CALLS)
    r = client.get("/round-block-analyses/rb/versions/1/compare",
                   params={"touch_a": "ta", "version_a": 1,
                           "touch_b": "outsider", "version_b": 1})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "TOUCH_NOT_IN_ANALYSIS"
    assert client.get("/round-block-analyses/ghost/versions/1/compare",
                      params={"touch_a": "ta", "version_a": 1,
                              "touch_b": "tb", "version_b": 1}).status_code == 404


def test_api_immutable_and_frozen(client, touches):
    r1 = _post_analysis(client)
    assert r1.status_code == 201
    # touch 出新版本后，分析 v1 仍冻结在 touch v1
    _touch(client, "ta", [None] * 5, calls=CALLS)
    got = client.get("/round-block-analyses/rb/versions/1").json()
    assert [t["touch"]["version"] for t in got["touches"]] == [1, 1]
    assert got["dependencies"]["touches"][0]["version"] == 1
    # 显式引用旧版本再建分析：input_hash 一致
    r2 = _post_analysis(client, id="rb2",
                        touches=[{"id": "ta", "version": 1}, {"id": "tb", "version": 1}])
    assert r2.status_code == 201
    assert r2.json()["input_hash"] == r1.json()["input_hash"]
    # 同名 id 递增版本
    assert r2.json()["version"] == 1
    r3 = _post_analysis(client, touches=[{"id": "ta"}, {"id": "tb"}])
    assert r3.status_code == 201 and r3.json()["version"] == 2
    # 重复读取一致
    again = client.get("/round-block-analyses/rb/versions/1").json()
    assert again == got


def test_api_too_many_shifts(client, touches, monkeypatch):
    import ringproof.main as main_mod

    monkeypatch.setattr(main_mod, "MAX_SHIFT_CELLS", 100)
    r = _post_analysis(client)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "TOO_MANY_SHIFTS"
