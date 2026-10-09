# LAZARUS-CI: Self-Healing CI/CD Agent (PS-12 Auto-Heal)

**CTRL+AI Challenge | 9 Oct 2026 | 6 hours | Team of 3**
**Built on:** the CI/CD Repair Environment (OpenEnv) repo, as a *disclosed* base.
**Tagline:** "Other agents suggest fixes. Lazarus proves them."

> **Read section 0 first.** It decides whether you are allowed to do this and how to do it without it backfiring.
> **Read section 1 next.** It tells you what I verified in the repo and what I did NOT. Do the 30-minute recon in Module 0 before you commit to this plan.

---

## 0. Provenance, rules and "fresh commits" (do not skip)

**What the repo is.** `chaitanyaadeokar/Meta-Hackathon-CICD-Repair-Environment` is a **fork** of `parthpetkar/Meta-Hackathon-CICD-Repair-Environment`, with 108 commits, built for the Meta PyTorch OpenEnv Hackathon (finalist project). It is finished prior work, not something built today.

**Why that matters for CTRL+AI.** The event is a 6-hour build. The jury scores what you built *in that window*. Bringing a pre-existing engine is only safe if it is **allowed and disclosed**. It backfires badly if it looks like you passed old work off as new.

1. **Check the rules.** The PS sheet says the details are in the separate "Problem Details, Rules and Regulations" document. I only had the checklist PDF, which says nothing about pre-built code. Read the full document, or ask an organiser *before 9:00*: "Can we build on our own earlier open-source project if we disclose it?" Get the answer in writing or on WhatsApp.
2. **New commits on top are fine. Rewriting history is not.** Do NOT squash, re-init, or backdate to make the repo look new. Tag the starting point (`git tag ctrlai-base`) so everyone can see exactly what existed at 9:00 and what you added after.
3. **Disclose it in the first 20 seconds of the demo.** Say: "The sandbox and fault library come from our earlier OpenEnv project. Everything you'll see on screen (agent, risk gate, verifier, approval flow, dashboard, orchestration, benchmark) was built today." Juries respect this. They punish discovering it themselves.
4. **Licence and credit.** I could not see a `LICENSE` file; `models.py` carries a Meta BSD-style header (OpenEnv template). Confirm the licence, credit the original authors (including the upstream fork owner) in a `NOTICE.md`, and make sure every contributor to that repo is okay with reuse.
5. **Keep all new code in a new folder (`lazarus/`)**, committed during the event. That folder is your submission's real value.

**Do not quote the repo's old numbers as yours.** The README's GRPO chart describes itself as "illustrative iterations", and its baseline table labels are inconsistent (a "deterministic policy" vs a "frontier model agent"). Any number you show the jury should be one you generate today (Module 8).

---

## 1. Fit analysis (honest)

### What I verified
From the README and `models.py` only:

- OpenEnv server with `POST /reset`, `POST /step`, `GET /state`, `GET /health`, `WS /ws`.
- Action model: `MetaHackathonAction{operation, target, value}`.
  Operations: `view_logs, tail_logs, inspect_config, inspect_dockerfile, inspect_permissions, set_hypothesis, modify_config, add_dependency, rerun_pipeline, verify_fix, finalize`.
- Rich observation: `pipeline_status, current_stage, pipeline_stages{stage: pending|running|failed|passed|blocked}, visible_alerts, visible_logs, surfaced_errors, config_files, findings, action_history, current_hypothesis, pipeline_health, destructive_actions, redundant_actions, incident_resolved, final_score, revealed_issue_count, log_tokens_remaining, ...`
- ~20 fault types (deps, Dockerfile order, hardcoded secrets, env vars, DB migrations, Terraform/IAM, cross-service...) applied as real file mutations. Cascading multi-fault incidents and red herrings at higher difficulty.
- Modes: `CICD_SIMULATE=true` (pure Python, no Docker), subprocess sandbox, full real mode.
- `finalize` is blocked until `verify_fix` has run after a successful rerun.

