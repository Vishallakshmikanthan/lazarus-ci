"""Dynamic CI/CD API Service - Theme #3 Compliant

Provides:
- REST endpoints for workspace/file management and pipeline job polling
- WebSocket endpoint (/api/ws/{workspace_id}) for real-time push events
  so agents receive stage-started / stage-completed / pipeline-done events
  instead of polling GET /api/pipeline/{job_id}/status every few seconds
- Background workspace GC that evicts workspaces idle for > WORKSPACE_TTL_SECONDS
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from cicd.simulated_runner import SimulatedPipelineRunner, SimulatedPipelineResult, STAGE_ORDER, StageStatus
from cicd.fault_types import FAULT_TYPES
from cicd.simulated_fault_injector import inject_fault_simulated as inject_fault

WORKSPACE_TTL_SECONDS = 1800  # evict workspaces idle for 30 minutes

# ── API Models ──────────────────────────────────────────────────────────────


class WorkspaceCreateRequest(BaseModel):
    fault_type: Optional[str] = Field(None)
    template: str = Field("sample-app")


class WorkspaceCreateResponse(BaseModel):
    workspace_id: str
    status: str
    fault_injected: Optional[str] = None
    message: str
    initial_failure_logs: Optional[str] = None  # pre-run failure output surfaced as the incident alert


class FileReadRequest(BaseModel):
    path: str = Field(...)


class FileReadResponse(BaseModel):
    path: str
    content: str
    exists: bool
    size: int


class FileWriteRequest(BaseModel):
    path: str = Field(...)
    content: str = Field(...)


class FileWriteResponse(BaseModel):
    path: str
    status: str
    message: str


class FileListResponse(BaseModel):
    files: List[str]
    directories: List[str]


class PipelineRunRequest(BaseModel):
    workspace_id: str = Field(...)


class PipelineRunResponse(BaseModel):
    job_id: str
    workspace_id: str
    status: str
    message: str


class PipelineStatusResponse(BaseModel):
    job_id: str
    status: str
    current_stage: Optional[str] = None
    failed_stage: Optional[str] = None
    stages: Dict[str, str]
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    duration: Optional[float] = None


class PipelineLogsResponse(BaseModel):
    job_id: str
    stage: str
    logs: str
    available_stages: List[str]


class WorkspaceStatusResponse(BaseModel):
    workspace_id: str
    exists: bool
    file_count: int
    last_modified: Optional[float] = None
    active_jobs: List[str]


# ── State ───────────────────────────────────────────────────────────────────


@dataclass
class WorkspaceState:
    workspace_id: str
    base_path: str
    created_at: float
    last_modified: float
    fault_type: Optional[str] = None
    active_jobs: List[str] = field(default_factory=list)
    initial_failure_logs: Optional[str] = None  # failure output from the pre-run at creation time


@dataclass
class PipelineJob:
    job_id: str
    workspace_id: str
    status: str  # queued | running | passed | failed
    current_stage: Optional[str] = None
    failed_stage: Optional[str] = None
    stages: Dict[str, str] = field(default_factory=dict)
    result: Optional[SimulatedPipelineResult] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None


# ── WebSocket Connection Manager ────────────────────────────────────────────


class ConnectionManager:
    """Tracks one persistent WebSocket per workspace_id.

    The agent opens ws://host/api/ws/{workspace_id} once per episode and keeps
    it alive. File-op responses and pipeline push events all flow over that
    single connection, eliminating per-request HTTP overhead.
    """

    def __init__(self) -> None:
        # workspace_id → active WebSocket
        self._connections: Dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, workspace_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            old = self._connections.get(workspace_id)
            if old is not None:
                try:
                    await old.close(code=1001)
                except Exception:
                    pass
            self._connections[workspace_id] = ws
        logger.info("WS connected: workspace=%s", workspace_id)

    async def disconnect(self, workspace_id: str) -> None:
        async with self._lock:
            self._connections.pop(workspace_id, None)
        logger.info("WS disconnected: workspace=%s", workspace_id)

    async def send(self, workspace_id: str, message: Dict[str, Any]) -> bool:
        """Send a JSON message to the workspace's connected client.

        Returns False if no client is connected.
        """
        ws = self._connections.get(workspace_id)
        if ws is None:
            return False
        try:
            await ws.send_json(message)
            return True
        except Exception as exc:
            logger.warning("WS send failed for workspace=%s: %s", workspace_id, exc)
            async with self._lock:
                self._connections.pop(workspace_id, None)
            return False

    def is_connected(self, workspace_id: str) -> bool:
        return workspace_id in self._connections


ws_manager = ConnectionManager()


# ── Global API State ────────────────────────────────────────────────────────


class CICDAPIState:
    def __init__(self) -> None:
        self.workspaces: Dict[str, WorkspaceState] = {}
        self.jobs: Dict[str, PipelineJob] = {}
        self.rate_limits: Dict[str, List[float]] = {}
        self._lock = asyncio.Lock()

    async def create_workspace(self, fault_type: Optional[str] = None) -> WorkspaceState:
        workspace_id = str(uuid.uuid4())
        import tempfile
        base_path = tempfile.mkdtemp(prefix=f"cicd-ws-{workspace_id[:8]}-")

        sample_app_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "sample-app",
        )
        if os.path.exists(sample_app_path):
            for item in os.listdir(sample_app_path):
                if item in (".git", "__pycache__", ".venv"):
                    continue
                src = os.path.join(sample_app_path, item)
                dst = os.path.join(base_path, item)
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)

        # Pick a random fault when none is specified
        if not fault_type or fault_type not in FAULT_TYPES:
            import random as _random
            fault_type = _random.choice(FAULT_TYPES)
        try:
            inject_fault(base_path, fault_type)
        except Exception as exc:
            logger.warning("Failed to inject fault %s: %s", fault_type, exc)

        # Run the pipeline once synchronously to capture the initial failure output.
        # This becomes the "incident alert" the agent receives without needing to
        # trigger the pipeline itself for discovery — saving one expensive pipeline run.
        initial_failure_logs: Optional[str] = None
        try:
            pre_runner = SimulatedPipelineRunner(
                workspace_path=base_path,
                fault_type=fault_type,
                episode_id=f"init-{workspace_id[:8]}",
            )
            log_parts: List[str] = []
            for stage_name in STAGE_ORDER:
                stage_result = pre_runner.run_stage(stage_name, base_path)
                stage_status = str(stage_result.status)
                stage_logs = stage_result.stdout
                if stage_result.stderr:
                    stage_logs = (stage_logs + "\n" + stage_result.stderr).strip()
                if stage_status == StageStatus.FAILED:
                    # Show failing stage in full — this is the diagnostic the agent needs
                    log_parts.append(f"--- {stage_name}: {stage_status} ---\n{stage_logs}")
                    break
                else:
                    # Trim passed-stage logs to a single summary line to save space
                    log_parts.append(f"--- {stage_name}: {stage_status} ---")
            initial_failure_logs = "\n".join(log_parts)
        except Exception as exc:
            logger.warning("Pre-run pipeline failed during workspace init: %s", exc)

        workspace = WorkspaceState(
            workspace_id=workspace_id,
            base_path=base_path,
            created_at=time.time(),
            last_modified=time.time(),
            fault_type=fault_type,
            initial_failure_logs=initial_failure_logs,
        )
        async with self._lock:
            self.workspaces[workspace_id] = workspace
        return workspace

    async def get_workspace(self, workspace_id: str) -> Optional[WorkspaceState]:
        return self.workspaces.get(workspace_id)

    async def read_file(self, workspace_id: str, path: str) -> tuple[bool, str]:
        workspace = await self.get_workspace(workspace_id)
        if not workspace:
            return False, ""
        file_path = os.path.join(workspace.base_path, path)
        if not os.path.exists(file_path):
            return False, ""
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                return True, f.read()
        except Exception as exc:
            logger.error("Failed to read file %s: %s", path, exc)
            return False, ""

    async def write_file(self, workspace_id: str, path: str, content: str) -> bool:
        workspace = await self.get_workspace(workspace_id)
        if not workspace:
            return False
        file_path = os.path.join(workspace.base_path, path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            workspace.last_modified = time.time()
            return True
        except Exception as exc:
            logger.error("Failed to write file %s: %s", path, exc)
            return False

    async def list_files(self, workspace_id: str, directory: str = "") -> tuple[List[str], List[str]]:
        workspace = await self.get_workspace(workspace_id)
        if not workspace:
            return [], []
        target_path = os.path.join(workspace.base_path, directory) if directory else workspace.base_path
        if not os.path.exists(target_path):
            return [], []
        files, directories = [], []
        try:
            for item in os.listdir(target_path):
                if item.startswith("."):
                    continue
                item_path = os.path.join(target_path, item)
                rel = os.path.relpath(item_path, workspace.base_path)
                (directories if os.path.isdir(item_path) else files).append(rel)
        except Exception as exc:
            logger.error("Failed to list directory %s: %s", directory, exc)
        return files, directories

    async def create_job(self, workspace_id: str) -> PipelineJob:
        job_id = str(uuid.uuid4())
        job = PipelineJob(
            job_id=job_id,
            workspace_id=workspace_id,
            status="queued",
            stages={s: "pending" for s in STAGE_ORDER},
        )
        async with self._lock:
            self.jobs[job_id] = job
            ws = self.workspaces.get(workspace_id)
            if ws:
                ws.active_jobs.append(job_id)
        return job

    async def get_job(self, job_id: str) -> Optional[PipelineJob]:
        return self.jobs.get(job_id)

    async def check_rate_limit(self, workspace_id: str, max_per_minute: int = 10) -> bool:
        now = time.time()
        bucket = self.rate_limits.setdefault(workspace_id, [])
        self.rate_limits[workspace_id] = [ts for ts in bucket if now - ts < 60]
        if len(self.rate_limits[workspace_id]) >= max_per_minute:
            return False
        self.rate_limits[workspace_id].append(now)
        return True

    async def evict_stale_workspaces(self) -> int:
        """Delete workspaces that have been idle for longer than WORKSPACE_TTL_SECONDS."""
        now = time.time()
        evicted = 0
        async with self._lock:
            stale = [
                wid for wid, ws in self.workspaces.items()
                if now - ws.last_modified > WORKSPACE_TTL_SECONDS
                and not ws.active_jobs
            ]
            for wid in stale:
                ws = self.workspaces.pop(wid)
                try:
                    shutil.rmtree(ws.base_path, ignore_errors=True)
                except Exception:
                    pass
                evicted += 1
                logger.info("GC evicted workspace %s (idle %.0fs)", wid, now - ws.last_modified)
        return evicted


api_state = CICDAPIState()


# ── Pipeline Execution (streaming over WebSocket) ───────────────────────────


async def execute_pipeline_job(job_id: str) -> None:
    """Execute a pipeline job stage-by-stage and push events over WebSocket."""
    job = await api_state.get_job(job_id)
    if not job:
        logger.error("Job %s not found", job_id)
        return

    workspace = await api_state.get_workspace(job.workspace_id)
    if not workspace:
        job.status = "failed"
        job.error = "Workspace not found"
        return

    job.status = "running"
    job.started_at = time.time()

    runner = SimulatedPipelineRunner(
        workspace_path=workspace.base_path,
        fault_type=workspace.fault_type,
        episode_id=job_id,
    )

    overall_failed = False

    async def _push(msg: Dict[str, Any]) -> None:
        await ws_manager.send(job.workspace_id, msg)

    async def _stream_stage_logs(stage_name: str, stage_result) -> str:
        merged_lines: List[str] = []
        chunks: List[tuple[str, str]] = []
        if stage_result.stdout:
            chunks.extend([("stdout", line) for line in stage_result.stdout.splitlines() if line.strip()])
        if stage_result.stderr:
            chunks.extend([("stderr", line) for line in stage_result.stderr.splitlines() if line.strip()])

        total = max(1, len(chunks))
        for idx, (stream_name, line) in enumerate(chunks, start=1):
            merged_lines.append(line)
            await _push({
                "type": "log_chunk",
                "stage": stage_name,
                "job_id": job_id,
                "stream": stream_name,
                "line": line,
                "line_index": idx,
                "total_lines": total,
                "timestamp": time.time(),
            })
            await asyncio.sleep(0.01)
        return "\n".join(merged_lines)

    for stage_name in STAGE_ORDER:
        if overall_failed:
            job.stages[stage_name] = "skipped"
            await _push({"type": "stage_skipped", "stage": stage_name, "job_id": job_id})
            continue

        job.current_stage = stage_name
        job.stages[stage_name] = "running"
        await _push({
            "type": "stage_started",
            "stage": stage_name,
            "job_id": job_id,
            "timestamp": time.time(),
        })

        # Run the stage in a thread so we don't block the event loop
        stage_result = await asyncio.get_event_loop().run_in_executor(
            None, runner.run_stage, stage_name, workspace.base_path
        )

        stage_status = str(stage_result.status)
        job.stages[stage_name] = stage_status

        logs = await _stream_stage_logs(stage_name, stage_result)

        await _push({
            "type": "stage_completed",
            "stage": stage_name,
            "job_id": job_id,
            "status": stage_status,
            "logs": logs,
            "duration": stage_result.duration_seconds,
            "timestamp": time.time(),
        })

        if stage_status == StageStatus.FAILED:
            overall_failed = True
            job.failed_stage = stage_name

    job.status = "failed" if overall_failed else "passed"
    job.completed_at = time.time()

    # Remove from active_jobs on workspace
    ws = await api_state.get_workspace(job.workspace_id)
    if ws and job_id in ws.active_jobs:
        ws.active_jobs.remove(job_id)

    await _push({
        "type": "pipeline_done",
        "job_id": job_id,
        "status": job.status,
        "failed_stage": job.failed_stage,
        "duration": job.completed_at - job.started_at,
        "timestamp": time.time(),
    })


# ── Workspace GC background task ────────────────────────────────────────────


async def _gc_loop() -> None:
    """Run every 5 minutes and evict idle workspaces."""
    while True:
        await asyncio.sleep(300)
        try:
            n = await api_state.evict_stale_workspaces()
            if n:
                logger.info("GC: evicted %d stale workspace(s)", n)
        except Exception as exc:
            logger.warning("GC error: %s", exc)


# ── FastAPI Application ──────────────────────────────────────────────────────


app = FastAPI(
    title="CI/CD Dynamic API",
    description="Dynamic CI/CD environment with stateful API and WebSocket interactions",
    version="2.0.0",
)


def _build_landing_page() -> str:
        return """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Meta Hackathon CI/CD Repair Environment</title>
    <style>
        :root {
            --bg: #08111f;
            --panel: rgba(15, 23, 42, 0.84);
            --panel-2: rgba(30, 41, 59, 0.92);
            --text: #e2e8f0;
            --muted: #94a3b8;
            --border: rgba(148, 163, 184, 0.22);
            --accent: #fb923c;
            --accent-2: #38bdf8;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            min-height: 100vh;
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--text);
            background:
                radial-gradient(circle at top left, rgba(56, 189, 248, 0.22), transparent 28%),
                radial-gradient(circle at top right, rgba(251, 146, 60, 0.18), transparent 26%),
                linear-gradient(180deg, #050816 0%, var(--bg) 100%);
        }
        .wrap { max-width: 1120px; margin: 0 auto; padding: 32px 20px 48px; }
        .hero {
            padding: 36px;
            border-radius: 28px;
            border: 1px solid var(--border);
            background: linear-gradient(135deg, rgba(15, 23, 42, 0.95), rgba(30, 41, 59, 0.9));
            box-shadow: 0 24px 60px rgba(2, 6, 23, 0.36);
        }
        .badge {
            display: inline-flex;
            padding: 6px 12px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.08);
            letter-spacing: 0.08em;
            text-transform: uppercase;
            font-size: 11px;
            font-weight: 700;
        }
        h1 {
            margin: 18px 0 14px;
            font-size: clamp(34px, 5vw, 62px);
            line-height: 1.02;
            letter-spacing: -0.05em;
            max-width: 12ch;
        }
        p {
            margin: 0;
            max-width: 780px;
            font-size: 18px;
            line-height: 1.65;
            color: rgba(226, 232, 240, 0.84);
        }
        .actions {
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            margin-top: 22px;
        }
        a.button {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 44px;
            padding: 0 16px;
            border-radius: 14px;
            text-decoration: none;
            font-weight: 700;
            border: 1px solid transparent;
            transition: transform 120ms ease, border-color 120ms ease;
        }
        a.button:hover { transform: translateY(-1px); }
        .primary { color: #0f172a; background: linear-gradient(135deg, #fde68a, #fb923c); }
        .secondary { color: var(--text); background: rgba(255, 255, 255, 0.06); border-color: var(--border); }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin-top: 18px;
        }
        .card {
            padding: 18px;
            border-radius: 20px;
            background: var(--panel);
            border: 1px solid var(--border);
        }
        .card h2 {
            margin: 0 0 10px;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 0.10em;
            color: #f8fafc;
        }
        .card p, .card li { color: var(--muted); font-size: 15px; }
        .card ul { margin: 0; padding-left: 18px; }
        code {
            padding: 2px 6px;
            border-radius: 8px;
            background: rgba(148, 163, 184, 0.14);
            color: #f8fafc;
        }
    </style>
