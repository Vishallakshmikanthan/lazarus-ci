# Ctrl + AI Agentic AI Hackathon Environment Design

## 1. Purpose

This repository implements a real CI/CD repair environment for RL agents under OpenEnv.
The key design intent is to evaluate world-modeling behavior in professional tasks:

- infer root cause from partial/noisy evidence,
- choose safe interventions,
- validate with real pipeline reruns,
- adapt when the world changes mid-episode.

The environment is not a synthetic rule table. It executes real file mutations and real subprocess pipeline stages over a per-episode workspace.

## 2. OpenEnv contract

The API contract is stable and intentionally small:

- `reset()` -> initial observation
- `step(action)` -> next observation, reward, done
- `state()` -> `episode_id`, `step_count`

All architecture changes are implemented behind this contract.

## 3. Architecture overview

### 3.1 Runtime layers

- API layer: `server/app.py`
  - Creates OpenEnv HTTP server wiring for `RealCICDRepairEnvironment`.
- Environment orchestration: `server/environment.py`
  - Owns episode lifecycle, action dispatch, reward shaping, termination, and metadata.
- CI/CD execution layer: `cicd/pipeline_runner.py`
  - Runs `clone -> build -> test -> deploy` with subprocesses and captured logs.
- Scenario and mutation layer:
  - Fault injection: `cicd/fault_injector.py`
  - Procedural scenario generator: `cicd/procedural_generator.py`
- Evidence layer: `cicd/observation_builder.py`
  - Builds surfaced errors, config snapshots, metrics, and stage-specific logs.
- Fix execution layer: `cicd/fix_applier.py`
  - Applies structured JSON edits or heuristic fixes, then commits.
- Learning and adaptation layer:
  - Curriculum scheduler: `server/curriculum.py` — UCB1 fault selection + EMA difficulty (alpha=0.35, step cap=0.15).
  - Adversarial incident designer: `server/adversarial_designer.py` — 15 s timeout with auto-retry at higher `max_tokens` on JSON truncation.
  - Phase-aware judge shaping: `server/adversarial_judge.py`
  - Cross-episode memory: `server/agent_memory.py` — fix recall + optimal-path storage keyed by fault type.
  - Rubric semantic judge: `server/rubric_judge.py`

### 3.2 Inference stack

- Entry point: `inference.py`
- Agent runtime: `agent/runner.py` — tool-calling loop, multi-pass guardrails, optimal-path hint injection, step-trace recording.
- Prompt construction: `agent/prompts.py` — BASE_SYSTEM_PROMPT + general skill cards + task-specific skill cards.
- Model tool-calling: `agent/model_client.py`
- Action normalization and guards: `agent/actions.py`
- HTTP adapter: `agent/http_environment.py`
- Structured logs: `agent/trajectory_logging.py`

## 4. Episode lifecycle

### 4.1 Reset path

1. Create isolated workspace from `sample-app`.
2. Select/adapt scenario via curriculum plus adversarial designer.
3. Inject one or more faults into real files.
4. Execute initial pipeline.
5. Build and return observation with logs, surfaced errors, and config snapshots.

### 4.2 Step path

1. Parse action (`operation|target|value`) and update episode history.
2. Dispatch to operation handler in `server/environment.py`.
3. Optionally mutate files, rerun pipeline, or verify fix state.
4. Apply phase bonuses/penalties and redundancy policy.
5. Build observation with updated evidence and metadata flags.

### 4.3 Termination path

- Termination occurs on verified finalize or step budget exhaustion.
- Terminal scoring combines deterministic score and optional rubric score.

## 5. Fault and drift model

### 5.1 Fault classes

There are 20 fault types across four categories:

**Core faults** (single-app): `merge_conflict`, `dependency_conflict`, `docker_order`, `flaky_test`, `missing_permission`, `secret_exposure`, `env_drift`.

**Logging/observability faults** (build or deploy stage): `log_bad_config`, `log_path_unwritable`, `log_volume_missing`, `log_rotation_missing`, `log_pii_leak`, `log_disabled`.

**Multi-app cross-service faults**: `shared_secret_rotation`, `infra_port_conflict`, `dependency_version_drift`.

**Database faults**: `bad_migration_sql`, `schema_drift`, `wrong_db_url`, `init_order_race`, `missing_volume_mount`.

