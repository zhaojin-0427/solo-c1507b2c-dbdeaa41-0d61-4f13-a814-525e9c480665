"""FastAPI 应用：方法/touch 的不可变版本管理与 touch 序列证明。

支持单方法 touch 与多方法拼接 touch：后者在创建时引用多个同钟数方法的
不可变版本，每个 lead 可固定或候选方法，并可配置各方法配额与相邻转换规则。
证明与枚举可引用不可变的音乐评分方案（精确 row / 前后端连续钟组 /
指定钟位置三类规则），返回总分、各规则命中数与首次/最高分 row 来源。
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from itertools import product

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import __version__
from .engine import TouchError, build_lead, prove_rows, transition_ok
from .feasibility import joint_assignment_feasible, quota_feasible
from .music import score_rows, validate_rules
from .notation import NotationError, expand_notation, parse_row, row_to_string
from .schemas import (
    EnumerateRequest,
    MethodCreate,
    MethodRef,
    MusicSchemeCreate,
    ProveRequest,
    TouchCreate,
)
from .storage import Storage, canonical_hash, utcnow

MAX_LEADS = 5000  # 单个 touch 展开后的 lead 数上限


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def create_app(db_path: str | None = None) -> FastAPI:
    storage = Storage(db_path or os.environ.get("RINGPROOF_DB", "ringproof.db"))
    app = FastAPI(
        title="RingProof — 变换鸣钟 touch 序列证明 API",
        version=__version__,
        description="供变换鸣钟作曲者校核 touch：展开记号、生成逐 row 序列、"
        "检测重复并定位来源、枚举 call×method 变体、多方法拼接编排。"
        "方法与 touch 以不可变版本保存。",
    )

    # ---------- 错误处理 ----------
    @app.exception_handler(NotationError)
    async def notation_error_handler(_: Request, exc: NotationError):
        return JSONResponse(status_code=422, content={"detail": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(TouchError)
    async def touch_error_handler(_: Request, exc: TouchError):
        return JSONResponse(status_code=422, content={"detail": {"code": exc.code, "message": exc.message}})

    # ---------- 方法 ----------
    @app.post("/methods", status_code=201)
    def create_method(body: MethodCreate):
        """创建方法的不可变版本；同一 id 重复提交会递增版本号。"""
        tokens, changes = expand_notation(body.notation, body.stage)
        method_id = body.id or _new_id()
        version = storage.next_method_version(method_id)
        input_hash = canonical_hash(
            {"name": body.name, "stage": body.stage, "notation": body.notation}
        )
        rec = {
            "id": method_id,
            "version": version,
            "name": body.name,
            "stage": body.stage,
            "notation_raw": body.notation,
            "notation_normalized": ".".join(tokens),
            "changes_json": json.dumps(
                {"tokens": tokens, "places": [sorted(p) for p in changes]}
            ),
            "input_hash": input_hash,
            "created_at": utcnow(),
        }
        try:
            storage.insert_method(rec)
        except sqlite3.IntegrityError:
            return JSONResponse(status_code=409, content={"detail": "该 (id, version) 已存在，版本不可变"})
        return _method_detail(rec)

    @app.get("/methods")
    def list_methods():
        return {"methods": storage.list_methods()}

    @app.get("/methods/{method_id}")
    def list_method_versions(method_id: str):
        versions = [m for m in storage.list_methods() if m["id"] == method_id]
        if not versions:
            return JSONResponse(status_code=404, content={"detail": f"方法不存在: {method_id}"})
        return {"id": method_id, "versions": versions}

    @app.get("/methods/{method_id}/versions/{version}")
    def get_method(method_id: str, version: int):
        rec = storage.get_method(method_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"方法版本不存在: {method_id} v{version}"})
        return _method_detail(rec)

    # ---------- 音乐评分方案 ----------
    @app.post("/music-schemes", status_code=201)
    def create_music_scheme(body: MusicSchemeCreate):
        """创建评分方案的不可变版本；同一 id 重复提交会递增版本号。

        不完整排列、越界钟号及与钟数不符的规则在此被拒绝（不落库）。"""
        rules = validate_rules(body.stage, [r.model_dump() for r in body.rules])
        scheme_id = body.id or _new_id()
        version = storage.next_music_version(scheme_id)
        spec = {
            "name": body.name,
            "stage": body.stage,
            "rules": rules,
            "score_start_row": body.score_start_row,
            "score_final_rounds": body.score_final_rounds,
        }
        rec = {
            "id": scheme_id,
            "version": version,
            "name": body.name,
            "stage": body.stage,
            "spec_json": json.dumps(spec, ensure_ascii=False, sort_keys=True),
            "input_hash": canonical_hash(spec),
            "created_at": utcnow(),
        }
        try:
            storage.insert_music_scheme(rec)
        except sqlite3.IntegrityError:
            return JSONResponse(status_code=409, content={"detail": "该 (id, version) 已存在，版本不可变"})
        return _music_detail(rec)

    @app.get("/music-schemes")
    def list_music_schemes():
        return {"music_schemes": storage.list_music_schemes()}

    @app.get("/music-schemes/{scheme_id}")
    def list_music_scheme_versions(scheme_id: str):
        versions = [m for m in storage.list_music_schemes() if m["id"] == scheme_id]
        if not versions:
            return JSONResponse(status_code=404, content={"detail": f"评分方案不存在: {scheme_id}"})
        return {"id": scheme_id, "versions": versions}

    @app.get("/music-schemes/{scheme_id}/versions/{version}")
    def get_music_scheme(scheme_id: str, version: int):
        rec = storage.get_music_scheme(scheme_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"评分方案版本不存在: {scheme_id} v{version}"})
        return _music_detail(rec)

    # ---------- touch ----------
    @app.post("/touches", status_code=201)
    def create_touch(body: TouchCreate):
        """创建 touch 的不可变版本（冻结所引用的全部方法版本）。"""
        refs = (
            body.methods
            if body.methods is not None
            else [MethodRef(id=body.method_id, version=body.method_version)]
        )
        methods = []
        for ref in refs:
            mv = ref.version or storage.latest_method_version(ref.id)
            method = storage.get_method(ref.id, mv) if mv else None
            if not method:
                detail = (
                    f"方法不存在: {ref.id}"
                    if ref.version is None
                    else f"方法版本不存在: {ref.id} v{ref.version}"
                )
                return JSONResponse(status_code=404, content={"detail": detail})
            methods.append(method)
        ctx = _build_context(body, methods)

        touch_id = body.id or _new_id()
        version = storage.next_touch_version(touch_id)
        input_hash = canonical_hash(
            {
                "methods": [
                    {"id": m["id"], "version": m["version"], "hash": m["input_hash"]}
                    for m in methods
                ],
                "stage": ctx["stage"],
                "start_row": row_to_string(ctx["start_row"]),
                "calls": {
                    k: {"notation": c["notation"], "replace": c["replace"]}
                    for k, c in ctx["call_defs"].items()
                },
                "sequence": [g.model_dump() for g in body.sequence],
                "max_calls": body.max_calls,
                "method_quotas": ctx["quotas"],
                "allowed_transitions": body.allowed_transitions,
                "forbidden_transitions": body.forbidden_transitions,
            }
        )
        normalized = {
            "methods": _method_summaries(methods),
            "start_row": row_to_string(ctx["start_row"]),
            "calls": {
                k: {"normalized": c["normalized"], "replace": c["replace"]}
                for k, c in ctx["call_defs"].items()
            },
            "leads": ctx["leads_summary"],
            "total_leads": len(ctx["flat_leads"]),
            "method_quotas": ctx["quotas"],
            "allowed_transitions": (
                sorted(ctx["allowed"]) if ctx["allowed"] is not None else None
            ),
            "forbidden_transitions": sorted(ctx["forbidden"]),
        }
        rec = {
            "id": touch_id,
            "version": version,
            "method_id": methods[0]["id"],
            "method_version": methods[0]["version"],
            "stage": ctx["stage"],
            "spec_json": json.dumps(body.model_dump(), ensure_ascii=False, sort_keys=True),
            "normalized_json": json.dumps(normalized, ensure_ascii=False),
            "input_hash": input_hash,
            "created_at": utcnow(),
        }
        try:
            storage.insert_touch(rec)
        except sqlite3.IntegrityError:
            return JSONResponse(status_code=409, content={"detail": "该 (id, version) 已存在，版本不可变"})
        return _touch_detail(rec, methods[0])

    @app.get("/touches")
    def list_touches():
        return {"touches": storage.list_touches()}

    @app.get("/touches/{touch_id}")
    def list_touch_versions(touch_id: str):
        versions = [t for t in storage.list_touches() if t["id"] == touch_id]
        if not versions:
            return JSONResponse(status_code=404, content={"detail": f"touch 不存在: {touch_id}"})
        return {"id": touch_id, "versions": versions}

    @app.get("/touches/{touch_id}/versions/{version}")
    def get_touch(touch_id: str, version: int):
        rec = storage.get_touch(touch_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"touch 版本不存在: {touch_id} v{version}"})
        method = storage.get_method(rec["method_id"], rec["method_version"])
        return _touch_detail(rec, method)

    @app.get("/touches/{touch_id}/versions/{version}/rows")
    def get_touch_rows(
        touch_id: str,
        version: int,
        offset: int = Query(0, ge=0),
        limit: int = Query(500, ge=1, le=10000),
    ):
        """逐行来源：每个 change 的记号、方法 id/版本、lead 序号、call 与拼接标记。"""
        rec = storage.get_touch(touch_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"touch 版本不存在: {touch_id} v{version}"})
        ctx = _context_from_record(rec)
        leads = _resolve_leads(ctx)  # 含 choice 槽位时抛 422
        result = prove_rows(
            ctx["stage"],
            ctx["method"]["name"],
            leads,
            ctx["start_row"],
            ctx["max_calls"],
            ctx["quotas"],
            ctx["allowed"],
            ctx["forbidden"],
        )
        events = result["events"][offset : offset + limit]
        return {
            "touch_id": touch_id,
            "touch_version": version,
            "input_hash": rec["input_hash"],
            "start_row": result["start_row"],
            "total_changes": result["total_changes"],
            "splices": result["splices"],
            "methods": _ctx_method_summaries(ctx),
            "offset": offset,
            "limit": limit,
            "rows": events,
        }

    # ---------- 证明 ----------
    @app.post("/touches/{touch_id}/versions/{version}/prove")
    def prove_touch(touch_id: str, version: int, body: ProveRequest | None = None):
        """序列证明；可引用音乐评分方案。结果按版本缓存（含方案版本），
        同一版本重复证明结果一致。"""
        rec = storage.get_touch(touch_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"touch 版本不存在: {touch_id} v{version}"})
        scheme = None
        music_id, music_version = "", 0
        if body and body.music:
            scheme, err = _resolve_music(touch_id, version, rec["stage"], body.music)
            if err is not None:
                return err
            music_id, music_version = scheme["id"], scheme["version"]
        cached = storage.get_proof(touch_id, version, music_id, music_version)
        if cached and cached["input_hash"] == rec["input_hash"]:
            return JSONResponse(
                json.loads(cached["result_json"]), headers={"X-Proof-Cache": "hit"}
            )
        ctx = _context_from_record(rec)
        leads = _resolve_leads(ctx)
        result = prove_rows(
            ctx["stage"],
            ctx["method"]["name"],
            leads,
            ctx["start_row"],
            ctx["max_calls"],
            ctx["quotas"],
            ctx["allowed"],
            ctx["forbidden"],
        )
        payload = {
            "touch_id": touch_id,
            "touch_version": version,
            "input_hash": rec["input_hash"],
            "method": _method_summary(ctx["method"]),
            "methods": _ctx_method_summaries(ctx),
            **result,
        }
        if scheme:
            payload["music"] = _music_payload(scheme, result)
        storage.insert_proof(
            {
                "touch_id": touch_id,
                "touch_version": version,
                "music_id": music_id,
                "music_version": music_version,
                "input_hash": rec["input_hash"],
                "result_json": json.dumps(payload, ensure_ascii=False),
                "created_at": utcnow(),
            }
        )
        return JSONResponse(payload, headers={"X-Proof-Cache": "miss"})

    @app.post("/touches/{touch_id}/versions/{version}/enumerate")
    def enumerate_touch(touch_id: str, version: int, body: EnumerateRequest):
        """枚举 call×method 候选组合：先按配额与转换规则剪枝，再按
        真值、rounds 回归、拼接数、方法分布均衡度排序（单方法 touch 保持
        改动数 → 总 change 数 → rounds 回归排序）。超出搜索预算时截断并说明。
        引用评分方案时，在既有约束筛选后按音乐门槛过滤（计数
        filtered_by_music），并按 真值 → rounds 回归 → 音乐分 → 既有排序项
        稳定排序。"""
        rec = storage.get_touch(touch_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"touch 版本不存在: {touch_id} v{version}"})
        ctx = _context_from_record(rec)
        scheme = None
        music_rules: list[dict] = []
        music_flags = (False, False)
        if body.music:
            scheme, err = _resolve_music(touch_id, version, rec["stage"], body.music)
            if err is not None:
                return err
            spec = json.loads(scheme["spec_json"])
            music_rules = spec["rules"]
            music_flags = (spec["score_start_row"], spec["score_final_rounds"])
        max_calls = body.max_calls if body.max_calls is not None else ctx["max_calls"]
        flat = ctx["flat_leads"]

        call_slots = [(i, lead["choice"]) for i, lead in enumerate(flat) if lead["choice"]]
        method_slots = [
            (i, lead["method_choice"]) for i, lead in enumerate(flat) if lead["method_choice"]
        ]
        total_combos = 1
        for _, options in call_slots + method_slots:
            total_combos *= len(options)
        if total_combos > body.max_variants:
            reason = f"组合数 {total_combos} 超过 max_variants={body.max_variants}"
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "TOO_MANY_VARIANTS",
                        "message": f"{reason}，请减少 choice 槽位或提高上限",
                    },
                    "total_combos": total_combos,
                    "max_variants": body.max_variants,
                    "checked": 0,
                    "truncated": True,
                    "truncation_reason": reason,
                },
            )

        quotas = ctx["quotas"]
        allowed, forbidden = ctx["allowed"], ctx["forbidden"]
        call_idx = [s for s, _ in call_slots]
        call_opts = [o for _, o in call_slots]
        method_idx = [s for s, _ in method_slots]
        method_opts = [o for _, o in method_slots]

        variants = []
        filtered = 0
        filtered_music = 0
        pruned_quota = 0
        pruned_transition = 0
        checked = 0
        truncated = False
        truncation_reason = None

        for combo in product(*call_opts, *method_opts):
            if checked >= body.max_search:
                truncated = True
                truncation_reason = f"达到搜索上限 max_search={body.max_search}"
                break
            checked += 1
            call_assign = dict(zip(call_idx, combo[: len(call_idx)]))
            method_assign = dict(zip(method_idx, combo[len(call_idx) :]))

            methods_seq = [
                method_assign.get(i, lead["method"]) for i, lead in enumerate(flat)
            ]
            calls_seq = [
                (call_assign.get(i) if lead["choice"] else lead["call"]) or None
                for i, lead in enumerate(flat)
            ]
            calls_seq = [None if c == "plain" else c for c in calls_seq]

            # 剪枝 1：各方法配额
            counts: dict[str, int] = {}
            for m in methods_seq:
                counts[m] = counts.get(m, 0) + 1
            if any(
                (q["min"] is not None and counts.get(mid, 0) < q["min"])
                or (q["max"] is not None and counts.get(mid, 0) > q["max"])
                for mid, q in quotas.items()
            ):
                pruned_quota += 1
                continue
            # 剪枝 2：相邻方法转换
            if any(
                not transition_ok(a, b, allowed, forbidden)[0]
                for a, b in zip(methods_seq, methods_seq[1:])
            ):
                pruned_transition += 1
                continue
            # 剪枝 3：替换次数上限
            calls_used = sum(1 for c in calls_seq if c)
            if max_calls is not None and calls_used > max_calls:
                filtered += 1
                continue

            leads = [
                _build_one_lead(ctx, m, c) for m, c in zip(methods_seq, calls_seq)
            ]
            r = prove_rows(
                ctx["stage"],
                ctx["method"]["name"],
                leads,
                ctx["start_row"],
                max_calls,
                quotas,
                allowed,
                forbidden,
            )
            dist = [counts.get(mid, 0) for mid in ctx["method_ids"]]
            variant = {
                "assignment": [
                    {"slot": n + 1, "lead": s + 1, "call": c}
                    for n, (s, c) in enumerate(
                        zip(call_idx, combo[: len(call_idx)])
                    )
                ],
                "method_assignment": [
                    {"slot": n + 1, "lead": s + 1, "method": m}
                    for n, (s, m) in enumerate(
                        zip(method_idx, combo[len(call_idx) :])
                    )
                ],
                "calls": [c or "plain" for c in calls_seq],
                "methods": list(methods_seq),
                "num_calls": calls_used,
                "num_splices": r["splice_count"],
                "method_counts": counts,
                "balance": (max(dist) - min(dist)) if dist else 0,
                "total_changes": r["total_changes"],
                "rounds_return": r["rounds_return"],
                "rounds_at": r["rounds_at"],
                "truth": r["truth"],
                "first_repeat": r["first_repeat"],
            }
            if scheme is not None:
                # 音乐评分：既有约束筛选之后，按门槛过滤并计入排序
                m = _score_result(r, music_rules, music_flags)
                variant["music_score"] = m["total_score"]
                variant["music_hits"] = m["total_hits"]
                if (
                    body.min_music_score is not None
                    and m["total_score"] < body.min_music_score
                ) or (
                    body.min_music_hits is not None
                    and m["total_hits"] < body.min_music_hits
                ):
                    filtered_music += 1
                    continue
            variants.append(variant)

        if scheme is not None:
            # 真值 → rounds 回归 → 音乐分（高者优先）→ 既有排序项，稳定排序
            if ctx["multi"]:
                variants.sort(
                    key=lambda v: (
                        v["truth"] != "true",
                        not v["rounds_return"],
                        -v["music_score"],
                        v["num_splices"],
                        v["balance"],
                        v["num_calls"],
                        v["total_changes"],
                        v["methods"],
                        v["calls"],
                    )
                )
                sorted_by = [
                    "truth",
                    "rounds_return",
                    "music_score",
                    "num_splices",
                    "balance",
                    "num_calls",
                    "total_changes",
                ]
            else:
                variants.sort(
                    key=lambda v: (
                        v["truth"] != "true",
                        not v["rounds_return"],
                        -v["music_score"],
                        v["num_calls"],
                        v["total_changes"],
                    )
                )
                sorted_by = [
                    "truth",
                    "rounds_return",
                    "music_score",
                    "num_calls",
                    "total_changes",
                ]
        elif ctx["multi"]:
            variants.sort(
                key=lambda v: (
                    v["truth"] != "true",
                    not v["rounds_return"],
                    v["num_splices"],
                    v["balance"],
                    v["num_calls"],
                    v["total_changes"],
                    v["methods"],
                    v["calls"],
                )
            )
            sorted_by = [
                "truth",
                "rounds_return",
                "num_splices",
                "balance",
                "num_calls",
                "total_changes",
            ]
        else:
            variants.sort(
                key=lambda v: (v["num_calls"], v["total_changes"], not v["rounds_return"])
            )
            sorted_by = ["num_calls", "total_changes", "rounds_return"]
        response = {
            "touch_id": touch_id,
            "touch_version": version,
            "input_hash": rec["input_hash"],
            "max_calls": max_calls,
            "sorted_by": sorted_by,
            "total_combos": total_combos,
            "checked": checked,
            "truncated": truncated,
            "truncation_reason": truncation_reason,
            "pruned_by_quota": pruned_quota,
            "pruned_by_transition": pruned_transition,
            "filtered_by_max_calls": filtered,
            "filtered_by_music": filtered_music,
            "total_variants": len(variants),
            "variants": variants,
        }
        if scheme is not None:
            response["music"] = {
                "id": scheme["id"],
                "version": scheme["version"],
                "name": scheme["name"],
                "input_hash": scheme["input_hash"],
            }
            response["min_music_score"] = body.min_music_score
            response["min_music_hits"] = body.min_music_hits
        return response

    # ---------- 其他 ----------
    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    # ---------- 内部辅助 ----------
    def _music_detail(rec: dict) -> dict:
        spec = json.loads(rec["spec_json"])
        return {
            "id": rec["id"],
            "version": rec["version"],
            "name": rec["name"],
            "stage": rec["stage"],
            "rules": spec["rules"],
            "score_start_row": spec["score_start_row"],
            "score_final_rounds": spec["score_final_rounds"],
            "input_hash": rec["input_hash"],
            "created_at": rec["created_at"],
        }

    def _resolve_music(touch_id: str, touch_version: int, stage: int, ref):
        """解析评分方案版本：显式版本直接使用；留空时随 touch 版本冻结
        （首次引用时定格为最新版本，之后同一 touch 版本复用同一方案版本）。
        返回 (scheme, None) 或 (None, 错误响应)。"""
        if ref.version is not None:
            scheme = storage.get_music_scheme(ref.id, ref.version)
            if not scheme:
                return None, JSONResponse(
                    status_code=404,
                    content={"detail": f"评分方案版本不存在: {ref.id} v{ref.version}"},
                )
        else:
            frozen = storage.get_touch_music(touch_id, touch_version, ref.id)
            mv = (
                frozen["music_version"]
                if frozen
                else storage.latest_music_version(ref.id)
            )
            if mv is None:
                return None, JSONResponse(
                    status_code=404, content={"detail": f"评分方案不存在: {ref.id}"}
                )
            scheme = storage.get_music_scheme(ref.id, mv)
            if not frozen:
                storage.insert_touch_music(
                    {
                        "touch_id": touch_id,
                        "touch_version": touch_version,
                        "music_id": ref.id,
                        "music_version": mv,
                        "created_at": utcnow(),
                    }
                )
        if scheme["stage"] != stage:
            return None, JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "MUSIC_STAGE_MISMATCH",
                        "message": f"评分方案 {ref.id!r} 为 {scheme['stage']} 口钟，"
                        f"与 touch 的 {stage} 口不符",
                    }
                },
            )
        return scheme, None

    def _score_result(result: dict, rules: list[dict], flags: tuple[bool, bool]) -> dict:
        """对一次证明结果按评分方案计分（起始 row / 末尾 rounds 由方案指定）。"""
        row_strings = [result["start_row"]] + [ev["row"] for ev in result["events"]]
        return score_rows(row_strings, result["events"], rules, *flags)

    def _music_payload(scheme: dict, result: dict) -> dict:
        spec = json.loads(scheme["spec_json"])
        scored = _score_result(
            result,
            spec["rules"],
            (spec["score_start_row"], spec["score_final_rounds"]),
        )
        return {
            "scheme": {
                "id": scheme["id"],
                "version": scheme["version"],
                "name": scheme["name"],
                "input_hash": scheme["input_hash"],
            },
            "score_start_row": spec["score_start_row"],
            "score_final_rounds": spec["score_final_rounds"],
            **scored,
        }

    def _method_detail(rec: dict) -> dict:
        changes = json.loads(rec["changes_json"])
        return {
            "id": rec["id"],
            "version": rec["version"],
            "name": rec["name"],
            "stage": rec["stage"],
            "notation_raw": rec["notation_raw"],
            "notation_normalized": rec["notation_normalized"],
            "lead_length": len(changes["tokens"]),
            "changes": [
                {"change": i + 1, "token": tok, "places": pl}
                for i, (tok, pl) in enumerate(zip(changes["tokens"], changes["places"]))
            ],
            "input_hash": rec["input_hash"],
            "created_at": rec["created_at"],
        }

    def _method_summary(rec: dict) -> dict:
        return {
            "id": rec["id"],
            "version": rec["version"],
            "name": rec["name"],
            "input_hash": rec["input_hash"],
        }

    def _method_summaries(recs: list[dict]) -> list[dict]:
        return [_method_summary(m) for m in recs]

    def _ctx_method_summaries(ctx: dict) -> list[dict]:
        return _method_summaries(ctx["methods"])

    def _touch_detail(rec: dict, method: dict | None) -> dict:
        spec = json.loads(rec["spec_json"])
        normalized = json.loads(rec["normalized_json"])
        methods_summary = normalized.get("methods") or [
            {
                "id": rec["method_id"],
                "version": rec["method_version"],
                "name": method["name"] if method else None,
                "input_hash": method["input_hash"] if method else None,
            }
        ]
        return {
            "id": rec["id"],
            "version": rec["version"],
            "method": methods_summary[0],
            "methods": methods_summary,
            "stage": rec["stage"],
            "start_row": normalized["start_row"],
            "calls": normalized["calls"],
            "leads": normalized["leads"],
            "total_leads": normalized["total_leads"],
            "max_calls": spec.get("max_calls"),
            "method_quotas": normalized.get("method_quotas") or {},
            "allowed_transitions": normalized.get("allowed_transitions"),
            "forbidden_transitions": normalized.get("forbidden_transitions") or [],
            "input_hash": rec["input_hash"],
            "created_at": rec["created_at"],
        }

    def _build_context(body: TouchCreate, methods: list[dict]) -> dict:
        """创建/重建时：校验并展开 touch 结构（对同一输入完全确定）。"""
        stage = methods[0]["stage"]
        for m in methods[1:]:
            if m["stage"] != stage:
                raise TouchError(
                    "STAGE_MISMATCH",
                    f"方法 {m['id']!r} 为 {m['stage']} 口钟，"
                    f"与 {methods[0]['id']!r} 的 {stage} 口不一致",
                )
        method_ids = [m["id"] for m in methods]
        method_by_id = {m["id"]: m for m in methods}
        parsed: dict[str, tuple[list[str], list[frozenset[int]]]] = {}
        for m in methods:
            ch = json.loads(m["changes_json"])
            parsed[m["id"]] = (ch["tokens"], [frozenset(p) for p in ch["places"]])

        start_row = (
            parse_row(body.start_row, stage)
            if body.start_row
            else tuple(range(1, stage + 1))
        )

        # call 须对所有声明方法的 lead 长度合法
        min_lead_len = min(len(t) for t, _ in parsed.values())
        call_defs: dict[str, dict] = {}
        for name, c in body.calls.items():
            ctoks, cchanges = expand_notation(c.notation, stage)
            if not 1 <= c.replace <= min_lead_len:
                raise TouchError(
                    "CALL_REPLACE_RANGE",
                    f"call {name!r} 替换 {c.replace} 个 change，超出 lead 长度 {min_lead_len}",
                )
            call_defs[name] = {
                "notation": c.notation,
                "normalized": ".".join(ctoks),
                "tokens": ctoks,
                "changes": cchanges,
                "replace": c.replace,
            }

        flat_leads: list[dict] = []
        for g in body.sequence:
            for _ in range(g.repeat):
                for ls in g.leads:
                    flat_leads.append(
                        {
                            "call": ls.call,
                            "choice": ls.choice,
                            "method": ls.method,
                            "method_choice": ls.method_choice,
                        }
                    )
        if not flat_leads:
            raise TouchError("EMPTY_SEQUENCE", "lead 顺序为空")
        if len(flat_leads) > MAX_LEADS:
            raise TouchError(
                "TOO_MANY_LEADS", f"展开后共 {len(flat_leads)} 个 lead，超过上限 {MAX_LEADS}"
            )
        default_method = method_ids[0]
        for i, lead in enumerate(flat_leads, 1):
            if lead["call"] is not None and lead["call"] not in call_defs:
                raise TouchError(
                    "UNKNOWN_CALL", f"第 {i} 个 lead 引用了未定义的 call: {lead['call']!r}"
                )
            if lead["choice"]:
                for c in lead["choice"]:
                    if c != "plain" and c not in call_defs:
                        raise TouchError(
                            "UNKNOWN_CALL",
                            f"第 {i} 个 lead 的 choice 引用了未定义的 call: {c!r}",
                        )
            if lead["method"] is not None and lead["method"] not in parsed:
                raise TouchError(
                    "UNKNOWN_METHOD",
                    f"第 {i} 个 lead 引用了未声明的方法: {lead['method']!r}",
                )
            if lead["method_choice"]:
                for mid in lead["method_choice"]:
                    if mid not in parsed:
                        raise TouchError(
                            "UNKNOWN_METHOD",
                            f"第 {i} 个 lead 的 method_choice 引用了未声明的方法: {mid!r}",
                        )
            if lead["method"] is None and not lead["method_choice"]:
                lead["method"] = default_method  # 缺省用首选方法

        # 配额：键须为已声明方法
        for mid in body.method_quotas:
            if mid not in parsed:
                raise TouchError(
                    "UNKNOWN_METHOD", f"method_quotas 引用了未声明的方法: {mid!r}"
                )
        quotas = {
            mid: {"min": q.min, "max": q.max}
            for mid, q in body.method_quotas.items()
        }

        # 转换规则：须引用已声明方法
        def _check_pairs(pairs, kind):
            for a, b in pairs or []:
                for x in (a, b):
                    if x not in parsed:
                        raise TouchError(
                            "UNKNOWN_TRANSITION",
                            f"{kind}转换规则引用了未声明的方法: {x!r}",
                        )

        _check_pairs(body.allowed_transitions, "允许")
        _check_pairs(body.forbidden_transitions, "禁止")
        allowed = (
            {tuple(p) for p in body.allowed_transitions}
            if body.allowed_transitions is not None
            else None
        )
        forbidden = {tuple(p) for p in body.forbidden_transitions or []}

        # 配额可行性：相对固定 lead 与候选槽位精确判定（最大流）
        candidates = [
            set(lead["method_choice"]) if lead["method_choice"] else {lead["method"]}
            for lead in flat_leads
        ]
        if not quota_feasible(len(flat_leads), candidates, quotas, method_ids):
            raise TouchError(
                "UNSATISFIABLE_QUOTA",
                "方法配额无法满足：固定 lead 与候选槽位下，"
                "不存在同时满足各方法 min/max 的分配",
            )

        # 相邻均为固定方法的 lead 对，转换冲突在创建时即拒绝
        for k in range(1, len(flat_leads)):
            a, b = flat_leads[k - 1], flat_leads[k]
            if a["method_choice"] or b["method_choice"]:
                continue
            ok, reason = transition_ok(a["method"], b["method"], allowed, forbidden)
            if not ok:
                msg = (
                    f"第 {k}→{k + 1} 个 lead 的方法转换 "
                    f"{a['method']}→{b['method']} 被禁止"
                    if reason == "forbidden"
                    else f"第 {k}→{k + 1} 个 lead 的方法转换 "
                    f"{a['method']}→{b['method']} 不在允许列表中"
                )
                raise TouchError("TRANSITION_VIOLATION", msg)

        # 配额 + 转换规则联合可行性：不存在同时满足两者的分配时拒绝创建，
        # 避免出现创建成功但枚举恒为 0 个方案的 touch；校验超出状态预算
        # （结果未知）时同样拒绝（fail-closed），保证未校验的 touch 不落库
        joint = joint_assignment_feasible(
            candidates, quotas, allowed, forbidden, method_ids
        )
        if joint is None:
            raise TouchError(
                "CONSTRAINT_CHECK_LIMIT",
                "方法配额与相邻转换规则的联合校验超出状态预算，无法确认可行性；"
                "请收紧配额或减少候选方法后重试",
            )
        if not joint:
            raise TouchError(
                "UNSATISFIABLE_CONSTRAINTS",
                "方法配额与相邻转换规则无法同时满足："
                "不存在既符合各方法 min/max 又符合转换规则的 lead 分配",
            )

        leads_summary = []
        for i, l in enumerate(flat_leads, 1):
            entry = {"lead": i}
            entry.update({"choice": l["choice"]} if l["choice"] else {"call": l["call"]})
            entry.update(
                {"method_choice": l["method_choice"]}
                if l["method_choice"]
                else {"method": l["method"]}
            )
            leads_summary.append(entry)
        return {
            "stage": stage,
            "multi": body.methods is not None,
            "methods": methods,
            "method_ids": method_ids,
            "method_by_id": method_by_id,
            "method": methods[0],
            "parsed": parsed,
            "start_row": start_row,
            "call_defs": call_defs,
            "flat_leads": flat_leads,
            "leads_summary": leads_summary,
            "max_calls": body.max_calls,
            "quotas": quotas,
            "allowed": allowed,
            "forbidden": forbidden,
        }

    def _context_from_record(rec: dict) -> dict:
        """从不可变记录重建证明上下文（冻结全部方法版本，确定性）。"""
        spec = json.loads(rec["spec_json"])
        body = TouchCreate(**spec)
        normalized = json.loads(rec["normalized_json"])
        refs = normalized.get("methods") or [
            {"id": rec["method_id"], "version": rec["method_version"]}
        ]
        methods = []
        for r in refs:
            m = storage.get_method(r["id"], r["version"])
            if not m:
                raise TouchError(
                    "METHOD_MISSING", f"touch 引用的方法版本缺失: {r['id']} v{r['version']}"
                )
            methods.append(m)
        ctx = _build_context(body, methods)
        ctx["max_calls"] = spec.get("max_calls")
        return ctx

    def _build_one_lead(ctx: dict, method_id: str, call: str | None):
        tokens, changes = ctx["parsed"][method_id]
        m = ctx["method_by_id"][method_id]
        return build_lead(
            tokens,
            changes,
            call,
            ctx["call_defs"],
            method_id=method_id,
            method_version=m["version"],
            method_name=m["name"],
        )

    def _resolve_leads(ctx: dict):
        """把 flat_leads 展开为 ExpandedLead；存在未决 choice 槽位时拒绝。"""
        leads = []
        for i, lead in enumerate(ctx["flat_leads"], 1):
            if lead["choice"]:
                raise TouchError(
                    "UNRESOLVED_CHOICE",
                    f"第 {i} 个 lead 是 call choice 槽位，请先通过 /enumerate 枚举变体",
                )
            if lead["method_choice"]:
                raise TouchError(
                    "UNRESOLVED_CHOICE",
                    f"第 {i} 个 lead 是方法 choice 槽位，请先通过 /enumerate 枚举变体",
                )
            leads.append(_build_one_lead(ctx, lead["method"], lead["call"]))
        return leads

    return app


app = create_app()
