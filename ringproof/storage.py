"""SQLite 不可变版本存储：方法、touch、音乐评分方案、all-the-work 覆盖方案、
呼叫位置方案、composition 与编译（compilation）结果、multipart 校核分析、
可复用 block 拼装（block composition）、方法相假图谱（falseness 分析）、
lead-head 可达图（lead graph 分析）、round block 等价分析。

方法、touch、评分方案、覆盖方案、位置方案、composition、multipart 分析、
block composition、falseness 分析与 round block 分析以 (id, version) 为主键只增不改；证明结果以 (touch_id,
touch_version, music_id, music_version, coverage_id, coverage_version) 为
主键缓存，编译结果以 (composition_id, composition_version, request_hash)
为主键缓存，multipart 枚举结果以 (analysis_id, analysis_version,
request_hash) 为主键缓存，block 组合搜索结果以 (composition_id,
composition_version, request_hash) 为主键缓存，保证同一版本重复
证明/编译/枚举/搜索结果一致。touch_music / touch_coverage 分别记录
touch 版本首次引用某评分/覆盖方案时冻结的版本。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS methods (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    stage INTEGER NOT NULL,
    notation_raw TEXT NOT NULL,
    notation_normalized TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS touches (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    method_id TEXT NOT NULL,
    method_version INTEGER NOT NULL,
    stage INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS proofs (
    touch_id TEXT NOT NULL,
    touch_version INTEGER NOT NULL,
    music_id TEXT NOT NULL,
    music_version INTEGER NOT NULL,
    coverage_id TEXT NOT NULL DEFAULT '',
    coverage_version INTEGER NOT NULL DEFAULT 0,
    input_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (touch_id, touch_version, music_id, music_version, coverage_id, coverage_version)
);
CREATE TABLE IF NOT EXISTS music_schemes (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    stage INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS touch_music (
    touch_id TEXT NOT NULL,
    touch_version INTEGER NOT NULL,
    music_id TEXT NOT NULL,
    music_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (touch_id, touch_version, music_id)
);
CREATE TABLE IF NOT EXISTS coverage_schemes (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    stage INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS touch_coverage (
    touch_id TEXT NOT NULL,
    touch_version INTEGER NOT NULL,
    coverage_id TEXT NOT NULL,
    coverage_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (touch_id, touch_version, coverage_id)
);
CREATE TABLE IF NOT EXISTS prefixes (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    stage INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    replay_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS prefix_music (
    prefix_id TEXT NOT NULL,
    prefix_version INTEGER NOT NULL,
    music_id TEXT NOT NULL,
    music_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (prefix_id, prefix_version, music_id)
);
CREATE TABLE IF NOT EXISTS prefix_methods (
    prefix_id TEXT NOT NULL,
    prefix_version INTEGER NOT NULL,
    method_id TEXT NOT NULL,
    method_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (prefix_id, prefix_version, method_id)
);
CREATE TABLE IF NOT EXISTS continuations (
    prefix_id TEXT NOT NULL,
    prefix_version INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (prefix_id, prefix_version, request_hash)
);
CREATE TABLE IF NOT EXISTS position_schemes (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    method_id TEXT NOT NULL,
    method_version INTEGER NOT NULL,
    stage INTEGER NOT NULL,
    observer INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS compositions (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    method_id TEXT NOT NULL,
    method_version INTEGER NOT NULL,
    stage INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS compilations (
    composition_id TEXT NOT NULL,
    composition_version INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    touch_id TEXT NOT NULL,
    touch_version INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (composition_id, composition_version, request_hash)
);
CREATE TABLE IF NOT EXISTS multipart_analyses (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    touch_id TEXT NOT NULL,
    touch_version INTEGER NOT NULL,
    stage INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS multipart_enumerations (
    analysis_id TEXT NOT NULL,
    analysis_version INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (analysis_id, analysis_version, request_hash)
);
CREATE TABLE IF NOT EXISTS block_compositions (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    stage INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS block_searches (
    composition_id TEXT NOT NULL,
    composition_version INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (composition_id, composition_version, request_hash)
);
CREATE TABLE IF NOT EXISTS falseness_analyses (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    stage INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS lead_graph_analyses (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    stage INTEGER NOT NULL,
    method_id TEXT NOT NULL,
    method_version INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE IF NOT EXISTS round_block_analyses (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    stage INTEGER NOT NULL,
    spec_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(obj) -> str:
    """输入的规范化 JSON 的 SHA-256，作为内容寻址哈希。"""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Storage:
    def __init__(self, path: str = "ringproof.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            # proofs 为可再生的缓存表：旧库缺少音乐/覆盖维度列时直接重建
            cols = [
                r[1] for r in self._conn.execute("PRAGMA table_info(proofs)").fetchall()
            ]
            if cols and ("music_id" not in cols or "coverage_id" not in cols):
                self._conn.execute("DROP TABLE proofs")
            self._conn.executescript(SCHEMA)

    # ---------------- methods ----------------
    def next_method_version(self, method_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM methods WHERE id = ?", (method_id,)
            ).fetchone()
            return (row["v"] or 0) + 1

    def insert_method(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO methods (id, version, name, stage, notation_raw,"
                " notation_normalized, changes_json, input_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    rec["id"],
                    rec["version"],
                    rec["name"],
                    rec["stage"],
                    rec["notation_raw"],
                    rec["notation_normalized"],
                    rec["changes_json"],
                    rec["input_hash"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    def get_method(self, method_id: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM methods WHERE id = ? AND version = ?",
                (method_id, version),
            ).fetchone()
        return dict(row) if row else None

    def latest_method_version(self, method_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM methods WHERE id = ?", (method_id,)
            ).fetchone()
        return row["v"]

    def list_methods(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, name, stage, input_hash, created_at"
                " FROM methods ORDER BY id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- touches ----------------
    def next_touch_version(self, touch_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM touches WHERE id = ?", (touch_id,)
            ).fetchone()
            return (row["v"] or 0) + 1

    def insert_touch(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO touches (id, version, method_id, method_version, stage,"
                " spec_json, normalized_json, input_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    rec["id"],
                    rec["version"],
                    rec["method_id"],
                    rec["method_version"],
                    rec["stage"],
                    rec["spec_json"],
                    rec["normalized_json"],
                    rec["input_hash"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    def get_touch(self, touch_id: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM touches WHERE id = ? AND version = ?",
                (touch_id, version),
            ).fetchone()
        return dict(row) if row else None

    def latest_touch_version(self, touch_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM touches WHERE id = ?", (touch_id,)
            ).fetchone()
        return row["v"]

    def list_touches(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, method_id, method_version, stage, input_hash,"
                " created_at FROM touches ORDER BY id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- proofs ----------------
    def get_proof(
        self,
        touch_id: str,
        touch_version: int,
        music_id: str = "",
        music_version: int = 0,
        coverage_id: str = "",
        coverage_version: int = 0,
    ) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proofs WHERE touch_id = ? AND touch_version = ?"
                " AND music_id = ? AND music_version = ?"
                " AND coverage_id = ? AND coverage_version = ?",
                (touch_id, touch_version, music_id, music_version, coverage_id, coverage_version),
            ).fetchone()
        return dict(row) if row else None

    def insert_proof(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO proofs"
                " (touch_id, touch_version, music_id, music_version,"
                " coverage_id, coverage_version, input_hash, result_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    rec["touch_id"],
                    rec["touch_version"],
                    rec.get("music_id", ""),
                    rec.get("music_version", 0),
                    rec.get("coverage_id", ""),
                    rec.get("coverage_version", 0),
                    rec["input_hash"],
                    rec["result_json"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    # ---------------- music schemes ----------------
    def next_music_version(self, scheme_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM music_schemes WHERE id = ?", (scheme_id,)
            ).fetchone()
            return (row["v"] or 0) + 1

    def insert_music_scheme(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO music_schemes"
                " (id, version, name, stage, spec_json, input_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    rec["id"],
                    rec["version"],
                    rec["name"],
                    rec["stage"],
                    rec["spec_json"],
                    rec["input_hash"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    def get_music_scheme(self, scheme_id: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM music_schemes WHERE id = ? AND version = ?",
                (scheme_id, version),
            ).fetchone()
        return dict(row) if row else None

    def latest_music_version(self, scheme_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM music_schemes WHERE id = ?", (scheme_id,)
            ).fetchone()
        return row["v"]

    def list_music_schemes(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, name, stage, input_hash, created_at"
                " FROM music_schemes ORDER BY id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- touch 的评分方案版本冻结 ----------------
    def get_touch_music(
        self, touch_id: str, touch_version: int, music_id: str
    ) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM touch_music"
                " WHERE touch_id = ? AND touch_version = ? AND music_id = ?",
                (touch_id, touch_version, music_id),
            ).fetchone()
        return dict(row) if row else None

    def insert_touch_music(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO touch_music"
                " (touch_id, touch_version, music_id, music_version, created_at)"
                " VALUES (?,?,?,?,?)",
                (
                    rec["touch_id"],
                    rec["touch_version"],
                    rec["music_id"],
                    rec["music_version"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    # ---------------- all-the-work 覆盖方案 ----------------
    def next_coverage_version(self, scheme_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM coverage_schemes WHERE id = ?",
                (scheme_id,),
            ).fetchone()
        return (row["v"] or 0) + 1

    def insert_coverage_scheme(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO coverage_schemes"
                " (id, version, name, stage, spec_json, input_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    rec["id"],
                    rec["version"],
                    rec["name"],
                    rec["stage"],
                    rec["spec_json"],
                    rec["input_hash"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    def get_coverage_scheme(self, scheme_id: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM coverage_schemes WHERE id = ? AND version = ?",
                (scheme_id, version),
            ).fetchone()
        return dict(row) if row else None

    def latest_coverage_version(self, scheme_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM coverage_schemes WHERE id = ?",
                (scheme_id,),
            ).fetchone()
        return row["v"]

    def list_coverage_schemes(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, name, stage, input_hash, created_at"
                " FROM coverage_schemes ORDER BY id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_touch_coverage(
        self, touch_id: str, touch_version: int, coverage_id: str
    ) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM touch_coverage"
                " WHERE touch_id = ? AND touch_version = ? AND coverage_id = ?",
                (touch_id, touch_version, coverage_id),
            ).fetchone()
        return dict(row) if row else None

    def insert_touch_coverage(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO touch_coverage"
                " (touch_id, touch_version, coverage_id, coverage_version, created_at)"
                " VALUES (?,?,?,?,?)",
                (
                    rec["touch_id"],
                    rec["touch_version"],
                    rec["coverage_id"],
                    rec["coverage_version"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    # ---------------- prefixes（部分 touch） ----------------
    def next_prefix_version(self, prefix_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM prefixes WHERE id = ?", (prefix_id,)
            ).fetchone()
        return (row["v"] or 0) + 1

    def insert_prefix(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO prefixes"
                " (id, version, stage, state_json, replay_json, input_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    rec["id"],
                    rec["version"],
                    rec["stage"],
                    rec["state_json"],
                    rec["replay_json"],
                    rec["input_hash"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    def get_prefix(self, prefix_id: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM prefixes WHERE id = ? AND version = ?",
                (prefix_id, version),
            ).fetchone()
        return dict(row) if row else None

    def list_prefixes(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, stage, input_hash, created_at"
                " FROM prefixes ORDER BY id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_prefix_music(
        self, prefix_id: str, prefix_version: int, music_id: str
    ) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM prefix_music"
                " WHERE prefix_id = ? AND prefix_version = ? AND music_id = ?",
                (prefix_id, prefix_version, music_id),
            ).fetchone()
        return dict(row) if row else None

    def insert_prefix_music(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO prefix_music"
                " (prefix_id, prefix_version, music_id, music_version, created_at)"
                " VALUES (?,?,?,?,?)",
                (
                    rec["prefix_id"],
                    rec["prefix_version"],
                    rec["music_id"],
                    rec["music_version"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    def get_prefix_methods(
        self, prefix_id: str, prefix_version: int
    ) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT method_id, method_version FROM prefix_methods"
                " WHERE prefix_id = ? AND prefix_version = ? ORDER BY rowid",
                (prefix_id, prefix_version),
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_prefix_method(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO prefix_methods"
                " (prefix_id, prefix_version, method_id, method_version, created_at)"
                " VALUES (?,?,?,?,?)",
                (
                    rec["prefix_id"],
                    rec["prefix_version"],
                    rec["method_id"],
                    rec["method_version"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    # ---------------- 续接搜索缓存 ----------------
    def get_continuation(
        self, prefix_id: str, prefix_version: int, request_hash: str
    ) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM continuations"
                " WHERE prefix_id = ? AND prefix_version = ? AND request_hash = ?",
                (prefix_id, prefix_version, request_hash),
            ).fetchone()
        return dict(row) if row else None

    def insert_continuation(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO continuations"
                " (prefix_id, prefix_version, request_hash, input_hash,"
                " result_json, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    rec["prefix_id"],
                    rec["prefix_version"],
                    rec["request_hash"],
                    rec["input_hash"],
                    rec["result_json"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    # ---------------- 呼叫位置方案 ----------------
    def next_position_version(self, scheme_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM position_schemes WHERE id = ?",
                (scheme_id,),
            ).fetchone()
        return (row["v"] or 0) + 1

    def insert_position_scheme(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO position_schemes"
                " (id, version, name, method_id, method_version, stage, observer,"
                " spec_json, input_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    rec["id"],
                    rec["version"],
                    rec["name"],
                    rec["method_id"],
                    rec["method_version"],
                    rec["stage"],
                    rec["observer"],
                    rec["spec_json"],
                    rec["input_hash"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    def get_position_scheme(self, scheme_id: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM position_schemes WHERE id = ? AND version = ?",
                (scheme_id, version),
            ).fetchone()
        return dict(row) if row else None

    def latest_position_version(self, scheme_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM position_schemes WHERE id = ?",
                (scheme_id,),
            ).fetchone()
        return row["v"]

    def list_position_schemes(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, name, method_id, method_version, stage, observer,"
                " input_hash, created_at FROM position_schemes ORDER BY id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- composition（呼叫位置写法） ----------------
    def next_composition_version(self, composition_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM compositions WHERE id = ?",
                (composition_id,),
            ).fetchone()
        return (row["v"] or 0) + 1

    def insert_composition(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO compositions"
                " (id, version, method_id, method_version, stage,"
                " spec_json, normalized_json, input_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    rec["id"],
                    rec["version"],
                    rec["method_id"],
                    rec["method_version"],
                    rec["stage"],
                    rec["spec_json"],
                    rec["normalized_json"],
                    rec["input_hash"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    def get_composition(self, composition_id: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM compositions WHERE id = ? AND version = ?",
                (composition_id, version),
            ).fetchone()
        return dict(row) if row else None

    def list_compositions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, method_id, method_version, stage, input_hash,"
                " created_at FROM compositions ORDER BY id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- multipart composition 校核 ----------------
    def next_multipart_version(self, analysis_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM multipart_analyses WHERE id = ?",
                (analysis_id,),
            ).fetchone()
            return (row["v"] or 0) + 1

    def insert_multipart(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO multipart_analyses"
                " (id, version, touch_id, touch_version, stage,"
                " spec_json, result_json, input_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    rec["id"],
                    rec["version"],
                    rec["touch_id"],
                    rec["touch_version"],
                    rec["stage"],
                    rec["spec_json"],
                    rec["result_json"],
                    rec["input_hash"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    def get_multipart(self, analysis_id: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM multipart_analyses WHERE id = ? AND version = ?",
                (analysis_id, version),
            ).fetchone()
        return dict(row) if row else None

    def list_multiparts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, touch_id, touch_version, stage, input_hash,"
                " created_at FROM multipart_analyses ORDER BY id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- multipart 枚举缓存 ----------------
    def get_multipart_enumeration(
        self, analysis_id: str, analysis_version: int, request_hash: str
    ) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM multipart_enumerations"
                " WHERE analysis_id = ? AND analysis_version = ? AND request_hash = ?",
                (analysis_id, analysis_version, request_hash),
            ).fetchone()
        return dict(row) if row else None

    def insert_multipart_enumeration(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO multipart_enumerations"
                " (analysis_id, analysis_version, request_hash, input_hash,"
                " result_json, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    rec["analysis_id"],
                    rec["analysis_version"],
                    rec["request_hash"],
                    rec["input_hash"],
                    rec["result_json"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    # ---------------- 可复用 block 拼装 ----------------
    def next_block_composition_version(self, composition_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM block_compositions WHERE id = ?",
                (composition_id,),
            ).fetchone()
            return (row["v"] or 0) + 1

    def insert_block_composition(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO block_compositions"
                " (id, version, stage, spec_json, normalized_json, input_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    rec["id"],
                    rec["version"],
                    rec["stage"],
                    rec["spec_json"],
                    rec["normalized_json"],
                    rec["input_hash"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    def get_block_composition(self, composition_id: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM block_compositions WHERE id = ? AND version = ?",
                (composition_id, version),
            ).fetchone()
        return dict(row) if row else None

    def list_block_compositions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, stage, input_hash, created_at"
                " FROM block_compositions ORDER BY id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- block 组合搜索缓存 ----------------
    def get_block_search(
        self, composition_id: str, composition_version: int, request_hash: str
    ) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM block_searches"
                " WHERE composition_id = ? AND composition_version = ?"
                " AND request_hash = ?",
                (composition_id, composition_version, request_hash),
            ).fetchone()
        return dict(row) if row else None

    def insert_block_search(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO block_searches"
                " (composition_id, composition_version, request_hash, input_hash,"
                " result_json, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    rec["composition_id"],
                    rec["composition_version"],
                    rec["request_hash"],
                    rec["input_hash"],
                    rec["result_json"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    # ---------------- 方法相假图谱 ----------------
    def next_falseness_version(self, analysis_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM falseness_analyses WHERE id = ?",
                (analysis_id,),
            ).fetchone()
            return (row["v"] or 0) + 1

    def insert_falseness(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO falseness_analyses"
                " (id, version, stage, spec_json, result_json, input_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    rec["id"],
                    rec["version"],
                    rec["stage"],
                    rec["spec_json"],
                    rec["result_json"],
                    rec["input_hash"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    def get_falseness(self, analysis_id: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM falseness_analyses WHERE id = ? AND version = ?",
                (analysis_id, version),
            ).fetchone()
        return dict(row) if row else None

    def list_falseness(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, stage, input_hash, created_at"
                " FROM falseness_analyses ORDER BY id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- lead-head 可达图 ----------------
    def next_lead_graph_version(self, analysis_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM lead_graph_analyses WHERE id = ?",
                (analysis_id,),
            ).fetchone()
        return (row["v"] or 0) + 1

    def insert_lead_graph(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO lead_graph_analyses"
                " (id, version, stage, method_id, method_version,"
                " spec_json, result_json, input_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    rec["id"],
                    rec["version"],
                    rec["stage"],
                    rec["method_id"],
                    rec["method_version"],
                    rec["spec_json"],
                    rec["result_json"],
                    rec["input_hash"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    def get_lead_graph(self, analysis_id: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lead_graph_analyses WHERE id = ? AND version = ?",
                (analysis_id, version),
            ).fetchone()
        return dict(row) if row else None

    def list_lead_graphs(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, stage, method_id, method_version, input_hash,"
                " created_at FROM lead_graph_analyses ORDER BY id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- round block 等价分析 ----------------
    def next_round_block_version(self, analysis_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM round_block_analyses WHERE id = ?",
                (analysis_id,),
            ).fetchone()
            return (row["v"] or 0) + 1

    def insert_round_block(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO round_block_analyses"
                " (id, version, stage, spec_json, result_json, input_hash, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    rec["id"],
                    rec["version"],
                    rec["stage"],
                    rec["spec_json"],
                    rec["result_json"],
                    rec["input_hash"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()

    def get_round_block(self, analysis_id: str, version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM round_block_analyses WHERE id = ? AND version = ?",
                (analysis_id, version),
            ).fetchone()
        return dict(row) if row else None

    def list_round_blocks(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, stage, input_hash, created_at"
                " FROM round_block_analyses ORDER BY id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 编译结果 ----------------
    def get_compilation(
        self, composition_id: str, composition_version: int, request_hash: str
    ) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM compilations"
                " WHERE composition_id = ? AND composition_version = ?"
                " AND request_hash = ?",
                (composition_id, composition_version, request_hash),
            ).fetchone()
        return dict(row) if row else None

    def insert_compilation(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO compilations"
                " (composition_id, composition_version, request_hash, input_hash,"
                " touch_id, touch_version, result_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    rec["composition_id"],
                    rec["composition_version"],
                    rec["request_hash"],
                    rec["input_hash"],
                    rec["touch_id"],
                    rec["touch_version"],
                    rec["result_json"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()
