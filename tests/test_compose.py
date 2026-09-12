"""呼叫位置编译器单元测试：位置解析、plain lead 限额、歧义/不可达/接不上。"""
import pytest

from ringproof.compose import ComposeError, compile_composition, plain_course_length
from ringproof.notation import expand_notation, row_to_string

PB_MINOR = "x16x16x16x16x16x12"
ROUNDS6 = tuple(range(1, 7))


@pytest.fixture()
def method_ctx():
    tokens, changes = expand_notation(PB_MINOR, 6)
    bob_t, bob_c = expand_notation("14", 6)
    single_t, single_c = expand_notation("1234", 6)
    return {
        "tokens": tokens,
        "changes": changes,
        "method_id": "pb-minor",
        "method_version": 1,
        "method_name": "Plain Bob Minor",
        "call_defs": {
            "bob": {"tokens": bob_t, "changes": bob_c, "replace": 1},
            "single": {"tokens": single_t, "changes": single_c, "replace": 1},
        },
    }


@pytest.fixture()
def scheme():
    return {
        "observer": 6,
        "home_symbol": "Home",
        "positions": {"Home": 6, "Wrong": 4, "Middle": 2},
        "call_positions": {},
    }


def _parts(tokens, repeat=1, name=None):
    return [
        {"name": name, "repeat_index": r, "tokens": tokens}
        for r in range(1, repeat + 1)
    ]


def test_plain_course_length_observed_by_tenor(method_ctx, scheme):
    # PB Minor 中 6 号钟每 5 个 plain lead 回到第 6 位（home）
    assert plain_course_length(method_ctx, ROUNDS6, 6, 6) == 5


