"""Lazarus-CI Server (Module 5).

FastAPI service exposing incident lifecycle, approval webhooks, state polling, and dashboard UI.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, bench, store
from .envclient import Env

logger = logging.getLogger(__name__)

app = FastAPI(title="Lazarus-CI", description="Self-Healing CI/CD Autonomous Agent Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


class StartReq(BaseModel):
    fault: Optional[str] = None


class ApproveReq(BaseModel):
    approved: bool


@app.get("/")
def index():
    """Serve main mission control dashboard."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Lazarus-CI API is live. Static dashboard index.html not yet generated."}


@app.post("/incident/start")
def start_incident(req: Optional[StartReq] = None):
    """Trigger an autonomous incident repair cycle."""
    inc_id = store.new_incident()
    fault = req.fault if req and req.fault else None
    reset_kwargs = {"task_key": fault} if fault else {}

    def background_repair():
        env = Env()
        try:
            obs = env.reset(**reset_kwargs)
        except Exception as e:
            logger.error("Sandbox reset failed for incident %d: %s", inc_id, e)
            store.emit(inc_id, "error", error=f"reset failed: {e}")
            store.finish(
                inc_id,
                "escalated",
                handoff={"reason": f"could not initialize sandbox environment: {e}"},
            )
            return

        agent.run_incident(
            inc_id,
            env,
            store,
            store.wait_for_approval,
            obs=obs,
        )

    t = threading.Thread(target=background_repair, daemon=True)
    t.start()
    return {"incident_id": inc_id, "status": "started"}


@app.get("/incident/{inc_id}/status")
def get_incident_status(inc_id: int):
    """Return current incident lifecycle status."""
    inc = store.INCIDENTS.get(inc_id)
    if not inc:
        return JSONResponse(status_code=404, content={"status": "not_found"})

    summary = None
    if inc["status"] == "escalated":
        summary = (inc["handoff"] or {}).get("reason")
    elif inc["status"] == "resolved":
        summary = "Pipeline successfully repaired and verified."

    return {
        "incident_id": inc_id,
        "status": inc["status"],
        "summary": summary,
        "events": len(inc["events"]),
    }


@app.get("/incident/{inc_id}/events")
def get_incident_events(inc_id: int):
    """Return all timeline events for a given incident."""
    inc = store.INCIDENTS.get(inc_id)
    if not inc:
        return JSONResponse(status_code=404, content={"detail": "not_found"})
    return {"incident_id": inc_id, "events": inc["events"]}


@app.post("/incident/{inc_id}/approve")
def approve_incident(inc_id: int, req: ApproveReq):
    """Submit human approval verdict."""
    if inc_id not in store.INCIDENTS:
        return JSONResponse(status_code=404, content={"error": "Incident not found"})
    store.decide(inc_id, req.approved)
    return {"ok": True, "incident_id": inc_id, "decision": req.approved}


@app.get("/api/state")
def get_state():
    """Comprehensive state polling endpoint for the dashboard."""
    latest_id = max(store.INCIDENTS.keys()) if store.INCIDENTS else None
    inc = store.INCIDENTS.get(latest_id) if latest_id else None

    safe_inc = None
    if inc:
        safe_inc = {k: v for k, v in inc.items() if k != "events"}

    return {
        "incident": safe_inc,
        "events": inc["events"] if inc else [],
        "bench": store.BENCH,
        "now": time.time(),
    }


@app.post("/bench/run")
def trigger_benchmark(body: Optional[Dict[str, Any]] = None):
    """Trigger background benchmark run."""
    n_episodes = int((body or {}).get("n", 6))

    def _worker():
        bench.run(n_episodes)

    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True, "episodes": n_episodes}
