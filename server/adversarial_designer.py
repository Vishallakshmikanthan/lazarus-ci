# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

"""Adversarial scenario designer: uses Groq to compose multi-fault CI/CD incidents."""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

from openai import OpenAI

try:
    from cicd.fault_types import FAULT_TYPES, FAULT_STAGE_MAP, FAULT_KEYWORDS, FaultMetadata
    from cicd.simulated_fault_injector import inject_fault_simulated as inject_fault
    from models import AdversarialCICDScenario, IncidentStep
except ImportError:
    from ..cicd.fault_types import FAULT_TYPES, FAULT_STAGE_MAP, FAULT_KEYWORDS, FaultMetadata
    from ..cicd.simulated_fault_injector import inject_fault_simulated as inject_fault
    from ..models import AdversarialCICDScenario, IncidentStep

LOGGER = logging.getLogger(__name__)

_DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct"

# Provider-aware model defaults
_PROVIDER_DEFAULT_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "meta-llama/llama-3.3-70b-instruct",
    "hf": "meta-llama/Llama-3.3-70B-Instruct",
}


def _default_model_for_provider() -> str:
    provider = os.getenv("LLM_PROVIDER", "hf").strip().lower()
    explicit = os.getenv("CICD_ADV_MODEL", "").strip()
    if explicit:
        return explicit
    return _PROVIDER_DEFAULT_MODELS.get(provider, _DEFAULT_OPENROUTER_MODEL)

ADVERSARIAL_DESIGNER_PROMPT = """You are a CI/CD chaos engineer designing realistic
production incidents for SRE training on a Flask+Docker pipeline (stages: build → test → deploy).

CRITICAL: The payload contains a "root_cause_fault" field. You MUST use that exact fault_type
as the step with is_root_cause=true and order=1. Do not change or ignore it.

Given available fault primitives, compose 2-3 faults that create:
1. ROOT CAUSE = the fault_type specified in root_cause_fault (is_root_cause=true, order=1)
2. CASCADING EFFECTS (1-2 secondary faults that depend_on the root cause)
3. A RED HERRING at difficulty >= 0.6: a symptom that looks related but isn't the root cause

Difficulty guide:
  0.2-0.4 → root cause only (1 fault), no red herring
  0.4-0.6 → 2 faults, mild cascade
  0.6-0.95 → 3 faults, explicit red herring

The agent must follow this resolution workflow:
  triage (view_logs per failing stage)
  → investigation (inspect_config / inspect_dockerfile / inspect_permissions)
  → set_hypothesis naming root-cause keywords
  → modify_config or add_dependency with the correct fix
  → rerun_pipeline → verify_fix → finalize

Rules:
- FIRST step MUST use root_cause_fault as fault_type with is_root_cause=true
- Only use fault_types from the provided available_faults list
- expected_hypothesis_terms must come ONLY from the ROOT CAUSE fault's keywords
- red_herrings lists symptoms (not fault names) that mislead

Return ONLY a JSON object matching this exact schema (no markdown, no explanation):
{
  "title": string,
  "narrative": string,
  "alert_message": string,
  "steps": [
    {
      "fault_type": string,
      "effect": string,
      "order": int,
      "is_root_cause": bool,
      "depends_on": [int]
    }
  ],
  "expected_triage": [string],
  "expected_investigation": [string],
  "expected_hypothesis_terms": [string],
  "expected_fix_sequence": [string],
  "expected_verification": ["rerun_pipeline", "verify_fix"],
    "red_herrings": [string],
    "root_cause_explanation": string,
    "difficulty": float
}"""


