"""Lazarus-CI LLM Interface (Module 2).

Multi-provider client with JSON enforcement, markdown stripping, and resilient offline fallbacks.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, Optional
import httpx

PROVIDER = os.getenv("LLM_PROVIDER", "openai_compat")  # anthropic | openai_compat | hf | groq
MODEL = os.getenv("LLM_MODEL", os.getenv("MODEL_NAME", "Qwen/Qwen2.5-72B-Instruct"))
KEY = os.getenv("LLM_API_KEY", os.getenv("HF_TOKEN", os.getenv("OPENAI_API_KEY", "")))
BASE = os.getenv("LLM_BASE_URL", os.getenv("API_BASE_URL", "https://router.huggingface.co/v1"))


class LLMError(Exception):
    """Raised when LLM call fails or returns non-conforming responses."""
    pass


def _raw(system: str, user: str, max_tokens: int = 1600) -> str:
    """Send prompt to configured LLM provider and return raw text."""
    if not KEY and not os.getenv("MOCK_LLM", ""):
        # If no key is set, raise LLMError to allow graceful heuristic fallback
        raise LLMError("No LLM API key provided in environment.")

    if PROVIDER == "anthropic":
        try:
            import anthropic  # type: ignore
            c = anthropic.Anthropic(api_key=KEY or None)
            kw = dict(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            try:
                r = c.messages.create(temperature=0, **kw)
            except Exception:
                r = c.messages.create(**kw)
            return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        except Exception as exc:
            raise LLMError(f"Anthropic API error: {exc}") from exc

    # Default: OpenAI-compatible endpoints (HF Router, OpenRouter, Groq, Ollama, OpenAI)
    endpoint = BASE.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    try:
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise LLMError(f"OpenAI-compatible request error: {exc}") from exc


def ask_json(system: str, user: str, retries: int = 2) -> Dict[str, Any]:
    """Query LLM and parse response strictly into a JSON dictionary."""
    last_err: Optional[Exception] = None
    for _ in range(retries + 1):
        try:
            text = _raw(system, user)
            cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1:
                return json.loads(cleaned[start : end + 1])
            raise ValueError(f"No JSON object found in response: {text[:200]}")
        except Exception as e:
            last_err = e
            time.sleep(0.5)

    raise LLMError(str(last_err))
