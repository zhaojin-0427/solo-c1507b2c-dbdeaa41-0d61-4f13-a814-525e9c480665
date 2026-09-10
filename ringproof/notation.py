"""Place notation 解析、对称/重复段展开与合法性校验。

记号约定（microsiril / Central Council 风格）：
- ``x`` / ``X`` / ``-`` 表示全交叉（所有相邻对互换），单独构成一个 change；
- 连续数字/字母构成一个 change 的 places，钟号 1-9、0=10、T=11、E=12；
- change 之间可用 ``.`` ``,`` 或空白分隔，也可直接连写（每个 x 开启新 change）；
- 段首 ``&`` 表示回文对称：序列为 ``seq + reverse(seq[:-1])``（末位 change 为对称轴）；
- ``+`` 之后为追加在（对称展开后的）序列末尾的 lead end 记号；
- ``(seq)xN`` 或 ``(seq)*N`` 表示重复段，展开为 N 份。

校验规则：
- 钟号缺失/越界（引用 1..stage 之外的钟）→ BELL_OUT_OF_RANGE；
- 位置冲突（同一 change 内钟号重复）→ DUPLICATE_PLACE；
- 无法由相邻换位产生（非 place 位置无法配成相邻对）→ NOT_ADJACENT。
"""
from __future__ import annotations

MIN_STAGE = 4
MAX_STAGE = 12

BELL_CHARS = "1234567890TE"  # 0=10, T=11, E=12
_CROSS = {"x", "X", "-"}
_DIGITS = "0123456789"
_SEPARATORS = {".", ",", ";"}


