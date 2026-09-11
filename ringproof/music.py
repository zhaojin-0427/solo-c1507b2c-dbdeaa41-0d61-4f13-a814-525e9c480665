"""音乐模式评分：精确 row、前/后端连续钟组、指定钟位置三类规则。

规则语义（对单个 row 判定）：
- ``row``：整行与指定排列完全一致，命中 1 次；
- ``run``：指定的正序或逆序连续钟组出现在 row 的前端（``front``）
  或后端（``back``），命中 1 次；
- ``positions``：若干 (钟, 位置) 对，每满足一对计 1 次命中；
  ``allow_overlap=False`` 时每行至多计 1 次（不叠加），为 True 时
  各对独立叠加；``max_per_row`` 限制每条规则从单个 row 计分的
  命中次数上限。

起始 row（0 号 change 的 row）仅当方案 ``score_start_row`` 为真时计分；
末尾回到 rounds 的最后一 row 仅当 ``score_final_rounds`` 为真时计分；
其余 row（含中间回到的 rounds）一律计分。
"""
from __future__ import annotations

from .engine import _provenance
from .notation import NotationError, char_from_bell, parse_row, row_to_string, validate_stage

RULE_TYPES = ("row", "run", "positions")


def _check_bell(bell: int, stage: int, rule_name: str) -> None:
    if not 1 <= bell <= stage:
        raise NotationError(
            "BELL_OUT_OF_RANGE",
            f"规则 {rule_name!r} 的钟号 {bell} 超出 {stage} 口钟范围（钟号缺失/越界）",
        )


def validate_rules(stage: int, rules: list[dict]) -> list[dict]:
    """校验并规范化规则列表；不完整排列、越界钟号及与钟数不符的规则
    在此抛出 NotationError，方案不得保存。返回的规范化规则可直接用于评分。"""
    validate_stage(stage)
    if not rules:
        raise NotationError("EMPTY_RULES", "评分方案至少包含一条规则")
    names: set[str] = set()
    normalized: list[dict] = []
    for rule in rules:
        name = rule["name"]
        if name in names:
            raise NotationError(
                "DUPLICATE_RULE_NAME", f"规则名称重复: {name!r}（同一方案内规则名须唯一）"
            )
        names.add(name)
        rtype = rule.get("type")
        entry = {
            "name": name,
            "type": rtype,
            "points": rule["points"],
            "allow_overlap": rule["allow_overlap"],
            "max_per_row": rule["max_per_row"],
        }
        if rtype == "row":
            # parse_row 拒绝：长度与钟数不符、非完整排列、越界钟号
            entry["row"] = row_to_string(parse_row(rule["row"], stage))
        elif rtype == "run":
            bells = list(rule["bells"])
            if not bells:
                raise NotationError("EMPTY_RUN", f"规则 {name!r} 的连续钟组为空")
            for b in bells:
                _check_bell(b, stage, name)
            if len(set(bells)) != len(bells):
                raise NotationError(
                    "DUPLICATE_BELL", f"规则 {name!r} 的连续钟组存在重复钟号: {bells}"
                )
            ascending = all(b + 1 == nxt for b, nxt in zip(bells, bells[1:]))
            descending = all(b - 1 == nxt for b, nxt in zip(bells, bells[1:]))
            if len(bells) > 1 and not (ascending or descending):
                raise NotationError(
                    "RUN_NOT_CONSECUTIVE",
                    f"规则 {name!r} 的钟组 {bells} 不是正序或逆序的连续钟组",
                )
            if rule.get("position") not in ("front", "back"):
                raise NotationError(
                    "RUN_POSITION", f"规则 {name!r} 的 position 须为 'front' 或 'back'"
                )
            entry["bells"] = bells
            entry["position"] = rule["position"]
        elif rtype == "positions":
            pairs = rule["positions"]
            if not pairs:
                raise NotationError(
                    "EMPTY_POSITIONS", f"规则 {name!r} 未指定任何钟位置"
                )
            seen: set[tuple[int, int]] = set()
            norm_pairs = []
            for p in pairs:
                bell, pos = p["bell"], p["position"]
                _check_bell(bell, stage, name)
                if not 1 <= pos <= stage:
                    raise NotationError(
                        "POSITION_OUT_OF_RANGE",
                        f"规则 {name!r} 的位置 {pos} 超出 1..{stage} 范围（与钟数不符）",
                    )
                if (bell, pos) in seen:
                    raise NotationError(
                        "DUPLICATE_POSITION",
                        f"规则 {name!r} 存在重复的 (钟 {bell}, 位置 {pos}) 对",
                    )
                seen.add((bell, pos))
                norm_pairs.append({"bell": bell, "position": pos})
            entry["positions"] = norm_pairs
        else:
            raise NotationError(
                "UNKNOWN_RULE_TYPE",
                f"未知规则类型: {rtype!r}（须为 {'/'.join(RULE_TYPES)} 之一）",
            )
        normalized.append(entry)
    return normalized


