# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Curriculum controller: UCB1 fault selection + EMA difficulty scheduling."""

from __future__ import annotations

import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional

try:
    from cicd.fault_types import FAULT_TYPES
except ImportError:
    from ..cicd.fault_types import FAULT_TYPES

_DB_PATH = Path(os.getenv("AGENT_MEMORY_DB_PATH", str(Path(__file__).parent / "agent_memory.db")))

_EMA_ALPHA = float(os.getenv("CURRICULUM_EMA_ALPHA", "0.20"))
_UCB_C = float(os.getenv("CURRICULUM_UCB_C", "0.5"))
_WARMUP_EPISODES = int(os.getenv("CURRICULUM_WARMUP", "2"))
_DIFFICULTY_MIN = 0.20
_DIFFICULTY_MAX = 0.95


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS curriculum_episodes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            fault_type  TEXT    NOT NULL,
            difficulty  REAL    NOT NULL DEFAULT 0.5,
            final_score REAL    NOT NULL DEFAULT 0.0,
            resolved    INTEGER NOT NULL DEFAULT 0,
            steps_used  INTEGER NOT NULL DEFAULT 0,
            gym_mode    TEXT    NOT NULL DEFAULT 'standard',
            created_at  REAL    NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS curriculum_state (
            key   TEXT PRIMARY KEY,
            value REAL NOT NULL
        )
    """)
    db.commit()
    return db


class CurriculumController:
    """
    Tracks every episode outcome and schedules the next fault type + difficulty.

    Two algorithms run in tandem:
      - EMA difficulty: maps recent score trend → global difficulty in [0.2, 0.95]
      - UCB1 fault selection: prioritizes under-trained and low win-rate faults
    """

    def record_episode(
        self,
        fault_type: str,
        difficulty: float,
        final_score: float,
        resolved: bool,
        steps_used: int,
    ) -> None:
        """Persist episode outcome and update EMA difficulty."""
        db = _conn()
        try:
            db.execute(
                "INSERT INTO curriculum_episodes "
                "(fault_type, difficulty, final_score, resolved, steps_used, gym_mode, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (fault_type, difficulty, final_score, int(resolved), steps_used, "unified", time.time()),
            )
            new_ema = self._compute_ema(db, final_score)
            db.execute(
                "INSERT INTO curriculum_state (key, value) VALUES ('ema_difficulty', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (new_ema,),
            )
            db.commit()
        finally:
            db.close()

    def get_difficulty(self) -> float:
        """Current scheduled difficulty in [0.2, 0.95]."""
        db = _conn()
        try:
            row = db.execute(
                "SELECT value FROM curriculum_state WHERE key = 'ema_difficulty'"
            ).fetchone()
        finally:
            db.close()
        return float(row[0]) if row else 0.20

    def select_fault_type(self) -> str:
        """
        UCB1 selection: balances exploiting agent weaknesses vs. exploring all faults.
        Falls back to round-robin until every fault has _WARMUP_EPISODES episodes.
        """
        db = _conn()
        try:
            stats = self._per_fault_stats(db)
            total = sum(s["attempts"] for s in stats.values())
        finally:
            db.close()

        # Warmup: ensure each fault type is seen at least _WARMUP_EPISODES times
        for ft in FAULT_TYPES:
            if stats.get(ft, {}).get("attempts", 0) < _WARMUP_EPISODES:
                return ft

        # UCB1: weakness + exploration bonus
        best_ft, best_score = FAULT_TYPES[0], -1.0
        for ft in FAULT_TYPES:
            s = stats.get(ft, {"win_rate": 0.5, "attempts": 1})
            weakness = 1.0 - s["win_rate"]
            exploration = _UCB_C * math.sqrt(math.log(max(2, total)) / max(1, s["attempts"]))
            ucb = weakness + exploration
            if ucb > best_score:
                best_score, best_ft = ucb, ft

        return best_ft

    def get_skill_profile(self) -> Dict[str, dict]:
        """
        Per-fault-type stats shaped for the adversarial designer.
        Mastery: low (<0.4 win_rate) / medium / high (>=0.7).
        """
        db = _conn()
        try:
            stats = self._per_fault_stats(db)
        finally:
            db.close()

        profile: Dict[str, dict] = {}
        for ft, s in stats.items():
            wr = s["win_rate"]
            mastery = "low" if wr < 0.4 else ("high" if wr >= 0.7 else "medium")
            profile[ft] = {
                "win_rate": round(wr, 3),
                "avg_score": round(s["avg_score"], 3),
                "attempts": s["attempts"],
                "mastery_level": mastery,
            }
        # Fill in zero-data entries for faults never yet attempted
        for ft in FAULT_TYPES:
            if ft not in profile:
                profile[ft] = {
                    "win_rate": 0.0,
                    "avg_score": 0.0,
                    "attempts": 0,
                    "mastery_level": "low",
                }
        return profile

    def get_stats_summary(self) -> dict:
        """Human-readable stats — useful for logging at episode start."""
        db = _conn()
        try:
            stats = self._per_fault_stats(db)
            total = db.execute("SELECT COUNT(*) FROM curriculum_episodes").fetchone()[0]
            ema_row = db.execute(
                "SELECT value FROM curriculum_state WHERE key='ema_difficulty'"
            ).fetchone()
        finally:
            db.close()
        return {
            "total_episodes": total,
            "current_difficulty": round(float(ema_row[0]) if ema_row else 0.40, 3),
            "per_fault": stats,
        }

    # ── internals ──────────────────────────────────────────────────────────

    def _per_fault_stats(self, db: sqlite3.Connection) -> Dict[str, dict]:
        rows = db.execute(
            "SELECT fault_type, COUNT(*), AVG(final_score), SUM(resolved) "
            "FROM curriculum_episodes GROUP BY fault_type"
        ).fetchall()
        return {
            ft: {
                "attempts": attempts,
                "avg_score": float(avg_score or 0.0),
                "win_rate": float(resolved_sum) / max(1, attempts),
            }
            for ft, attempts, avg_score, resolved_sum in rows
        }

    def _compute_ema(self, db: sqlite3.Connection, new_score: float) -> float:
        """
        EMA difficulty scheduler anchored to current difficulty.

        The target is computed relative to the current difficulty so that:
          - score >= 0.6  → push difficulty up   (agent is succeeding)
          - score == 0.6  → hold current difficulty (neutral threshold)
          - score <  0.6  → pull difficulty down  (agent is struggling)

        The step size scales with how far the score is from the neutral threshold,
        capped at ±STEP_CAP per episode to prevent wild swings.

        This means the EMA can always climb past any plateau as long as the agent
        keeps scoring above the neutral threshold, regardless of absolute score value.
        """
        _NEUTRAL_THRESHOLD = 0.60   # score at which difficulty holds steady
        _STEP_CAP = 0.15            # max difficulty change per episode (increased from 0.08)

        row = db.execute(
            "SELECT value FROM curriculum_state WHERE key = 'ema_difficulty'"
        ).fetchone()
        current = float(row[0]) if row else 0.40

        # Delta: positive when agent succeeds, negative when struggling
        delta = (new_score - _NEUTRAL_THRESHOLD) * _STEP_CAP / (1.0 - _NEUTRAL_THRESHOLD)
        delta = max(-_STEP_CAP, min(_STEP_CAP, delta))

        # Increased alpha for faster adaptation
        alpha = 0.35  # increased from 0.20 for faster difficulty progression
        new_ema = (1.0 - alpha) * current + alpha * (current + delta)
        return round(max(_DIFFICULTY_MIN, min(_DIFFICULTY_MAX, new_ema)), 4)
