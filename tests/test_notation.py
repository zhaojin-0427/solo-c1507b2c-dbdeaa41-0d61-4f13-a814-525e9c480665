"""place notation 解析、展开与校验测试。"""
import pytest

from ringproof.notation import (
    NotationError,
    expand_notation,
    normalize_notation,
    parse_change_token,
    parse_row,
)


def test_plain_bob_minor_tokens():
    tokens, changes = expand_notation("x16x16x16x16x16x12", 6)
    assert tokens == ["x", "16", "x", "16", "x", "16", "x", "16", "x", "16", "x", "12"]
    assert changes[0] == frozenset()
    assert changes[1] == frozenset({1, 6})
    assert changes[-1] == frozenset({1, 2})


def test_symmetric_expansion_cambridge_minor():
    # Cambridge Surprise Minor：& 回文对称 + lead end，展开为 24 个 change
    tokens, _ = expand_notation("&x3x4x2x3x4x5,+2", 6)
    assert len(tokens) == 24
    half = tokens[:12]
    assert tokens[12:23] == half[-2::-1]  # 第二段为第一段的镜像（对称轴不重复）
    assert tokens[-1] == "2"


def test_repeat_group_expansion():
    tokens, _ = expand_notation("(x16)x5 x12", 6)
    assert tokens == ["x", "16"] * 5 + ["x", "12"]
    tokens2, _ = expand_notation("(x.16)*3.x.12", 6)
    assert tokens2 == ["x", "16"] * 3 + ["x", "12"]


def test_implied_external_places():
    assert parse_change_token("2", 6) == frozenset({1, 2})       # 偶数首 place 补 1 位
    assert parse_change_token("14", 6) == frozenset({1, 4})
    assert parse_change_token("36", 6) == frozenset({3, 6})
    assert parse_change_token("5", 6) == frozenset({5, 6})       # 尾部间隔为奇补 6 位
    assert parse_change_token("3", 5) == frozenset({3})
    assert parse_change_token("1234", 6) == frozenset({1, 2, 3, 4})
    assert parse_change_token("x", 8) == frozenset()


def test_bell_letters_for_high_stages():
    assert parse_change_token("0", 12) == frozenset({1, 10})     # 0 = 10 号钟
    assert parse_change_token("TE", 12) == frozenset({11, 12})
    assert parse_change_token("te", 12) == frozenset({11, 12})


def test_bell_out_of_range_rejected():
    with pytest.raises(NotationError) as e:
        expand_notation("x17x16", 6)
    assert e.value.code == "BELL_OUT_OF_RANGE"


def test_duplicate_place_rejected():
    with pytest.raises(NotationError) as e:
        expand_notation("x113x16", 6)
    assert e.value.code == "DUPLICATE_PLACE"


def test_non_adjacent_change_rejected():
    with pytest.raises(NotationError) as e:
        expand_notation("13", 4)  # 2 位与 4 位落单，无法配成相邻对
    assert e.value.code == "NOT_ADJACENT"
    with pytest.raises(NotationError) as e2:
        expand_notation("x", 5)  # 奇数口钟无法全交叉
    assert e2.value.code == "NOT_ADJACENT"


def test_stage_bounds():
    with pytest.raises(NotationError) as e:
        expand_notation("x1", 3)
    assert e.value.code == "STAGE_OUT_OF_RANGE"
    with pytest.raises(NotationError):
        expand_notation("x1", 13)


def test_syntax_errors():
    for bad in ["", "(x16", "x16)", "()x3", "(x16)x", "&", "x16+"]:
        with pytest.raises(NotationError):
            expand_notation(bad, 6)


def test_normalize_notation():
    assert normalize_notation("(x16)x2 x12", 6) == "x.16.x.16.x.12"
    assert normalize_notation("&x3x4,+2", 4) == "x.3.x.4.x.3.x.2"


def test_parse_row():
    assert parse_row("123456", 6) == (1, 2, 3, 4, 5, 6)
    assert parse_row("654321", 6) == (6, 5, 4, 3, 2, 1)
    with pytest.raises(NotationError) as e:
        parse_row("112345", 6)
    assert e.value.code == "ROW_NOT_PERMUTATION"
    with pytest.raises(NotationError) as e2:
        parse_row("12345", 6)
    assert e2.value.code == "ROW_LENGTH_MISMATCH"
