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
from .compose import ComposeError, compile_composition, plain_course_length
from .continuation import search_continuations
from .engine import (
    ExpandedLead,
    TouchError,
    build_lead,
    prove_rows,
    transition_ok,
)
from .feasibility import joint_assignment_feasible, quota_feasible
from .music import score_rows, validate_rules
from .notation import (
    NotationError,
    expand_notation,
    parse_row,
    row_to_string,
    validate_stage,
)
from .prefix import replay_prefix
from .schemas import (
    CallDef,
    CompileRequest,
    CompositionCreate,
    ContinueRequest,
    EnumerateRequest,
    MethodCreate,
    MethodRef,
    MusicSchemeCreate,
    PositionSchemeCreate,
    PrefixCreate,
    ProveRequest,
    TouchCreate,
)
from .schemas import _validate_call_ref
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

    @app.exception_handler(ComposeError)
    async def compose_error_handler(_: Request, exc: ComposeError):
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": exc.code, "message": exc.message}, **exc.details},
        )

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

    # ---------- 呼叫位置方案 ----------
    @app.post("/position-schemes", status_code=201)
    def create_position_scheme(body: PositionSchemeCreate):
        """创建呼叫位置方案的不可变版本（按方法版本冻结观察钟与位置映射）。

        home 符号须存在且能由 plain lead end 到达；位置越界或 call 名重复
        在此被拒绝（不落库）。call 专属映射仅在 composition 编译时随该
        composition 声明的 call 记号参与推演。"""
        mv = body.method_version or storage.latest_method_version(body.method_id)
        method = storage.get_method(body.method_id, mv) if mv else None
        if not method:
            detail = (
                f"方法不存在: {body.method_id}"
                if body.method_version is None
                else f"方法版本不存在: {body.method_id} v{body.method_version}"
            )
            return JSONResponse(status_code=404, content={"detail": detail})
        stage = method["stage"]
        if not 1 <= body.observer <= stage:
            return JSONResponse(
                status_code=422,
                content={"detail": {"code": "BELL_OUT_OF_RANGE",
                                    "message": f"观察钟 {body.observer} 超出 {stage} 口钟范围"}},
            )
        positions: dict[str, int] = {}
        for symbol, pos in body.positions.items():
            if not 1 <= pos <= stage:
                return JSONResponse(
                    status_code=422,
                    content={"detail": {"code": "POSITION_OUT_OF_RANGE",
                                        "message": f"符号 {symbol!r} 的位置 {pos} 超出 1..{stage}"}},
                )
            positions[symbol] = pos
        if body.home_symbol not in positions:
            return JSONResponse(
                status_code=422,
                content={"detail": {"code": "HOME_SYMBOL_MISSING",
                                    "message": f"home 符号 {body.home_symbol!r} 未在 positions 中给出映射"}},
            )
        call_positions: dict[str, dict[str, int]] = {}
        for cp in body.call_positions:
            if cp.call in call_positions:
                return JSONResponse(
                    status_code=422,
                    content={"detail": {"code": "DUPLICATE_CALL",
                                        "message": f"call 位置映射重复: {cp.call!r}"}},
                )
            try:
                _validate_call_ref(cp.call)
            except ValueError:
                return JSONResponse(
                    status_code=422,
                    content={"detail": {"code": "INVALID_CALL_NAME",
                                        "message": f"非法 call 名: {cp.call!r}"}},
                )
            mapped: dict[str, int] = {}
            for symbol, pos in cp.positions.items():
                if not 1 <= pos <= stage:
                    return JSONResponse(
                        status_code=422,
                        content={"detail": {"code": "POSITION_OUT_OF_RANGE",
                                            "message": f"call {cp.call!r} 的符号 {symbol!r} 位置 {pos} 超出 1..{stage}"}},
                    )
                mapped[symbol] = pos
            call_positions[cp.call] = mapped

        # home 可达性：从 rounds 起只敲 plain lead，观察钟须在 plain course
        # 内回到 home 位置（这是后续 part 收尾与 course 界定的前提）。
        method_ctx = _method_ctx(method, {})
        home_position = positions[body.home_symbol]
        start = tuple(range(1, stage + 1))
        course_len = plain_course_length(method_ctx, start, body.observer, home_position, limit=200)
        if course_len is None:
            return JSONResponse(
                status_code=422,
                content={"detail": {"code": "HOME_NOT_REACHABLE",
                                    "message": f"观察钟 {body.observer} 号钟无法在 plain course（200 个 plain lead）"
                                               f"内回到 home（{body.home_symbol}，第 {home_position} 位）"}},
            )

        scheme_id = body.id or _new_id()
        version = storage.next_position_version(scheme_id)
        spec = {
            "name": body.name,
            "method_id": body.method_id,
            "method_version": mv,
            "observer": body.observer,
            "positions": dict(sorted(positions.items())),
            "call_positions": {k: dict(sorted(v.items())) for k, v in sorted(call_positions.items())},
            "home_symbol": body.home_symbol,
        }
        rec = {
            "id": scheme_id,
            "version": version,
            "name": body.name,
            "method_id": body.method_id,
            "method_version": mv,
            "stage": stage,
            "observer": body.observer,
            "spec_json": json.dumps(spec, ensure_ascii=False, sort_keys=True),
            "input_hash": canonical_hash({**spec, "method_hash": method["input_hash"]}),
            "created_at": utcnow(),
        }
        try:
            storage.insert_position_scheme(rec)
        except sqlite3.IntegrityError:
            return JSONResponse(status_code=409, content={"detail": "该 (id, version) 已存在，版本不可变"})
        return _position_detail(rec)

    @app.get("/position-schemes")
    def list_position_schemes():
        return {"position_schemes": storage.list_position_schemes()}

    @app.get("/position-schemes/{scheme_id}")
    def list_position_scheme_versions(scheme_id: str):
        versions = [m for m in storage.list_position_schemes() if m["id"] == scheme_id]
        if not versions:
            return JSONResponse(status_code=404, content={"detail": f"位置方案不存在: {scheme_id}"})
        return {"id": scheme_id, "versions": versions}

    @app.get("/position-schemes/{scheme_id}/versions/{version}")
    def get_position_scheme(scheme_id: str, version: int):
        rec = storage.get_position_scheme(scheme_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"位置方案版本不存在: {scheme_id} v{version}"})
        return _position_detail(rec)

    # ---------- composition（呼叫位置写法） ----------
    @app.post("/compositions", status_code=201)
    def create_composition(body: CompositionCreate):
        """创建呼叫位置 composition 的不可变版本，冻结位置方案与方法版本。"""
        sv = body.scheme.version or storage.latest_position_version(body.scheme.id)
        scheme_rec = storage.get_position_scheme(body.scheme.id, sv) if sv else None
        if not scheme_rec:
            detail = (
                f"位置方案不存在: {body.scheme.id}"
                if body.scheme.version is None
                else f"位置方案版本不存在: {body.scheme.id} v{body.scheme.version}"
            )
            return JSONResponse(status_code=404, content={"detail": detail})
        scheme_spec = json.loads(scheme_rec["spec_json"])
        method = storage.get_method(scheme_rec["method_id"], scheme_rec["method_version"])
        if not method:
            return JSONResponse(
                status_code=422,
                content={"detail": {"code": "METHOD_MISSING",
                                    "message": f"位置方案引用的方法版本缺失: {scheme_rec['method_id']} "
                                               f"v{scheme_rec['method_version']}"}},
            )
        stage = method["stage"]
        ch = json.loads(method["changes_json"])
        method_ctx = {
            "tokens": ch["tokens"],
            "changes": [frozenset(p) for p in ch["places"]],
            "call_defs": {},
            "method_id": method["id"],
            "method_version": method["version"],
            "method_name": method["name"],
        }

        # call 定义展开 + replace 范围
        call_defs: dict[str, dict] = {}
        for name, c in body.calls.items():
            try:
                ctoks, cchanges = expand_notation(c.notation, stage)
            except NotationError as e:
                return JSONResponse(status_code=422, content={"detail": {"code": e.code, "message": e.message}})
            if not 1 <= c.replace <= len(ch["tokens"]):
                return JSONResponse(
                    status_code=422,
                    content={"detail": {"code": "CALL_REPLACE_RANGE",
                                        "message": f"call {name!r} 替换 {c.replace} 个 change，超出 lead 长度 {len(ch['tokens'])}"}},
                )
            call_defs[name] = {
                "notation": c.notation,
                "normalized": ".".join(ctoks),
                "tokens": ctoks,
                "changes": cchanges,
                "replace": c.replace,
            }
        method_ctx["call_defs"] = call_defs

        # token 校验：符号须在方案映射中（该 call 专属或 default）；call 须已声明；
        # plain_leads（call 前经过的 plain lead 数）须在 course 窗口内
        positions = scheme_spec["positions"]
        call_positions = scheme_spec.get("call_positions", {})
        home_symbol = scheme_spec["home_symbol"]
        observer = scheme_rec["observer"]
        start_row = parse_row(body.start_row, stage) if body.start_row else tuple(range(1, stage + 1))
        course_len = plain_course_length(
            method_ctx, start_row, observer, positions[home_symbol], limit=200
        )
        window = course_len or 200
        normalized_parts: list[dict] = []
        total_leads_bound = 0
        for p_idx, part in enumerate(body.parts, 1):
            norm_tokens = []
            for t_idx, tok in enumerate(part.tokens, 1):
                call_name = tok.call
                if call_name is not None and call_name not in call_defs:
                    return JSONResponse(
                        status_code=422,
                        content={"detail": {"code": "UNKNOWN_CALL",
                                            "message": f"第 {p_idx} part 第 {t_idx} 个 token 引用了未定义的 call: {call_name!r}"}},
                    )
                per_call = call_positions.get(call_name or "")
                if tok.symbol not in positions and not (per_call and tok.symbol in per_call):
                    return JSONResponse(
                        status_code=422,
                        content={"detail": {"code": "UNKNOWN_SYMBOL",
                                            "message": f"第 {p_idx} part 第 {t_idx} 个 token 引用了方案未映射的位置符号: {tok.symbol!r}"}},
                    )
                if tok.plain_leads is not None and tok.plain_leads >= window:
                    return JSONResponse(
                        status_code=422,
                        content={"detail": {"code": "PLAIN_LEADS_OUT_OF_COURSE",
                                            "message": f"第 {p_idx} part 第 {t_idx} 个 token 的 plain_leads="
                                                       f"{tok.plain_leads} 达到或超过一个 course 窗口（{window} 个 lead）"}},
                    )
                if tok.max_plain_leads is not None and tok.max_plain_leads >= window:
                    return JSONResponse(
                        status_code=422,
                        content={"detail": {"code": "PLAIN_LEADS_OUT_OF_COURSE",
                                            "message": f"第 {p_idx} part 第 {t_idx} 个 token 的 max_plain_leads="
                                                       f"{tok.max_plain_leads} 达到或超过一个 course 窗口（{window} 个 lead）"}},
                    )
                norm_tokens.append({
                    "token": t_idx,
                    "symbol": tok.symbol,
                    "call": call_name,
                    "plain_leads": tok.plain_leads,
                    "max_plain_leads": tok.max_plain_leads,
                })
            normalized_parts.append({
                "part": p_idx,
                "name": part.name,
                "repeat": part.repeat,
                "tokens": norm_tokens,
            })
            total_leads_bound += part.repeat * (window * (len(part.tokens) + 1))
        if total_leads_bound > MAX_LEADS * (window + 1):
            return JSONResponse(
                status_code=422,
                content={"detail": {"code": "TOO_MANY_LEADS",
                                    "message": f"composition 展开规模过大（上限约 {MAX_LEADS} 个 lead）"}},
            )

        composition_id = body.id or _new_id()
        version = storage.next_composition_version(composition_id)
        input_hash = canonical_hash({
            "scheme": {"id": scheme_rec["id"], "version": scheme_rec["version"], "hash": scheme_rec["input_hash"]},
            "method": {"id": method["id"], "version": method["version"], "hash": method["input_hash"]},
            "stage": stage,
            "start_row": row_to_string(start_row),
            "calls": {k: {"notation": call_defs[k]["notation"], "replace": call_defs[k]["replace"]}
                      for k in sorted(call_defs)},
            "parts": [p.model_dump() for p in body.parts],
            "max_calls": body.max_calls,
        })
        normalized = {
            "scheme": {"id": scheme_rec["id"], "version": scheme_rec["version"],
                       "name": scheme_rec["name"], "input_hash": scheme_rec["input_hash"]},
            "method": {"id": method["id"], "version": method["version"],
                       "name": method["name"], "input_hash": method["input_hash"]},
            "stage": stage,
            "start_row": row_to_string(start_row),
            "plain_course_length": course_len,
            "calls": {k: {"normalized": c["normalized"], "replace": c["replace"]}
                      for k, c in sorted(call_defs.items())},
            "parts": normalized_parts,
        }
        rec = {
            "id": composition_id,
            "version": version,
            "method_id": method["id"],
            "method_version": method["version"],
            "stage": stage,
            "spec_json": json.dumps(body.model_dump(), ensure_ascii=False, sort_keys=True),
            "normalized_json": json.dumps(normalized, ensure_ascii=False),
            "input_hash": input_hash,
            "created_at": utcnow(),
        }
        try:
            storage.insert_composition(rec)
        except sqlite3.IntegrityError:
            return JSONResponse(status_code=409, content={"detail": "该 (id, version) 已存在，版本不可变"})
        return _composition_detail(rec)

    @app.get("/compositions")
    def list_compositions():
        return {"compositions": storage.list_compositions()}

    @app.get("/compositions/{composition_id}")
    def list_composition_versions(composition_id: str):
        versions = [m for m in storage.list_compositions() if m["id"] == composition_id]
        if not versions:
            return JSONResponse(status_code=404, content={"detail": f"composition 不存在: {composition_id}"})
        return {"id": composition_id, "versions": versions}

    @app.get("/compositions/{composition_id}/versions/{version}")
    def get_composition(composition_id: str, version: int):
        rec = storage.get_composition(composition_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"composition 版本不存在: {composition_id} v{version}"})
        return _composition_detail(rec)

    @app.post("/compositions/{composition_id}/versions/{version}/compile")
    def compile_composition_endpoint(composition_id: str, version: int, body: CompileRequest | None = None):
        """把呼叫位置写法编译为 touch：逐 lead 推演位置、解析 call，成功后另存
        不可变 touch 版本并返回证明结果；失败返回出错 token、候选 lead 与当前
        排列。相同输入重复编译结果一致（按请求内容哈希缓存）。"""
        body = body or CompileRequest()
        rec = storage.get_composition(composition_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"composition 版本不存在: {composition_id} v{version}"})
        spec = json.loads(rec["spec_json"])
        comp_body = CompositionCreate(**spec)
        scheme_rec = storage.get_position_scheme(comp_body.scheme.id, _frozen_scheme_version(rec, comp_body))
        scheme_spec = json.loads(scheme_rec["spec_json"])
        method = storage.get_method(rec["method_id"], rec["method_version"])
        stage = rec["stage"]
        ch = json.loads(method["changes_json"])

        call_defs: dict[str, dict] = {}
        for name, c in comp_body.calls.items():
            ctoks, cchanges = expand_notation(c.notation, stage)
            call_defs[name] = {
                "notation": c.notation,
                "normalized": ".".join(ctoks),
                "tokens": ctoks,
                "changes": cchanges,
                "replace": c.replace,
            }
        method_ctx = {
            "tokens": ch["tokens"],
            "changes": [frozenset(p) for p in ch["places"]],
            "call_defs": call_defs,
            "method_id": method["id"],
            "method_version": method["version"],
            "method_name": method["name"],
        }
        scheme = {
            "observer": scheme_rec["observer"],
            "home_symbol": scheme_spec["home_symbol"],
            "positions": scheme_spec["positions"],
            "call_positions": scheme_spec.get("call_positions", {}),
        }
        comp_start = parse_row(comp_body.start_row, stage) if comp_body.start_row else tuple(range(1, stage + 1))
        start_row = parse_row(body.course_head, stage) if body.course_head else comp_start

        parts: list[dict] = []
        for part in comp_body.parts:
            for rep in range(1, part.repeat + 1):
                parts.append({
                    "name": part.name,
                    "repeat_index": rep,
                    "tokens": [t.model_dump() for t in part.tokens],
                })

        touch_id = body.touch_id or f"compiled:{composition_id}"
        request_key = {
            "course_head": row_to_string(start_row),
            "course_length": body.course_length,
            "expect_rounds": body.expect_rounds,
            "save_touch": body.save_touch,
            # touch_id 决定另存 touch 的身份，必须纳入缓存键，否则改成新 id
            # 会命中旧 id 的缓存（返回旧 touch、新 touch 从未落库）
            "touch_id": touch_id if body.save_touch else None,
            "composition_hash": rec["input_hash"],
            "ringproof_version": __version__,
        }
        request_hash = canonical_hash(request_key)
        cached = storage.get_compilation(composition_id, version, request_hash)
        if cached and cached["input_hash"] == rec["input_hash"]:
            # 缓存命中仍须确认其指向的 touch 版本存在（可能已被外部删除），
            # 缺失时不沿用旧结果，重新编译并补写。
            touch_ok = (
                not body.save_touch
                or storage.get_touch(cached["touch_id"], cached["touch_version"]) is not None
            )
            if touch_ok:
                return JSONResponse(
                    json.loads(cached["result_json"]), headers={"X-Compile-Cache": "hit"}
                )

        compiled = compile_composition(
            scheme=scheme,
            method_ctx=method_ctx,
            parts=parts,
            start_row=start_row,
            course_length=body.course_length,
        )
        if len(compiled["leads"]) > MAX_LEADS:
            raise ComposeError(
                "TOO_MANY_LEADS",
                f"编译后共 {len(compiled['leads'])} 个 lead，超过上限 {MAX_LEADS}",
            )

        proof = prove_rows(
            stage,
            method["name"],
            compiled["leads"],
            start_row,
            comp_body.max_calls,
        )

        compiled_leads = []
        for rec_lead in compiled["records"]:
            compiled_leads.append({
                "lead": rec_lead.lead,
                "part": rec_lead.part,
                "part_repeat": rec_lead.part_repeat,
                "part_name": rec_lead.part_name,
                "token": rec_lead.token,
                "call": rec_lead.call,
                "symbol": rec_lead.symbol,
                "position": rec_lead.observer_position,
                "lead_head_before": rec_lead.lead_head_before,
                "lead_head_after": rec_lead.lead_head_after,
                "observer_bell": scheme_rec["observer"],
            })
        compiled_tokens = [
            {
                "part": t.part,
                "token": t.token,
                "symbol": t.symbol,
                "call": t.call,
                "plain_leads": t.plain_leads_before,
                "lead_index_in_part": t.lead_index,
                "position": t.observer_position,
            }
            for t in compiled["compiled_tokens"]
        ]
        final_row = compiled["final_row"]
        rounds = tuple(range(1, stage + 1))

        # 另存不可变 touch 版本：序列为逐 lead 固定 call。normalized 的每个
        # lead 除 lead/call 外，完整保留 part/token/位置/前后 lead head/观察钟
        # 标注，使 GET /touches 读回时与编译响应一致（spec.sequence 仍是
        # 最小的 call 序列，供证明引擎重放）。
        touch_spec = {
            "method_id": method["id"],
            "method_version": method["version"],
            "start_row": row_to_string(start_row),
            "calls": {
                k: {"notation": c["notation"], "replace": c["replace"]}
                for k, c in sorted(call_defs.items())
            },
            "sequence": [
                {"leads": [{"call": lead.call}], "repeat": 1}
                for lead in compiled["leads"]
            ],
            "max_calls": comp_body.max_calls,
        }
        normalized_touch = {
            "methods": [{"id": method["id"], "version": method["version"],
                         "name": method["name"], "input_hash": method["input_hash"]}],
            "start_row": row_to_string(start_row),
            "calls": {k: {"normalized": c["normalized"], "replace": c["replace"]}
                      for k, c in sorted(call_defs.items())},
            "leads": [
                {
                    "lead": cl["lead"],
                    "call": cl["call"],
                    "part": cl["part"],
                    "part_repeat": cl["part_repeat"],
                    "part_name": cl["part_name"],
                    "token": cl["token"],
                    "symbol": cl["symbol"],
                    "position": cl["position"],
                    "lead_head_before": cl["lead_head_before"],
                    "lead_head_after": cl["lead_head_after"],
                    "observer_bell": cl["observer_bell"],
                }
                for cl in compiled_leads
            ],
            "total_leads": len(compiled["leads"]),
            "method_quotas": {},
            "allowed_transitions": None,
            "forbidden_transitions": [],
            "compiled_tokens": compiled_tokens,
            "course_heads": compiled["course_heads"],
            "course_lengths": compiled["course_lengths"],
            "generated_by": {
                "type": "composition",
                "composition_id": composition_id,
                "composition_version": version,
                "scheme_id": scheme_rec["id"],
                "scheme_version": scheme_rec["version"],
                "observer": scheme_rec["observer"],
            },
        }
        touch_version = None
        touch_hash = None
        if body.save_touch:
            touch_version = storage.next_touch_version(touch_id)
            touch_hash = canonical_hash({
                "kind": "compiled-touch",
                "composition_id": composition_id,
                "composition_version": version,
                "composition_hash": rec["input_hash"],
                "request_hash": request_hash,
                "sequence": [(lead.call or "plain") for lead in compiled["leads"]],
                "start_row": row_to_string(start_row),
            })
            touch_rec = {
                "id": touch_id,
                "version": touch_version,
                "method_id": method["id"],
                "method_version": method["version"],
                "stage": stage,
                "spec_json": json.dumps(touch_spec, ensure_ascii=False, sort_keys=True),
                "normalized_json": json.dumps(normalized_touch, ensure_ascii=False),
                "input_hash": touch_hash,
                "created_at": utcnow(),
            }
            storage.insert_touch(touch_rec)
        payload = {
            "composition_id": composition_id,
            "composition_version": version,
            "input_hash": rec["input_hash"],
            "request_hash": request_hash,
            "course_head": row_to_string(start_row),
            "course_heads": compiled["course_heads"],
            "course_lengths": compiled["course_lengths"],
            "scheme": {
                "id": scheme_rec["id"],
                "version": scheme_rec["version"],
                "name": scheme_rec["name"],
                "observer": scheme_rec["observer"],
                "home_symbol": scheme_spec["home_symbol"],
                "input_hash": scheme_rec["input_hash"],
            },
            "compiled_tokens": compiled_tokens,
            "compiled_leads": compiled_leads,
            "total_leads": len(compiled["leads"]),
            "final_row": row_to_string(final_row),
            "rounds_return": final_row == rounds,
            "expect_rounds": body.expect_rounds,
            "expect_rounds_satisfied": (final_row == rounds) if body.expect_rounds else True,
            "touch": (
                {"id": touch_id, "version": touch_version, "input_hash": touch_hash}
                if body.save_touch else None
            ),
            "dependencies": {
                "ringproof_version": __version__,
                "method": {"id": method["id"], "version": method["version"], "input_hash": method["input_hash"]},
                "position_scheme": {"id": scheme_rec["id"], "version": scheme_rec["version"],
                                    "input_hash": scheme_rec["input_hash"]},
                "composition": {"id": composition_id, "version": version, "input_hash": rec["input_hash"]},
                "calls": {
                    k: {"notation": c["notation"], "replace": c["replace"]}
                    for k, c in sorted(call_defs.items())
                },
            },
            "proof": {
                k: v
                for k, v in proof.items()
                if k != "events"
            },
            "events": proof["events"],
        }
        if body.save_touch:
            storage.insert_compilation({
                "composition_id": composition_id,
                "composition_version": version,
                "request_hash": request_hash,
                "input_hash": rec["input_hash"],
                "touch_id": touch_id,
                "touch_version": touch_version,
                "result_json": json.dumps(payload, ensure_ascii=False),
                "created_at": utcnow(),
            })
        return JSONResponse(payload, headers={"X-Compile-Cache": "miss"})

    def _frozen_scheme_version(rec: dict, comp_body: CompositionCreate) -> int:
        """composition 记录中冻结的位置方案版本（spec 中版本留空时取 normalized）。"""
        normalized = json.loads(rec["normalized_json"])
        return normalized["scheme"]["version"]

    def _method_ctx(method: dict, call_defs: dict[str, dict]) -> dict:
        ch = json.loads(method["changes_json"])
        return {
            "tokens": ch["tokens"],
            "changes": [frozenset(p) for p in ch["places"]],
            "call_defs": call_defs,
            "method_id": method["id"],
            "method_version": method["version"],
            "method_name": method["name"],
        }

    def _position_detail(rec: dict) -> dict:
        spec = json.loads(rec["spec_json"])
        return {
            "id": rec["id"],
            "version": rec["version"],
            "name": rec["name"],
            "method": {"id": rec["method_id"], "version": rec["method_version"]},
            "stage": rec["stage"],
            "observer": rec["observer"],
            "positions": spec["positions"],
            "call_positions": spec.get("call_positions", {}),
            "home_symbol": spec["home_symbol"],
            "input_hash": rec["input_hash"],
            "created_at": rec["created_at"],
        }

    def _composition_detail(rec: dict) -> dict:
        spec = json.loads(rec["spec_json"])
        normalized = json.loads(rec["normalized_json"])
        return {
            "id": rec["id"],
            "version": rec["version"],
            "name": spec.get("name"),
            "stage": rec["stage"],
            "scheme": normalized["scheme"],
            "method": normalized["method"],
            "start_row": normalized["start_row"],
            "plain_course_length": normalized.get("plain_course_length"),
            "calls": normalized["calls"],
            "parts": normalized["parts"],
            "max_calls": spec.get("max_calls"),
            "input_hash": rec["input_hash"],
            "created_at": rec["created_at"],
        }

    # ---------- 部分 touch：前缀校核 ----------
    @app.post("/prefixes", status_code=201)
    def create_prefix(body: PrefixCreate):
        """提交已经敲出的 row 前缀并重放校核；合法时冻结当前排列、已出现
        row 与相关依赖版本（方法、call、ringproof 版本），供续接搜索。"""
        ctx_info, err = _build_prefix_input(body)
        if err is not None:
            return err
        replay = replay_prefix(
            ctx_info["stage"],
            ctx_info["leads"],
            ctx_info["rows"],
            ctx_info["start_row"],
        )
        if not replay["valid"]:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": {
                        "code": "PREFIX_INVALID",
                        "message": "前缀校核未通过",
                        "error": replay["first_error"],
                    },
                    "prefix": _prefix_replay_payload(
                        body.id or "", 0, ctx_info, replay, ctx_info["input_hash"]
                    ),
                },
            )

        prefix_id = body.id or _new_id()
        version = storage.next_prefix_version(prefix_id)
        state = {
            "stage": ctx_info["stage"],
            "start_row": row_to_string(ctx_info["start_row"]),
            "current_row": replay["current_row"],
            "seen_rows": replay["seen_rows"],
            "accepted_changes": replay["accepted_changes"],
            "lead_methods": replay["lead_methods"],
            "partial_lead": replay["partial_lead"],
            "leads": [
                {
                    "lead": i + 1,
                    "method": l["method_id"],
                    "method_version": l["method_version"],
                    "call": l["call"],
                    "changes": l["n_changes"],
                }
                for i, l in enumerate(ctx_info["leads"])
            ],
            "methods": [
                {"id": m["id"], "version": m["version"], "hash": m["input_hash"]}
                for m in ctx_info["methods"]
            ],
            "calls": ctx_info["call_specs"],
            "dependencies": _dependencies(
                ctx_info["methods"], ctx_info["call_specs"], None
            ),
        }
        replay_payload = _prefix_replay_payload(
            prefix_id, version, ctx_info, replay, ctx_info["input_hash"]
        )
        rec = {
            "id": prefix_id,
            "version": version,
            "stage": ctx_info["stage"],
            "state_json": json.dumps(state, ensure_ascii=False, sort_keys=True),
            "replay_json": json.dumps(replay_payload, ensure_ascii=False),
            "input_hash": ctx_info["input_hash"],
            "created_at": utcnow(),
        }
        try:
            storage.insert_prefix(rec)
        except sqlite3.IntegrityError:
            return JSONResponse(status_code=409, content={"detail": "该 (id, version) 已存在，版本不可变"})
        for m in ctx_info["methods"]:
            storage.insert_prefix_method(
                {
                    "prefix_id": prefix_id,
                    "prefix_version": version,
                    "method_id": m["id"],
                    "method_version": m["version"],
                    "created_at": utcnow(),
                }
            )
        return JSONResponse(replay_payload, status_code=201)

    @app.get("/prefixes")
    def list_prefixes():
        return {"prefixes": storage.list_prefixes()}

    @app.get("/prefixes/{prefix_id}/versions/{version}")
    def get_prefix(prefix_id: str, version: int):
        rec = storage.get_prefix(prefix_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"前缀版本不存在: {prefix_id} v{version}"})
        return json.loads(rec["replay_json"])

    @app.get("/prefixes/{prefix_id}/versions/{version}/rows")
    def get_prefix_rows(
        prefix_id: str,
        version: int,
        offset: int = Query(0, ge=0),
        limit: int = Query(500, ge=1, le=10000),
    ):
        """前缀逐行来源（change、lead、记号、方法 id/版本、call、拼接标记）。"""
        rec = storage.get_prefix(prefix_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"前缀版本不存在: {prefix_id} v{version}"})
        replay = json.loads(rec["replay_json"])
        events = replay["events"][offset : offset + limit]
        return {
            "prefix_id": prefix_id,
            "prefix_version": version,
            "input_hash": rec["input_hash"],
            "start_row": replay["start_row"],
            "total_changes": replay["total_changes"],
            "current_row": replay["current_row"],
            "methods": replay["methods"],
            "offset": offset,
            "limit": limit,
            "rows": events,
        }

    # ---------- 部分 touch：续接搜索 ----------
    @app.post("/prefixes/{prefix_id}/versions/{version}/continuation")
    def continue_prefix(prefix_id: str, version: int, body: ContinueRequest):
        """在校核通过的前缀之后搜索不重复的续接尾段；结果按到达目标、
        尾段长度、call 数、拼接数与音乐分稳定排序，同一前缀与约束重复
        请求结果一致（按请求内容哈希缓存）。"""
        rec = storage.get_prefix(prefix_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"前缀版本不存在: {prefix_id} v{version}"})
        state = json.loads(rec["state_json"])

        # 尾段可用方法：显式给出则按显式（版本留空则冻结为最新），否则沿用前缀方法
        tail_methods, err = _resolve_continuation_methods(prefix_id, version, state, body)
        if err is not None:
            return err
        stage = state["stage"]

        # 前缀止于 lead 中途时，强制段需要部分 lead 的方法定义；该方法记录
        # （按前缀冻结版本）并入构建上下文，但不进入可自由选择的方法列表，
        # 除非它本就在尾段方法中。
        partial_state = state.get("partial_lead")
        extra_ctx_methods: list[dict] = []
        if partial_state is not None:
            pm_id = partial_state["method_id"]
            if not any(m["id"] == pm_id for m in tail_methods):
                pm_version = partial_state["method_version"]
                pm_rec = storage.get_method(pm_id, pm_version)
                if not pm_rec:
                    return JSONResponse(
                        status_code=422,
                        content={"detail": {"code": "METHOD_MISSING",
                                            "message": f"部分 lead 引用的方法版本缺失: {pm_id} v{pm_version}"}},
                    )
                extra_ctx_methods.append(pm_rec)

        # call 定义：前缀 call 为底，请求 call 同名覆盖/新增
        call_specs, call_err = _merge_continuation_calls(state, body, tail_methods)
        if call_err is not None:
            return call_err

        # 配额 / 转换规则引用的方法须在尾段可用方法内
        method_ids = [m["id"] for m in tail_methods]
        for mid in body.method_quotas:
            if mid not in method_ids:
                return JSONResponse(
                    status_code=422,
                    content={"detail": {"code": "UNKNOWN_METHOD",
                                        "message": f"method_quotas 引用了尾段不可用的方法: {mid!r}"}},
                )

        def _pairs_err(pairs, kind):
            for a, b in pairs or []:
                for x in (a, b):
                    if x not in method_ids:
                        return JSONResponse(
                            status_code=422,
                            content={"detail": {"code": "UNKNOWN_TRANSITION",
                                                "message": f"{kind}转换规则引用了尾段不可用的方法: {x!r}"}},
                        )
            return None

        err = _pairs_err(body.allowed_transitions, "允许") or _pairs_err(
            body.forbidden_transitions, "禁止"
        )
        if err is not None:
            return err

        parsed = {}
        method_by_id = {}
        for m in tail_methods + extra_ctx_methods:
            ch = json.loads(m["changes_json"])
            parsed[m["id"]] = (ch["tokens"], [frozenset(p) for p in ch["places"]])
            method_by_id[m["id"]] = m
        min_lead_len = min(len(t) for t, _ in parsed.values())
        call_defs = {}
        for name, spec in call_specs.items():
            ctoks, cchanges = expand_notation(spec["notation"], stage)
            if not 1 <= spec["replace"] <= min_lead_len:
                return JSONResponse(
                    status_code=422,
                    content={"detail": {"code": "CALL_REPLACE_RANGE",
                                        "message": f"call {name!r} 替换 {spec['replace']} 个 change，"
                                                   f"超出尾段方法最小 lead 长度 {min_lead_len}"}},
                )
            call_defs[name] = {
                "notation": spec["notation"],
                "normalized": ".".join(ctoks),
                "tokens": ctoks,
                "changes": cchanges,
                "replace": spec["replace"],
            }
        ctx = {
            "stage": stage,
            "method_ids": method_ids,
            "method_by_id": method_by_id,
            "parsed": parsed,
            "call_defs": call_defs,
        }

        target = parse_row(body.target_row, stage) if body.target_row else tuple(
            range(1, stage + 1)
        )
        start_row = parse_row(state["current_row"], stage)
        seen_rows = {parse_row(r, stage) for r in state["seen_rows"]}
        quotas = {
            mid: {"min": q.min, "max": q.max}
            for mid, q in body.method_quotas.items()
        }
        allowed = (
            {tuple(p) for p in body.allowed_transitions}
            if body.allowed_transitions is not None
            else None
        )
        forbidden = {tuple(p) for p in body.forbidden_transitions or []}

        # 音乐方案（版本随前缀冻结）
        scheme = None
        music_rules = None
        music_flags = (False, False)
        if body.music:
            scheme, merr = _resolve_prefix_music(
                prefix_id, version, stage, body.music
            )
            if merr is not None:
                return merr
            spec_music = json.loads(scheme["spec_json"])
            music_rules = spec_music["rules"]
            music_flags = (
                spec_music["score_start_row"],
                spec_music["score_final_rounds"],
            )

        request_key = {
            "current_row": state["current_row"],
            "seen_rows": state["seen_rows"],
            "lead_methods": state["lead_methods"],
            "partial_lead": state.get("partial_lead"),
            "max_leads": body.max_leads,
            "target_row": row_to_string(target),
            "methods": [
                {"id": m["id"], "version": m["version"], "hash": m["input_hash"]}
                for m in tail_methods
            ],
            "calls": {
                k: {"notation": c["notation"], "replace": c["replace"]}
                for k, c in sorted(call_specs.items())
            },
            "max_calls": body.max_calls,
            "method_quotas": quotas,
            "allowed_transitions": sorted(allowed) if allowed is not None else None,
            "forbidden_transitions": sorted(forbidden),
            "max_results": body.max_results,
            "max_search": body.max_search,
            "music": (
                {"id": scheme["id"], "version": scheme["version"],
                 "hash": scheme["input_hash"]}
                if scheme
                else None
            ),
            "min_music_score": body.min_music_score,
            "min_music_hits": body.min_music_hits,
        }
        request_hash = canonical_hash(request_key)
        cached = storage.get_continuation(prefix_id, version, request_hash)
        if cached and cached["input_hash"] == rec["input_hash"]:
            return JSONResponse(
                json.loads(cached["result_json"]),
                headers={"X-Continuation-Cache": "hit"},
            )

        partial_arg = None
        if partial_state is not None:
            # 完成被冻结的部分 lead 时，沿用前缀保存时的 call 定义；续接请求
            # 中对同名 call 的覆盖只用于后续新增 lead，不影响这段强制余段。
            pm_id = partial_state["method_id"]
            frozen_call = partial_state["call"]
            frozen_lead = build_lead(
                *ctx["parsed"][pm_id],
                frozen_call,
                _frozen_call_defs(state["calls"], stage),
                method_id=pm_id,
                method_version=partial_state["method_version"],
                method_name=ctx["method_by_id"][pm_id]["name"],
            )
            partial_arg = {
                "method_id": pm_id,
                "method_version": partial_state["method_version"],
                "method_name": ctx["method_by_id"][pm_id]["name"],
                "call": frozen_call,
                "skip": partial_state["consumed"],
                "tokens": frozen_lead.tokens,
                "changes": [sorted(p) for p in frozen_lead.changes],
            }

        search = search_continuations(
            ctx,
            start_row,
            seen_rows,
            state["lead_methods"],
            max_leads=body.max_leads,
            target=target,
            max_calls=body.max_calls,
            max_results=body.max_results,
            max_search=body.max_search,
            quotas=quotas,
            allowed=allowed,
            forbidden=forbidden,
            partial=partial_arg,
            music_rules=music_rules,
            music_flags=music_flags,
            min_music_score=body.min_music_score,
            min_music_hits=body.min_music_hits,
            prefix_changes=state["accepted_changes"],
        )
        if (
            not search["truncated"]
            and search["solutions_found"] == 0
            and not search.get("forced_remainder_impossible")
        ):
            search["truncation_reason"] = "搜索空间穷尽，未发现满足全部约束的续接方案"
        elif search.get("forced_remainder_impossible"):
            search["truncation_reason"] = search["forced_remainder_impossible"]

        sorted_by = ["target_reached", "num_leads", "num_calls", "num_splices", "num_changes"]
        if scheme is not None:
            sorted_by.append("music_score")
        payload = {
            "prefix_id": prefix_id,
            "prefix_version": version,
            "input_hash": rec["input_hash"],
            "request_hash": request_hash,
            "start_row": state["current_row"],
            "target_row": row_to_string(target),
            "max_leads": body.max_leads,
            "max_calls": body.max_calls,
            "methods": [_method_summary(m) for m in tail_methods],
            "calls": sorted(call_defs),
            "method_quotas": quotas,
            "allowed_transitions": sorted(allowed) if allowed is not None else None,
            "forbidden_transitions": sorted(forbidden),
            "sorted_by": sorted_by,
            "dependencies": _dependencies(tail_methods, call_specs, scheme),
            **search,
        }
        if scheme is not None:
            payload["music"] = {
                "id": scheme["id"],
                "version": scheme["version"],
                "name": scheme["name"],
                "input_hash": scheme["input_hash"],
                "min_music_score": body.min_music_score,
                "min_music_hits": body.min_music_hits,
            }
        storage.insert_continuation(
            {
                "prefix_id": prefix_id,
                "prefix_version": version,
                "request_hash": request_hash,
                "input_hash": rec["input_hash"],
                "result_json": json.dumps(payload, ensure_ascii=False),
                "created_at": utcnow(),
            }
        )
        return JSONResponse(payload, headers={"X-Continuation-Cache": "miss"})

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

    def _dependencies(methods: list[dict], call_specs: dict[str, dict], scheme) -> dict:
        """续接/前缀冻结的相关依赖版本（方法、call 定义、评分方案与本服务版本）。"""
        return {
            "ringproof_version": __version__,
            "methods": [
                {"id": m["id"], "version": m["version"], "input_hash": m["input_hash"]}
                for m in methods
            ],
            "calls": {
                k: {"notation": c["notation"], "replace": c["replace"]}
                for k, c in sorted(call_specs.items())
            },
            "music": (
                {"id": scheme["id"], "version": scheme["version"],
                 "input_hash": scheme["input_hash"]}
                if scheme
                else None
            ),
        }

    def _frozen_call_defs(saved_calls: dict[str, dict], stage: int) -> dict[str, dict]:
        """用前缀状态中冻结的 call 定义重建 build_lead 所需 call_defs。

        续接请求对同名 call 的覆盖不参与此处展开，保证被冻结部分 lead 的
        强制余段与前缀创建时完全一致。
        """
        defs: dict[str, dict] = {}
        for name, spec in saved_calls.items():
            ctoks, cchanges = expand_notation(spec["notation"], stage)
            defs[name] = {
                "notation": spec["notation"],
                "normalized": ".".join(ctoks),
                "tokens": ctoks,
                "changes": cchanges,
                "replace": spec["replace"],
            }
        return defs

    def _build_call_defs(
        calls_in: dict, stage: int, methods: list[dict]
    ) -> tuple[dict[str, dict], dict[str, dict]]:
        """展开 call 定义；返回 (供重放/构建用 call_defs, 规范化 call_specs)。"""
        min_lead_len = min(
            len(json.loads(m["changes_json"])["tokens"]) for m in methods
        )
        call_defs: dict[str, dict] = {}
        call_specs: dict[str, dict] = {}
        for name, c in calls_in.items():
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
            call_specs[name] = {"notation": c.notation, "replace": c.replace}
        return call_defs, call_specs

    def _resolve_method_refs(refs: list[MethodRef], frozen: list[dict] | None = None):
        """把方法引用解析为不可变方法记录。

        显式版本必须存在；版本留空时优先取 frozen 中已冻结的版本，
        否则冻结为当前最新版本。返回 (records, None) 或 (None, 错误响应)。
        """
        frozen_map = {f["method_id"]: f["method_version"] for f in (frozen or [])}
        methods = []
        for ref in refs:
            if ref.version is not None:
                mv = ref.version
            elif ref.id in frozen_map:
                mv = frozen_map[ref.id]
            else:
                mv = storage.latest_method_version(ref.id)
            m = storage.get_method(ref.id, mv) if mv else None
            if not m:
                detail = (
                    f"方法不存在: {ref.id}"
                    if ref.version is None and ref.id not in frozen_map
                    else f"方法版本不存在: {ref.id} v{mv}"
                )
                return None, JSONResponse(status_code=404, content={"detail": detail})
            methods.append(m)
        return methods, None

    def _prefix_replay_leads(
        methods: list[dict], lead_labels, call_defs: dict[str, dict]
    ) -> list[dict]:
        """把 (方法 id, call) 标注展开为重放用 lead 描述符（含实际 change 序列）。"""
        by_id = {m["id"]: m for m in methods}
        default_id = methods[0]["id"]
        out = []
        for i, label in enumerate(lead_labels, 1):
            mid = label.get("method") or default_id
            if mid not in by_id:
                raise TouchError(
                    "UNKNOWN_METHOD", f"第 {i} 个 lead 引用了未声明的方法: {mid!r}"
                )
            call = label.get("call")
            if call is not None and call not in call_defs:
                raise TouchError(
                    "UNKNOWN_CALL", f"第 {i} 个 lead 引用了未定义的 call: {call!r}"
                )
            m = by_id[mid]
            ch = json.loads(m["changes_json"])
            lead = build_lead(
                ch["tokens"],
                [frozenset(p) for p in ch["places"]],
                call,
                call_defs,
                method_id=mid,
                method_version=m["version"],
                method_name=m["name"],
            )
            out.append(
                {
                    "method_id": mid,
                    "method_version": m["version"],
                    "method_name": m["name"],
                    "call": lead.call,
                    "tokens": lead.tokens,
                    "changes": lead.changes,
                    "n_changes": len(lead.tokens),
                }
            )
        return out

    def _build_prefix_input(body: PrefixCreate):
        """解析前缀提交（显式 rows+leads 或 from_touch 引用）。

        返回 (info, None) 或 (None, 错误响应)。NotationError/TouchError
        由全局异常处理器转为 422。
        """
        if body.from_touch is not None:
            ref = body.from_touch
            rec = storage.get_touch(ref.touch_id, ref.touch_version)
            if not rec:
                return None, JSONResponse(
                    status_code=404,
                    content={"detail": f"touch 版本不存在: {ref.touch_id} v{ref.touch_version}"},
                )
            ctx = _context_from_record(rec)
            try:
                leads_resolved = _resolve_leads(ctx)
            except TouchError as e:
                return None, JSONResponse(
                    status_code=422,
                    content={"detail": {"code": e.code, "message": e.message}},
                )
            result = prove_rows(
                ctx["stage"],
                ctx["method"]["name"],
                leads_resolved,
                ctx["start_row"],
            )
            lead_end_changes = _lead_end_changes(leads_resolved)
            if not 1 <= ref.up_to_change <= lead_end_changes[-1]:
                return None, JSONResponse(
                    status_code=422,
                    content={
                        "detail": {
                            "code": "CHANGE_OUT_OF_RANGE",
                            "message": (
                                f"change {ref.up_to_change} 超出 touch 范围"
                                f"（1..{lead_end_changes[-1]}）"
                            ),
                        }
                    },
                )
            methods = list(ctx["methods"])
            # 截止 change 所在 lead 及其已敲 change 数（允许止于 lead 中途）
            cut_lead = 0
            cum = 0
            consumed = 0
            for i, l in enumerate(leads_resolved, 1):
                lead_len = len(l.tokens)
                if ref.up_to_change <= cum + lead_len:
                    cut_lead = i
                    consumed = ref.up_to_change - cum
                    break
                cum += lead_len
            events = result["events"][: ref.up_to_change]
            rows = [ev["row"] for ev in events]
            labels = [
                {"method": result["lead_methods"][i], "call": leads_resolved[i].call}
                for i in range(cut_lead)
            ]
            call_specs = {
                k: {"notation": c["notation"], "replace": c["replace"]}
                for k, c in ctx["call_defs"].items()
            }
            leads = _prefix_replay_leads(methods, labels, ctx["call_defs"])
            input_hash = canonical_hash(
                {
                    "source": "touch",
                    "touch_id": ref.touch_id,
                    "touch_version": ref.touch_version,
                    "touch_hash": rec["input_hash"],
                    "up_to_change": ref.up_to_change,
                }
            )
            return {
                "stage": ctx["stage"],
                "methods": methods,
                "start_row": ctx["start_row"],
                "leads": leads,
                "rows": rows,
                "call_specs": call_specs,
                "source": {
                    "type": "touch",
                    "touch_id": ref.touch_id,
                    "touch_version": ref.touch_version,
                    "up_to_change": ref.up_to_change,
                },
                "input_hash": input_hash,
            }, None

        # 显式模式
        validate_stage(body.stage)
        methods, err = _resolve_method_refs(body.methods)
        if err is not None:
            return None, err
        stages = {m["stage"] for m in methods}
        if len(stages) != 1 or body.stage not in stages:
            return None, JSONResponse(
                status_code=422,
                content={"detail": {"code": "STAGE_MISMATCH",
                                    "message": "方法钟数须彼此一致且与 stage 相符"}},
            )
        start_row = parse_row(body.start_row, body.stage) if body.start_row else tuple(
            range(1, body.stage + 1)
        )
        call_defs, call_specs = _build_call_defs(body.calls, body.stage, methods)
        leads = _prefix_replay_leads(methods, [l.model_dump() for l in body.leads], call_defs)
        input_hash = canonical_hash(
            {
                "source": "explicit",
                "stage": body.stage,
                "start_row": row_to_string(start_row),
                "methods": [
                    {"id": m["id"], "version": m["version"], "hash": m["input_hash"]}
                    for m in methods
                ],
                "calls": {
                    k: {"notation": c["notation"], "replace": c["replace"]}
                    for k, c in sorted(call_specs.items())
                },
                "leads": [{"method": l["method_id"], "call": l["call"]} for l in leads],
                "rows": [r.strip().upper() for r in body.rows],
            }
        )
        return {
            "stage": body.stage,
            "methods": methods,
            "start_row": start_row,
            "leads": leads,
            "rows": body.rows,
            "call_specs": call_specs,
            "source": {"type": "explicit"},
            "input_hash": input_hash,
        }, None

    def _lead_end_changes(leads: list[ExpandedLead]) -> list[int]:
        out, cum = [], 0
        for l in leads:
            cum += len(l.tokens)
            out.append(cum)
        return out

    def _prefix_replay_payload(
        prefix_id: str, version: int, info: dict, replay: dict, input_hash: str
    ) -> dict:
        return {
            "id": prefix_id,
            "version": version,
            "source": info["source"],
            "stage": replay["stage"],
            "start_row": replay["start_row"],
            "current_row": replay["current_row"],
            "valid": replay["valid"],
            "total_changes": replay["total_changes"],
            "accepted_changes": replay["accepted_changes"],
            "permutation_complete": replay["permutation_complete"],
            "lead_methods": replay["lead_methods"],
            "lead_end_changes": replay["lead_end_indices"],
            "partial_lead": replay["partial_lead"],
            "ends_at_lead_end": replay["partial_lead"] is None,
            "seen_rows": replay["seen_rows"],
            "frozen": {
                "current_row": replay["current_row"],
                "seen_rows": replay["seen_rows"],
                "lead_methods": replay["lead_methods"],
            },
            "first_error": replay["first_error"],
            "methods": _method_summaries(info["methods"]),
            "calls": info["call_specs"],
            "dependencies": _dependencies(info["methods"], info["call_specs"], None),
            "events": replay["events"],
            "input_hash": input_hash,
        }

    def _resolve_prefix_music(prefix_id: str, prefix_version: int, stage: int, ref):
        """解析评分方案版本：显式版本直接使用；留空则随前缀版本冻结
        （首次引用时定格为最新版本）。返回 (scheme, None) 或 (None, 响应)。"""
        if ref.version is not None:
            scheme = storage.get_music_scheme(ref.id, ref.version)
            if not scheme:
                return None, JSONResponse(
                    status_code=404,
                    content={"detail": f"评分方案版本不存在: {ref.id} v{ref.version}"},
                )
        else:
            frozen = storage.get_prefix_music(prefix_id, prefix_version, ref.id)
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
                storage.insert_prefix_music(
                    {
                        "prefix_id": prefix_id,
                        "prefix_version": prefix_version,
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
                        f"与前缀的 {stage} 口不符",
                    }
                },
            )
        return scheme, None

    def _resolve_continuation_methods(prefix_id: str, version: int, state: dict, body: ContinueRequest):
        """尾段可用方法：请求显式给出时按请求解析（并冻结未标版本的引用），
        否则沿用前缀冻结的方法集合。"""
        frozen = storage.get_prefix_methods(prefix_id, version)
        refs = body.methods or [MethodRef(id=m["id"], version=m["version"]) for m in state["methods"]]
        ids = [r.id for r in refs]
        if len(set(ids)) != len(ids):
            return None, JSONResponse(
                status_code=422,
                content={"detail": {"code": "DUPLICATE_METHOD",
                                    "message": "尾段方法列表存在重复的方法 id"}},
            )
        methods, err = _resolve_method_refs(refs, frozen)
        if err is not None:
            return None, err
        stage = state["stage"]
        if any(m["stage"] != stage for m in methods):
            return None, JSONResponse(
                status_code=422,
                content={"detail": {"code": "STAGE_MISMATCH",
                                    "message": f"尾段方法均须为 {stage} 口钟"}},
            )
        # 未标版本且首次引用的方法在此冻结
        for ref, m in zip(refs, methods):
            if ref.version is None and not any(
                f["method_id"] == m["id"] for f in frozen
            ):
                storage.insert_prefix_method(
                    {
                        "prefix_id": prefix_id,
                        "prefix_version": version,
                        "method_id": m["id"],
                        "method_version": m["version"],
                        "created_at": utcnow(),
                    }
                )
        return methods, None

    def _merge_continuation_calls(state: dict, body: ContinueRequest, methods: list[dict]):
        """前缀 call 定义为底，请求 call 同名覆盖/新增；逐个展开校验。"""
        merged: dict[str, CallDef] = {
            k: CallDef(**v) for k, v in state["calls"].items()
        }
        if body.calls:
            for name, c in body.calls.items():
                merged[name] = c
        if not merged:
            return {}, None
        try:
            _, call_specs = _build_call_defs(merged, state["stage"], methods)
        except NotationError as e:
            return None, JSONResponse(
                status_code=422,
                content={"detail": {"code": e.code, "message": e.message}},
            )
        except TouchError as e:
            return None, JSONResponse(
                status_code=422,
                content={"detail": {"code": e.code, "message": e.message}},
            )
        return call_specs, None

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
        detail = {
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
        # 由呼叫位置 composition 编译生成的 touch：附带编译来源与位置标注，
        # 使 GET 读回与编译响应一致（part/token/位置/前后 lead head/观察钟）。
        if normalized.get("generated_by"):
            detail["generated_by"] = normalized["generated_by"]
            detail["compiled_tokens"] = normalized.get("compiled_tokens", [])
            detail["course_heads"] = normalized.get("course_heads")
            detail["course_lengths"] = normalized.get("course_lengths")
        return detail

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
