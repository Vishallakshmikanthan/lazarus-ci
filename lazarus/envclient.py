"""Lazarus-CI Environment Adapter (Module 1).

Connects to the OpenEnv CI/CD Repair Environment over HTTP/WebSocket.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional
import httpx

ENV_URL = os.getenv("ENV_URL", "http://localhost:8000")


class Env:
    """Thin HTTP/WebSocket adapter for OpenEnv CI/CD environment."""

    def __init__(self, base: str = ENV_URL):
        self.base = base.rstrip("/")
        self.c = httpx.Client(base_url=self.base, timeout=180.0)

    @staticmethod
    def _obs(j: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize observation dictionary to include reward, done, and status fields."""
        o = dict(j.get("observation", j))
        o["_reward"] = j.get("reward")
        o["_done"] = j.get("done", o.get("done", False))
        return o

    def reset(self, **kw: Any) -> Dict[str, Any]:
        """Reset the environment for a new episode."""
        r = self.c.post("/reset", json=kw)
        r.raise_for_status()
        return self._obs(r.json())

    def step(self, operation: str, target: str = "", value: str = "") -> Dict[str, Any]:
        """Execute a pipeline or inspection action."""
        action = {"operation": operation, "target": target, "value": value}
        r = self.c.post("/step", json={"action": action})
        if r.status_code == 422:
            # Fall back to flat action dictionary if wrapped schema was rejected
            r = self.c.post("/step", json=action)
        r.raise_for_status()
        return self._obs(r.json())

    def state(self) -> Dict[str, Any]:
        """Fetch current environment state."""
        r = self.c.get("/state")
        r.raise_for_status()
        return r.json()

    def close(self):
        """Close HTTP client session."""
        self.c.close()
