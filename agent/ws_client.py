"""WebSocket client for the CI/CD Dynamic API.

Replaces the poll-based CICDAPIClient with a persistent WebSocket connection
per workspace. All commands (read_file, write_file, list_files, trigger_pipeline)
and all push events (stage_started, stage_completed, pipeline_done) flow over
the same socket, eliminating per-request HTTP overhead and busy-polling.

Usage pattern (one connection per episode):
    async with CICDWebSocketClient(workspace_id) as client:
        content = await client.read_file("Dockerfile")
        await client.write_file("Dockerfile", new_content)
        result = await client.trigger_pipeline()   # awaits pipeline_done push
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False
    logger.warning("websockets package not installed — WS client disabled, falling back to HTTP")


class PipelineResult:
    """Summary of a completed pipeline run received via push event."""

    def __init__(self, event: Dict[str, Any]) -> None:
        self.job_id: str = event.get("job_id", "")
        self.status: str = event.get("status", "unknown")
        self.failed_stage: Optional[str] = event.get("failed_stage")
        self.duration: Optional[float] = event.get("duration")
        # stage_name → {"status": ..., "logs": ..., "duration": ...}
        self.stage_details: Dict[str, Dict[str, Any]] = {}

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def get_logs(self, stage: str) -> str:
        return self.stage_details.get(stage, {}).get("logs", "")


class CICDWebSocketClient:
    """Persistent WebSocket client for one workspace session.

    Thread-safety: this class is async-only. Run it inside a single asyncio
    event loop — do not share across threads.

    Attributes:
        workspace_id: The workspace this client is connected to.
        base_url:     WebSocket base URL, e.g. 'ws://localhost:8001'.
        timeout:      Seconds to wait for a response to a single command.
        pipeline_timeout: Seconds to wait for a full pipeline run to complete.
    """

    def __init__(
        self,
        workspace_id: str,
        base_url: str = "ws://localhost:8001",
        timeout: float = 30.0,
        pipeline_timeout: float = 300.0,
    ) -> None:
        self.workspace_id = workspace_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.pipeline_timeout = pipeline_timeout

        self._ws = None  # websockets.WebSocketClientProtocol
        self._pending: Dict[str, asyncio.Future] = {}  # request_id → Future
        self._pipeline_future: Optional[asyncio.Future] = None
        self._current_pipeline_result: Optional[PipelineResult] = None
        self._recv_task: Optional[asyncio.Task] = None

    # ── Context manager ─────────────────────────────────────────────────────

    async def __aenter__(self) -> "CICDWebSocketClient":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ── Connection lifecycle ─────────────────────────────────────────────────

    async def connect(self) -> None:
        if not _HAS_WEBSOCKETS:
            raise RuntimeError("websockets package is required for WS client")
        uri = f"{self.base_url}/api/ws/{self.workspace_id}"
        # Disable library-level pings: the sync wrapper runs run_until_complete per call,
        # leaving the loop idle between calls. The ping coroutine never gets CPU time during
        # those gaps, which causes spurious ping-timeout disconnects.
        self._ws = await websockets.connect(uri, ping_interval=None)
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.info("WS connected to %s", uri)

    async def reconnect(self) -> None:
        """Close the existing connection and open a fresh one."""
        await self.close()
        await self.connect()

    async def close(self) -> None:
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("WS connection closed for workspace=%s", self.workspace_id)

    # ── Receive loop ─────────────────────────────────────────────────────────

    async def _recv_loop(self) -> None:
        """Dispatch incoming messages to waiting futures or pipeline result."""
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON WS message: %s", raw[:200])
                    continue
                await self._dispatch(msg)
        except ConnectionClosed:
            logger.info("WS connection closed by server")
        except Exception as exc:
            logger.error("WS recv loop error: %s", exc)
        finally:
            # Cancel pending futures and retrieve their exceptions so asyncio does
            # not log "Future exception was never retrieved" to stderr.
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.cancel()
                    try:
                        fut.exception()
                    except Exception:
                        pass
            self._pending.clear()
            if self._pipeline_future and not self._pipeline_future.done():
                self._pipeline_future.cancel()
                try:
                    self._pipeline_future.exception()
                except Exception:
                    pass

    async def _dispatch(self, msg: Dict[str, Any]) -> None:
        msg_type = msg.get("type", "")
        request_id = msg.get("request_id", "")

        # Request/response pairing
        if request_id and request_id in self._pending:
            fut = self._pending.pop(request_id)
            if not fut.done():
                if msg_type == "error":
                    fut.set_exception(RuntimeError(msg.get("message", "unknown error")))
                else:
                    fut.set_result(msg)
            return

        # Pipeline push events
        if msg_type == "stage_completed" and self._current_pipeline_result is not None:
            stage = msg.get("stage", "")
            existing = self._current_pipeline_result.stage_details.get(stage, {})
            existing_logs = str(existing.get("logs", "") or "")
            completed_logs = msg.get("logs", "") or ""
            merged_logs = completed_logs or existing_logs
            self._current_pipeline_result.stage_details[stage] = {
                "status": msg.get("status"),
                "logs": merged_logs,
                "duration": msg.get("duration"),
            }
        elif msg_type == "log_chunk" and self._current_pipeline_result is not None:
            stage = msg.get("stage", "")
            line = str(msg.get("line", "") or "")
            detail = self._current_pipeline_result.stage_details.setdefault(
                stage,
                {"status": "running", "logs": "", "duration": None},
            )
            prior = str(detail.get("logs", "") or "")
            detail["logs"] = (prior + ("\n" if prior else "") + line).strip()
        elif msg_type == "stage_started":
            logger.debug("Pipeline stage started: %s", msg.get("stage"))
        elif msg_type == "pipeline_done":
            if self._current_pipeline_result is not None:
                # Merge top-level result fields
                self._current_pipeline_result.status = msg.get("status", "unknown")
                self._current_pipeline_result.failed_stage = msg.get("failed_stage")
                self._current_pipeline_result.duration = msg.get("duration")
            if self._pipeline_future and not self._pipeline_future.done():
                self._pipeline_future.set_result(self._current_pipeline_result)
        elif msg_type == "pong":
            pass
        else:
            logger.debug("Unhandled WS message type=%s", msg_type)

    # ── Command helpers ──────────────────────────────────────────────────────

    async def _send_command(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send a command and wait for the matching response, reconnecting once on drop."""
        from websockets.exceptions import ConnectionClosed as _CC
        for attempt in range(2):
            if self._ws is None:
                raise RuntimeError("Not connected — call connect() first")
            request_id = str(uuid.uuid4())[:8]
            payload["request_id"] = request_id
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending[request_id] = fut
            try:
                await self._ws.send(json.dumps(payload))
                return await asyncio.wait_for(fut, timeout=self.timeout)
            except asyncio.TimeoutError:
                self._pending.pop(request_id, None)
                raise TimeoutError(f"WS command {payload['type']!r} timed out after {self.timeout}s")
            except (_CC, ConnectionError, OSError) as exc:
                self._pending.pop(request_id, None)
                if attempt == 0:
                    logger.info("WS connection dropped (%s), reconnecting…", exc)
                    await self.reconnect()
                    continue
                raise ConnectionError(f"WS reconnect failed: {exc}") from exc

    # ── Public API ───────────────────────────────────────────────────────────

    async def read_file(self, path: str) -> Tuple[bool, str]:
        """Read a file from the workspace.

        Returns:
            (exists, content) tuple.
        """
        resp = await self._send_command({"type": "read_file", "path": path})
        return resp.get("exists", False), resp.get("content", "")

    async def write_file(self, path: str, content: str) -> bool:
        """Write a file to the workspace.

        Returns:
            True if the write succeeded.
        """
        resp = await self._send_command({"type": "write_file", "path": path, "content": content})
        return resp.get("success", False)

    async def list_files(self, directory: str = "") -> Tuple[List[str], List[str]]:
        """List files in the workspace.

        Returns:
            (files, directories) tuple of relative paths.
        """
        resp = await self._send_command({"type": "list_files", "directory": directory})
        return resp.get("files", []), resp.get("directories", [])

    async def trigger_pipeline(self) -> PipelineResult:
        """Trigger a pipeline run and wait for the pipeline_done push event.

        Stage events (stage_started, stage_completed) are processed internally
        and their logs are accessible on the returned PipelineResult.

        Returns:
            PipelineResult with status, failed_stage, and per-stage logs.
        """
        loop = asyncio.get_event_loop()
        self._pipeline_future = loop.create_future()
        self._current_pipeline_result = PipelineResult({})

        # Request pipeline start — get back a job_id immediately
        resp = await self._send_command({"type": "trigger_pipeline"})
        job_id = resp.get("job_id", "unknown")
        self._current_pipeline_result.job_id = job_id

        # Wait for pipeline_done push event (pushed by the server when all stages finish)
        try:
            result = await asyncio.wait_for(self._pipeline_future, timeout=self.pipeline_timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Pipeline {job_id} did not complete within {self.pipeline_timeout}s")
        finally:
            self._pipeline_future = None
            self._current_pipeline_result = None

        return result

    async def ping(self) -> None:
        """Send a ping and verify the connection is alive."""
        await self._ws.send(json.dumps({"type": "ping"}))


# ── Synchronous wrapper for use in non-async code ──────────────────────────


class SyncCICDWebSocketClient:
    """Thin synchronous wrapper around CICDWebSocketClient.

    Spins up a private event loop so that existing synchronous agent code
    (runner.py) can call WS operations without being rewritten as async.

    Usage:
        with SyncCICDWebSocketClient(workspace_id) as client:
            exists, content = client.read_file("Dockerfile")
            client.write_file("Dockerfile", new_content)
            result = client.trigger_pipeline()
    """

    def __init__(
        self,
        workspace_id: str,
        base_url: str = "ws://localhost:8001",
        timeout: float = 30.0,
        pipeline_timeout: float = 300.0,
    ) -> None:
        self._async_client = CICDWebSocketClient(
            workspace_id=workspace_id,
            base_url=base_url,
            timeout=timeout,
            pipeline_timeout=pipeline_timeout,
        )
        self._loop = asyncio.new_event_loop()

    def __enter__(self) -> "SyncCICDWebSocketClient":
        self._loop.run_until_complete(self._async_client.connect())
        return self

    def __exit__(self, *_) -> None:
        self._loop.run_until_complete(self._async_client.close())
        self._loop.close()

    def _run_with_reconnect(self, coro_factory):
        """Run a coroutine factory, reconnecting once if the connection is closed."""
        try:
            return self._loop.run_until_complete(coro_factory())
        except Exception as exc:
            err = str(exc).lower()
            if "closed" in err or "connection" in err or isinstance(exc, (ConnectionError, OSError)):
                logger.info("WS connection lost (%s), reconnecting…", exc)
                try:
                    self._loop.run_until_complete(self._async_client.reconnect())
                except Exception as reconnect_exc:
                    raise ConnectionError(f"WS reconnect failed: {reconnect_exc}") from exc
                return self._loop.run_until_complete(coro_factory())
            raise

    def read_file(self, path: str) -> Tuple[bool, str]:
        return self._run_with_reconnect(lambda: self._async_client.read_file(path))

    def write_file(self, path: str, content: str) -> bool:
        return self._run_with_reconnect(lambda: self._async_client.write_file(path, content))

    def list_files(self, directory: str = "") -> Tuple[List[str], List[str]]:
        return self._run_with_reconnect(lambda: self._async_client.list_files(directory))

    def trigger_pipeline(self) -> PipelineResult:
        return self._run_with_reconnect(lambda: self._async_client.trigger_pipeline())
