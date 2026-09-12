"""all-the-work 覆盖分析：按每个 lead 开始前的实际排列生成
bell×method×place-bell 覆盖矩阵。

place-bell 语义：一口钟在某个 lead 中敲的是“第 p 号 place bell”，当且仅当
该 lead 开始前的排列（lead head）中这口钟位于第 p 个位置（1 起）。因此 call
改变后续 lead head 后，各钟的 place-bell 归属自动按实际位置变化，无需对
call 做任何特殊处理。

方案（spec）形如：
    {
        "stage": 6,
        "working_bells": [2, 3, 4, 5, 6],
        "methods": [{"id": ..., "version": ..., "name": ...}],
        "cells": [{"bell", "method", "method_version", "place_bell", "min_leads"}],
    }
cell 的 place_bell 为具体类别（创建时已把“全部类别”展开为 1..stage）。

证明结果（``engine.prove_rows`` 的返回）给出每个 lead 结束行（lead_heads）
与逐 change 的方法 id/版本，lead L 开始前的排列即起始 row（L=1）或
lead_heads[L-2]（L>1）。只有 (方法 id, 版本) 均与方案冻结方法一致的 lead
才计入矩阵；其余 lead 汇总为 unmatched_methods。
"""
from __future__ import annotations

from .notation import parse_row

_COMPLETION_DIGITS = 6  # 完成率输出舍入位数，保证 JSON 整洁且确定性


def _round(value: float) -> float:
    return round(value, _COMPLETION_DIGITS)


def _method_meta(spec: dict, method_id: str | None, method_version: int | None) -> dict:
    for m in spec["methods"]:
        if m["id"] == method_id and m["version"] == method_version:
            return {"id": m["id"], "version": m["version"], "name": m["name"]}
    return {"id": method_id, "version": method_version, "name": None}