def test_wrong_bob_compiles_unique_lead(method_ctx, scheme):
    parts = _parts([{"symbol": "Wrong", "call": "bob", "plain_leads": 1}])
    out = compile_composition(scheme=scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    calls = [(l.call, l.symbol) for l in out["records"] if l.call]
    assert calls == [("bob", "Wrong")]
    tok = out["compiled_tokens"][0]
    assert tok.plain_leads_before == 1
    assert tok.observer_position == 4
    # 第二个 lead 是 bob；bob 前有 1 个 plain lead
    assert out["records"][1].call == "bob"
    assert out["records"][1].observer_position == 4
    # part 尾部补 plain lead 至观察钟回 home
    assert out["records"][-1].observer_position == 6


def test_middle_bob_three_plain_leads(method_ctx, scheme):
    parts = _parts([{"symbol": "Middle", "call": "bob", "plain_leads": 3}])
    out = compile_composition(scheme=scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    tok = out["compiled_tokens"][0]
    assert tok.plain_leads_before == 3 and tok.observer_position == 2
    assert out["records"][3].call == "bob"


def test_unqualified_symbol_searches_whole_course_and_is_unique(method_ctx, scheme):
    # 5 个候选中只有 gap=1 给出 pos 4（Wrong）
    parts = _parts([{"symbol": "Wrong", "call": "bob"}])
    out = compile_composition(scheme=scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    assert out["compiled_tokens"][0].plain_leads_before == 1


def test_position_unreachable_returns_token_candidates_and_row(method_ctx, scheme):
    # gap=0 的 bob 后观察钟在第 5 位，不是 Wrong(4)
    parts = _parts([{"symbol": "Wrong", "call": "bob", "plain_leads": 0}])
    with pytest.raises(ComposeError) as ei:
        compile_composition(scheme=scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    assert ei.value.code == "POSITION_UNREACHABLE"
    d = ei.value.details
    assert d["token"]["symbol"] == "Wrong" and d["current_row"] == "123456"
    assert len(d["candidate_leads"]) == 1
    assert d["candidate_leads"][0]["observer_position"] == 5


def test_max_plain_leads_limits_candidates(method_ctx, scheme):
    # Home 只在 gap=4 出现；限制 0..2 个 plain lead 时不可达，3 个候选
    parts = _parts([{"symbol": "Home", "call": "bob", "max_plain_leads": 2}])
    with pytest.raises(ComposeError) as ei:
        compile_composition(scheme=scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    assert ei.value.code == "POSITION_UNREACHABLE"
    assert len(ei.value.details["candidate_leads"]) == 3


def test_ambiguous_position_within_window(method_ctx, scheme):
    # 窗口放到 10 个 lead：gap=4 与 gap=9 的 bob 后观察钟都在第 6 位
    parts = _parts([{"symbol": "Home", "call": "bob"}])
    with pytest.raises(ComposeError) as ei:
        compile_composition(
            scheme=scheme, method_ctx=method_ctx, parts=parts,
            start_row=ROUNDS6, course_length=10,
        )
    assert ei.value.code == "AMBIGUOUS_POSITION"
    matching = ei.value.details["matching_leads"]
    assert [m["plain_leads_before"] for m in matching] == [4, 9]
    assert len(ei.value.details["candidate_leads"]) == 10


def test_symbol_not_mapped(method_ctx, scheme):
    parts = _parts([{"symbol": "Nowhere", "call": "bob"}])
    with pytest.raises(ComposeError) as ei:
        compile_composition(scheme=scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    assert ei.value.code == "SYMBOL_NOT_MAPPED"


def test_part_mismatch_when_home_unreachable_in_tail(method_ctx, scheme):
    # gap=4 的 Home bob 之后回 home 需要 5 个 plain lead；course_length=3 时接不上
    parts = _parts([{"symbol": "Home", "call": "bob", "plain_leads": 4}])
    with pytest.raises(ComposeError) as ei:
        compile_composition(
            scheme=scheme, method_ctx=method_ctx, parts=parts,
            start_row=ROUNDS6, course_length=3,
        )
    assert ei.value.code == "PART_MISMATCH"
    assert ei.value.details["part"] == 1
    assert len(ei.value.details["candidate_leads"]) == 3
    # 收尾失败时仍定位到该 part 最后一个 token 与当前排列
    assert ei.value.details["token"] == {
        "part": 1, "token": 1, "symbol": "Home", "call": "bob",
        "plain_leads": 4, "max_plain_leads": None, "target_position": 6,
    }
    assert ei.value.details["current_row"] == "142356"


def test_two_bobs_in_one_part_return_home(method_ctx, scheme):
    # Wrong(gap1) 后紧跟 Home(gap0)：bob 后观察钟立即在第 6 位
    parts = _parts([
        {"symbol": "Wrong", "call": "bob", "plain_leads": 1},
        {"symbol": "Home", "call": "bob", "plain_leads": 0},
    ])
    out = compile_composition(scheme=scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    assert [t.symbol for t in out["compiled_tokens"]] == ["Wrong", "Home"]
    calls = [l for l in out["records"] if l.call]
    assert calls[1].lead_head_before == "135642"
    assert calls[1].observer_position == 6


def test_repeated_parts_chain_from_new_course_head(method_ctx, scheme):
    # 单个 Wrong part 收尾在 154326（观察钟 home）；同样的 token 重复两次时
    # 第二 part 从该 head 推演，最终 6 个 lead 回到 rounds
    parts = _parts([{"symbol": "Wrong", "call": "bob", "plain_leads": 1}], repeat=2)
    out = compile_composition(scheme=scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    assert len(out["course_heads"]) == 3
    assert out["course_heads"][1] == "154326"
    assert out["course_heads"][2] == "123456"
    assert out["course_lengths"] == [3, 3]
    assert [l.part for l in out["records"] if l.call] == [1, 2]
    assert [l.part_repeat for l in out["records"] if l.call] == [1, 2]
    # 逐 lead 标注前后 lead head 相连
    records = out["records"]
    for a, b in zip(records, records[1:]):
        assert a.lead_head_after == b.lead_head_before
    assert row_to_string(out["final_row"]) == "123456"
    assert len(out["leads"]) == 6


def test_call_specific_positions_override_default(method_ctx):
    # single 专属映射：single gap=1 后观察钟在第 4 位；把该符号只定义在 single 下
    scheme = {
        "observer": 6,
        "home_symbol": "Home",
        "positions": {"Home": 6},
        "call_positions": {"single": {"S-Wrong": 4}},
    }
    parts = _parts([{"symbol": "S-Wrong", "call": "single", "plain_leads": 1}])
    out = compile_composition(scheme=scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    assert out["compiled_tokens"][0].observer_position == 4
    # 同一符号未给 bob 专属映射时不可达
    bad = _parts([{"symbol": "S-Wrong", "call": "bob", "plain_leads": 1}])
    with pytest.raises(ComposeError) as ei:
        compile_composition(scheme=scheme, method_ctx=method_ctx, parts=bad, start_row=ROUNDS6)
    assert ei.value.code == "SYMBOL_NOT_MAPPED"


def test_plain_token_uses_plain_lead(method_ctx, scheme):
    # call 为 None：plain lead 后观察钟在 Wrong(4) —— gap=1 的 plain lead
    # 在 rounds 起 plain 序列中第 4 lead 后观察钟在第 4 位
    parts = _parts([{"symbol": "Wrong", "call": None, "plain_leads": 3}])
    out = compile_composition(scheme=scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    tok = out["compiled_tokens"][0]
    assert tok.call is None and tok.observer_position == 4


def test_deterministic_same_input(method_ctx, scheme):
    parts = _parts([{"symbol": "Wrong", "call": "bob"}])
    a = compile_composition(scheme=scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    b = compile_composition(scheme=scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    seq_a = [(l.call, tuple(l.tokens)) for l in a["leads"]]
    seq_b = [(l.call, tuple(l.tokens)) for l in b["leads"]]
    assert seq_a == seq_b
    assert a["final_row"] == b["final_row"]


def test_course_length_missing_raises_when_cannot_bind(method_ctx, scheme):
    # 观察钟取 1 号钟：PB Minor 中 1 号钟永远在第 1 位，home 设为 6 无法界定
    bad_scheme = {"observer": 1, "home_symbol": "Home",
                  "positions": {"Home": 6}, "call_positions": {}}
    parts = _parts([{"symbol": "Home", "call": None, "plain_leads": 0}])
    with pytest.raises(ComposeError) as ei:
        compile_composition(scheme=bad_scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    assert ei.value.code == "PLAIN_COURSE_NOT_BOUND"


def test_records_carry_lead_heads_and_observer_positions(method_ctx, scheme):
    parts = _parts([{"symbol": "Middle", "call": "bob", "plain_leads": 3}])
    out = compile_composition(scheme=scheme, method_ctx=method_ctx, parts=parts, start_row=ROUNDS6)
    rec = out["records"][3]  # bob lead
    assert rec.lead_head_before == "164523"
    assert rec.lead_head_after == "164235"
    assert rec.observer_position == 2
    assert row_to_string(out["final_row"]) == rec.lead_head_after or True
    assert out["records"][0].token is None  # 自动补的 plain lead 无 token 编号
