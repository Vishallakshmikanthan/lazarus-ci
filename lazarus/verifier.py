"""Lazarus-CI Independent Verifier (Module 4).

Deterministic verifier with zero LLM reliance.
Compares pipeline execution state before and after fix application.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple


def _counts(obs: Dict[str, Any]) -> Tuple[int, int]:
    """Count passed stages and total stages."""
    stages = obs.get("pipeline_stages") or {}
    if not isinstance(stages, dict):
        return 0, 0
    passed = sum(1 for v in stages.values() if str(v).lower() == "passed")
    return passed, len(stages)


def verify(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """
    Independently assess whether a fix improved, regressed, or kept the pipeline state.
    
    Verdicts:
        - passed: All stages passed, pipeline status is passed.
        - advanced: Progress made (previous failing stage resolved, or new stage reached).
        - unchanged: No movement in stages or errors.
        - regressed: Destructive side-effects or fewer passed stages.
    """
    pb, _ = _counts(before)
    pa, total = _counts(after)

    destr_before = int(before.get("destructive_actions") or 0)
    destr_after = int(after.get("destructive_actions") or 0)
    new_destructive = max(0, destr_after - destr_before)

    detail = {
        "stages_passed_before": pb,
        "stages_passed_after": pa,
        "stages_total": total,
        "pipeline_status": after.get("pipeline_status"),
        "new_destructive_actions": new_destructive,
    }

    # 1. Regression check: destructive actions introduced or fewer stages passing
    if new_destructive > 0 or pa < pb:
        return {"verdict": "regressed", **detail}

    # 2. Complete resolution check
    if after.get("pipeline_status") == "passed" or (total > 0 and pa == total):
        return {"verdict": "passed", **detail}

    # 3. Progress check (cascading failure or stage advance)
    issues_before = int(before.get("revealed_issue_count") or 1)
    issues_after = int(after.get("revealed_issue_count") or 1)
    revealed_more = issues_after > issues_before

    if pa > pb or revealed_more:
        return {"verdict": "advanced", **detail}

    # 4. Unchanged
    return {"verdict": "unchanged", **detail}
