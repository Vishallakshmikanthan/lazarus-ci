"""Lazarus-CI Risk Gate (Module 3).

Deterministic code policy for assessing action risk and gating human approval.
The LLM never makes this decision.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

ALLOWED_FIX_OPS = {"modify_config", "add_dependency"}

# Sensitive infrastructure, secrets, credentials, or permission keywords
HIGH_PATTERN = re.compile(
    r"(secret|password|passwd|token|credential|api[_-]?key|iam|permission|terraform|tfstate|\.env\b|rotate)",
    re.IGNORECASE,
)

# Destructive operation keywords
DESTRUCTIVE_PATTERN = re.compile(
    r"(\brm\b|\bdelete\b|\bdrop\s|truncate|wipe|--force|force[- ]push)",
    re.IGNORECASE,
)


def assess(fix: Dict[str, Any], confidence: float = 0.8) -> Dict[str, Any]:
    """
    Assess candidate fix risk using deterministic rules.
    
    Returns:
        {
            "level": "low" | "medium" | "high" | "blocked",
            "needs_approval": bool,
            "reasons": List[str]
        }
    """
    op = fix.get("operation", "")
    target = str(fix.get("target", ""))
    val = str(fix.get("value", ""))
    blob = f"{target} {val}"

    # Disallowed operations are strictly blocked
    if op not in ALLOWED_FIX_OPS:
        return {
            "level": "blocked",
            "needs_approval": True,
            "reasons": [f"operation '{op}' is not an allowed fix action (allowed: {sorted(ALLOWED_FIX_OPS)})"],
        }

    level = "medium" if op == "modify_config" else "low"
    reasons: List[str] = []

    # High-risk target or value checks
    if HIGH_PATTERN.search(blob):
        level = "high"
        reasons.append("touches secrets / credentials / IAM permissions / infrastructure config")

    # Destructive pattern checks
    if DESTRUCTIVE_PATTERN.search(blob):
        level = "high"
        reasons.append("contains potentially destructive keywords")

    # Low confidence forces human review
    if confidence < 0.60:
        reasons.append(f"low confidence score ({confidence:.2f} < 0.60 threshold)")

    needs_approval = (level == "high") or (confidence < 0.60)

    return {
        "level": level,
        "needs_approval": needs_approval,
        "reasons": reasons,
    }