def analyze_coverage(spec: dict, proof: dict) -> dict:
    """依据方案与一次证明结果计算 all-the-work 覆盖。结果对同一输入完全确定。"""
    stage = spec["stage"]
    working = list(spec["working_bells"])
    method_index = {(m["id"], m["version"]) for m in spec["methods"]}
    method_order = {key: i for i, key in enumerate(
        (m["id"], m["version"]) for m in spec["methods"]
    )}
    bell_order = {b: i for i, b in enumerate(working)}

    # 每个 lead 实际使用的 (方法 id, 版本)：取该 lead 首个 change 的来源标注
    version_by_lead: dict[int, tuple[str | None, int | None]] = {}
    for ev in proof["events"]:
        version_by_lead.setdefault(ev["lead"], (ev["method_id"], ev["method_version"]))

    heads = [parse_row(proof["start_row"], stage)] + [
        parse_row(h, stage) for h in proof["lead_heads"][:-1]
    ]
    lead_methods = proof["lead_methods"]

    # observed[(bell, method_id, method_version, place_bell)] = 出现过的 lead 序号
    observed: dict[tuple, list[int]] = {}
    unmatched: dict[tuple[str | None, int | None], list[int]] = {}
    counted_leads = 0
    for lead_no, head in enumerate(heads, 1):
        mid = lead_methods[lead_no - 1]
        mver = version_by_lead.get(lead_no, (mid, None))[1]
        key = (mid, mver)
        if key not in method_index:
            unmatched.setdefault(key, []).append(lead_no)
            continue
        counted_leads += 1
        for bell in working:
            place_bell = head.index(bell) + 1  # lead 开始前钟的实际位置
            observed.setdefault((bell, mid, mver, place_bell), []).append(lead_no)

    def _entry_stats(leads: list[int]) -> dict:
        gaps = [b - a for a, b in zip(leads, leads[1:])]
        if not leads:
            return {
                "count": 0,
                "first_lead": None,
                "last_lead": None,
                "gaps": [],
                "longest_gap": None,
            }
        return {
            "count": len(leads),
            "first_lead": leads[0],
            "last_lead": leads[-1],
            "gaps": gaps,
            "longest_gap": max(gaps) if gaps else None,
        }

    # ---- 要求格（方案 cells）与观测连接 ----
    required_keys = {
        (c["bell"], c["method"], c["method_version"], c["place_bell"]) for c in spec["cells"]
    }
    cells: list[dict] = []
    required_leads = 0
    capped_leads = 0
    satisfied_cells = 0
    for c in sorted(
        spec["cells"],
        key=lambda c: (
            method_order[(c["method"], c["method_version"])],
            bell_order[c["bell"]],
            c["place_bell"],
        ),
    ):
        key = (c["bell"], c["method"], c["method_version"], c["place_bell"])
        stats = _entry_stats(observed.get(key, []))
        deficit = max(0, c["min_leads"] - stats["count"])
        satisfied = deficit == 0
        required_leads += c["min_leads"]
        capped_leads += min(stats["count"], c["min_leads"])
        satisfied_cells += 1 if satisfied else 0
        cells.append({
            "bell": c["bell"],
            "method": _method_meta(spec, c["method"], c["method_version"]),
            "place_bell": c["place_bell"],
            "min_leads": c["min_leads"],
            **stats,
            "deficit": deficit,
            "satisfied": satisfied,
        })

    # ---- 完整观测矩阵（含方案未要求的 place-bell 类别）----
    matrix: list[dict] = []
    extra_observations = 0
    for key, leads in sorted(
        observed.items(),
        key=lambda kv: (
            method_order.get((kv[0][1], kv[0][2]), len(method_order)),
            bell_order.get(kv[0][0], len(bell_order)),
            kv[0][3],
        ),
    ):
        bell, mid, mver, place_bell = key
        required = key in required_keys
        extra_observations += 0 if required else len(leads)
        matrix.append({
            "bell": bell,
            "method": _method_meta(spec, mid, mver),
            "place_bell": place_bell,
            "required": required,
            **_entry_stats(leads),
        })

    # ---- 各钟完成率 ----
    bells: list[dict] = []
    for bell in working:
        bell_cells = [c for c in cells if c["bell"] == bell]
        req = sum(c["min_leads"] for c in bell_cells)
        obs = sum(c["count"] for c in bell_cells)
        cap = sum(min(c["count"], c["min_leads"]) for c in bell_cells)
        done = sum(1 for c in bell_cells if c["satisfied"])
        completion = _round(cap / req) if req else 1.0
        bells.append({
            "bell": bell,
            "required_leads": req,
            "observed_leads": obs,
            "capped_leads": cap,
            "completion": completion,
            "cells_total": len(bell_cells),
            "cells_satisfied": done,
            "cell_completion": _round(done / len(bell_cells)) if bell_cells else 1.0,
            "satisfied": done == len(bell_cells),
        })
    completions = [b["completion"] for b in bells] or [1.0]
    balance = _round(max(completions) - min(completions))

    completion = _round(capped_leads / required_leads) if required_leads else 1.0
    observed_leads = sum(c["count"] for c in cells)
    deficits = [
        {
            "bell": c["bell"],
            "method": c["method"],
            "place_bell": c["place_bell"],
            "count": c["count"],
            "min_leads": c["min_leads"],
            "deficit": c["deficit"],
        }
        for c in cells
        if not c["satisfied"]
    ]
    missing = [
        {
            "bell": c["bell"],
            "method": c["method"],
            "place_bell": c["place_bell"],
            "min_leads": c["min_leads"],
        }
        for c in cells
        if c["count"] == 0
    ]
    unmatched_methods = [
        {
            "method": {"id": mid, "version": mver, "name": None},
            "lead_count": len(leads),
            "leads": leads,
        }
        for (mid, mver), leads in sorted(
            unmatched.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or 0)
        )
    ]
    return {
        "stage": stage,
        "working_bells": working,
        "methods": [
            {"id": m["id"], "version": m["version"], "name": m["name"]}
            for m in spec["methods"]
        ],
        "total_leads": proof["total_leads"],
        "counted_leads": counted_leads,
        "required_leads": required_leads,
        "observed_leads": observed_leads,
        "capped_leads": capped_leads,
        "completion": completion,
        "cell_completion": _round(satisfied_cells / len(cells)) if cells else 1.0,
        "balance": balance,
        "full_coverage": satisfied_cells == len(cells) and len(cells) > 0,
        "cells": cells,
        "bells": bells,
        "matrix": matrix,
        "extra_observations": extra_observations,
        "deficits": deficits,
        "missing_cells": missing,
        "unmatched_methods": unmatched_methods,
    }


def coverage_summary(analysis: dict) -> dict:
    """枚举时每变体附带的紧凑覆盖指标。"""
    return {
        "completion": analysis["completion"],
        "cell_completion": analysis["cell_completion"],
        "full_coverage": analysis["full_coverage"],
        "balance": analysis["balance"],
        "required_leads": analysis["required_leads"],
        "observed_leads": analysis["observed_leads"],
        "missing_cells": len(analysis["missing_cells"]),
        "unsatisfied_cells": len(analysis["deficits"]),
    }
