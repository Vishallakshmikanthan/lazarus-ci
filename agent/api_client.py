"""CI/CD API tool dispatcher — bridges agent tool calls to the WebSocket API.

The agent's tool loop calls ``execute_tool(tool_name, arguments, client)``
where ``client`` is a ``SyncCICDWebSocketClient`` held open for the episode.

Supported tools (matching API_TOOL_SCHEMAS in api_tool_schemas.py):
  read_file          → client.read_file(path)
  write_file         → client.write_file(path, content)
  list_files         → client.list_files(directory)
  trigger_pipeline   → client.trigger_pipeline()  [blocks until pipeline_done]
  set_hypothesis     → local — returns {"success": True, "acknowledged": True}
  finalize           → local — returns {"success": True, "finalized": True}

Errors are caught and returned as {"success": False, "error": "..."} so the
agent's loop can surface them as tool results without crashing.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Lazy import — ws_client requires websockets which may not be installed
_ws_client_module = None


def _get_ws_client_module():
    global _ws_client_module
    if _ws_client_module is None:
        from agent import ws_client as m
        _ws_client_module = m
    return _ws_client_module


def create_ws_client(
    workspace_id: str,
    base_url: Optional[str] = None,
    timeout: float = 30.0,
    pipeline_timeout: float = 300.0,
):
    """Create and return a SyncCICDWebSocketClient (not yet connected).

    Caller must use it as a context manager or call connect()/close() manually.
    """
    url = base_url or os.getenv("CICD_API_WS_URL", "ws://localhost:8001")
    mod = _get_ws_client_module()
    return mod.SyncCICDWebSocketClient(
        workspace_id=workspace_id,
        base_url=url,
        timeout=timeout,
        pipeline_timeout=pipeline_timeout,
    )


def execute_tool(
    tool_name: str,
    arguments: Dict[str, Any],
    client,  # SyncCICDWebSocketClient
) -> Dict[str, Any]:
    """Dispatch a tool call to the CI/CD WebSocket API.

    Args:
        tool_name:  Name of the tool (must match API_TOOL_SCHEMAS).
        arguments:  Parsed tool arguments dict from the LLM response.
        client:     An open SyncCICDWebSocketClient instance.

    Returns:
        A dict with at least {"success": bool} and tool-specific fields.
    """
    try:
        if tool_name == "read_file":
            path = arguments.get("path", "")
            if not path:
                return {"success": False, "error": "read_file requires 'path'"}
            exists, content = client.read_file(path)
            return {"success": exists, "path": path, "content": content, "exists": exists}

        elif tool_name == "write_file":
            path = arguments.get("path", "")
            content = arguments.get("content", "")
            if not path:
                return {"success": False, "error": "write_file requires 'path'"}
            ok = client.write_file(path, content)
            return {
                "success": ok,
                "path": path,
                "message": f"File {path} {'updated' if ok else 'update failed'}",
            }

        elif tool_name == "list_files":
            directory = arguments.get("directory", "")
            files, directories = client.list_files(directory)
            return {"success": True, "files": files, "directories": directories}

        elif tool_name == "trigger_pipeline":
            result = client.trigger_pipeline()
            stage_summaries = {}
            for stage, detail in result.stage_details.items():
                stage_summaries[stage] = {
                    "status": detail.get("status"),
                    "duration": detail.get("duration"),
                    "logs": (detail.get("logs") or "")[:2000],  # cap log size per stage
                }
            return {
                "success": True,
                "job_id": result.job_id,
                "status": result.status,
                "passed": result.passed,
                "failed_stage": result.failed_stage,
                "duration": result.duration,
                "stages": stage_summaries,
            }

        elif tool_name == "set_hypothesis":
            hypothesis = arguments.get("hypothesis", "")
            if not hypothesis:
                return {"success": False, "error": "set_hypothesis requires 'hypothesis'"}
            return {"success": True, "acknowledged": True, "hypothesis": hypothesis}

        elif tool_name == "finalize":
            return {"success": True, "finalized": True}

        else:
            return {"success": False, "error": f"Unknown tool: {tool_name!r}"}

    except Exception as exc:
        logger.error("Tool %s failed: %s", tool_name, exc, exc_info=True)
        return {"success": False, "error": str(exc)}


# ── Convenience: format tool result as a human-readable string ──────────────

def format_tool_result(tool_name: str, result: Dict[str, Any]) -> str:
    """Convert a tool result dict into a compact string for the LLM context."""
    if not result.get("success"):
        return f"[{tool_name}] ERROR: {result.get('error', 'unknown error')}"

    if tool_name == "read_file":
        if not result.get("exists"):
            return f"[read_file] File not found: {result.get('path')}"
        content = result.get("content", "")
        lines = content.splitlines()
        preview = "\n".join(lines[:200])
        suffix = f"\n... ({len(lines) - 200} more lines)" if len(lines) > 200 else ""
        return f"[read_file] {result['path']} ({len(lines)} lines):\n{preview}{suffix}"

    if tool_name == "write_file":
        return f"[write_file] {result.get('message', 'done')}"

    if tool_name == "list_files":
        files = result.get("files", [])
        dirs = result.get("directories", [])
        parts = []
        if dirs:
            parts.append("Directories:\n  " + "\n  ".join(sorted(dirs)))
        if files:
            parts.append("Files:\n  " + "\n  ".join(sorted(files)))
        return "[list_files]\n" + "\n".join(parts) if parts else "[list_files] (empty directory)"

    if tool_name == "trigger_pipeline":
        status = result.get("status", "unknown")
        failed = result.get("failed_stage")
        dur = result.get("duration")
        header = f"[trigger_pipeline] status={status}"
        if failed:
            header += f"  failed_stage={failed}"
        if dur:
            header += f"  duration={dur:.1f}s"
        stages = result.get("stages", {})
        stage_lines = []
        for stage, detail in stages.items():
            st = detail.get("status", "?")
            logs = (detail.get("logs") or "").strip()
            stage_lines.append(f"\n--- {stage}: {st} ---\n{logs}" if logs else f"\n--- {stage}: {st} ---")
        return header + "".join(stage_lines)

    if tool_name == "set_hypothesis":
        return f"[set_hypothesis] Hypothesis recorded: {result.get('hypothesis', '')}"

    if tool_name == "finalize":
        return "[finalize] Episode finalised — awaiting score."

    return f"[{tool_name}] {result}"