### What I did NOT verify (Module 0 answers these)
- How `/step` is called (body shape), and whether **state persists across HTTP calls** (some OpenEnv setups need the WebSocket client).
- Whether `/reset` lets you **choose the fault type** or only random/curriculum.
- The exact `value` format for `modify_config` / `add_dependency` (the repo's `agent/` prompts and tool schemas show it).
- Step latency, reset time, and whether faults are reproducible.
- The licence.

### Does it fit PS-12?

PS-12: *diagnose faults in a sandbox app from alerts and logs, test hypotheses, run the right fix, verify recovery, page a human when the fix fails or is too risky.*

| PS-12 requirement | Repo gives you | You must build today |
|---|---|---|
| Sandbox app + faults | Yes (sample app, 20 faults) | Chaos Panel UI |
| Diagnose from alerts/logs | Observations carry alerts, logs, errors | Hypothesis-driven investigator |
| Test hypotheses | `set_hypothesis` + inspect actions | **Experiment-per-hypothesis loop with verdicts** |
| Run the right fix | `modify_config`, `add_dependency` | Fix selection + **risk gate** |
| Verify recovery | `rerun_pipeline`, `verify_fix` | **Independent verifier** |
| Page a human if failed/risky | Nothing | **Approval flow + escalation handoff** (this is mandatory) |

**Verdict:** a good fit and a stronger story than a toy app ("every judge has seen a red pipeline"). It saves roughly 1.5 to 2 hours of sandbox work. But the repo is an **RL training environment**, not a live-demo system, so confirm it behaves under a 5-minute demo.

**Skip GRPO training entirely.** It needs a GPU and hours. Do not claim or attempt it today.

### Go / No-Go at minute 30
Proceed only if ALL of these are true after Module 0:
- [ ] Env starts in simulated mode in under 2 minutes.
- [ ] `reset -> step(view_logs) -> state` works with state persisting.
- [ ] At least 3 distinct faults can be triggered reliably (by choice or repeated reset).
- [ ] A step returns in under ~3 seconds.
- [ ] The organisers / rules allow disclosed reuse.

If any fail, fall back to the from-scratch **CanteenDash** plan (ask me to finish that guide).

---

## 2. What we are building

**Lazarus-CI**: an autonomous SRE agent team that watches a CI/CD pipeline. When a stage goes red it:

1. **Triages** (reads the failing stage's logs).
2. Writes **3 competing root-cause hypotheses**, each with one cheap read-only experiment.
3. **Runs the experiments**, then keeps or drops each hypothesis *with evidence*.
4. Picks a **fix**, scored by a **code-enforced risk gate**. Low risk runs alone; high risk **pauses for human approval**.
5. Applies the fix, reruns the pipeline, and an **independent verifier** (no LLM, separate code path) judges: `passed / advanced / unchanged / regressed`.
6. Handles **cascading failures** (fix one, the next surfaces) for up to 3 cycles.
7. If verification fails twice, or the fix is rejected/blocked, it **escalates with a handoff report** (Telegram via n8n + dashboard).
8. A **benchmark** (Lazarus vs a naive "chat with logs" agent) runs on the same environment and produces *real* numbers for the jury.

### The pitch line for the jury
> "Most CI-fixing agents guess until something turns green. Lazarus forms hypotheses, tests them, gates risky changes behind a human, and proves recovery with a verifier that never trusts the fixer."

### Why the risk gate demo is natural here
Faults like **hardcoded secrets**, **IAM denial** and **Terraform** changes inherently touch sensitive config. The approval flow triggers on real faults, with no contrived "risky fix" needed.

---

## 3. Architecture

```
 Chaos Panel (dashboard)         GitHub Actions webhook (real-world entry, optional)
        |                                   |
        +----------------+------------------+
                         v
                  n8n  (intake + notify + escalate)
                         |  POST /incident/start
                         v
        +------------------------------------------------+
        |  LAZARUS SERVER (FastAPI, new code)            |
        |   agent.py   : triage -> hypotheses -> judge   |
        |   risk.py    : static policy, human gate       |
        |   verifier.py: independent, no LLM             |
        |   store.py   : incidents + event timeline      |
        +----------------------+-------------------------+
                               | HTTP (reset / step / state)
                               v
        +------------------------------------------------+
        |  OPENENV CI/CD REPAIR ENV (existing repo)      |
        |  fault injector, sample app, pipeline runner   |
        +------------------------------------------------+
   Dashboard (single HTML, polls /api/state)   Telegram (via n8n, outbound only)
```

**Principle: the LLM proposes, code disposes.** The LLM only produces hypotheses, verdicts and a candidate fix. Risk policy, allowed operations, approval and verification are plain code.

### Ports
| Service | Port |
|---|---|
| OpenEnv env | 8000 |
| Lazarus server + dashboard | 8100 |
| n8n | 5678 |

---

## 4. Team split and AI-IDE collaboration

| Owner | Modules | Folder (only they edit it) |
|---|---|---|
| **A: Env + Agent** | M0, M1, M2 | `lazarus/envclient.py`, `lazarus/agent.py`, `lazarus/llm.py` |
| **B: Safety + API** | M3, M4, M5, M8 | `lazarus/risk.py`, `verifier.py`, `store.py`, `server.py`, `bench.py` |
| **C: UX + Orchestration** | M6, M7, demo | `lazarus/static/`, `n8n/`, `docs/` |

**Rules that prevent merge pain with AI IDEs**
1. One owner per file. If you need a change in someone else's file, ask them.
2. Everyone codes against the **contracts in section 5**, not against each other's code. Mock the other side.
3. Branch per module (`mod/agent`, `mod/safety`, `mod/ux`); merge to `main` at every checkpoint; small commits.
4. Put this in `AGENTS.md` (or `.cursorrules`) at the repo root so every AI IDE follows it:

```
# AGENTS.md
Project: Lazarus-CI. New code lives ONLY in lazarus/. Never edit server/, cicd/, agent/, models.py (inherited base).
The LLM proposes; code disposes: risk, approval, verification are deterministic code, never LLM decisions.
Contracts are in docs/CONTRACTS.md. Do not change them without telling the other owners.
Fail safe: any unexpected state -> escalate to a human, never retry blindly.
Every step must emit an event via store.emit(kind, **payload) so the dashboard shows it.
```

---

## 5. Contracts (shared)

### 5.1 Env adapter (A provides, B and C use)
```python
env.reset(**kw)  -> obs: dict
env.step(operation, target="", value="") -> obs: dict
# obs always includes the OpenEnv observation fields plus:
#   obs["_reward"], obs["_done"]
```

### 5.2 Lazarus HTTP API (B provides, C consumes)
| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/incident/start` | `{"fault": "<optional>"}` | `{"incident_id": 1}` |
| GET | `/incident/{id}/status` | | `{"status": "...", "summary": "..."}` |
| GET | `/api/state` | | latest incident + events + stages + bench (below) |
| POST | `/incident/{id}/approve` | `{"approved": true}` | `{"ok": true}` |
| POST | `/bench/run` | `{"n": 6}` | `{"started": true}` |

`status` values: `running, awaiting_approval, resolved, escalated`.

### 5.3 Event kinds (what the dashboard renders)
`alert, triage, hypotheses, experiment, verdicts, plan, approval_request, approval, apply, verify, cascade, resolved, escalated, error`

Each event: `{"id": n, "ts": float, "kind": "...", "payload": {...}}`

### 5.4 `/api/state` shape
```json
{
  "incident": {"id": 1, "status": "running", "created_at": 0.0, "resolved_at": null,
               "stages": {"clone": "passed", "build": "failed", "test": "blocked", "deploy": "blocked"},
               "approval": null, "handoff": null},
  "events": [{"id": 1, "ts": 0.0, "kind": "alert", "payload": {}}],
  "bench": {"lazarus": {"n": 6, "resolved": 5, "avg_steps": 11.2, "avg_destructive": 0.0, "avg_score": 0.61},
            "naive":   {"n": 6, "resolved": 2, "avg_steps": 16.8, "avg_destructive": 0.7, "avg_score": 0.29}}
}
```

---

## 6. Timeline (6 hours)

| Time | Goal | Owners |
|---|---|---|
| 0:00 to 0:40 | **M0 recon**, Go/No-Go decision, rules check | All |
| 0:40 to 2:00 | M1 adapter + M2 agent running **one fault end to end** (this is your Evaluation 1 demo). B mocks the API; C builds the dashboard against `/api/state` mock | A / B / C |
| 2:00 to 3:30 | M3 risk gate, M4 verifier, M5 approval + escalation; dashboard live-wired | B (A helps) / C |
| 3:30 to 4:30 | M6 n8n flow; M8 benchmark starts running in the background | C / B |
| 4:30 to 5:15 | Cascade handling, handoff report, optional memory, polish | All |
| 5:15 to 6:00 | **Freeze.** Rehearse the demo 3x, record a backup video, write `NOTICE.md` | All |

**Evaluation 1 is at an unknown time.** Ask the organisers. Whatever time it is, you need the one-fault slice working by then.

**Cut order if behind:** memory, then n8n Telegram, then benchmark breadth (fewer episodes), then cascade handling (cap at 1 cycle).
**Never cut:** hypothesis board, risk gate + approval, independent verifier, escalation handoff. PS-12 explicitly asks for these.

---

## 7. Modules

---

### M0. Recon, setup and provenance (everyone, 40 min)

**Goal:** a running env, answers to the unknowns, and a clean provenance record.

```bash
git clone https://github.com/chaitanyaadeokar/Meta-Hackathon-CICD-Repair-Environment ctrlai-lazarus
cd ctrlai-lazarus
git tag ctrlai-base            # exact snapshot of what existed at event start
git checkout -b main-event     # all event work goes here
uv sync
cp .env.example .env           # set HF_TOKEN / MODEL_NAME if you want to run their baseline
CICD_SIMULATE=true uv run uvicorn server.app:app --host 0.0.0.0 --port 8000
```

In another terminal:
```bash
curl -s localhost:8000/health
open http://localhost:8000/docs        # FastAPI docs: shows the exact /reset and /step body shapes
```

**Answer these six questions and write the answers into `docs/RECON.md`:**

1. **Step body.** Is it `{"action": {...}}` or the raw action? (see `/docs`)
2. **State persistence.** Do `reset` then `step` then `step` keep state across plain HTTP calls? If not, read `client.py` and use the WebSocket client in the adapter.
3. **Choosing a fault.** Does `/reset` take a fault type or difficulty param? Read `server/` (look for where `reset` handles kwargs / curriculum). If not, note how to force a fault (env var? seed?).
4. **Fix formats.** Open `agent/` prompts and tool schemas. Copy the exact `value` format for `modify_config` and `add_dependency` examples. You need them in M2.
5. **Latency.** Time a `reset` and a `step`.
6. **Licence.** Open `LICENSE`; note it.

Also read `DESIGN.md` (extension guidance) and skim `eval_runner.py` (shows how they drive an episode).

**Then create the new folder and provenance file:**
```bash
mkdir -p lazarus/static docs n8n
touch lazarus/__init__.py
cat > NOTICE.md <<'EOF'
# NOTICE
## Inherited base (existing before CTRL+AI, tag: ctrlai-base)
OpenEnv CI/CD Repair Environment: server/, cicd/, agent/, models.py, sample-app/, fault library.
Original authors: <fill in, incl. upstream parthpetkar/Meta-Hackathon-CICD-Repair-Environment>.
## New in CTRL+AI (everything under lazarus/, commits after ctrlai-base)
Hypothesis-driven agent, risk gate, independent verifier, approval + escalation, dashboard,
n8n orchestration, Lazarus-vs-naive benchmark.
EOF
git add -A && git commit -m "ctrlai: recon notes, NOTICE, lazarus/ scaffold"
```

**Acceptance:** `docs/RECON.md` has six answers; the Go/No-Go boxes in section 1 are ticked; organiser answer on reuse noted.

---

### M1. Env adapter (Owner A, 20 min)

**Goal:** one tiny class every other module uses.

**File: `lazarus/envclient.py`**
```python
import os
import httpx

ENV_URL = os.getenv("ENV_URL", "http://localhost:8000")


class Env:
    """Thin HTTP adapter. If RECON shows state does not persist over HTTP, swap internals for the repo's client.py."""

    def __init__(self, base=ENV_URL):
        self.c = httpx.Client(base_url=base, timeout=180)

    @staticmethod
    def _obs(j):
        o = dict(j.get("observation", j))
        o["_reward"] = j.get("reward")
        o["_done"] = j.get("done", o.get("done", False))
        return o

    def reset(self, **kw):
        r = self.c.post("/reset", json=kw)
        r.raise_for_status()
        return self._obs(r.json())

    def step(self, operation, target="", value=""):
        action = {"operation": operation, "target": target, "value": value}
        r = self.c.post("/step", json={"action": action})
        if r.status_code == 422:                      # body shape differs: try the raw action
            r = self.c.post("/step", json=action)
        r.raise_for_status()
        return self._obs(r.json())

    def state(self):
        return self.c.get("/state").json()
```

**Smoke test (must pass before moving on):**
```python
from lazarus.envclient import Env
e = Env(); o = e.reset()
print(o["pipeline_status"], o["current_stage"], o["surfaced_errors"][:2])
o = e.step("view_logs", o["current_stage"]); print(o["visible_logs"][-3:])
```

**AI-IDE prompt:** "Using FastAPI docs at localhost:8000/docs and client.py, make lazarus/envclient.py work for reset/step/state with persistent state. Keep the Env class API: reset(**kw), step(operation,target,value), state()."

---

### M2. Hypothesis-driven agent (Owner A, ~75 min)

**Goal:** given a red pipeline, run triage, hypotheses, experiments, verdicts and a fix, emitting events as it goes.

**File: `lazarus/llm.py`**
```python
import json
import os
import re
import time
import httpx

PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")        # anthropic | openai_compat
MODEL = os.getenv("LLM_MODEL", "claude-sonnet-5-5")
KEY = os.getenv("LLM_API_KEY", "")
BASE = os.getenv("LLM_BASE_URL", "")                      # e.g. HF router, Groq, Gemini OpenAI-compat


class LLMError(Exception):
    pass


def _raw(system, user, max_tokens=1600):
    if PROVIDER == "anthropic":
        import anthropic
        c = anthropic.Anthropic(api_key=KEY or None)
        kw = dict(model=MODEL, max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}])
        try:
            r = c.messages.create(temperature=0, **kw)
        except Exception:
            r = c.messages.create(**kw)
        return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
    r = httpx.post(BASE.rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {KEY}"},
                   json={"model": MODEL, "temperature": 0, "max_tokens": max_tokens,
                         "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                   timeout=90)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def ask_json(system, user, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            t = re.sub(r"^```(?:json)?|```$", "", _raw(system, user).strip(), flags=re.M).strip()
            return json.loads(t[t.find("{"): t.rfind("}") + 1])
        except Exception as e:
            last = e
            time.sleep(1)
    raise LLMError(str(last))
```

**File: `lazarus/agent.py`**
```python
import json
import time
from .llm import ask_json, LLMError
from . import risk, verifier

READ_ONLY = {"view_logs", "tail_logs", "inspect_config", "inspect_dockerfile", "inspect_permissions"}
MAX_CYCLES = 3
MAX_FAILED_ATTEMPTS = 2

# >>> PASTE the exact modify_config / add_dependency `value` examples from the repo's agent/ prompts here (RECON Q4) <<<
FIX_FORMAT_HELP = "TODO: copy examples of the exact value format for modify_config and add_dependency."

SYSTEM = ("You are Lazarus, an autonomous SRE agent repairing a broken CI/CD pipeline. "
          "You reason from evidence, never guess. Reply with ONE JSON object only, no prose, no markdown.")


def digest(o, logs=25, cfg_chars=1500):
    """Compact view of an observation for the LLM."""
    return {
        "pipeline_status": o.get("pipeline_status"), "current_stage": o.get("current_stage"),
        "pipeline_stages": o.get("pipeline_stages"), "surfaced_errors": (o.get("surfaced_errors") or [])[:8],
        "alerts": (o.get("visible_alerts") or [])[:5], "recent_logs": (o.get("visible_logs") or [])[-logs:],
        "findings": (o.get("findings") or [])[-8:],
        "config_files": {k: str(v)[:cfg_chars] for k, v in (o.get("config_files") or {}).items()},
        "available_tools": o.get("available_tools"),
    }


def hypotheses(obs, excluded):
    user = (f"EVIDENCE:\n{json.dumps(digest(obs), indent=1)}\n\n"
            f"Write exactly 3 competing root-cause hypotheses, most plausible first. For each, name ONE read-only "
            f"inspection action that would confirm or refute it. Allowed operations: {sorted(READ_ONLY)}. "
            f"`target` is a pipeline stage or a config file name from the evidence. Use 3 different actions where possible.\n"
            f"Do not repeat fixes already tried and failed: {excluded}\n"
            'Return {"hypotheses":[{"id":"H1","statement":"...","action":{"operation":"...","target":"..."},"confirm_if":"..."}]}')
    hs = ask_json(SYSTEM, user)["hypotheses"][:3]
    for h in hs:                                   # constrain: read-only operations only
        if h["action"].get("operation") not in READ_ONLY:
            h["action"] = {"operation": "view_logs", "target": obs.get("current_stage", "")}
    return hs


def judge(obs, hyps, results, excluded):
    user = (f"INITIAL EVIDENCE:\n{json.dumps(digest(obs, 15), indent=1)}\n\nHYPOTHESES:\n{json.dumps(hyps)}\n\n"
            f"EXPERIMENT RESULTS (per hypothesis id):\n{json.dumps(results, indent=1)[:9000]}\n\n"
            "TASK: give each hypothesis a verdict: confirmed | rejected | symptom_only | inconclusive, citing concrete evidence. "
            "Separate CAUSE from SYMPTOM (a failing test can be a symptom of a dependency pin). Beware red herrings: "
            "an error that appears first is not always the root cause. Then choose ONE minimal fix for the confirmed root cause.\n"
            "Allowed fix operations: modify_config, add_dependency. Prefer the smallest change; never delete or wipe files. "
            f"Fix `value` format notes:\n{FIX_FORMAT_HELP}\n"
            f"Already tried and failed (do not repeat): {excluded}\n"
            'Return {"verdicts":[{"id":"H1","verdict":"...","evidence":"..."}],"root_cause":"...","confidence":0.0,'
            '"fix":{"operation":"modify_config","target":"...","value":"..."},"reasoning":"..."}')
    return ask_json(SYSTEM, user)


def run_incident(inc, env, store, wait_for_approval, obs=None):
    """The whole loop. `store.emit(kind, **payload)` feeds the dashboard. Returns 'resolved' or 'escalated'."""
    emit = lambda kind, **p: store.emit(inc, kind, **p)
    obs = obs or env.reset()
    emit("alert", stage=obs.get("current_stage"), errors=(obs.get("surfaced_errors") or [])[:4],
         alerts=(obs.get("visible_alerts") or [])[:3])
    store.set_stages(inc, obs.get("pipeline_stages"))
    excluded, failed = [], 0

    try:
        for cycle in range(1, MAX_CYCLES + 1):
            # 1) TRIAGE (code, no LLM)
            obs = env.step("view_logs", obs.get("current_stage", ""))
            emit("triage", cycle=cycle, stage=obs.get("current_stage"), errors=(obs.get("surfaced_errors") or [])[:4])

            # 2) HYPOTHESES + EXPERIMENTS
            hyps = hypotheses(obs, excluded)
            emit("hypotheses", cycle=cycle, hypotheses=hyps)
            results = {}
            for h in hyps:
                o2 = env.step(h["action"]["operation"], h["action"].get("target", ""))
                results[h["id"]] = {"action": h["action"], "errors": (o2.get("surfaced_errors") or [])[:5],
                                    "new_findings": (o2.get("findings") or [])[-4:],
                                    "logs": (o2.get("visible_logs") or [])[-12:],
                                    "config": {k: str(v)[:800] for k, v in (o2.get("config_files") or {}).items()}}
                emit("experiment", hypothesis=h["id"], action=h["action"], errors=results[h["id"]]["errors"])
                obs = o2

            # 3) VERDICTS + FIX
            v = judge(obs, hyps, results, excluded)
            emit("verdicts", cycle=cycle, verdicts=v.get("verdicts"), root_cause=v.get("root_cause"),
                 confidence=v.get("confidence"))
            env.step("set_hypothesis", "", v.get("root_cause", ""))

            # 4) RISK GATE (code)
            fix = v["fix"]
            assess = risk.assess(fix, float(v.get("confidence", 0)))
            emit("plan", fix=fix, **assess)
            if assess["level"] == "blocked":
                return _escalate(inc, store, emit, "fix uses a disallowed operation", v)
            if assess["needs_approval"]:
                emit("approval_request", fix=fix, reasons=assess["reasons"], root_cause=v.get("root_cause"))
                decision = wait_for_approval(inc)            # blocks; True / False / None (timeout)
                emit("approval", approved=decision)
                if not decision:
                    return _escalate(inc, store, emit, "human rejected or did not answer", v)

            # 5) APPLY + RERUN + INDEPENDENT VERIFY
            before = obs
            obs = env.step(fix["operation"], fix.get("target", ""), fix.get("value", ""))
            emit("apply", fix=fix)
            obs = env.step("rerun_pipeline")
            store.set_stages(inc, obs.get("pipeline_stages"))
            res = verifier.verify(before, obs)
            emit("verify", cycle=cycle, **res)

            if res["verdict"] == "passed":
                obs = env.step("verify_fix")
                obs = env.step("finalize")
                emit("resolved", final_score=obs.get("final_score"), steps=len(obs.get("action_history") or []))
                store.finish(inc, "resolved")
                return "resolved"
            if res["verdict"] == "advanced":                 # cascading fault: the next one surfaced
                emit("cascade", note="fix worked; a downstream failure surfaced", stage=obs.get("current_stage"))
                continue
            failed += 1
            excluded.append(f'{fix.get("operation")}:{fix.get("target")}')
            if failed >= MAX_FAILED_ATTEMPTS:
                return _escalate(inc, store, emit, f"verification failed {failed}x ({res['verdict']})", v)
        return _escalate(inc, store, emit, "cycle limit reached with the pipeline still failing", {})
    except LLMError as e:
        return _escalate(inc, store, emit, f"LLM unavailable: {e}", {})
    except Exception as e:
        emit("error", error=str(e))
        return _escalate(inc, store, emit, f"unexpected error: {e}", {})


def _escalate(inc, store, emit, reason, last_verdict):
    handoff = store.build_handoff(inc, reason, last_verdict)
    emit("escalated", reason=reason, handoff=handoff)
    store.finish(inc, "escalated", handoff=handoff)
    return "escalated"
```

**Acceptance for M2:** run `run_incident` with a stub store on ONE easy fault. You should see: 3 hypotheses, 3 experiments, verdicts, a fix, a rerun, and a verifier verdict. Run it 5 times; note the resolve rate. If hypotheses come back malformed, tighten the prompt and lower the evidence size.

**AI-IDE prompt:** "Read lazarus/agent.py. Make hypotheses() and judge() robust: validate JSON shape, clamp evidence size, retry once on malformed output. Do not change function signatures or event kinds in docs/CONTRACTS.md."

**Pitfalls**
- Keep experiments **read-only**. The code forces this; do not remove it.
- The env throttles log access (`log_tokens_remaining`, `log_access_mode`). If `view_logs` stops returning full logs, switch triage to `tail_logs`.
- Repeated identical actions cost reward in the env (`redundant_actions`). Avoid inspecting the same target twice per cycle.

---

### M3. Risk gate (Owner B, 30 min)

**Goal:** a deterministic policy that decides auto vs human. The LLM never decides this.

**File: `lazarus/risk.py`**
```python
import re

ALLOWED_FIX_OPS = {"modify_config", "add_dependency"}
HIGH = re.compile(r"(secret|password|passwd|token|credential|api[_-]?key|iam|permission|terraform|tfstate|\.env\b|rotate)", re.I)
DESTRUCTIVE = re.compile(r"(\brm\b|\bdelete\b|\bdrop\s|truncate|wipe|--force|force[- ]push)", re.I)


def assess(fix: dict, confidence: float) -> dict:
    op = fix.get("operation", "")
    blob = f"{fix.get('target', '')} {fix.get('value', '')}"
    if op not in ALLOWED_FIX_OPS:
        return {"level": "blocked", "needs_approval": True,
                "reasons": [f"operation '{op}' is not an allowed fix action"]}
    level, reasons = ("medium" if op == "modify_config" else "low"), []
    if HIGH.search(blob):
        level = "high"
        reasons.append("touches secrets / permissions / infrastructure config")
    if DESTRUCTIVE.search(blob):
        level = "high"
        reasons.append("contains destructive keywords")
    if confidence < 0.6:
        reasons.append(f"low confidence ({confidence:.2f} < 0.60)")
    return {"level": level, "needs_approval": level == "high" or confidence < 0.6, "reasons": reasons}
```

**Tune after recon:** run it against all the faults you can trigger and check that secrets/IAM/Terraform faults come out `high` while dependency pins and Dockerfile ordering come out `low/medium`. The regex is deliberately simple; the point is that it is **auditable**.

**Say to the jury:** "Allowed actions are a short whitelist, and approval is decided by policy code, not by the model."

---

### M4. Independent verifier (Owner B, 20 min)

**Goal:** a judge that never trusts the fixer. No LLM, separate file, reads only the before/after observations.

**File: `lazarus/verifier.py`**
```python
def _counts(o):
    s = o.get("pipeline_stages") or {}
    return sum(1 for v in s.values() if v == "passed"), len(s)


def verify(before: dict, after: dict) -> dict:
    pb, _ = _counts(before)
    pa, n = _counts(after)
    new_destructive = (after.get("destructive_actions") or 0) - (before.get("destructive_actions") or 0)
    detail = {"stages_passed_before": pb, "stages_passed_after": pa, "stages_total": n,
              "pipeline_status": after.get("pipeline_status"), "new_destructive_actions": new_destructive}
    if new_destructive > 0 or pa < pb:
        return {"verdict": "regressed", **detail}
    if after.get("pipeline_status") == "passed" or (n and pa == n):
        return {"verdict": "passed", **detail}
    revealed_more = (after.get("revealed_issue_count") or 1) > (before.get("revealed_issue_count") or 1)
    if pa > pb or revealed_more:
        return {"verdict": "advanced", **detail}
    return {"verdict": "unchanged", **detail}
```

**Honest framing:** "independent" means a different code path with no model in it and no shared state with the fixer. The env's own `verify_fix` still runs afterwards as a final gate, because `finalize` requires it.

---

### M5. Store, API, approval and escalation (Owner B, 60 min)

**Goal:** incidents and events in memory (plus a JSON dump for safety), the HTTP API from section 5.2, and the handoff report.

**File: `lazarus/store.py`**
```python
import json
import threading
import time

_L = threading.RLock()
INCIDENTS = {}
APPROVALS = {}          # incident_id -> {"decision": None|True|False, "requested_at": float}
BENCH = {"lazarus": None, "naive": None}


def new_incident():
    with _L:
        i = len(INCIDENTS) + 1
        INCIDENTS[i] = {"id": i, "status": "running", "created_at": time.time(), "resolved_at": None,
                        "events": [], "stages": {}, "approval": None, "handoff": None}
        return i


def emit(i, kind, **payload):
    with _L:
        inc = INCIDENTS[i]
        inc["events"].append({"id": len(inc["events"]) + 1, "ts": time.time(), "kind": kind, "payload": payload})
        if kind == "approval_request":
            inc["status"] = "awaiting_approval"
            inc["approval"] = payload
            APPROVALS[i] = {"decision": None, "requested_at": time.time()}
        elif kind in ("approval", "plan") and inc["status"] == "awaiting_approval":
            inc["status"] = "running"


def set_stages(i, stages):
    with _L:
        INCIDENTS[i]["stages"] = dict(stages or {})


def finish(i, status, handoff=None):
    with _L:
        INCIDENTS[i].update(status=status, resolved_at=time.time(), handoff=handoff)
    try:
        json.dump(INCIDENTS, open("lazarus_incidents.json", "w"), default=str)
    except Exception:
        pass


def decide(i, approved):
    with _L:
        if i in APPROVALS:
            APPROVALS[i]["decision"] = bool(approved)


def wait_for_approval(i, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        with _L:
            d = APPROVALS.get(i, {}).get("decision")
        if d is not None:
            return d
        time.sleep(1)
    return None


def build_handoff(i, reason, last_verdict):
    """Everything an on-call human needs, in one object. This is the 'page a human' payload."""
    ev = INCIDENTS[i]["events"]
    get = lambda k: [e["payload"] for e in ev if e["kind"] == k]
    return {"reason": reason,
            "root_cause_best_guess": (last_verdict or {}).get("root_cause"),
            "confidence": (last_verdict or {}).get("confidence"),
            "hypotheses_tested": get("hypotheses")[-1:] or None,
            "fixes_attempted": get("apply"),
            "verifier_results": get("verify"),
            "suggested_next_step": "Review the failing stage logs and the attempted fixes above; the pipeline was left in its last verified state."}
```

**File: `lazarus/server.py`**
```python
import threading
import time
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import store, agent
from .envclient import Env

app = FastAPI(title="Lazarus-CI")
STATIC = Path(__file__).parent / "static"


class StartReq(BaseModel):
    fault: str | None = None


class ApproveReq(BaseModel):
    approved: bool


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.post("/incident/start")
def start(req: StartReq):
    i = store.new_incident()
    kw = {"fault": req.fault} if req.fault else {}        # adjust to however /reset selects a fault (RECON Q3)

    def work():
        env = Env()
        try:
            obs = env.reset(**kw)
        except Exception as e:
            store.emit(i, "error", error=f"reset failed: {e}")
            store.finish(i, "escalated", handoff={"reason": f"could not start the sandbox: {e}"})
            return
        agent.run_incident(i, env, store, store.wait_for_approval, obs=obs)

    threading.Thread(target=work, daemon=True).start()
    return {"incident_id": i}


@app.get("/incident/{i}/status")
def status(i: int):
    inc = store.INCIDENTS.get(i)
    if not inc:
        return {"status": "unknown"}
    summary = (inc["handoff"] or {}).get("reason") if inc["status"] == "escalated" else None
    return {"status": inc["status"], "summary": summary, "events": len(inc["events"])}


@app.post("/incident/{i}/approve")
def approve(i: int, req: ApproveReq):
    store.decide(i, req.approved)
    return {"ok": True}


@app.get("/api/state")
def state():
    inc = store.INCIDENTS[max(store.INCIDENTS)] if store.INCIDENTS else None
    return {"incident": inc and {k: v for k, v in inc.items() if k != "events"},
            "events": inc["events"] if inc else [], "bench": store.BENCH, "now": time.time()}


@app.post("/bench/run")
def bench_run(body: dict | None = None):
    from . import bench
    n = int((body or {}).get("n", 6))
    threading.Thread(target=bench.run, args=(n,), daemon=True).start()
    return {"started": True}
```

Run it: `uv run uvicorn lazarus.server:app --port 8100` (no `--reload` during demos; it kills running incidents).

**Escalation rules (code, already in agent.py):** escalate when the fix is blocked, the human rejects/times out, verification fails twice, the cycle limit is hit, the LLM is unreachable, or anything unexpected happens. **Fail safe: unknown means a human.**

**AI-IDE prompt:** "Implement docs/CONTRACTS.md section 5.2 in lazarus/server.py using lazarus/store.py. Add a /incident/{id}/events endpoint. Keep it dependency-light."

---

### M6. n8n orchestration (Owner C, 45 min)

**Honest role of n8n here:** it is the **intake, notification and escalation layer** (the "automation technology"). The reasoning loop lives in Python for reliability. Say this plainly in the demo.

Add n8n to Docker (one command):
```bash
docker run -d --name lazarus-n8n -p 5678:5678 -e N8N_SECURE_COOKIE=false \
  --add-host=host.docker.internal:host-gateway -v n8n_data:/home/node/.n8n n8nio/n8n
```
Open `http://localhost:5678`, create a workflow, then **Activate** it.

| # | Node | Config |
|---|---|---|
| 1 | **Webhook** "Pipeline Alert" | POST, path `lazarus-alert`. (In the real world: GitHub trigger on a failed `workflow_run`.) |
| 2 | **HTTP Request** "Start Lazarus" | POST `http://host.docker.internal:8100/incident/start`, JSON body `{}`; timeout 30s |
| 3 | **Set** "Remember id" | `incident_id = {{ $json.incident_id }}` |
| 4 | **Wait** | 5 seconds |
| 5 | **HTTP Request** "Check status" | GET `http://host.docker.internal:8100/incident/{{ $('Set').item.json.incident_id }}/status` |
| 6 | **IF** | `{{ $json.status }}` in `resolved`, `escalated` (else: back to node 4) |
| 7 | **Telegram** "Notify" | Chat ID from your bot; text: `Lazarus incident #{{ ... }}: {{ $json.status }} {{ $json.summary }}` |

**Telegram setup (outbound only, needs no public URL):** message @BotFather, create a bot, copy the token, message your bot once, then read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`. Add the credential in n8n. Test it on the phone you will use in the demo.

**Approvals** happen on the dashboard (buttons call `/incident/{id}/approve`). Telegram inline-button approval needs a public webhook; treat it as a stretch.

**Trigger for the demo:** `curl -X POST http://localhost:5678/webhook/lazarus-alert` (use the *Production* URL, so the workflow must be active).

---

### M7. Dashboard (Owner C, 75 min)

**Goal:** one page that makes the agent's thinking visible. Build against the mock `/api/state` in section 5.4 first.

**Must show**
1. **Stage strip:** clone, build, test, deploy, coloured by status.
2. **Hypothesis board:** cards H1..H3 showing statement, experiment, evidence line and verdict badge (confirmed green, rejected red, symptom_only amber).
3. **Timeline** of events.
4. **Approval card** with Approve / Reject when `incident.status == "awaiting_approval"`.
5. **Handoff report** when escalated.
6. **Timer** (alert to resolved) and **Break pipeline** button.
7. **Benchmark table** (Lazarus vs naive).

**File: `lazarus/static/index.html`**
```html
<!doctype html><html><head><meta charset="utf-8"><title>Lazarus-CI</title>
<style>
:root{--bg:#0f1318;--card:#1a2029;--tx:#e6edf3;--mut:#8b98a5;--ok:#2ea043;--bad:#f85149;--warn:#d29922;--run:#388bfd}
body{margin:0;font:14px system-ui;background:var(--bg);color:var(--tx);padding:16px}
h1{margin:0 0 12px;font-size:20px} .row{display:flex;gap:12px;flex-wrap:wrap}
.card{background:var(--card);border-radius:8px;padding:12px;flex:1;min-width:300px}
.stage{display:inline-block;padding:6px 14px;margin-right:6px;border-radius:6px;background:#30363d}
.passed{background:var(--ok)}.failed{background:var(--bad)}.running{background:var(--run)}.blocked,.pending{background:#30363d;color:var(--mut)}
.badge{padding:2px 8px;border-radius:10px;font-size:12px}
.confirmed{background:var(--ok)}.rejected{background:var(--bad)}.symptom_only{background:var(--warn);color:#000}.inconclusive{background:#555}
button{background:var(--run);color:#fff;border:0;border-radius:6px;padding:8px 14px;cursor:pointer;margin-right:6px}
button.ok{background:var(--ok)}button.no{background:var(--bad)}
.ev{border-left:3px solid #30363d;padding:2px 8px;margin:3px 0;color:var(--mut)} .ev b{color:var(--tx)}
table{width:100%;border-collapse:collapse}td,th{padding:4px 8px;text-align:left;border-bottom:1px solid #30363d}
pre{white-space:pre-wrap;color:var(--mut)}
</style></head><body>
<h1>LAZARUS-CI <span id="status" class="badge"></span> <span id="timer" style="float:right;color:var(--mut)"></span></h1>
<div class="card" style="margin-bottom:12px"><div id="stages"></div>
  <p><button onclick="start()">Break the pipeline</button><button onclick="bench()">Run benchmark</button></p></div>
<div id="approval"></div>
<div class="row"><div class="card"><b>Hypothesis board</b><div id="board"></div></div>
<div class="card"><b>Timeline</b><div id="timeline"></div></div></div>
<div class="row" style="margin-top:12px"><div class="card"><b>Benchmark (same environment)</b><div id="bench"></div></div>
<div class="card"><b>Handoff / result</b><div id="handoff"></div></div></div>
<script>
const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function start(){await fetch('/incident/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})}
async function bench(){await fetch('/bench/run',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"n":6}'})}
async function decide(id,a){await fetch(`/incident/${id}/approve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approved:a})})}
function render(s){
  const inc=s.incident; if(!inc){return}
  $('status').textContent=inc.status; $('status').className='badge '+({resolved:'passed',escalated:'failed',running:'running',awaiting_approval:'blocked'}[inc.status]||'');
  const end=inc.resolved_at||s.now; $('timer').textContent='incident #'+inc.id+' | '+(end-inc.created_at).toFixed(1)+'s';
  $('stages').innerHTML=Object.entries(inc.stages||{}).map(([k,v])=>`<span class="stage ${v}">${esc(k)}: ${esc(v)}</span>`).join('');
  $('approval').innerHTML=inc.status==='awaiting_approval'?`<div class="card" style="margin-bottom:12px;border:1px solid var(--warn)"><b>Human approval needed</b>
    <pre>${esc(JSON.stringify(inc.approval,null,1))}</pre><button class="ok" onclick="decide(${inc.id},true)">Approve</button><button class="no" onclick="decide(${inc.id},false)">Reject</button></div>`:'';
  // hypothesis board from the latest hypotheses/verdicts/experiment events
  const ev=s.events, hy=[...ev].reverse().find(e=>e.kind==='hypotheses'), vd=[...ev].reverse().find(e=>e.kind==='verdicts');
  const verdicts={}; (vd?.payload.verdicts||[]).forEach(v=>verdicts[v.id]=v);
  $('board').innerHTML=(hy?.payload.hypotheses||[]).map(h=>{const v=verdicts[h.id];
    return `<div class="ev"><b>${esc(h.id)}</b> ${esc(h.statement)}<br>experiment: ${esc(h.action?.operation)} ${esc(h.action?.target)}<br>`+
    (v?`<span class="badge ${esc(v.verdict)}">${esc(v.verdict)}</span> ${esc(v.evidence)}`:'<i>testing…</i>')+`</div>`}).join('')||'<i>waiting…</i>';
  $('timeline').innerHTML=ev.slice(-25).map(e=>`<div class="ev"><b>${esc(e.kind)}</b> ${esc(JSON.stringify(e.payload).slice(0,160))}</div>`).join('');
  $('handoff').innerHTML=inc.handoff?`<pre>${esc(JSON.stringify(inc.handoff,null,1))}</pre>`:(inc.status==='resolved'?'<span class="badge passed">resolved + verified</span>':'');
  const b=s.bench||{}; const row=(n,x)=>x?`<tr><td>${n}</td><td>${x.resolved}/${x.n}</td><td>${x.avg_steps}</td><td>${x.avg_destructive}</td><td>${x.avg_score}</td></tr>`:`<tr><td>${n}</td><td colspan=4>not run</td></tr>`;
  $('bench').innerHTML=`<table><tr><th>agent</th><th>resolved</th><th>steps</th><th>destructive</th><th>score</th></tr>${row('Lazarus',b.lazarus)}${row('Naive',b.naive)}</table>`;
}
async function tick(){try{render(await (await fetch('/api/state')).json())}catch(e){} }
setInterval(tick,1000); tick();
</script></body></html>
```

**AI-IDE prompt (Owner C):** "Improve lazarus/static/index.html: keep it a single file, no build step, no external CDN. Keep the polling of /api/state and the element ids. Make the hypothesis cards look great, animate the stage strip, and make the approval card unmissable. Do not change the API contract."

---

### M8. Benchmark: Lazarus vs naive (Owner B, 50 min, run it in the background)

**Goal:** produce **real** numbers today. Same environment, same number of episodes, two agents.

- **Lazarus:** `agent.run_incident` with auto-approval (bench only).
- **Naive:** one LLM call per step, "pick the next action from the available tools", no hypotheses, no risk gate, no independent verifier, up to 20 steps. This models "chatting with the logs".
- Record from the final observation: `incident_resolved`, `final_score`, `destructive_actions`, `redundant_actions`, and the step count.

**File: `lazarus/bench.py`**
```python
import json
import time
from . import store, agent
from .envclient import Env
from .llm import ask_json