</head>
<body>
    <main class="wrap">
        <section class="hero">
            <div class="badge">Meta Hackathon · CI/CD repair lab</div>
            <h1>Something should be visible here.</h1>
            <p>
                The Space is alive, and the API is serving the repair environment. Use the links below to inspect
                the docs or check health instead of seeing a blank Not Found page.
            </p>
            <div class="actions">
                <a class="button primary" href="/docs">Open API docs</a>
                <a class="button secondary" href="/health">Check health</a>
                <a class="button secondary" href="/api/workspace/create">Workspace API</a>
            </div>
            <div class="grid">
                <article class="card">
                    <h2>What this is</h2>
                    <p>A deterministic benchmark for diagnosing and repairing broken CI/CD pipelines.</p>
                </article>
                <article class="card">
                    <h2>Visible endpoints</h2>
                    <ul>
                        <li><code>GET /health</code></li>
                        <li><code>POST /api/workspace/create</code></li>
                        <li><code>WS /api/ws/{workspace_id}</code></li>
                    </ul>
                </article>
                <article class="card">
                    <h2>Next step</h2>
                    <p>Open the docs, create a workspace, and follow the surfaced logs to the smallest safe fix.</p>
                </article>
            </div>
        </section>
    </main>
</body>
</html>"""


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_gc_loop())


@app.get("/", include_in_schema=False)
@app.get("/web", include_in_schema=False)
@app.get("/web/", include_in_schema=False)
async def root() -> HTMLResponse:
    return HTMLResponse(_build_landing_page())


# ── WebSocket endpoint ───────────────────────────────────────────────────────


@app.websocket("/api/ws/{workspace_id}")
async def websocket_endpoint(websocket: WebSocket, workspace_id: str):
    """Persistent WebSocket session for a workspace.

    Client → Server commands (JSON):
      {"type": "read_file",        "path": "...",            "request_id": "..."}
      {"type": "write_file",       "path": "...", "content": "...", "request_id": "..."}
      {"type": "list_files",       "directory": "",          "request_id": "..."}
      {"type": "trigger_pipeline",                           "request_id": "..."}
      {"type": "ping"}

    Server → Client events (JSON):
      {"type": "file_content",    "request_id": "...", "content": "...", "exists": bool}
      {"type": "write_ack",       "request_id": "...", "path": "...", "success": bool}
      {"type": "file_list",       "request_id": "...", "files": [...], "directories": [...]}
      {"type": "pipeline_queued", "request_id": "...", "job_id": "..."}
      {"type": "stage_started",   "stage": "...", "job_id": "...", "timestamp": float}
      {"type": "log_chunk",       "stage": "...", "job_id": "...", "stream": "stdout|stderr",
                                  "line": "...", "line_index": int, "total_lines": int}
      {"type": "stage_completed", "stage": "...", "job_id": "...", "status": "...",
                                  "logs": "...", "duration": float}
      {"type": "stage_skipped",   "stage": "...", "job_id": "..."}
      {"type": "pipeline_done",   "job_id": "...", "status": "...", "failed_stage": "..."}
      {"type": "pong"}
      {"type": "error",           "request_id": "...", "message": "..."}
    """
    workspace = await api_state.get_workspace(workspace_id)
    if workspace is None:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "Workspace not found"})
        await websocket.close(code=4004)
        return

    await ws_manager.connect(workspace_id, websocket)
    try:
        while True:
            try:
                # Use a timeout so the loop stays alive during long pipeline runs.
                # If no message arrives within 45 s we loop back and wait again —
                # this keeps the coroutine scheduled so the event loop can service
                # WebSocket ping/pong frames sent by the client.
                data = await asyncio.wait_for(websocket.receive_json(), timeout=45.0)
            except asyncio.TimeoutError:
                # No command received — just keep the connection alive and loop.
                continue
            except Exception:
                break

            msg_type = data.get("type", "")
            request_id = data.get("request_id", "")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            elif msg_type == "read_file":
                path = data.get("path", "")
                if not path:
                    await websocket.send_json({
                        "type": "error", "request_id": request_id, "message": "path required"
                    })
                    continue
                exists, content = await api_state.read_file(workspace_id, path)
                await websocket.send_json({
                    "type": "file_content",
                    "request_id": request_id,
                    "path": path,
                    "content": content,
                    "exists": exists,
                    "size": len(content),
                })

            elif msg_type == "write_file":
                path = data.get("path", "")
                content = data.get("content", "")
                if not path:
                    await websocket.send_json({
                        "type": "error", "request_id": request_id, "message": "path required"
                    })
                    continue
                if not await api_state.check_rate_limit(workspace_id):
                    await websocket.send_json({
                        "type": "error", "request_id": request_id, "message": "rate limit exceeded"
                    })
                    continue
                success = await api_state.write_file(workspace_id, path, content)
                await websocket.send_json({
                    "type": "write_ack",
                    "request_id": request_id,
                    "path": path,
                    "success": success,
                })

            elif msg_type == "list_files":
                directory = data.get("directory", "")
                files, directories = await api_state.list_files(workspace_id, directory)
                await websocket.send_json({
                    "type": "file_list",
                    "request_id": request_id,
                    "directory": directory,
                    "files": files,
                    "directories": directories,
                })

            elif msg_type == "trigger_pipeline":
                if not await api_state.check_rate_limit(workspace_id, max_per_minute=5):
                    await websocket.send_json({
                        "type": "error", "request_id": request_id, "message": "pipeline rate limit exceeded"
                    })
                    continue
                job = await api_state.create_job(workspace_id)
                await websocket.send_json({
                    "type": "pipeline_queued",
                    "request_id": request_id,
                    "job_id": job.job_id,
                })
                # Fire and forget — push events come back on this same socket
                asyncio.create_task(execute_pipeline_job(job.job_id))

            else:
                await websocket.send_json({
                    "type": "error",
                    "request_id": request_id,
                    "message": f"Unknown command type: {msg_type!r}",
                })

    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(workspace_id)


# ── REST endpoints (kept for compatibility / polling fallback) ───────────────


@app.post("/api/workspace/create", response_model=WorkspaceCreateResponse)
async def create_workspace(request: WorkspaceCreateRequest):
    try:
        workspace = await api_state.create_workspace(fault_type=request.fault_type)
        return WorkspaceCreateResponse(
            workspace_id=workspace.workspace_id,
            status="created",
            fault_injected=workspace.fault_type,
            initial_failure_logs=workspace.initial_failure_logs,
            message=f"Workspace created with {workspace.fault_type or 'no'} fault",
        )
    except Exception as exc:
        logger.exception("Failed to create workspace")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/workspace/{workspace_id}/status", response_model=WorkspaceStatusResponse)
async def get_workspace_status(workspace_id: str):
    workspace = await api_state.get_workspace(workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    file_count = sum(len(files) for _, _, files in os.walk(workspace.base_path))
    return WorkspaceStatusResponse(
        workspace_id=workspace.workspace_id,
        exists=True,
        file_count=file_count,
        last_modified=workspace.last_modified,
        active_jobs=workspace.active_jobs,
    )


@app.post("/api/workspace/{workspace_id}/files/read", response_model=FileReadResponse)
async def read_file(workspace_id: str, request: FileReadRequest):
    if not await api_state.get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    exists, content = await api_state.read_file(workspace_id, request.path)
    return FileReadResponse(path=request.path, content=content, exists=exists, size=len(content))


@app.post("/api/workspace/{workspace_id}/files/write", response_model=FileWriteResponse)
async def write_file(workspace_id: str, request: FileWriteRequest):
    if not await api_state.get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    if not await api_state.check_rate_limit(workspace_id):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    if not await api_state.write_file(workspace_id, request.path, request.content):
        raise HTTPException(status_code=500, detail="Failed to write file")
    return FileWriteResponse(path=request.path, status="updated", message=f"{request.path} updated")


@app.get("/api/workspace/{workspace_id}/files/list", response_model=FileListResponse)
async def list_files(workspace_id: str, directory: str = ""):
    if not await api_state.get_workspace(workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    files, directories = await api_state.list_files(workspace_id, directory)
    return FileListResponse(files=files, directories=directories)


@app.post("/api/pipeline/run", response_model=PipelineRunResponse)
async def run_pipeline(request: PipelineRunRequest, background_tasks: BackgroundTasks):
    if not await api_state.get_workspace(request.workspace_id):
        raise HTTPException(status_code=404, detail="Workspace not found")
    if not await api_state.check_rate_limit(request.workspace_id, max_per_minute=5):
        raise HTTPException(status_code=429, detail="Pipeline rate limit exceeded")
    job = await api_state.create_job(request.workspace_id)
    background_tasks.add_task(execute_pipeline_job, job.job_id)
    return PipelineRunResponse(
        job_id=job.job_id,
        workspace_id=request.workspace_id,
        status="queued",
        message="Pipeline job queued. Connect via WebSocket for real-time stage events.",
    )


@app.get("/api/pipeline/{job_id}/status", response_model=PipelineStatusResponse)
async def get_pipeline_status(job_id: str):
    job = await api_state.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    duration = None
    if job.started_at:
        duration = (job.completed_at or time.time()) - job.started_at
    return PipelineStatusResponse(
        job_id=job.job_id,
        status=job.status,
        current_stage=job.current_stage,
        failed_stage=job.failed_stage,
        stages=job.stages,
        started_at=job.started_at,
        completed_at=job.completed_at,
        duration=duration,
    )


@app.get("/api/pipeline/{job_id}/logs/{stage}", response_model=PipelineLogsResponse)
async def get_pipeline_logs(job_id: str, stage: str):
    job = await api_state.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.result:
        return PipelineLogsResponse(
            job_id=job_id, stage=stage,
            logs="Pipeline still running or no results available",
            available_stages=list(job.stages.keys()),
        )
    return PipelineLogsResponse(
        job_id=job_id,
        stage=stage,
        logs=job.result.get_stage_logs(stage),
        available_stages=list(job.result.stages.keys()),
    )


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "workspaces": len(api_state.workspaces),
        "jobs": len(api_state.jobs),
        "ws_connections": len(ws_manager._connections),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, ws_ping_interval=30, ws_ping_timeout=60)