Each fault declares:

- expected failing stage,
- affected files,
- keyword set for hypothesis alignment,
- injection mutation behavior.

Fault injection reliability constraints:

- `flaky_test` uses a deterministic impossibly tight threshold so every episode has a real failure. The agent still sees a timing-sensitive assertion and must diagnose it as a flaky/policy issue.
- `missing_permission` injects an `external: true` network reference without a `name:` alias, which is reliably rejected by Docker Compose across versions.
- `infra_port_conflict` targets port `8001:8001` (the current template) with a fallback scan for the legacy `8000:8000` mapping. Both the injector and fix applier maintain this dual-port logic for backward compatibility.

## 6. Reward and scoring model

### 6.1 Step rewards

Per-step rewards encourage:

- stage-relevant inspection,
- accurate hypothesis formulation,
- safe fix application,
- rerun-verify-finalize discipline.

Penalties discourage:

- wrong hypotheses,
- failed or destructive fixes,
- repeated redundant actions,
- premature finalize without verification.

### 6.2 Deterministic terminal score

Deterministic scoring factors:

- incident resolution and verification,
- hypothesis quality signal,
- fix hits,
- efficiency versus difficulty budget,
- penalties for redundant/destructive/wrong actions,
- pipeline health multiplier.

### 6.3 Rubric delayed reward

Rubric scoring is optional and blended at episode end:

- primary: OpenEnv LLMJudge path,
- fallback: OpenRouter-compatible API scoring,
- final fallback: heuristic semantic scorer.

Blend formula:

`final = (1 - w) * deterministic + w * rubric`

where `w = META_HACKATHON_RUBRIC_WEIGHT`.

## 7. Safety and completion guards

Key safeguards in runtime:

- finalize is blocked unless `verify_fix` has confirmed latest rerun,
- destructive fixes are explicitly penalized,
- repeated identical actions are clamped by redundancy policy,
- verification requires rerun evidence context.

These prevent score gaming and align behavior with production SRE workflow.

## 8. Agent memory and optimal-path learning

`server/agent_memory.py` provides two cross-episode memory mechanisms:

1. **Fix recall**: error fingerprint → suggested fix, with confidence and recency weighting. Injected as a hint in the initial observation of each episode.
2. **Optimal-path recall**: at the end of a resolved episode, the positive-reward step sequence is distilled (negative-reward steps stripped) and stored keyed by `fault_type`. On subsequent episodes of the same fault type, the path is injected into the first user message so the agent can follow a proven resolution sequence.

Optimal paths are built in `agent/runner.py` (`_build_optimal_path`) — only steps with `reward >= 0` are kept, truncated at the first `finalize`. This produces a clean, efficient template free of wasted moves.

## 9. Observability and reproducibility

### 9.1 Runtime observability

Observation payload surfaces:

- stage status and logs,
- extracted/surfaced errors,
- config file snapshots,
- reasoning trace (`findings`, action/hypothesis history),
- scoring diagnostics (`deterministic_score`, `rubric_score`, `delayed_reward`),
- verification readiness metadata.

### 9.2 Audit trail and reproducibility

With audit trail enabled, metadata includes deterministic lineage fields (`episode_seed`, variant metadata, sampled pattern events), enabling reviewer-side traceability for surfaced evidence.

Generated artifacts in `results/` capture deterministic evaluation and inference trajectories.

## 10. Extensibility guide

Preferred extension points:

- Add new fault types in `cicd/fault_injector.py` (and register in `FAULT_TYPES`, `FAULT_STAGE_MAP`, `FAULT_KEYWORDS`) and clue extraction in `cicd/observation_builder.py`.
- Tune rewards/terminal components in `server/environment.py`.
- Extend semantic judging policy in `server/rubric_judge.py`.
- Add agent guards/strategies in `agent/actions.py` and `agent/runner.py`.
- Extend optimal-path storage schema in `server/agent_memory.py`.

## 11. Design constraints

- Keep OpenEnv API surface unchanged.
- Maintain backward-compatible env vars and defaults where possible.
- Avoid silent, implicit shortcuts that bypass inspect/rerun/verify loops.
- Preserve reproducibility for benchmark runs.
- Fault injection must produce a real pipeline failure; unreliable injectors cause silent zero-score episodes and corrupt curriculum statistics.
