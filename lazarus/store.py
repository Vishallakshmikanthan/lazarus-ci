"""Lazarus-CI Incident & Event Store (Module 5).

Thread-safe memory store with incident lifecycle, approval state, and handoff reports.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

_L = threading.RLock()
INCIDENTS: Dict[int, Dict[str, Any]] = {}
APPROVALS: Dict[int, Dict[str, Any]] = {}  # id -> {"decision": Optional[bool], "requested_at": float}
BENCH: Dict[str, Any] = {"lazarus": None, "naive": None}


def new_incident() -> int:
    """Create and initialize a new incident entry."""
    with _L:
        i = len(INCIDENTS) + 1
        INCIDENTS[i] = {
            "id": i,
            "status": "running",
            "created_at": time.time(),
            "resolved_at": None,
            "events": [],
            "stages": {},
            "approval": None,
            "handoff": None,
        }
        return i


def emit(i: int, kind: str, **payload: Any) -> None:
    """Emit an event onto the incident timeline."""
    with _L:
        if i not in INCIDENTS:
            return
        inc = INCIDENTS[i]
        event_entry = {
            "id": len(inc["events"]) + 1,
            "ts": time.time(),
            "kind": kind,
            "payload": payload,
        }
        inc["events"].append(event_entry)

        if kind == "approval_request":
            inc["status"] = "awaiting_approval"
            inc["approval"] = payload
            APPROVALS[i] = {"decision": None, "requested_at": time.time()}
        elif kind in ("approval", "plan") and inc["status"] == "awaiting_approval":
            inc["status"] = "running"


def set_stages(i: int, stages: Optional[Dict[str, str]]) -> None:
    """Update pipeline stages status dictionary for incident."""
    with _L:
        if i in INCIDENTS:
            INCIDENTS[i]["stages"] = dict(stages or {})


def finish(i: int, status: str, handoff: Optional[Dict[str, Any]] = None) -> None:
    """Mark an incident as finished (resolved or escalated)."""
    with _L:
        if i in INCIDENTS:
            INCIDENTS[i].update(status=status, resolved_at=time.time(), handoff=handoff)
    try:
        with open("lazarus_incidents.json", "w", encoding="utf-8") as f:
            json.dump(INCIDENTS, f, default=str, indent=2)
    except Exception:
        pass


def decide(i: int, approved: bool) -> None:
    """Submit a human approval decision."""
    with _L:
        if i in APPROVALS:
            APPROVALS[i]["decision"] = bool(approved)


def wait_for_approval(i: int, timeout: float = 180.0) -> Optional[bool]:
    """Block until human submits approval decision or timeout expires."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        with _L:
            d = APPROVALS.get(i, {}).get("decision")
        if d is not None:
            return d
        time.sleep(0.5)
    return None


def build_handoff(i: int, reason: str, last_verdict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Assemble complete SRE handoff report for escalated incidents."""
    with _L:
        inc = INCIDENTS.get(i, {"events": []})
        ev = inc.get("events", [])
        get = lambda k: [e["payload"] for e in ev if e.get("kind") == k]

        return {
            "incident_id": i,
            "reason": reason,
            "root_cause_best_guess": (last_verdict or {}).get("root_cause") or "Root cause undetermined",
            "confidence": (last_verdict or {}).get("confidence", 0.0),
            "hypotheses_tested": get("hypotheses")[-1:] if get("hypotheses") else None,
            "fixes_attempted": get("apply"),
            "verifier_results": get("verify"),
            "suggested_next_step": (
                "Review failing stage logs and candidate diffs above. "
                "The pipeline was preserved in its last known verified checkpoint."
            ),
        }
