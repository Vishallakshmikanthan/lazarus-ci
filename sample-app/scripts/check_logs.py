#!/usr/bin/env python3
"""Log validation script for the CI/CD pipeline build stage.

Performs two independent checks:

  1. Static config validation (always runs):
       - logging_config.py is syntactically valid Python
       - Required variables are defined: LOG_PATH, LOG_LEVEL, MAX_BYTES, BACKUP_COUNT
       - RotatingFileHandler is used (rotation configured)
       - LOG_LEVEL is not CRITICAL (effective logging disabled)
       - JSON formatter emits all required fields: timestamp, level, message, service
       - Source files do not log credential/token values (PII static scan)

  2. Runtime log-file validation (skipped with --config-only):
       - Log file exists at LOG_PATH (volume mount reachable)
       - Each line is valid JSON
       - No ERROR-level records present
       - No PII patterns in any field (token prefixes, Bearer headers)
       - WARN ratio does not exceed threshold (default 25%)

Exit codes:
    0   All active checks passed.
    1   One or more failures detected.

Usage:
    python scripts/check_logs.py                        # full check
    python scripts/check_logs.py --config-only          # static only (pipeline build gate)
    python scripts/check_logs.py --log-path /tmp/x.log  # override log path
    python scripts/check_logs.py --strict               # treat warn-ratio as failure
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# PII patterns — checked in both source files and runtime log lines
# ---------------------------------------------------------------------------

_RUNTIME_PII: list[tuple[str, re.Pattern]] = [
    ("api_key_token",  re.compile(r"(?:sk-live|sk-test|sk_live|sk_test|ghp_|gho_|github_pat_|AKIA)[A-Za-z0-9_\-]{8,}")),
    ("password_value", re.compile(r"(?i)(?:password|passwd|secret)\s*[=:]\s*['\"]?\S{6,}")),
    ("bearer_token",   re.compile(r"(?i)Authorization:\s*Bearer\s+\S+")),
    ("credit_card",    re.compile(r"\b4[0-9]{12}(?:[0-9]{3})?\b")),
]

# Patterns that indicate a .py source file is logging credential-like values
_SOURCE_PII: list[tuple[str, re.Pattern]] = [
    ("token_in_log_call",    re.compile(r'_log\.\w+\s*\([^)]*(?:sk-live|sk-test|AKIA|Bearer\s+sk)[^)]*\)', re.DOTALL)),
    ("password_in_log_call", re.compile(r'_log\.\w+\s*\([^)]*(?i:password|passwd|secret)[^)]*[=:]\s*["\'][^"\']{6,}["\'][^)]*\)', re.DOTALL)),
]

_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "WARN", "ERROR", "CRITICAL"}
_SILENCING_LEVELS = {"CRITICAL"}

# ---------------------------------------------------------------------------
# 1. Static config validation
# ---------------------------------------------------------------------------

def validate_config(config_path: Path, routes_path: Path | None = None) -> list[str]:
    """Validate logging_config.py statically. Returns list of failure strings."""
    failures: list[str] = []

    if not config_path.exists():
        return [f"logging_config.py not found at {config_path}"]

    src = config_path.read_text(encoding="utf-8")

    # Parseable Python
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [f"SyntaxError in logging_config.py: {exc}"]

    # Required module-level assignments
    assigned: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assigned.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned.add(node.target.id)

    for var in ("LOG_PATH", "LOG_LEVEL", "MAX_BYTES", "BACKUP_COUNT"):
        if var not in assigned:
            failures.append(f"logging_config.py is missing required variable: {var}")

    # Rotation must be configured — check for the call site, not just the word
    # (the word may appear in docstrings/comments even after the handler is removed)
    if "RotatingFileHandler(" not in src:
        failures.append(
            "logging_config.py does not use RotatingFileHandler — "
            "logs may grow unboundedly without rotation"
        )

    # LOG_LEVEL must not silence all output
    level_m = re.search(r'LOG_LEVEL\s*(?::\s*\w+)?\s*=\s*["\']([A-Z]+)["\']', src)
    if level_m:
        level = level_m.group(1)
        if level in _SILENCING_LEVELS:
            failures.append(
                f"LOG_LEVEL is hardcoded to {level!r} — "
                "this silences all log output below CRITICAL"
            )
        elif level not in _VALID_LEVELS:
            failures.append(f"LOG_LEVEL {level!r} is not a recognised logging level")

    # LOG_PATH must not point to a restricted system directory
    path_m = re.search(r'LOG_PATH\s*(?::\s*\w+)?\s*=\s*["\']([^"\']+)["\']', src)
    if path_m:
        declared = path_m.group(1)
        if declared.startswith(("/var/log", "/root", "/sys", "/proc")):
            failures.append(
                f"LOG_PATH default {declared!r} points to a restricted system directory "
                "— the application process cannot write there"
            )

    # Formatter must serialise to JSON (not str(dict) or similar)
    if "json.dumps" not in src:
        failures.append(
            "logging_config.py formatter does not call json.dumps() — "
            "log records will not be valid JSON"
        )

    # JSON formatter must emit required fields
    for field in ("timestamp", "level", "message", "service"):
        if f'"{field}"' not in src and f"'{field}'" not in src:
            failures.append(
                f"JSON formatter appears to be missing required field: {field!r} — "
                "structured log records will be incomplete"
            )

    # Static PII scan of source files
    for label, pattern in _SOURCE_PII:
        if pattern.search(src):
            failures.append(
                f"logging_config.py contains a log call that may emit credential values "
                f"[pattern: {label}]"
            )

    if routes_path and routes_path.exists():
        routes_src = routes_path.read_text(encoding="utf-8")
        for label, pattern in _SOURCE_PII:
            if pattern.search(routes_src):
                failures.append(
                    f"routes.py contains a log call that may emit credential values "
                    f"[pattern: {label}] — PII leak risk"
                )

    return failures


# ---------------------------------------------------------------------------
# 2. Runtime log-file validation
# ---------------------------------------------------------------------------

def validate_logfile(
    log_path: Path,
    strict_warns: bool = False,
    max_warn_ratio: float = 0.25,
) -> list[str]:
    """Parse a JSON log file and look for errors, PII, and malformed records."""
    if not log_path.exists():
        return [
            f"Log file not found: {log_path} — "
            "check that the logs volume is mounted and LOG_PATH is correct"
        ]

    failures: list[str] = []
    error_lines: list[str] = []
    pii_hits: list[str] = []
    warn_count = 0
    total = 0
    bad_json = 0

    for lineno, raw in enumerate(
        log_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        raw = raw.strip()
        if not raw:
            continue
        total += 1

        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            bad_json += 1
            if bad_json <= 5:
                failures.append(
                    f"Line {lineno}: not valid JSON (malformed log record): {raw[:80]!r}"
                )
            continue

        level = record.get("level", "").upper()
        message = record.get("message", "")

        if level == "ERROR":
            error_lines.append(f"line {lineno}: {message[:120]}")
        if level in ("WARNING", "WARN"):
            warn_count += 1

        full_text = json.dumps(record)
        for label, pattern in _RUNTIME_PII:
            m = pattern.search(full_text)
            if m:
                pii_hits.append(f"line {lineno} [{label}]: {m.group()[:80]}")

    if bad_json > 5:
        failures.append(f"... and {bad_json - 5} more malformed JSON lines")

    if error_lines:
        failures.append(f"Found {len(error_lines)} ERROR-level log record(s):")
        failures.extend(f"  {e}" for e in error_lines[:10])
        if len(error_lines) > 10:
            failures.append(f"  ... and {len(error_lines) - 10} more")

    if pii_hits:
        failures.append(f"PII or credential value detected in {len(pii_hits)} log line(s):")
        failures.extend(f"  {h}" for h in pii_hits[:5])

    if total > 0:
        ratio = warn_count / total
        if ratio > max_warn_ratio:
            msg = (
                f"Excessive WARN ratio: {warn_count}/{total} records "
                f"({ratio:.0%}) — investigate log noise"
            )
            if strict_warns:
                failures.append(msg)
            else:
                print(f"[WARN] {msg}", file=sys.stderr)

    return failures


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _default_config_path() -> str:
    script_dir = Path(__file__).resolve().parent
    return str(script_dir.parent / "services" / "api" / "logging_config.py")


def _default_routes_path() -> str:
    script_dir = Path(__file__).resolve().parent
    return str(script_dir.parent / "services" / "api" / "routes.py")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate structured logging configuration and log file output."
    )
    parser.add_argument(
        "--log-path",
        default=os.environ.get("LOG_PATH", "/app/logs/app.log"),
        help="Path to the JSON log file to validate.",
    )
    parser.add_argument(
        "--config-path",
        default=_default_config_path(),
        help="Path to logging_config.py to validate.",
    )
    parser.add_argument(
        "--routes-path",
        default=_default_routes_path(),
        help="Path to routes.py for static PII scan.",
    )
    parser.add_argument(
        "--config-only",
        action="store_true",
        help="Only run static config validation; skip log file checks.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat excessive WARN ratio as a hard failure.",
    )
    args = parser.parse_args()

    all_failures: list[str] = []

    config_failures = validate_config(
        Path(args.config_path),
        routes_path=Path(args.routes_path),
    )
    if config_failures:
        print("CONFIG VALIDATION FAILED:", file=sys.stderr)
        for f in config_failures:
            print(f"  FAIL: {f}", file=sys.stderr)
        all_failures.extend(config_failures)
    else:
        print("Config validation passed.", file=sys.stderr)

    if not args.config_only:
        log_failures = validate_logfile(Path(args.log_path), strict_warns=args.strict)
        if log_failures:
            print("LOG FILE VALIDATION FAILED:", file=sys.stderr)
            for f in log_failures:
                print(f"  FAIL: {f}", file=sys.stderr)
            all_failures.extend(log_failures)
        else:
            print(f"Log file validation passed ({args.log_path}).", file=sys.stderr)

    if all_failures:
        print(f"\ncheck_logs: {len(all_failures)} issue(s) found.", file=sys.stderr)
        return 1

    print("check_logs: all validations passed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
