# Contracts (Lazarus-CI)

## 5.1 Env Adapter (A provides, B and C use)
```python
env.reset(**kw)  -> obs: dict
env.step(operation, target="", value="") -> obs: dict
# obs always includes the OpenEnv observation fields plus:
#   obs["_reward"], obs["_done"]
```

## 5.2 Lazarus HTTP API (B provides, C consumes)
| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/incident/start` | `{"fault": "<optional>"}` | `{"incident_id": 1}` |
| GET | `/incident/{id}/status` | | `{"status": "...", "summary": "..."}` |
| GET | `/api/state` | | latest incident + events + stages + bench |
| POST | `/incident/{id}/approve` | `{"approved": true}` | `{"ok": true}` |
| POST | `/bench/run` | `{"n": 6}` | `{"started": true}` |

`status` values: `running, awaiting_approval, resolved, escalated`.

## 5.3 Event Kinds (Dashboard timeline)
`alert, triage, hypotheses, experiment, verdicts, plan, approval_request, approval, apply, verify, cascade, resolved, escalated, error`

Each event: `{"id": n, "ts": float, "kind": "...", "payload": {...}}`

## 5.4 `/api/state` Shape
```json
{
  "incident": {
    "id": 1,
    "status": "running",
    "created_at": 0.0,
    "resolved_at": null,
    "stages": {"clone": "passed", "build": "failed", "test": "blocked", "deploy": "blocked"},
    "approval": null,
    "handoff": null
  },
  "events": [{"id": 1, "ts": 0.0, "kind": "alert", "payload": {}}],
  "bench": {
    "lazarus": {"n": 6, "resolved": 5, "avg_steps": 11.2, "avg_destructive": 0.0, "avg_score": 0.61},
    "naive":   {"n": 6, "resolved": 2, "avg_steps": 16.8, "avg_destructive": 0.7, "avg_score": 0.29}
  }
}
```
