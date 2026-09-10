"""证明引擎测试：row 生成、call 替换、重复定位、rounds 回归。"""
from ringproof.engine import apply_change, build_lead, prove_rows
from ringproof.notation import expand_notation

PB_MINOR_TOKENS, PB_MINOR_CHANGES = expand_notation("x16x16x16x16x16x12", 6)
ROUNDS6 = (1, 2, 3, 4, 5, 6)


def _plain_leads(n):
    return [build_lead(PB_MINOR_TOKENS, PB_MINOR_CHANGES, None, {}) for _ in range(n)]


def test_apply_change():
    assert apply_change(ROUNDS6, frozenset()) == (2, 1, 4, 3, 6, 5)
    assert apply_change((2, 1, 4, 3, 6, 5), frozenset({1, 6})) == (2, 4, 1, 6, 3, 5)


def test_plain_bob_minor_first_rows():
    result = prove_rows(6, "Plain Bob Minor", _plain_leads(1), ROUNDS6)
    rows = [result["start_row"]] + [e["row"] for e in result["events"]]
    # Plain Bob Minor 第一个 lead 的经典 row 序列
    assert rows[:4] == ["123456", "214365", "241635", "426153"]
    assert rows[12] == "135264"  # lead head
    assert result["lead_heads"] == ["135264"]
    assert result["permutation_complete"] and result["notation_consistent"]


def test_plain_course_comes_round_and_is_true():
    result = prove_rows(6, "Plain Bob Minor", _plain_leads(5), ROUNDS6)
    assert result["total_changes"] == 60
    assert result["rounds_return"] is True
    assert result["rounds_at"] == [0, 60]
    assert result["truth"] == "true"
    assert result["first_repeat"] is None


def test_repeat_locates_two_changes_with_provenance():
    # 6 个 plain lead：60 号 change 回到 rounds，与 0 号（起始 row）构成首次重复
    result = prove_rows(6, "Plain Bob Minor", _plain_leads(6), ROUNDS6)
    assert result["truth"] == "false"
    rep = result["first_repeat"]
    assert rep["changes"] == [0, 60]
    assert rep["row"] == "123456"
    assert rep["first"] == {"type": "start", "change": 0}
    assert rep["second"]["lead"] == 5 and rep["second"]["change_in_lead"] == 12
    assert rep["second"]["method"] == "Plain Bob Minor"
    assert rep["second"]["source"] == "method"


def test_bob_replaces_lead_end_and_is_sourced():
    _, bob_changes = expand_notation("14", 6)
    call_defs = {"bob": {"tokens": ["14"], "changes": bob_changes, "replace": 1}}
    leads = _plain_leads(3) + [
        build_lead(PB_MINOR_TOKENS, PB_MINOR_CHANGES, "bob", call_defs)
    ]
    result = prove_rows(6, "Plain Bob Minor", leads, ROUNDS6)
    ev = result["events"][47]  # 第 4 个 lead 的最后一个 change
    assert ev["lead"] == 4 and ev["change_in_lead"] == 12
    assert ev["notation"] == "14" and ev["source"] == "call:bob"
    assert result["calls_used"] == 1
    # bob lead head 与 plain lead head 不同
    assert result["lead_heads"][3] != "135264"


def test_custom_call_replacing_two_changes():
    toks, chs = expand_notation("14x", 6)
    call_defs = {"extreme": {"tokens": toks, "changes": chs, "replace": 2}}
    lead = build_lead(PB_MINOR_TOKENS, PB_MINOR_CHANGES, "extreme", call_defs)
    assert lead.tokens == PB_MINOR_TOKENS[:-2] + ["14", "x"]
    assert len(lead.tokens) == len(PB_MINOR_TOKENS)


def test_max_calls_flag():
    _, bob_changes = expand_notation("14", 6)
    call_defs = {"bob": {"tokens": ["14"], "changes": bob_changes, "replace": 1}}
    leads = [build_lead(PB_MINOR_TOKENS, PB_MINOR_CHANGES, "bob", call_defs) for _ in range(3)]
    result = prove_rows(6, "Plain Bob Minor", leads, ROUNDS6, max_calls=2)
    assert result["calls_used"] == 3
    assert result["exceeds_max_calls"] is True


def test_non_rounds_start_row():
    result = prove_rows(6, "Plain Bob Minor", _plain_leads(1), (2, 1, 4, 3, 6, 5))
    assert result["start_row"] == "214365"
    assert result["rounds_return"] is False
    assert result["permutation_complete"]