NAIVE_SYS = "You are an engineer fixing a failing CI/CD pipeline. Reply with ONE JSON object only."


def _summ(runs):
    n = len(runs)
    avg = lambda k: round(sum(r[k] for r in runs) / n, 2) if n else 0
    return {"n": n, "resolved": sum(1 for r in runs if r["resolved"]), "avg_steps": avg("steps"),
            "avg_destructive": avg("destructive"), "avg_score": avg("score")}


def run_naive(env):
    obs = env.reset()
    for step in range(20):
        ctx = {k: obs.get(k) for k in ("pipeline_status", "current_stage", "surfaced_errors", "visible_logs",
                                        "config_files", "available_tools", "action_history")}
        a = ask_json(NAIVE_SYS, json.dumps(ctx, default=str)[:9000] +
                     '\nChoose the next action. Return {"operation":"...","target":"...","value":"..."}. '
                     "Finish with finalize when you think it is fixed.")
        obs = env.step(a.get("operation", "view_logs"), a.get("target", ""), a.get("value", ""))
        if obs.get("_done"):
            break
    return obs, step + 1


def run_lazarus(env):
    i = store.new_incident()
    obs = env.reset()
    agent.run_incident(i, env, store, lambda _i: True, obs=obs)       # auto-approve ONLY inside the benchmark
    st = env.state() if hasattr(env, "state") else {}
    return st, len(store.INCIDENTS[i]["events"])