class AdversarialDesigner:
    """Uses Groq (via OpenAI-compatible API) to design multi-fault CI/CD incidents."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_seconds: int = 15,  # reduced from 30 to 15
    ) -> None:
        self._api_key = (
            api_key
            or os.getenv("OPENROUTER_API_KEY")
            or os.getenv("API_KEY")
            or ""
        )
        self._model = model or _default_model_for_provider()
        self._base_url = base_url or os.getenv("CICD_ADV_BASE_URL") or _DEFAULT_OPENROUTER_BASE_URL
        self._timeout = timeout_seconds
        self._client = OpenAI(
            base_url=self._base_url,
            api_key=self._api_key or "not-needed",
            timeout=float(self._timeout),
            default_headers={
                "HTTP-Referer": os.getenv("OPENROUTER_REFERER", "https://github.com/meta-hackathon"),
                "X-Title": os.getenv("OPENROUTER_TITLE", "meta-hackathon-cicd"),
            },
        )

    def design(
        self,
        root_cause_fault: str,
        difficulty: float = 0.5,
        skill_profile: Optional[Dict[str, dict]] = None,
    ) -> AdversarialCICDScenario:
        """
        Design a multi-fault incident where root_cause_fault is always the primary fault.
        The curriculum picks root_cause_fault; OpenRouter composes the full scenario around it.
        Falls back to a single-fault scenario if the API call fails.
        """
        primitives = [
            {
                "fault_type": ft,
                "fails_stage": FAULT_STAGE_MAP[ft],
                "keywords": FAULT_KEYWORDS[ft],
            }
            for ft in FAULT_TYPES
        ]
        payload = {
            "root_cause_fault": root_cause_fault,
            "available_faults": primitives,
            "pipeline_stages": ["build", "test", "deploy"],
            "difficulty": round(difficulty, 2),
            "skill_profile": skill_profile or {},
        }

        try:
            raw = self._client.chat.completions.create(
                model=self._model,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": ADVERSARIAL_DESIGNER_PROMPT},
                    {"role": "user", "content": json.dumps(payload)},
                ],
                temperature=0.7,
                max_tokens=1200,  # reduced from 1400 to 1200 (1000 was too aggressive)
            ).choices[0].message.content or "{}"

            raw = raw.strip()
            if raw.startswith("```json"):
                raw = raw[7:]
            elif raw.startswith("```"):
                raw = raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as json_err:
                # If JSON is truncated (unterminated string), retry with more tokens
                if "Unterminated string" in str(json_err) or "Expecting" in str(json_err):
                    LOGGER.warning("JSON truncated, retrying with max_tokens=1400")
                    raw = self._client.chat.completions.create(
                        model=self._model,
                        response_format={"type": "json_object"},
                        messages=[
                            {"role": "system", "content": ADVERSARIAL_DESIGNER_PROMPT},
                            {"role": "user", "content": json.dumps(payload)},
                        ],
                        temperature=0.7,
                        max_tokens=1400,  # retry with original limit
                    ).choices[0].message.content or "{}"
                    
                    raw = raw.strip()
                    if raw.startswith("```json"):
                        raw = raw[7:]
                    elif raw.startswith("```"):
                        raw = raw[3:]
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()
                    data = json.loads(raw)
                else:
                    raise
            # Strip unknown DB fields the LLM may still emit
            data.pop("db_backend", None)
            data.pop("db_faults", None)

            scenario = AdversarialCICDScenario(**data)
            # Validate: ensure root_cause_fault is actually marked is_root_cause
            if not any(s.is_root_cause and s.fault_type == root_cause_fault for s in scenario.steps):
                LOGGER.warning("LLM ignored root_cause_fault=%s; using fallback", root_cause_fault)
                return self._fallback_scenario(root_cause_fault, difficulty)
            return scenario

        except Exception as exc:
            LOGGER.warning("AdversarialDesigner.design failed (%s); using fallback scenario", exc)
            return self._fallback_scenario(root_cause_fault, difficulty)

    def inject(self, workspace: str, scenario: AdversarialCICDScenario) -> List[FaultMetadata]:
        """Inject all faults in the scenario into the workspace, ordered by step."""
        injected: List[FaultMetadata] = []
        for step in sorted(scenario.steps, key=lambda s: s.order):
            try:
                meta = inject_fault(workspace, step.fault_type)
                injected.append(meta)
            except Exception as exc:
                LOGGER.warning(
                    "Failed to inject fault '%s' (step %d): %s", step.fault_type, step.order, exc
                )
        return injected

    # ── fallback ───────────────────────────────────────────────────────────

    def _fallback_scenario(self, root_cause_fault: str, difficulty: float) -> AdversarialCICDScenario:
        """Single-fault fallback used when OpenRouter is unavailable."""
        stage = FAULT_STAGE_MAP.get(root_cause_fault, "build")
        keywords = FAULT_KEYWORDS.get(root_cause_fault, [root_cause_fault])
        return AdversarialCICDScenario(
            title=f"{root_cause_fault.replace('_', ' ').title()} incident",
            narrative=f"Pipeline failing at {stage} stage due to {root_cause_fault.replace('_', ' ')}.",
            alert_message=f"ALERT: {stage} stage failing — {root_cause_fault.replace('_', ' ')}",
            steps=[
                IncidentStep(
                    fault_type=root_cause_fault,
                    effect=f"{stage} stage fails due to {root_cause_fault.replace('_', ' ')}",
                    order=1,
                    is_root_cause=True,
                    depends_on=[],
                ),
            ],
            expected_triage=[f"view_logs:{stage}"],
            expected_investigation=["inspect_config", "inspect_dockerfile"],
            expected_hypothesis_terms=keywords,
            expected_fix_sequence=[root_cause_fault.replace("_", "-")],
            expected_verification=["rerun_pipeline", "verify_fix"],
            red_herrings=[],
            root_cause_explanation=f"{root_cause_fault.replace('_', ' ')} in the pipeline",
            difficulty=difficulty,
        )
