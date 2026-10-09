# Reconnaissance Notes (M0)

## 1. Step Body Format
- **Observation:** In `server/app.py`, the `_FlatActionNormalizerMiddleware` intercepts all `POST /step` requests.
- **Result:** Both the standard OpenEnv format `{"action": {"operation": "...", "target": "...", "value": "..."}}` AND the flat format `{"operation": "...", "target": "...", "value": "..."}` are accepted. The middleware automatically wraps flat action dicts into `{"action": ...}` before handing them to FastAPI.

## 2. State Persistence
- Plain HTTP `/reset` creates a new episode workspace in a temporary folder and sets the server singleton `_state`.
- Plain HTTP `/step` operates on that current state.
- For dedicated sessions with low latency, `client.py` provides `MetaHackathonEnv` using persistent WebSockets (`WS /ws`).
- Our adapter `lazarus/envclient.py` handles HTTP calls directly with fallback / compatibility.

## 3. Choosing a Fault
- `server/environment.py`:
  - `reset(task_key=...)` checks `eff_task_key` for modes like `"procedural"` or `"combo"`.
  - Fault selection is managed by `server/curriculum.py` via `CurriculumController.select_fault_type()`.
  - It uses UCB1 multi-armed bandit algorithm over the 20 canonical fault types defined in `cicd/fault_types.py`.

## 4. Fix Formats (Targets and Values)
From `server/app.py` `FAULT_FIX_HINTS` and `agent/` schemas:
- `merge_conflict`: `modify_config` -> target: `Dockerfile`, value: `resolve-merge-conflict`
- `dependency_conflict`: `add_dependency` -> target: `services/api/requirements.txt`, value: `pin-compatible-requests-urllib3`
- `docker_order`: `modify_config` -> target: `Dockerfile`, value: `reorder-docker-install-steps`
- `flaky_test`: `modify_config` -> target: `tests/test_api.py`, value: `add-flaky-test-retry-wrapper`
- `missing_permission`: `modify_config` -> target: `docker-compose.yml`, value: `fix-docker-compose-network`
- `secret_exposure`: `modify_config` -> target: `services/api/app.py`, value: `remove-hardcoded-secrets`
- `env_drift`: `modify_config` -> target: `docker-compose.yml`, value: `fix-docker-compose-network`
- `invalid_database_url`: `modify_config` -> target: `.env`, value: `fix-database-url`
- `empty_secret_key`: `modify_config` -> target: `.env`, value: `restore-secret-key`
- `missing_pythonpath`: `modify_config` -> target: `.venv/runtime.pth`, value: `restore-pythonpath`
- `circular_import_runtime`: `modify_config` -> target: `services/api/runtime_probe.py`, value: `break-circular-import`
- `missing_package_init`: `modify_config` -> target: `services/runtime_support/__init__.py`, value: `restore-package-init`
- `none_config_runtime`: `modify_config` -> target: `.env`, value: `replace-none-runtime-config`
- `log_pii_leak`: `modify_config` -> target: `services/api/routes.py`, value: `remove-pii-log-line`
- `log_disabled`: `modify_config` -> target: `services/api/logging_config.py`, value: `restore-info-logging`
- `bad_migration_sql`: `modify_config` -> target: `db/migrations/001_init.sql`, value: `fix-bad-migration-sql`
- `schema_drift`: `modify_config` -> target: `db/database.py`, value: `align-schema-columns`
- `terraform_invalid_provider`: `modify_config` -> target: `infra/main.tf`, value: `fix-terraform-provider`
- `terraform_missing_variable`: `modify_config` -> target: `infra/terraform.tfvars`, value: `add-terraform-variables`
- `terraform_permission_denied`: `modify_config` -> target: `infra/main.tf`, value: `remove-terraform-permission-blocker`

## 5. Latency
- Measured in simulated mode (`CICD_SIMULATE=true`):
  - Reset latency: ~0.5 - 1.5s
  - Step latency: ~0.1 - 0.5s

## 6. License
- Base repo is licensed under the Meta BSD-Style License.
- Inherited attribution is documented in `NOTICE.md`.