def run(n=6):
    res = {"lazarus": [], "naive": []}
    for which, fn in (("lazarus", run_lazarus), ("naive", run_naive)):
        for _ in range(n):
            env = Env()
            try:
                o, steps = fn(env)
                # >>> If env.state() does not expose these fields, read them from the last step observation instead (RECON). <<<
                res[which].append({"resolved": bool(o.get("incident_resolved")), "score": float(o.get("final_score") or 0),
                                   "destructive": int(o.get("destructive_actions") or 0), "steps": steps})
            except Exception as e:
                res[which].append({"resolved": False, "score": 0.0, "destructive": 0, "steps": 0})
            store.BENCH[which] = _summ(res[which])
    json.dump(res, open(f"bench_{int(time.time())}.json", "w"))
```

**Fix before trusting it:** `run_lazarus` should read the final observation fields from the env the same way `run_naive` does. Wire it to whatever `finalize` returns (RECON). If the numbers don't come out clean, say so. **Report the raw per-episode results**, not just averages.

**How to present it honestly:** "Same environment, 6 episodes each, faults vary per episode so there is variance; here are the raw runs." Do not cherry-pick. If Lazarus only wins on safety (fewer destructive actions) and not on resolve rate, say that; it is still a good story.

**Start this by 3:30 and let it run while you build other things.**

---

## 8. Optional stretch (only if ahead)

- **Memory:** after a verified fix, store `{normalized error signature -> root cause, fix}` in SQLite; on the next similar incident, inject it into the hypothesis prompt as a *hint* (not a blind replay: the env's adversarial designer changes context each reset). Show "investigation time 31s to 12s". The env has its own cross-episode hints, so be clear which one you're showing.
- **Fix as a pull request:** for high-risk fixes, open a PR on a demo repo with the diff and verifier evidence instead of mutating directly; "human approves = merge". Needs a GitHub token.
- **Rehearsal on a clone:** only if RECON shows you can start a second env instance on the same fault. Otherwise skip; do not fake it.
- **GitHub webhook entry:** n8n GitHub trigger on failed `workflow_run`.

---

## 9. Demo script (about 5 minutes)

| Time | Beat |
|---|---|
| 0:00 | **Disclose + frame (20s):** "Sandbox and fault library come from our earlier OpenEnv project; the agent, risk gate, verifier, approvals, dashboard and benchmark were built today." |
| 0:20 | Green pipeline on the dashboard. A judge presses **Break the pipeline**. |
| 0:35 | Stage strip goes red, the hypothesis board fills, experiments run, **one hypothesis is rejected with evidence**, one confirmed. |
| 1:30 | Fix applied, rerun, the **verifier** says passed (or advanced, then the cascade cycle). |
| 2:15 | Run a second incident that triggers the **risk gate** (secrets / IAM fault). Your phone buzzes (n8n Telegram); press **Approve** on the dashboard. |
| 3:15 | Show the **benchmark table**: Lazarus vs naive on the same env, real numbers from today. |
| 4:00 | Show an **escalation handoff report** (from a rejected approval or a failed verification). |
| 4:30 | Tagline. Questions. |

If a fault doesn't trigger the way you want live, narrate from the **backup video** (record it in the last hour).

---

## 10. Judge Q&A

| Question | Answer |
|---|---|
| "Is this just your old hackathon project?" | "The sandbox and fault library are, and we disclosed that. The agent team, risk gate, verifier, approvals, dashboard, orchestration and benchmark were written today; the commits after the `ctrlai-base` tag show it." |
| "Is it really autonomous?" | "Yes for low and medium risk. High risk pauses for a human by policy. Anything unexpected escalates." |
| "What if the LLM is wrong?" | "The risk gate and verifier are code. A wrong fix gets caught by the verifier and escalates after two failures." |
| "Can it delete things?" | "Only two fix operations are allowed (`modify_config`, `add_dependency`), and destructive patterns force approval." |
| "Is the verifier really independent?" | "It's a separate code path with no model and no shared state, reading before/after pipeline state. The env's own `verify_fix` runs as a final gate." |
| "Where's n8n?" | "Alert intake, status polling, and Telegram notification/escalation. The reasoning loop is Python so the demo is reliable." |
| "How do you know it beats a chat agent?" | "We ran both on the same environment today; raw runs are on screen. There's variance, and we show it." |
| "Did you train a model?" | "No. We reused an environment built for RL, but today's agent uses prompting plus code-enforced safety." |

---

## 11. Day-of checklist

- [ ] Rules/disclosure question answered; written down
- [ ] `git tag ctrlai-base` made **before** any new commit
- [ ] `NOTICE.md` committed
- [ ] Env runs with `CICD_SIMULATE=true`; Go/No-Go ticked by 0:40
- [ ] `.env` has LLM key; tested from the venue network (use Ethernet; mobile hotspot as backup)
- [ ] Telegram bot tested on the demo phone
- [ ] Benchmark run completed at least once; raw JSON saved
- [ ] Backup demo video recorded
- [ ] Laptop, charger, **Ethernet cable**, formal attire; registration before 9:15; stay in the hall until 4:00

---

## 12. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Env can't be driven reliably over HTTP | Use the repo's own `client.py` / WebSocket; decide at the 30-minute Go/No-Go |
| Faults aren't selectable / not reproducible | Demo with whatever `reset` gives; have the backup video; pre-run to learn which faults appear |
| LLM returns malformed JSON | `ask_json` retries; judge/hypothesis validation; escalate on repeated failure |
| LLM latency makes the demo slow | Trim evidence size; keep 3 hypotheses; use a fast model |
| Reuse not allowed | Fall back to the from-scratch CanteenDash plan |
| Benchmark shows no advantage | Report honestly; lead with safety (destructive actions, escalation) instead |
| Everything unexpected | Escalate to a human; never retry blindly |

---

## 13. Definition of done

- [ ] A red pipeline triggers the agent automatically (button, curl, or n8n webhook)
- [ ] Three hypotheses are shown, tested, and judged **with evidence** on the dashboard
- [ ] A high-risk fix pauses for human approval and resumes on approval
- [ ] The independent verifier judges every rerun; `finalize` only after a pass
- [ ] Verification failing twice (or a rejection) produces an escalation handoff report and a Telegram message
- [ ] A Lazarus-vs-naive table exists from real runs done today
- [ ] The disclosure line is in the demo and `NOTICE.md` is in the repo
