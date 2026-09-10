"""SQLite 不可变版本存储：方法、touch 与证明结果。

方法与 touch 以 (id, version) 为主键只增不改；证明结果以
(touch_id, touch_version) 为主键缓存，保证同一版本重复证明结果一致。
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
    input_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (touch_id, touch_version)
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

    def list_touches(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, version, method_id, method_version, stage, input_hash,"
                " created_at FROM touches ORDER BY id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- proofs ----------------
    def get_proof(self, touch_id: str, touch_version: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM proofs WHERE touch_id = ? AND touch_version = ?",
                (touch_id, touch_version),
            ).fetchone()
        return dict(row) if row else None

    def insert_proof(self, rec: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO proofs"
                " (touch_id, touch_version, input_hash, result_json, created_at)"
                " VALUES (?,?,?,?,?)",
                (
                    rec["touch_id"],
                    rec["touch_version"],
                    rec["input_hash"],
                    rec["result_json"],
                    rec["created_at"],
                ),
            )
            self._conn.commit()
