"""Rubric-based delayed reward helpers with optional OpenEnv LLMJudge integration."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from openai import OpenAI

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional import safety
    load_dotenv = None


LOGGER = logging.getLogger(__name__)


DEFAULT_HF_BASE_URL = "https://router.huggingface.co/v1"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _normalize(text: str) -> str:
    cleaned = (text or "").strip().lower()
    cleaned = re.sub(r"[_\-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


@dataclass
class RubricJudgeResult:
    """Structured rubric result for delayed reward blending."""

    score: float
    rationale: str
    source: str
    used_fallback: bool
    error: str = ""


class RubricJudge(Protocol):
    """Protocol for hypothesis-quality judges."""

    def evaluate_hypothesis_quality(self, payload: dict[str, Any]) -> RubricJudgeResult:
        ...

    def evaluate_action_quality(self, payload: dict[str, Any]) -> RubricJudgeResult:
        ...


DEFAULT_OPENROUTER_MODEL = "qwen/qwen-2.5-72b-instruct"


class OpenEnvLLMJudgeAdapter:
    """OpenEnv LLMJudge first, API LLM fallback second, heuristic last."""

    _IMPORT_CANDIDATES = [
        "openenv.core.rubrics.llm_judge",
        "openenv.core.rubrics",
        "openenv.evaluation.llm_judge",
        "openenv.evaluation.llmjudge",
    ]

    def __init__(
        self,
        *,
        enabled: bool,
        model_name: str,
        timeout_seconds: int,
    ) -> None:
        if load_dotenv is not None:
            load_dotenv()

        self._enabled = enabled
        self._model_name = model_name
        self._timeout_seconds = max(1, int(timeout_seconds))
        self._provider = os.getenv("RUBRIC_LLM_PROVIDER", "openrouter").strip().lower()
        self._api_base_url = self._resolve_api_base_url()
        self._api_key = self._resolve_api_key()
        self._openrouter_referer = (os.getenv("OPENROUTER_REFERER") or "").strip()
        self._openrouter_title = (os.getenv("OPENROUTER_TITLE") or "meta_hackathon").strip()
        self._debug_enabled = os.getenv("META_HACKATHON_RUBRIC_DEBUG", "false").strip().lower() == "true"
        self._openenv_judge: Any = None
        self._openenv_client: Any = None
        self._openenv_init_error: str = ""
        if enabled:
            self._openenv_judge, self._openenv_client, self._openenv_init_error = self._load_openenv_llmjudge()
            if self._openenv_judge is not None:
                self._debug_log("judge_init path=openenv_llmjudge status=ready")
            else:
                self._debug_log(
                    "judge_init path=openenv_llmjudge status=unavailable error=%s",
                    self._openenv_init_error or "unknown",
                )

    class _DebugClientProxy:
        """Capture raw responses from OpenEnv client.complete()."""

        def __init__(self, inner_client: Any):
            self._inner = inner_client
            self.last_raw_response = ""

        async def complete(self, prompt: str, **kwargs: Any) -> str:
            response = await self._inner.complete(prompt, **kwargs)
            self.last_raw_response = str(response or "")
            return response

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    def is_active(self) -> bool:
        return self._enabled

    def evaluate_hypothesis_quality(self, payload: dict[str, Any]) -> RubricJudgeResult:
        if not self._enabled:
            fallback = self._heuristic_score(payload)
            fallback.error = "rubric judging disabled"
            return fallback

        prompt = self._build_prompt(payload)

        openenv_error = self._openenv_init_error.strip()
        if self._openenv_judge is not None:
            try:
                raw, score = self._call_openenv_llmjudge(prompt=prompt, payload=payload)
                score = max(0.0, min(1.0, round(float(score), 3)))
                self._debug_log(
                    "judge_path=openenv_llmjudge raw_response=%s",
                    self._truncate(raw),
                )
                return RubricJudgeResult(
                    score=score,
                    rationale="OpenEnv LLMJudge semantic score",
                    source="openenv_llmjudge",
                    used_fallback=False,
                )
            except Exception as exc:  # pragma: no cover - defensive runtime path
                openenv_error = f"OpenEnv LLMJudge call failed: {exc}"
                self._debug_log("judge_path=openenv_llmjudge failed error=%s", openenv_error)

        try:
            raw = self._call_api_llm(prompt)
            score, rationale = self._extract_score(raw)
            score = max(0.0, min(1.0, round(float(score), 3)))
            self._debug_log(
                "judge_path=api_fallback raw_response=%s",
                self._truncate(raw),
            )
            return RubricJudgeResult(
                score=score,
                rationale=rationale,
                source="api_fallback",
                used_fallback=False,
            )
        except Exception as api_exc:  # pragma: no cover - defensive runtime path
            fallback = self._heuristic_score(payload)
            parts = [openenv_error, f"API LLM fallback failed: {api_exc}"]
            fallback.error = " | ".join(part for part in parts if part)
            self._debug_log("judge_path=heuristic_fallback error=%s", fallback.error)
            return fallback

    def evaluate_action_quality(self, payload: dict[str, Any]) -> RubricJudgeResult:
        if not self._enabled:
            fallback = self._heuristic_action_score(payload)
            fallback.error = "rubric judging disabled"
            return fallback

        prompt = self._build_action_prompt(payload)

        openenv_error = self._openenv_init_error.strip()
        if self._openenv_judge is not None:
            try:
                raw, score = self._call_openenv_llmjudge(
                    prompt=prompt,
                    payload={
                        **payload,
                        "judge_operation": payload.get("current_action", {}).get("operation", "unknown"),
                        "judge_value": payload.get("current_action", {}),
                    },
                )
                score = max(0.0, min(1.0, round(float(score), 3)))
                self._debug_log(
                    "judge_path=openenv_llmjudge raw_response=%s",
                    self._truncate(raw),
                )
                return RubricJudgeResult(
                    score=score,
                    rationale="OpenEnv LLMJudge semantic step score",
                    source="openenv_llmjudge",
                    used_fallback=False,
                )
            except Exception as exc:  # pragma: no cover - defensive runtime path
                openenv_error = f"OpenEnv LLMJudge call failed: {exc}"
                self._debug_log("judge_path=openenv_llmjudge failed error=%s", openenv_error)

        try:
            raw = self._call_api_llm(prompt)
            score, rationale = self._extract_score(raw)
            score = max(0.0, min(1.0, round(float(score), 3)))
            self._debug_log(
                "judge_path=api_fallback raw_response=%s",
                self._truncate(raw),
            )
            return RubricJudgeResult(
                score=score,
                rationale=rationale,
                source="api_fallback",
                used_fallback=False,
            )
        except Exception as api_exc:  # pragma: no cover - defensive runtime path
            fallback = self._heuristic_action_score(payload)
            parts = [openenv_error, f"API LLM fallback failed: {api_exc}"]
            fallback.error = " | ".join(part for part in parts if part)
            self._debug_log("judge_path=heuristic_fallback error=%s", fallback.error)
            return fallback

    def _load_openenv_llmjudge(self) -> tuple[Any | None, Any | None, str]:
        judge_class = None
        for module_path in self._IMPORT_CANDIDATES:
            try:
                module = importlib.import_module(module_path)
                judge_class = getattr(module, "LLMJudge", None)
                if judge_class is not None:
                    break
            except Exception as exc:
                self._debug_log("import_failed module=%s error=%s", module_path, exc)
                continue

        if judge_class is None:
            return None, None, "OpenEnv LLMJudge class not found"

        try:
            llm_client_module = importlib.import_module("openenv.core.llm_client")
            openai_client_class = getattr(llm_client_module, "OpenAIClient")

            endpoint, port = self._endpoint_and_port_from_base_url(self._api_base_url)
            inner_client = openai_client_class(
                endpoint,
                port,
                self._model_name,
                api_key=self._api_key or "not-needed",
                temperature=0.0,
                max_tokens=220,
            )
            debug_client = self._DebugClientProxy(inner_client)

            prompt_template = (
                "You are an evaluator for CI/CD debugging hypotheses. "
                "Return only JSON: {{\"score\": number in [0,1], \"rationale\": string}}.\\n"
                "Action:\\n{action}\\n\\nObservation:\\n{observation}"
            )

            constructor = inspect.signature(judge_class)
            kwargs = {
                "prompt_template": prompt_template,
                "client": debug_client,
                "score_pattern": r'"score"\s*:\s*(0(?:\.\d+)?|1(?:\.0+)?)',
                "default_score": 0.0,
                "normalize": True,
            }
            valid_kwargs = {k: v for k, v in kwargs.items() if k in constructor.parameters}
            judge = judge_class(**valid_kwargs)
            return judge, debug_client, ""
        except Exception as exc:
            return None, None, str(exc)

    def _call_openenv_llmjudge(self, *, prompt: str, payload: dict[str, Any]) -> tuple[str, float]:
        if self._openenv_judge is None:
            raise RuntimeError("OpenEnv LLMJudge not initialized")

        action_payload = {
            "operation": payload.get("judge_operation", "set_hypothesis"),
            "value": payload.get("judge_value", payload.get("evidence", {}).get("hypothesis_history", [])),
        }
        observation_payload = {
            "prompt": prompt,
            "task_id": payload.get("task_id", ""),
            "difficulty": payload.get("difficulty", ""),
            "evidence": payload.get("evidence", {}),
            "incident_chain": payload.get("incident_chain", []),
            "rubric": payload.get("rubric", {}),
        }

        async def _invoke() -> float:
            return await self._openenv_judge(action_payload, observation_payload)

        score = self._run_async_with_timeout(_invoke(), timeout_seconds=self._timeout_seconds)
        raw_response = ""
        if self._openenv_client is not None:
            raw_response = str(getattr(self._openenv_client, "last_raw_response", "") or "")
        return raw_response, float(score)

    def _call_api_llm(self, prompt: str) -> str:
        if not self._api_base_url:
            raise RuntimeError("API_BASE_URL is empty")

        client_kwargs = {
            "base_url": self._api_base_url,
            "api_key": self._api_key or "not-needed",
            "timeout": float(self._timeout_seconds),
        }
        if self._is_openrouter():
            headers = {}
            if self._openrouter_referer:
                headers["HTTP-Referer"] = self._openrouter_referer
            if self._openrouter_title:
                headers["X-Title"] = self._openrouter_title
            if headers:
                client_kwargs["default_headers"] = headers

        client = OpenAI(
            **client_kwargs,
        )
        response = client.chat.completions.create(
            model=self._model_name,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict JSON scorer. Return only JSON with keys score and rationale. "
                        "Score must be in [0,1]."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=220,
        )
        return str((response.choices[0].message.content or "").strip())

    def _resolve_api_base_url(self) -> str:
        explicit = (os.getenv("API_BASE_URL") or "").strip()
        if explicit:
            return explicit
        if self._provider == "hf":
            return DEFAULT_HF_BASE_URL
        return DEFAULT_OPENROUTER_BASE_URL

    def _resolve_api_key(self) -> str:
        explicit = (os.getenv("API_KEY") or "").strip()
        if explicit:
            return explicit
        if self._provider == "hf":
            return (
                os.getenv("HF_TOKEN")
                or os.getenv("OPENAI_API_KEY")
                or os.getenv("OPENROUTER_API_KEY")
                or ""
            ).strip()
        return (
            os.getenv("OPENROUTER_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("HF_TOKEN")
            or ""
        ).strip()

    def _is_openrouter(self) -> bool:
        return self._provider == "openrouter" or "openrouter.ai" in self._api_base_url

    def _run_async_with_timeout(self, coroutine_obj: Any, *, timeout_seconds: int) -> Any:
        async def _runner() -> Any:
            return await asyncio.wait_for(coroutine_obj, timeout=timeout_seconds)

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop and running_loop.is_running():
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda: asyncio.run(_runner()))
                return future.result(timeout=timeout_seconds + 1)
        return asyncio.run(_runner())

    def _endpoint_and_port_from_base_url(self, base_url: str) -> tuple[str, int]:
        parsed = urlparse(base_url)
        scheme = parsed.scheme or "https"
        host = parsed.hostname or "openrouter.ai"
        port = parsed.port or (443 if scheme == "https" else 80)
        endpoint = f"{scheme}://{host}"
        return endpoint, port

    def _extract_score(self, raw: Any) -> tuple[float, str]:
        if isinstance(raw, (int, float)):
            return float(raw), "numeric score returned by judge"

        if isinstance(raw, dict):
            for key in ("score", "rubric_score", "value"):
                if key in raw:
                    try:
                        score = float(raw[key])
                        rationale = str(raw.get("rationale") or raw.get("reason") or "")
                        return score, rationale or "dict score returned by judge"
                    except Exception:
                        continue

            serialized = json.dumps(raw)
            return self._extract_score(serialized)

        text = str(raw or "").strip()
        if not text:
            raise ValueError("empty judge response")

        try:
            parsed = json.loads(text)
            return self._extract_score(parsed)
        except Exception:
            pass

        score_match = re.search(r"(0(?:\.\d+)?|1(?:\.0+)?)", text)
        if score_match:
            return float(score_match.group(1)), text

        raise ValueError(f"unable to parse judge score from response: {text[:120]}")

    def _build_prompt(self, payload: dict[str, Any]) -> str:
        rubric = payload.get("rubric", {})
        evidence = payload.get("evidence", {})
        return (
            "Score the agent hypothesis quality from 0.0 to 1.0. "
            "Use semantic correctness, evidence alignment, and completeness. "
            "Return only JSON: {\"score\": float in [0,1], \"rationale\": string}. "
            f"Rubric: {json.dumps(rubric, ensure_ascii=True)} "
            f"Evidence: {json.dumps(evidence, ensure_ascii=True)}"
        )

    def _debug_log(self, message: str, *args: Any) -> None:
        if not self._debug_enabled:
            return
        LOGGER.warning("[RubricJudge] " + message, *args)

    def _truncate(self, text: str, limit: int = 400) -> str:
        value = (text or "").replace("\n", " ").strip()
        if len(value) <= limit:
            return value
        return value[:limit] + "..."

    def _heuristic_score(self, payload: dict[str, Any]) -> RubricJudgeResult:
        evidence = payload.get("evidence", {})
        hypotheses = [
            _normalize(item)
            for item in evidence.get("hypothesis_history", [])
            if isinstance(item, str) and item.strip()
        ]
        chain = payload.get("incident_chain", [])
        if not hypotheses or not chain:
            return RubricJudgeResult(
                score=0.0,
                rationale="no hypotheses or incident chain available",
                source="heuristic_fallback",
                used_fallback=True,
            )

        per_issue_scores: list[float] = []
        for issue in chain:
            hypothesis_terms = [_normalize(term) for term in issue.get("hypothesis_terms", [])]
            family_sets = [
                [_normalize(term) for term in family_set]
                for family_set in issue.get("family_term_sets", [])
                if isinstance(family_set, list)
            ]
            true_cause = _normalize(issue.get("true_cause", ""))

            best = 0.0
            for hypothesis in hypotheses:
                if hypothesis_terms and all(term in hypothesis for term in hypothesis_terms):
                    best = max(best, 1.0)
                    continue

                family_match = any(term_set and all(term in hypothesis for term in term_set) for term_set in family_sets)
                if family_match:
                    best = max(best, 0.75)

                overlap_terms = [term for term in hypothesis_terms if term in hypothesis]
                if overlap_terms:
                    best = max(best, 0.45 + (0.35 * (len(overlap_terms) / max(1, len(hypothesis_terms)))))

                if true_cause and any(token in hypothesis for token in true_cause.split()[:3]):
                    best = max(best, 0.5)

            per_issue_scores.append(min(1.0, best))

        score = sum(per_issue_scores) / max(1, len(per_issue_scores))
        if bool(evidence.get("incident_resolved")):
            score = min(1.0, score + 0.05)

        return RubricJudgeResult(
            score=round(score, 3),
            rationale="heuristic semantic rubric fallback",
            source="heuristic_fallback",
            used_fallback=True,
        )

    def _build_action_prompt(self, payload: dict[str, Any]) -> str:
        current_action = payload.get("current_action", {})
        return (
            "You are a strict process reward model for CI/CD debugging. "
            "Score the current action from 0.0 to 1.0 based on reasoning quality, "
            "coherence with prior steps, evidence usage, and whether the action logically "
            "follows from the current state. Penalize exploitative or premature actions, "
            "including reruns, verify, or finalize steps that are not justified yet. "
            "Positive scores should require genuine justification from the evidence and trajectory. "
            "Return only JSON: {\"score\": float in [0,1], \"rationale\": string}. "
            f"Current action: {json.dumps(current_action, ensure_ascii=True)} "
            f"Prior context: {json.dumps(payload.get('prior_context', {}), ensure_ascii=True)} "
            f"Evidence: {json.dumps(payload.get('evidence', {}), ensure_ascii=True)} "
            f"Rubric: {json.dumps(payload.get('rubric', {}), ensure_ascii=True)}"
        )

    def _heuristic_action_score(self, payload: dict[str, Any]) -> RubricJudgeResult:
        current_action = payload.get("current_action", {})
        prior_context = payload.get("prior_context", {})
        evidence = payload.get("evidence", {})
        op = _normalize(str(current_action.get("operation", "")))
        target = _normalize(str(current_action.get("target", "")))
        value = _normalize(str(current_action.get("value", "")))

        repeated = bool(prior_context.get("was_redundant"))
        finalize_blocked = bool(prior_context.get("finalize_blocked"))
        hypothesis_correct = bool(prior_context.get("hypothesis_correct"))
        pending_fix_outcome = _normalize(str(prior_context.get("pending_fix_outcome", "")))
        errors_stale_after_fix = bool(prior_context.get("errors_stale_after_fix"))
        last_rerun_progressed = bool(prior_context.get("last_rerun_progressed"))
        verified_for_latest_rerun = bool(prior_context.get("verified_for_latest_rerun"))
        rerun_attempts = int(prior_context.get("rerun_attempts", 0) or 0)
        destructive_action = bool(prior_context.get("destructive_action"))
        relevant_targets = {
            _normalize(item)
            for item in evidence.get("relevant_targets", [])
            if isinstance(item, str) and item.strip()
        }
        surfaced_errors = _normalize(" ".join(str(item) for item in evidence.get("surfaced_errors", [])))
        findings = _normalize(" ".join(str(item) for item in evidence.get("findings", [])))
        keywords = [
            _normalize(item)
            for item in evidence.get("fault_keywords", [])
            if isinstance(item, str) and item.strip()
        ]

        score = 0.45
        reasons: list[str] = []

        if repeated:
            score -= 0.30
            reasons.append("repeats an identical earlier action")

        if destructive_action:
            score = min(score, 0.02)
            reasons.append("attempts a destructive fix")

        if op == "view logs":
            if target and target in relevant_targets:
                score += 0.28
                reasons.append("inspects the failing stage directly")
            elif not prior_context.get("has_recent_evidence"):
                score += 0.18
                reasons.append("collects missing evidence before acting")
            else:
                score -= 0.08
                reasons.append("adds little beyond existing evidence")
        elif op in {"inspect config", "inspect dockerfile", "inspect permissions"}:
            if target and target in relevant_targets:
                score += 0.26
                reasons.append("inspects a file implicated by the evidence")
            elif op == "inspect dockerfile" and "dockerfile" in surfaced_errors:
                score += 0.22
                reasons.append("matches the surfaced Docker-related failure")
            elif not prior_context.get("has_recent_evidence"):
                score += 0.12
                reasons.append("reasonable evidence gathering step")
            else:
                score -= 0.08
                reasons.append("inspection is weakly supported by current evidence")
        elif op == "set hypothesis":
            overlap = sum(1 for kw in keywords if kw and kw in value)
            if overlap > 0 or hypothesis_correct:
                score += 0.30 if overlap >= 2 else 0.18
                reasons.append("hypothesis aligns with observed fault evidence")
            else:
                score -= 0.22
                reasons.append("hypothesis is not grounded in the available evidence")
        elif op in {"modify config", "add dependency"}:
            if pending_fix_outcome == "applied" and hypothesis_correct:
                score += 0.34
                reasons.append("applies a fix that follows a supported hypothesis")
            elif pending_fix_outcome == "applied":
                score += 0.14
                reasons.append("applies a fix, but justification is only partial")
            elif pending_fix_outcome == "failed":
                score -= 0.24
                reasons.append("attempted fix could not be applied")
            else:
                score -= 0.10
                reasons.append("fix attempt lacks clear support")
        elif op == "rerun pipeline":
            if pending_fix_outcome == "applied" or errors_stale_after_fix:
                score += 0.36
                reasons.append("rerun is justified to validate the latest fix")
            elif last_rerun_progressed and not verified_for_latest_rerun:
                score += 0.12
                reasons.append("rerun can probe next exposed failure after progress")
            else:
                score -= 0.28
                reasons.append("rerun appears premature or exploitative")
        elif op == "verify fix":
            if rerun_attempts == 0:
                score -= 0.28
                reasons.append("verification has no preceding rerun to validate")
            elif verified_for_latest_rerun:
                score -= 0.18
                reasons.append("verification is redundant")
            elif last_rerun_progressed or prior_context.get("pipeline_passed"):
                score += 0.30
                reasons.append("verification logically follows the rerun result")
            else:
                score -= 0.18
                reasons.append("verification is not supported by the latest rerun outcome")
        elif op == "finalize":
            if finalize_blocked:
                score = min(score, 0.02)
                reasons.append("finalize is premature because verification is incomplete")
            elif verified_for_latest_rerun:
                score += 0.28
                reasons.append("finalize follows successful verification")
            else:
                score -= 0.16
                reasons.append("finalize is weakly justified")

        if prior_context.get("incident_resolved") and op not in {"verify fix", "finalize"}:
            score -= 0.12
            reasons.append("keeps acting after resolution instead of closing out")

        score = max(0.0, min(1.0, round(score, 3)))
        rationale = "; ".join(reasons) if reasons else "action is weakly justified by the trajectory"
        return RubricJudgeResult(
            score=score,
            rationale=rationale,
            source="heuristic_fallback",
            used_fallback=True,
        )
