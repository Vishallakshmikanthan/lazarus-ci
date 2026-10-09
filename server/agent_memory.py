# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Persistent agent memory: cross-episode belief updates via sqlite3."""

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

_DB_PATH = Path(os.getenv("AGENT_MEMORY_DB_PATH", str(Path(__file__).parent / "agent_memory.db")))


def _init_db() -> None:
    """Create tables on first import so the DB file always exists."""
    conn = _connect()
    conn.close()


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            pattern_hash TEXT NOT NULL,
            fix_text      TEXT NOT NULL,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (pattern_hash, fix_text)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS optimal_paths (
            fault_type   TEXT PRIMARY KEY,
            path_json    TEXT NOT NULL,
            recorded_at  REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


def _normalize_error(error: str) -> str:
    """Strip volatile tokens so semantically identical errors produce the same hash.

    Removed:
    - UUIDs / hex run-IDs  (e.g. job-3f2a1b...)
    - Line/column numbers  (line 42, col 7)
    - Memory addresses     (0x7f3a...)
    - Absolute timestamps  (2024-01-01T12:00:00)
    - Temp paths           (/tmp/cicd-ws-abc123-/)
    - Docker layer hashes  (---> 4b8a3f2e1c9d)
    """
    s = error.strip().lower()
    # UUIDs
    s = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<uuid>", s)
    # Hex run-IDs / docker layer digests (8+ hex chars)
    s = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", s)
    # line N / col N / column N
    s = re.sub(r"\b(line|col|column)\s+\d+\b", r"\1 <n>", s)
    # :<digits>: (e.g. file.py:42:7:)
    s = re.sub(r":\d+", ":<n>", s)
    # memory addresses
    s = re.sub(r"0x[0-9a-f]+", "<addr>", s)
    # ISO timestamps
    s = re.sub(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:\d{2})?", "<ts>", s)
    # temp workspace paths like /tmp/cicd-ws-3f2a1b45-/
    s = re.sub(r"/tmp/cicd-ws-[^/\s]+", "/tmp/cicd-ws-<id>", s)
    # collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def fingerprint(errors: list[str]) -> str:
    """Deterministic 16-hex-char hash of a normalised error list.

    Normalisation strips UUIDs, line numbers, hex digests, memory addresses,
    and timestamps so semantically identical errors from different episodes
    hash to the same value, improving cross-episode recall hit rates.
    """
    normalised = sorted(_normalize_error(e) for e in errors if e.strip())
    payload = json.dumps(normalised, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def recall(errors: list[str], fault_type: str = "unknown") -> dict:
    """Return best known fix for this error pattern, or an empty suggestion."""
    if not errors:
        return {
            "suggested_fix": "",
            "confidence": 0.0,
            "times_seen": 0,
            "historical_success_rate": 0.0,
            "memory_log": "",
        }

    h = fingerprint(errors)
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT fix_text, success_count, failure_count FROM memory "
            "WHERE pattern_hash = ? "
            "ORDER BY success_count DESC, (success_count + failure_count) DESC "
            "LIMIT 1",
            (h,),
        ).fetchone()
        conn.close()
    except Exception:
        return {
            "suggested_fix": "",
            "confidence": 0.0,
            "times_seen": 0,
            "historical_success_rate": 0.0,
            "memory_log": "",
        }

    if row is None:
        return {
            "suggested_fix": "",
            "confidence": 0.0,
            "times_seen": 0,
            "historical_success_rate": 0.0,
            "memory_log": "",
        }

    fix_text, success_count, failure_count = row
    total = success_count + failure_count
    confidence = success_count / total if total > 0 else 0.0
    memory_log = (
        "[MEMORY] Recalling fix for fault_type="
        f"{fault_type}: action={fix_text}, historical_success_rate={confidence:.2f}"
    )
    return {
        "suggested_fix": fix_text,
        "confidence": round(confidence, 3),
        "times_seen": total,
        "historical_success_rate": round(confidence, 3),
        "memory_log": memory_log,
    }


import time as _time

# Ensure DB and tables exist as soon as the module is imported.
_init_db()


def remember_optimal_path(fault_type: str, path: list[dict]) -> None:
    """Persist the optimal action sequence observed for a fault type.

    Each entry in *path* is a dict with keys:
        operation  – canonical action name (e.g. "inspect_config")
        target     – file or stage name
        value      – fix payload or hypothesis text
        rationale  – one-sentence explanation of why this step was taken
    Only stored when the episode was resolved (caller's responsibility to gate).
    Overwrites any prior path for the same fault_type so the DB always holds
    the most-recently-observed successful trace.
    """
    if not fault_type or not path:
        return
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO optimal_paths (fault_type, path_json, recorded_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(fault_type) DO UPDATE SET "
            "path_json = excluded.path_json, recorded_at = excluded.recorded_at",
            (fault_type, json.dumps(path, separators=(",", ":")), _time.time()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def recall_optimal_path(fault_type: str) -> list[dict]:
    """Return the stored optimal path for *fault_type*, or an empty list."""
    if not fault_type:
        return []
    try:
        conn = _connect()
        row = conn.execute(
            "SELECT path_json FROM optimal_paths WHERE fault_type = ?",
            (fault_type,),
        ).fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception:
        pass
    return []


def remember(errors: list[str], fix: str, success: bool) -> None:
    """Upsert fix outcome for this error pattern into persistent memory."""
    if not errors or not fix:
        return

    h = fingerprint(errors)
    fix_clean = fix.strip()
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO memory (pattern_hash, fix_text, success_count, failure_count) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(pattern_hash, fix_text) DO UPDATE SET "
            "success_count = success_count + excluded.success_count, "
            "failure_count = failure_count + excluded.failure_count",
            (h, fix_clean, 1 if success else 0, 0 if success else 1),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