class NotationError(ValueError):
    """记号非法。``code`` 为机器可读错误码，``message`` 为中文说明。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def bell_from_char(ch: str) -> int:
    if ch in _DIGITS:
        n = int(ch)
        return 10 if n == 0 else n
    if ch in "Tt":
        return 11
    if ch in "Ee":
        return 12
    raise NotationError("INVALID_CHAR", f"无法识别的钟号字符: {ch!r}")


def char_from_bell(n: int) -> str:
    if not 1 <= n <= MAX_STAGE:
        raise NotationError("BELL_OUT_OF_RANGE", f"钟号 {n} 超出 1..{MAX_STAGE} 范围")
    return BELL_CHARS[n - 1]


def row_to_string(row: tuple[int, ...]) -> str:
    return "".join(char_from_bell(b) for b in row)


def validate_stage(stage: int) -> None:
    if not MIN_STAGE <= stage <= MAX_STAGE:
        raise NotationError(
            "STAGE_OUT_OF_RANGE",
            f"钟数须为 {MIN_STAGE}~{MAX_STAGE} 口，收到 {stage}",
        )


def parse_row(text: str, stage: int) -> tuple[int, ...]:
    """解析起始排列，须为 1..stage 的完整排列（无缺号、无重复）。"""
    validate_stage(stage)
    s = text.strip().upper()
    if len(s) != stage:
        raise NotationError(
            "ROW_LENGTH_MISMATCH",
            f"起始排列长度 {len(s)} 与钟数 {stage} 不符: {text!r}",
        )
    row = tuple(bell_from_char(c) for c in s)
    if sorted(row) != list(range(1, stage + 1)):
        raise NotationError(
            "ROW_NOT_PERMUTATION",
            f"起始排列 {text!r} 不是 1..{stage} 的完整排列（存在钟号缺失或位置冲突）",
        )
    return row


def parse_change_token(token: str, stage: int) -> frozenset[int]:
    """把单个 change token 解析为 places 集合（含隐含外部位）并校验。

    隐含外部位规则：最前 place 为偶数位则补 1 位；最后 place 与 stage
    之间间隔为奇数则补 stage 位。随后检查所有非 place 位置能配成相邻对。
    """
    validate_stage(stage)
    if token == "x":
        if stage % 2:
            raise NotationError(
                "NOT_ADJACENT",
                f"奇数口钟（{stage}）无法全体交叉（x），必有一口钟无处配对",
            )
        return frozenset()
    places: list[int] = []
    for ch in token:
        b = bell_from_char(ch)
        if b > stage:
            raise NotationError(
                "BELL_OUT_OF_RANGE",
                f"钟号缺失/越界：记号 {token!r} 中的钟号 {b} 超出 {stage} 口钟范围",
            )
        if b in places:
            raise NotationError(
                "DUPLICATE_PLACE",
                f"位置冲突：change {token!r} 中钟号 {b} 重复出现",
            )
        places.append(b)
    places.sort()
    if places[0] % 2 == 0:
        places.insert(0, 1)
    if (stage - places[-1]) % 2 == 1:
        places.append(stage)
    for a, b in zip(places, places[1:]):
        if (b - a) % 2 == 0:
            raise NotationError(
                "NOT_ADJACENT",
                f"change {token!r} 无法由相邻换位产生：{a} 位与 {b} 位之间"
                f"留有奇数个未配对位置",
            )
    return frozenset(places)


def _tokenize_seq(s: str) -> list[str]:
    """把一段记号切分为 change token 列表，展开 ``(…)xN`` / ``(…)*N`` 重复段。"""
    tokens: list[str] = []
    cur: list[str] = []
    i, n = 0, len(s)

    def flush() -> None:
        if cur:
            tokens.append("".join(cur))
            cur.clear()

    while i < n:
        ch = s[i]
        if ch in _SEPARATORS or ch.isspace():
            flush()
            i += 1
        elif ch in _CROSS:
            flush()
            tokens.append("x")
            i += 1
        elif ch in _DIGITS or ch in "TtEe":
            cur.append(ch.upper())
            i += 1
        elif ch == "(":
            flush()
            depth, j = 1, i + 1
            while j < n and depth:
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                j += 1
            if depth:
                raise NotationError("SYNTAX", "重复段括号未闭合")
            inner = _tokenize_seq(s[i + 1 : j - 1])
            if not inner:
                raise NotationError("SYNTAX", "重复段 () 内容为空")
            repeat, i = 1, j
            if i < n and s[i] in "xX*":
                i += 1
                m = i
                while m < n and s[m] in _DIGITS:
                    m += 1
                if m == i:
                    raise NotationError("SYNTAX", "重复段缺少重复次数，如 (x16)x3")
                repeat = int(s[i:m])
                if repeat < 1:
                    raise NotationError("SYNTAX", "重复次数须 ≥ 1")
                i = m
            tokens.extend(inner * repeat)
        elif ch == ")":
            raise NotationError("SYNTAX", "存在多余的右括号 )")
        elif ch in "&+":
            raise NotationError(
                "SYNTAX", f"符号 {ch!r} 位置非法：& 只能位于段首，+ 用于分隔 lead end 段"
            )
        else:
            raise NotationError("INVALID_CHAR", f"无法识别的字符: {ch!r}")
    flush()
    return tokens


def _split_sections(raw: str) -> tuple[bool, str, str | None]:
    s = raw.strip()
    if not s:
        raise NotationError("EMPTY_NOTATION", "place notation 为空")
    symmetric = False
    if s.startswith("&"):
        symmetric = True
        s = s[1:].strip()
    main, sep, lead_end = s.partition("+")
    if sep:
        if not lead_end.strip():
            raise NotationError("SYNTAX", "+ 后缺少 lead end 记号")
        if "+" in lead_end:
            raise NotationError("SYNTAX", "记号中只允许一个 + 段")
    return symmetric, main, (lead_end if sep else None)


def expand_notation(raw: str, stage: int) -> tuple[list[str], list[frozenset[int]]]:
    """展开对称记号与重复段，返回 (规范化 token 列表, 每个 change 的 places 集合)。"""
    validate_stage(stage)
    symmetric, main_raw, lead_end_raw = _split_sections(raw)
    tokens = _tokenize_seq(main_raw)
    if not tokens:
        raise NotationError("EMPTY_NOTATION", "place notation 主体为空")
    if symmetric:
        tokens = tokens + tokens[-2::-1]
    if lead_end_raw is not None:
        lead_tokens = _tokenize_seq(lead_end_raw)
        if not lead_tokens:
            raise NotationError("EMPTY_NOTATION", "+ 后 lead end 记号为空")
        tokens += lead_tokens
    changes = [parse_change_token(tok, stage) for tok in tokens]
    return tokens, changes


def normalize_notation(raw: str, stage: int) -> str:
    """返回规范化记号：完全展开后以 ``.`` 连接的 token 序列。"""
    tokens, _ = expand_notation(raw, stage)
    return ".".join(tokens)
