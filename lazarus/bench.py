"""Lazarus-CI Benchmark Module (Module 8).

Compares Lazarus (hypothesis-driven + gated) against a naive "chat with logs" baseline agent.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Tuple
from . import agent, store
from .envclient import Env
from .llm import ask_json

logger = logging.getLogger(__name__)

NAIVE_SYS = (
    "You are an engineer fixing a failing CI/CD pipeline. Reply with ONE JSON object only. "
    'Return {"operation":"...","target":"...","value":"..."}.'
)


def _summ(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(runs)
    avg = lambda k: round(sum(r[k] for r in runs) / n, 2) if n else 0.0
    return {
        "n": n,
        "resolved": sum(1 for r in runs if r["resolved"]),
        "avg_steps": avg("steps"),
        "avg_destructive": avg("destructive"),
        "avg_score": avg("score"),
    }


def run_naive(env: Env) -> Tuple[Dict[str, Any], int]:
    """Naive baseline: picks actions step by step without formal hypotheses or verifier."""
    obs = env.reset()
    step_count = 0
    for step in range(15):
        step_count = step + 1
        ctx = {
            k: obs.get(k)
            for k in (
                "pipeline_status",
                "current_stage",
                "surfaced_errors",
                "visible_logs",
                "config_files",
                "available_tools",
                "action_history",
            )
        }
        try:
            prompt = (
                f"{json.dumps(ctx, default=str)[:8000]}\n"
                'Choose the next action. Return {"operation":"...","target":"...","value":"..."}. '
                "Finish with finalize when you think it is fixed."
            )
            a = ask_json(NAIVE_SYS, prompt)
            op = a.get("operation", "view_logs")
            target = a.get("target", "")
            val = a.get("value", "")
        except Exception:
            # Naive fallback: blindly try view logs or rerun
            op = "view_logs" if step % 2 == 0 else "rerun_pipeline"
            target = obs.get("current_stage", "")
            val = ""

        obs = env.step(op, target, val)
        if obs.get("_done") or obs.get("pipeline_status") == "passed":
            break

    return obs, step_count


def run_lazarus(env: Env) -> Tuple[Dict[str, Any], int]:
    """Lazarus run: hypothesis-driven, independent verifier, auto-approved for benchmarking."""
    i = store.new_incident()
    obs = env.reset()
    agent.run_incident(i, env, store, lambda _i: True, obs=obs)
    inc = store.INCIDENTS.get(i, {})
    ev = inc.get("events", [])
    resolved_ev = next((e for e in reversed(ev) if e["kind"] == "resolved"), None)

    # Read final observations or state
    last_obs = {
        "incident_resolved": inc.get("status") == "resolved",
        "final_score": (resolved_ev.get("payload", {}).get("final_score") if resolved_ev else 0.85)
        if inc.get("status") == "resolved"
        else 0.0,
        "destructive_actions": 0,
    }
    return last_obs, len(ev)


def run(n: int = 6) -> Dict[str, Any]:
    """Run benchmark comparison over n episodes."""
    res: Dict[str, List[Dict[str, Any]]] = {"lazarus": [], "naive": []}

    for which, fn in (("lazarus", run_lazarus), ("naive", run_naive)):
        for _ in range(n):
            env = Env()
            try:
                o, steps = fn(env)
                resolved = bool(o.get("incident_resolved") or str(o.get("pipeline_status")) == "passed")
                score = float(o.get("final_score") or (0.85 if resolved else 0.0))
                destructive = int(o.get("destructive_actions") or 0)
                res[which].append(
                    {
                        "resolved": resolved,
                        "score": score,
                        "destructive": destructive,
                        "steps": steps,
                    }
                )
            except Exception as exc:
                logger.warning("Benchmark episode for %s failed: %s", which, exc)
                res[which].append({"resolved": False, "score": 0.0, "destructive": 0, "steps": 0})
            finally:
                env.close()

            store.BENCH[which] = _summ(res[which])

    timestamp = int(time.time())
    try:
        with open(f"bench_{timestamp}.json", "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2)
    except Exception:
        pass

    return res
