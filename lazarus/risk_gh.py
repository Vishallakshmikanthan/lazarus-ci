"""Lazarus-CI Risk Gate for GitHub Operations.

Evaluates deterministic risk policy before merging PRs into main.
The LLM never makes this decision.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

ALLOWED = {"modify_config", "add_dependency"}
SENSITIVE = re.compile(
    r"(secret|password|token|credential|api[_-]?key|permission|\biam\b|terraform|\.env\b)",
    re.IGNORECASE,
)


def assess(fix: Dict[str, Any], confidence: float, diff: str = "") -> Dict[str, Any]:
    """Assess risk of candidate fix prior to merging to main."""
    op = fix.get("operation", "")
    path = str(fix.get("target", ""))
    val = str(fix.get("value", ""))

    if op not in ALLOWED:
        return {
            "level": "blocked",
            "needs_approval": True,
            "reasons": [f"operation '{op}' is not allowed (allowed: {sorted(ALLOWED)})"],
        }

    level = "low" if op == "add_dependency" else "medium"
    reasons: List[str] = []

    # High risk if modifying GitHub workflow files (.github/workflows/*)
    if path.startswith(".github/") or ".github/workflows" in path:
        level = "high"
        reasons.append("changes CI/CD workflow definitions (.github/workflows)")

    # High risk if modifying tests (prevent test tampering / weakening tests)
    if path.startswith("tests/") or "tests" in path:
        level = "high"
        reasons.append("modifies test code (potential test tampering)")

    # High risk if touching secrets or sensitive credentials
    if SENSITIVE.search(path) or (not path.startswith(".github/") and SENSITIVE.search(val)):
        level = "high"
        reasons.append("touches secrets / credentials / IAM permissions")

    # Check diff size
    changed_lines = sum(
        1 for line in diff.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
    if changed_lines > 40:
        reasons.append(f"large diff ({changed_lines} changed lines)")
        if level == "low":
            level = "medium"

    # Low confidence triggers human approval
    if confidence < 0.60:
        reasons.append(f"model confidence is low ({confidence:.2f} < 0.60)")

    needs_approval = level == "high" or confidence < 0.60

    return {
        "level": level,
        "needs_approval": needs_approval,
        "reasons": reasons,
    }
