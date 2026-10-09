"""Lazarus-CI Live Demonstration Script.

Runs an end-to-end incident, displays detailed CLI telemetry, and outputs benchmark comparisons.
"""

from __future__ import annotations

import json
import time
from lazarus.envclient import Env
from lazarus import store, agent, bench

def run_demo():
    print("=" * 80)
    print("                 LAZARUS-CI: AUTONOMOUS SRE EXECUTION REPORT")
    print("=" * 80)

    env = Env()
    inc_id = store.new_incident()

    print(f"\n[PHASE 0] Incident Triggered -> Created Incident #{inc_id}")
    obs = env.reset()
    print(f"  Current Stage    : {obs.get('current_stage')}")
    print(f"  Pipeline Status  : {obs.get('pipeline_status')}")
    errors = obs.get('surfaced_errors') or []
    print(f"  Surfaced Errors  : {errors[:2] if errors else 'None'}")

    def approval_callback(i: int):
        print("\n  [RISK GATE] High-risk change detected! Human approval requested.")
        print("  [APPROVAL] Simulating operator approval (Approve = TRUE)...")
        return True

    print("\n[PHASE 1] Executing Autonomous Diagnosis & Remediation Loop...")
    start_t = time.time()
    outcome = agent.run_incident(inc_id, env, store, approval_callback, obs=obs)
    elapsed = time.time() - start_t

    inc = store.INCIDENTS[inc_id]
    print(f"\n[PHASE 2] Incident Resolution Finished in {elapsed:.2f}s -> Verdict: {outcome.upper()}")
    print("-" * 80)
    print("SRE TIMELINE EVENTS (Dashboard Stream):")

    for ev in inc["events"]:
        kind = ev["kind"]
        payload = ev["payload"]
        if kind == "alert":
            print(f"  * [ALERT]              Stage: {payload.get('stage')} | Errors detected: {len(payload.get('errors', []))}")
        elif kind == "triage":
            print(f"  * [TRIAGE]             Cycle {payload.get('cycle')}: Inspected logs for stage '{payload.get('stage')}'")
        elif kind == "hypotheses":
            print(f"  * [HYPOTHESES]         Formulated {len(payload.get('hypotheses', []))} competing hypotheses:")
            for h in payload.get("hypotheses", []):
                act = h.get("action", {})
                print(f"      - {h['id']}: {h['statement'][:65]}... [Experiment: {act.get('operation')} -> {act.get('target')}]")
        elif kind == "experiment":
            act = payload.get("action", {})
            print(f"  * [EXPERIMENT]         Executed {act.get('operation')} on '{act.get('target')}' for hypothesis {payload.get('hypothesis')}")
        elif kind == "verdicts":
            print(f"  * [VERDICTS]           Root Cause Confirmed: '{payload.get('root_cause')}' (Confidence: {payload.get('confidence')})")
            for v in payload.get("verdicts", []):
                print(f"      - {v['id']}: {v['verdict'].upper()} | {v['evidence'][:65]}...")
        elif kind == "plan":
            fix = payload.get("fix", {})
            print(f"  * [RISK GATE]          Fix: {fix.get('operation')} on '{fix.get('target')}' | Risk Level: {payload.get('level').upper()} | Needs Approval: {payload.get('needs_approval')}")
        elif kind == "apply":
            fix = payload.get("fix", {})
            print(f"  * [APPLY]              Applied remediation: {fix.get('operation')} (value: {fix.get('value')})")
        elif kind == "verify":
            print(f"  * [INDEPENDENT VERIFY] Cycle {payload.get('cycle')}: Verdict = {payload.get('verdict').upper()} (Stages: {payload.get('stages_passed_after')}/{payload.get('stages_total')} passed)")
        elif kind == "resolved":
            print(f"  * [RESOLVED]           Pipeline green! Verified Score: {payload.get('final_score')} | Total steps: {payload.get('steps')}")
        elif kind == "escalated":
            print(f"  * [ESCALATED]          Paged On-Call Human: {payload.get('reason')}")

    print("\n" + "=" * 80)
    print("          BENCHMARK RUN: LAZARUS vs NAIVE 'CHAT WITH LOGS' (1x sample)")
    print("=" * 80)
    b_res = bench.run(n=1)
    laz = store.BENCH.get("lazarus", {})
    naive = store.BENCH.get("naive", {})
    print(f"  Lazarus-CI : Resolved = {laz.get('resolved')}/{laz.get('n')} | Avg Steps = {laz.get('avg_steps')} | Destructive Actions = {laz.get('avg_destructive')} | Score = {laz.get('avg_score')}")
    print(f"  Naive Agent: Resolved = {naive.get('resolved')}/{naive.get('n')} | Avg Steps = {naive.get('avg_steps')} | Destructive Actions = {naive.get('avg_destructive')} | Score = {naive.get('avg_score')}")
    print("=" * 80)
    print("\nMission Control Dashboard is LIVE at: http://localhost:8100")
    print("=" * 80)

if __name__ == "__main__":
    run_demo()
