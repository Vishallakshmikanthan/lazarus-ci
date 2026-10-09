# AGENTS.md
Project: Lazarus-CI. New code lives ONLY in lazarus/. Never edit server/, cicd/, agent/, models.py (inherited base).
The LLM proposes; code disposes: risk, approval, verification are deterministic code, never LLM decisions.
Contracts are in docs/CONTRACTS.md. Do not change them without telling the other owners.
Fail safe: any unexpected state -> escalate to a human, never retry blindly.
Every step must emit an event via store.emit(kind, **payload) so the dashboard shows it.