def _compile(rule: dict) -> dict:
    """把规范化规则编译为可直接对 row 字符串判定的形式。"""
    rtype = rule["type"]
    compiled = dict(rule)
    if rtype == "run":
        compiled["pattern"] = "".join(char_from_bell(b) for b in rule["bells"])
    elif rtype == "positions":
        compiled["pairs"] = [
            (p["position"] - 1, char_from_bell(p["bell"])) for p in rule["positions"]
        ]
    return compiled


def _rule_hits(rule: dict, row: str) -> int:
    """一条规则在一个 row 上的命中次数（已应用 allow_overlap 叠加规则）。"""
    rtype = rule["type"]
    if rtype == "row":
        return 1 if row == rule["row"] else 0
    if rtype == "run":
        pattern = rule["pattern"]
        hit = (
            row.startswith(pattern)
            if rule["position"] == "front"
            else row.endswith(pattern)
        )
        return 1 if hit else 0
    # positions：每满足一个 (钟, 位置) 对计 1 次；不允许叠加时每行至多 1 次
    hits = sum(1 for idx, ch in rule["pairs"] if row[idx] == ch)
    if not rule["allow_overlap"]:
        hits = min(hits, 1)
    return hits


def score_rows(
    row_strings: list[str],
    events: list[dict],
    rules: list[dict],
    score_start_row: bool = False,
    score_final_rounds: bool = False,
) -> dict:
    """对逐 row 序列按评分方案计分。结果对同一输入完全确定。

    ``row_strings`` 含起始 row（下标 0）与每个 change 产生的 row；
    ``events[i-1]`` 为第 i 号 change 的来源（用于首次/最高分 row 追溯）。
    """
    stage = len(row_strings[0])
    rounds = "".join(char_from_bell(b) for b in range(1, stage + 1))
    n = len(row_strings) - 1
    compiled = [_compile(r) for r in rules]

    rule_hits = [0] * len(compiled)
    rule_score = [0] * len(compiled)
    total_score = 0
    rows_scored = 0
    first_scoring: dict | None = None
    best_row: dict | None = None

    for i, row in enumerate(row_strings):
        if i == 0 and not score_start_row:
            continue  # 起始 row 不计分（方案指定）
        if i == n and row == rounds and not score_final_rounds:
            continue  # 末尾 rounds 不计分（方案指定）
        rows_scored += 1
        row_score = 0
        for ri, rule in enumerate(compiled):
            hits = _rule_hits(rule, row)
            if not hits:
                continue
            cap = rule["max_per_row"]
            if cap is not None:
                hits = min(hits, cap)  # 单个 row 的计分上限
            earned = hits * rule["points"]
            rule_hits[ri] += hits
            rule_score[ri] += earned
            row_score += earned
        total_score += row_score
        if row_score != 0:
            info = {"row": row, "score": row_score, **_provenance(i, events)}
            if first_scoring is None:
                first_scoring = info
            if best_row is None or row_score > best_row["score"]:
                best_row = info

    return {
        "total_score": total_score,
        "total_hits": sum(rule_hits),
        "rows_scored": rows_scored,
        "rules": [
            {
                "name": r["name"],
                "type": r["type"],
                "points": r["points"],
                "hits": rule_hits[i],
                "score": rule_score[i],
            }
            for i, r in enumerate(compiled)
        ],
        "first_scoring_row": first_scoring,
        "highest_scoring_row": best_row,
    }
