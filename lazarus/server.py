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


CHAOS_FAULTS = [
    {
        "id": "dependency_conflict",
        "name": "Dependency Conflict",
        "category": "build",
        "stage": "build",
        "risk": "low",
        "target": "services/api/requirements.txt",
        "description": "Incompatible requests & urllib3 pins causing pip resolver failure during build.",
        "badge": "LOW RISK",
        "demo_tag": "Demo: Auto-Heal",
    },
    {
        "id": "docker_order",
        "name": "Dockerfile Layer Order",
        "category": "build",
        "stage": "build",
        "risk": "medium",
        "target": "Dockerfile",
        "description": "Source files copied before package dependencies are installed, breaking build.",
        "badge": "MEDIUM RISK",
    },
    {
        "id": "merge_conflict",
        "name": "Git Merge Conflict Markers",
        "category": "build",
        "stage": "build",
        "risk": "medium",
        "target": "Dockerfile",
        "description": "Unresolved git conflict markers ('<<<<<<< HEAD') left in Dockerfile or routes.",
        "badge": "MEDIUM RISK",
    },
    {
        "id": "missing_package_init",
        "name": "Missing Package __init__.py",
        "category": "build",
        "stage": "build",
        "risk": "medium",
        "target": "services/runtime_support/__init__.py",
        "description": "Python runtime support directory missing __init__.py causing ModuleNotFoundError.",
        "badge": "MEDIUM RISK",
    },
    {
        "id": "bad_migration_sql",
        "name": "Bad Migration SQL Syntax",
        "category": "build",
        "stage": "build",
        "risk": "medium",
        "target": "db/migrations/001_init.sql",
        "description": "SQL syntax error inside database migration script causing schema crash.",
        "badge": "MEDIUM RISK",
    },
    {
        "id": "secret_exposure",
        "name": "Hardcoded Secret Exposure",
        "category": "security",
        "stage": "build",
        "risk": "high",
        "target": "services/api/app.py",
        "description": "Sensitive API token/credential leaked in application source; triggers SRE Risk Gate.",
        "badge": "🚨 HIGH RISK (GATE)",
        "demo_tag": "Demo: Risk Gate",
    },
    {
        "id": "log_pii_leak",
        "name": "PII & Credit Card Log Leak",
        "category": "security",
        "stage": "build",
        "risk": "high",
        "target": "services/api/routes.py",
        "description": "Unmasked credit card data emitted directly to stdout/stderr application logs.",
        "badge": "🚨 HIGH RISK (GATE)",
    },
    {
        "id": "log_disabled",
        "name": "Logging Suppressed / Disabled",
        "category": "security",
        "stage": "build",
        "risk": "medium",
        "target": "services/api/logging_config.py",
        "description": "Logging level set to critical/disabled, creating blind spot for CI/CD observability.",
        "badge": "MEDIUM RISK",
    },
    {
        "id": "flaky_test",
        "name": "Non-Deterministic Flaky Test",
        "category": "test",
        "stage": "test",
        "risk": "low",
        "target": "tests/test_api.py",
        "description": "Asynchronous race condition causing intermittent test pipeline failures.",
        "badge": "LOW RISK",
        "demo_tag": "Test Failure",
    },
    {
        "id": "empty_secret_key",
        "name": "Empty SECRET_KEY Environment",
        "category": "security",
        "stage": "deploy",
        "risk": "high",
        "target": ".env",
        "description": "Blank SECRET_KEY in .env causing runtime session encryption failure at boot.",
        "badge": "🚨 HIGH RISK (GATE)",
        "demo_tag": "Demo: Risk Gate",
    },
    {
        "id": "missing_permission",
        "name": "Container Network Permissions",
        "category": "deploy",
        "stage": "deploy",
        "risk": "medium",
        "target": "docker-compose.yml",
        "description": "Docker bridge network permission mismatch preventing inter-service communication.",
        "badge": "MEDIUM RISK",
    },
    {
        "id": "env_drift",
        "name": "Environment Variable Drift",
        "category": "deploy",
        "stage": "deploy",
        "risk": "medium",
        "target": "docker-compose.yml",
        "description": "Port and host configuration drift between compose service definition and runtime.",
        "badge": "MEDIUM RISK",
    },
    {
        "id": "invalid_database_url",
        "name": "Invalid DATABASE_URL Config",
        "category": "deploy",
        "stage": "deploy",
        "risk": "medium",
        "target": ".env",
        "description": "Malformed database connection URL causing connection refused during health check.",
        "badge": "MEDIUM RISK",
    },
    {
        "id": "missing_pythonpath",
        "name": "Corrupted Virtualenv PYTHONPATH",
        "category": "deploy",
        "stage": "deploy",
        "risk": "medium",
        "target": ".venv/runtime.pth",
        "description": "Virtual environment path bootstrap corrupted, causing runtime module import errors.",
        "badge": "MEDIUM RISK",
    },
    {
        "id": "circular_import_runtime",
        "name": "Runtime Circular Import",
        "category": "deploy",
        "stage": "deploy",
        "risk": "medium",
        "target": "services/api/runtime_probe.py",
        "description": "Lazy circular import loop triggered only when handling runtime probe requests.",
        "badge": "MEDIUM RISK",
    },
    {
        "id": "none_config_runtime",
        "name": "NoneType Runtime Config Attribute",
        "category": "deploy",
        "stage": "deploy",
        "risk": "medium",
        "target": ".env",
        "description": "Unset configuration value evaluated to None causing AttributeError on first request.",
        "badge": "MEDIUM RISK",
    },
    {
        "id": "schema_drift",
        "name": "Database Schema Column Drift",
        "category": "deploy",
        "stage": "deploy",
        "risk": "medium",
        "target": "db/database.py",
        "description": "Application query column list out of sync with actual database migration schema.",
        "badge": "MEDIUM RISK",
    },
    {
        "id": "terraform_permission_denied",
        "name": "Terraform IAM Permission Denied",
        "category": "terraform",
        "stage": "deploy",
        "risk": "high",
        "target": "infra/main.tf",
        "description": "Cloud IAM policy denying terraform apply; requires deterministic SRE risk gate sign-off.",
        "badge": "🚨 HIGH RISK (GATE)",
        "demo_tag": "Demo: IaC Gate",
    },
    {
        "id": "terraform_invalid_provider",
        "name": "Terraform Invalid Provider Registry",
        "category": "terraform",
        "stage": "deploy",
        "risk": "high",
        "target": "infra/main.tf",
        "description": "Malformed provider block in main.tf pointing to non-existent registry source.",
        "badge": "🚨 HIGH RISK (GATE)",
    },
    {
        "id": "terraform_missing_variable",
        "name": "Terraform Missing Required Variable",
        "category": "terraform",
        "stage": "deploy",
        "risk": "medium",
        "target": "infra/terraform.tfvars",
        "description": "Required infrastructure input variable missing from terraform.tfvars breaking plan.",
        "badge": "MEDIUM RISK",
    },
]


@app.get("/api/faults")
def list_chaos_faults():
    """Return catalog of 20 canonical chaos faults with metadata."""
    return {"faults": CHAOS_FAULTS}


@app.post("/incident/start")
def start_incident(req: Optional[StartReq] = None):
    """Trigger an autonomous incident repair cycle."""
    inc_id = store.new_incident()
    fault = req.fault if req and req.fault else None
    if fault and inc_id in store.INCIDENTS:
        store.INCIDENTS[inc_id]["fault"] = fault
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
    return {"incident_id": inc_id, "status": "started", "fault": fault}


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
