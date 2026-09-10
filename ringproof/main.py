"""FastAPI 应用：方法/touch 的不可变版本管理与 touch 序列证明。"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from itertools import product

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import __version__
from .engine import TouchError, build_lead, prove_rows
from .notation import NotationError, expand_notation, parse_row, row_to_string
from .schemas import EnumerateRequest, MethodCreate, TouchCreate
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
        "检测重复并定位来源、枚举 call 变体。",
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

    # ---------- touch ----------
    @app.post("/touches", status_code=201)
    def create_touch(body: TouchCreate):
        """创建 touch 的不可变版本（快照所引用的方法版本）。"""
        mv = body.method_version or storage.latest_method_version(body.method_id)
        method = storage.get_method(body.method_id, mv) if mv else None
        if not method:
            return JSONResponse(status_code=404, content={"detail": f"方法不存在: {body.method_id}"})
        ctx = _build_context(body, method)

        touch_id = body.id or _new_id()
        version = storage.next_touch_version(touch_id)
        input_hash = canonical_hash(
            {
                "method_id": method["id"],
                "method_version": method["version"],
                "method_hash": method["input_hash"],
                "stage": ctx["stage"],
                "start_row": row_to_string(ctx["start_row"]),
                "calls": {
                    k: {"notation": c["notation"], "replace": c["replace"]}
                    for k, c in ctx["call_defs"].items()
                },
                "sequence": [g.model_dump() for g in body.sequence],
                "max_calls": body.max_calls,
            }
        )
        normalized = {
            "start_row": row_to_string(ctx["start_row"]),
            "calls": {
                k: {"normalized": c["normalized"], "replace": c["replace"]}
                for k, c in ctx["call_defs"].items()
            },
            "leads": ctx["leads_summary"],
            "total_leads": len(ctx["flat_leads"]),
        }
        rec = {
            "id": touch_id,
            "version": version,
            "method_id": method["id"],
            "method_version": method["version"],
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
        return _touch_detail(rec, method)

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
        """逐行来源：每个 change 的记号、lead 序号与 call 来源。"""
        rec = storage.get_touch(touch_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"touch 版本不存在: {touch_id} v{version}"})
        ctx = _context_from_record(rec)
        leads = _resolve_leads(ctx)  # 含 choice 槽位时抛 422
        result = prove_rows(ctx["stage"], ctx["method"]["name"], leads, ctx["start_row"])
        events = result["events"][offset : offset + limit]
        return {
            "touch_id": touch_id,
            "touch_version": version,
            "input_hash": rec["input_hash"],
            "start_row": result["start_row"],
            "total_changes": result["total_changes"],
            "offset": offset,
            "limit": limit,
            "rows": events,
        }

    # ---------- 证明 ----------
    @app.post("/touches/{touch_id}/versions/{version}/prove")
    def prove_touch(touch_id: str, version: int):
        """序列证明；结果按版本缓存，同一版本重复证明结果一致。"""
        rec = storage.get_touch(touch_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"touch 版本不存在: {touch_id} v{version}"})
        cached = storage.get_proof(touch_id, version)
        if cached and cached["input_hash"] == rec["input_hash"]:
            return JSONResponse(
                json.loads(cached["result_json"]), headers={"X-Proof-Cache": "hit"}
            )
        ctx = _context_from_record(rec)
        leads = _resolve_leads(ctx)
        result = prove_rows(
            ctx["stage"], ctx["method"]["name"], leads, ctx["start_row"], ctx["max_calls"]
        )
        payload = {
            "touch_id": touch_id,
            "touch_version": version,
            "input_hash": rec["input_hash"],
            "method": {
                "id": ctx["method"]["id"],
                "version": ctx["method"]["version"],
                "name": ctx["method"]["name"],
                "input_hash": ctx["method"]["input_hash"],
            },
            **result,
        }
        storage.insert_proof(
            {
                "touch_id": touch_id,
                "touch_version": version,
                "input_hash": rec["input_hash"],
                "result_json": json.dumps(payload, ensure_ascii=False),
                "created_at": utcnow(),
            }
        )
        return JSONResponse(payload, headers={"X-Proof-Cache": "miss"})

    @app.post("/touches/{touch_id}/versions/{version}/enumerate")
    def enumerate_touch(touch_id: str, version: int, body: EnumerateRequest):
        """枚举 choice 槽位的全部 call 变体，按改动数、总 change 数、rounds 回归排序。"""
        rec = storage.get_touch(touch_id, version)
        if not rec:
            return JSONResponse(status_code=404, content={"detail": f"touch 版本不存在: {touch_id} v{version}"})
        ctx = _context_from_record(rec)
        max_calls = body.max_calls if body.max_calls is not None else ctx["max_calls"]

        slots = [
            (i, lead["choice"]) for i, lead in enumerate(ctx["flat_leads"]) if lead["choice"]
        ]
        total_combos = 1
        for _, options in slots:
            total_combos *= len(options)
        if total_combos > body.max_variants:
            raise TouchError(
                "TOO_MANY_VARIANTS",
                f"组合数 {total_combos} 超过 max_variants={body.max_variants}，"
                "请减少 choice 槽位或提高上限",
            )

        slot_indices = [s for s, _ in slots]
        slot_options = [opts for _, opts in slots]
        variants = []
        filtered = 0
        for combo in product(*slot_options) if slots else [()]:
            assignment = dict(zip(slot_indices, combo))
            calls_seq = [
                (assignment.get(i) if lead["choice"] else lead["call"]) or None
                for i, lead in enumerate(ctx["flat_leads"])
            ]
            calls_seq = [None if c == "plain" else c for c in calls_seq]
            leads = [
                build_lead(ctx["tokens"], ctx["changes"], c, ctx["call_defs"])
                for c in calls_seq
            ]
            calls_used = sum(1 for c in calls_seq if c)
            if max_calls is not None and calls_used > max_calls:
                filtered += 1
                continue
            r = prove_rows(ctx["stage"], ctx["method"]["name"], leads, ctx["start_row"])
            variants.append(
                {
                    "assignment": [
                        {"slot": n + 1, "lead": s + 1, "call": c}
                        for n, (s, c) in enumerate(zip(slot_indices, combo))
                    ],
                    "calls": [c or "plain" for c in calls_seq],
                    "num_calls": calls_used,
                    "total_changes": r["total_changes"],
                    "rounds_return": r["rounds_return"],
                    "rounds_at": r["rounds_at"],
                    "truth": r["truth"],
                    "first_repeat": r["first_repeat"],
                }
            )
        variants.sort(
            key=lambda v: (v["num_calls"], v["total_changes"], not v["rounds_return"])
        )
        return {
            "touch_id": touch_id,
            "touch_version": version,
            "input_hash": rec["input_hash"],
            "max_calls": max_calls,
            "sorted_by": ["num_calls", "total_changes", "rounds_return"],
            "total_variants": len(variants),
            "filtered_by_max_calls": filtered,
            "variants": variants,
        }

    # ---------- 其他 ----------
    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    # ---------- 内部辅助 ----------
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

    def _touch_detail(rec: dict, method: dict | None) -> dict:
        spec = json.loads(rec["spec_json"])
        normalized = json.loads(rec["normalized_json"])
        return {
            "id": rec["id"],
            "version": rec["version"],
            "method": {
                "id": rec["method_id"],
                "version": rec["method_version"],
                "name": method["name"] if method else None,
                "input_hash": method["input_hash"] if method else None,
            },
            "stage": rec["stage"],
            "start_row": normalized["start_row"],
            "calls": normalized["calls"],
            "leads": normalized["leads"],
            "total_leads": normalized["total_leads"],
            "max_calls": spec.get("max_calls"),
            "input_hash": rec["input_hash"],
            "created_at": rec["created_at"],
        }

    def _build_context(body: TouchCreate, method: dict) -> dict:
        """创建时：校验并展开 touch 结构。"""
        stage = method["stage"]
        changes = json.loads(method["changes_json"])
        tokens = changes["tokens"]
        method_changes = [frozenset(p) for p in changes["places"]]

        start_row = (
            parse_row(body.start_row, stage)
            if body.start_row
            else tuple(range(1, stage + 1))
        )

        call_defs: dict[str, dict] = {}
        for name, c in body.calls.items():
            ctoks, cchanges = expand_notation(c.notation, stage)
            if not 1 <= c.replace <= len(tokens):
                raise TouchError(
                    "CALL_REPLACE_RANGE",
                    f"call {name!r} 替换 {c.replace} 个 change，超出 lead 长度 {len(tokens)}",
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
                    flat_leads.append({"call": ls.call, "choice": ls.choice})
        if not flat_leads:
            raise TouchError("EMPTY_SEQUENCE", "lead 顺序为空")
        if len(flat_leads) > MAX_LEADS:
            raise TouchError(
                "TOO_MANY_LEADS", f"展开后共 {len(flat_leads)} 个 lead，超过上限 {MAX_LEADS}"
            )
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

        leads_summary = [
            {"lead": i, **({"call": l["call"]} if not l["choice"] else {"choice": l["choice"]})}
            for i, l in enumerate(flat_leads, 1)
        ]
        return {
            "stage": stage,
            "method": method,
            "tokens": tokens,
            "changes": method_changes,
            "start_row": start_row,
            "call_defs": call_defs,
            "flat_leads": flat_leads,
            "leads_summary": leads_summary,
            "max_calls": body.max_calls,
        }

    def _context_from_record(rec: dict) -> dict:
        """从不可变记录重建证明上下文（确定性）。"""
        method = storage.get_method(rec["method_id"], rec["method_version"])
        if not method:
            raise TouchError("METHOD_MISSING", "touch 引用的方法版本缺失")
        spec = json.loads(rec["spec_json"])
        body = TouchCreate(**spec)
        ctx = _build_context(body, method)
        ctx["max_calls"] = spec.get("max_calls")
        return ctx

    def _resolve_leads(ctx: dict):
        """把 flat_leads 展开为 ExpandedLead；存在未决 choice 槽位时拒绝。"""
        leads = []
        for i, lead in enumerate(ctx["flat_leads"], 1):
            if lead["choice"]:
                raise TouchError(
                    "UNRESOLVED_CHOICE",
                    f"第 {i} 个 lead 是 choice 槽位，请先通过 /enumerate 枚举变体",
                )
            leads.append(
                build_lead(ctx["tokens"], ctx["changes"], lead["call"], ctx["call_defs"])
            )
        return leads

    return app


app = create_app()
