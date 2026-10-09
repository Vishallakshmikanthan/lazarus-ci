"""Database fault registry, injection, and fix verification.

Each fault name maps to a spec that describes:
  - what it simulates
  - what observable failure it produces
  - the correct repair action
  - which backend it applies to

inject_fault(name, workspace)  → mutates real files to activate the fault
verify_fix(name, workspace)    → returns (passed: bool, message: str)

Both functions accept an optional workspace_path; they default to the repo root.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Tuple, TypedDict

# ---------------------------------------------------------------------------
# Paths (resolved relative to this file's location)
# ---------------------------------------------------------------------------

_REPO_ROOT   = Path(__file__).parent.parent
_MIGRATION   = _REPO_ROOT / "db" / "migrations" / "001_init.sql"
_DATABASE_PY = _REPO_ROOT / "db" / "database.py"


def _migration(wp: Path) -> Path:
    return wp / "db" / "migrations" / "001_init.sql"

def _database_py(wp: Path) -> Path:
    return wp / "db" / "database.py"


# ---------------------------------------------------------------------------
# Fault registry
# ---------------------------------------------------------------------------

class FaultSpec(TypedDict):
    description: str
    breaks: str
    correct_fix: str
    affects: str   # "sqlite" | "postgres" | "both"


FAULT_REGISTRY: Dict[str, FaultSpec] = {
    "BAD_MIGRATION_SQL": {
        "description": (
            "Syntax error injected in 001_init.sql: 'CREATE TABLE' replaced with "
            "'CREAT TABLE', making the migration unparseable."
        ),
        "breaks": (
            "Database fails to initialise on startup. Every API endpoint returns 500 "
            "with 'OperationalError: near \"TABLE\": syntax error'."
        ),
        "correct_fix": (
            "Fix the typo in db/migrations/001_init.sql: change 'CREAT TABLE' back "
            "to 'CREATE TABLE'."
        ),
        "affects": "both",
    },
    "SCHEMA_DRIFT": {
        "description": (
            "db/database.py CANONICAL_COLUMNS includes 'artifact_url', a column the "
            "schema migration never added to the builds table."
        ),
        "breaks": (
            "Any query that selects or inserts artifact_url raises "
            "'OperationalError: table builds has no column named artifact_url'."
        ),
        "correct_fix": (
            "Either add a new migration that adds the artifact_url column to builds, "
            "or remove 'artifact_url' from CANONICAL_COLUMNS in db/database.py."
        ),
        "affects": "both",
    },
}


# ---------------------------------------------------------------------------
# Injection helpers
# ---------------------------------------------------------------------------

def inject_fault(fault_name: str, workspace_path: Path | None = None) -> None:
    """Mutate real project files to activate *fault_name*."""
    wp = workspace_path or _REPO_ROOT
    _require_known(fault_name)
    {
        "BAD_MIGRATION_SQL": _inject_bad_migration,
        "SCHEMA_DRIFT":      _inject_schema_drift,
    }[fault_name](wp)


def _inject_bad_migration(wp: Path) -> None:
    path = _migration(wp)
    text = path.read_text(encoding="utf-8")
    patched = text.replace(
        "CREATE TABLE IF NOT EXISTS builds",
        "CREAT TABLE IF NOT EXISTS builds",
        1,
    )
    path.write_text(patched, encoding="utf-8")


def _inject_schema_drift(wp: Path) -> None:
    path = _database_py(wp)
    text = path.read_text(encoding="utf-8")
    old = '    "id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail",'
    new = '    "id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail", "artifact_url",'
    if old not in text:
        raise RuntimeError("SCHEMA_DRIFT inject: expected CANONICAL_COLUMNS line not found in database.py")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

def verify_fix(fault_name: str, workspace_path: Path | None = None) -> Tuple[bool, str]:
    """
    Check whether the agent has correctly resolved *fault_name*.
    Returns (passed, message).
    """
    wp = workspace_path or _REPO_ROOT
    _require_known(fault_name)
    return {
        "BAD_MIGRATION_SQL": _verify_bad_migration,
        "SCHEMA_DRIFT":      _verify_schema_drift,
    }[fault_name](wp)


def _verify_bad_migration(wp: Path) -> Tuple[bool, str]:
    sql = _migration(wp).read_text(encoding="utf-8")
    if "CREAT TABLE" in sql:
        return False, "Migration still contains the 'CREAT TABLE' syntax error."
    if "CREATE TABLE" not in sql:
        return False, "Migration is missing CREATE TABLE statement entirely."
    return True, "Migration SQL is syntactically correct."


def _verify_schema_drift(wp: Path) -> Tuple[bool, str]:
    src = _database_py(wp).read_text(encoding="utf-8")
    migration_text = _migration(wp).read_text(encoding="utf-8")
    # The fault injects "artifact_url" into the CANONICAL_COLUMNS list literal.
    # We detect it by looking for the injected line specifically, not just any mention
    # of the string (which also appears in the conditional EXPECTED_COLUMNS expression).
    canonical_has_phantom = bool(
        re.search(r'"log_tail",\s*"artifact_url"', src)
    )
    if canonical_has_phantom and "artifact_url" not in migration_text:
        return (
            False,
            "database.py CANONICAL_COLUMNS still includes 'artifact_url' but no migration adds it.",
        )
    return True, "Schema and application model are aligned."


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _require_known(fault_name: str) -> None:
    if fault_name not in FAULT_REGISTRY:
        raise ValueError(
            f"Unknown fault: {fault_name!r}. Valid names: {sorted(FAULT_REGISTRY)}"
        )
