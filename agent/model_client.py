"""LLM tool-call translation for the inference runner."""

import json
import re
from typing import Any, Dict, Optional, Tuple

from .config import MAX_TOKENS, MODEL_NAME, TEMPERATURE, HTTP_TIMEOUT_SECONDS, USE_WS_API
from .tool_schemas import TOOL_SCHEMAS
from .api_tool_schemas import API_TOOL_SCHEMAS

# Select tool schema based on mode
_ACTIVE_TOOL_SCHEMAS = API_TOOL_SCHEMAS if USE_WS_API else TOOL_SCHEMAS


def _parse_tool_arguments(arguments: Any) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _parse_xml_tool_call(text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Parse the XML-style tool call format some models emit instead of native tool calls.
    Handles: <function=tool_name {"arg": "val"}</function>
             <function=tool_name>{"arg": "val"}</function>
    Returns (tool_name, args_dict) or None if not matched.
    """
    pattern = r"<function=(\w+)\s*>?\s*(\{.*?\})\s*(?:</function>|$)"
    m = re.search(pattern, text, re.DOTALL)
    if not m:
        return None
    tool_name = m.group(1).strip()
    args = _parse_tool_arguments(m.group(2).strip())
    return tool_name, args


def _tool_call_to_action_parts(tool_name: str, tool_args: Dict[str, Any]) -> Tuple[str, str, str]:
    name = (tool_name or "").strip()

    # ── Legacy 10-tool schema ──────────────────────────────────────────────
    if name == "view_logs":
        stage = str(tool_args.get("stage", "") or "")
        detail = str(tool_args.get("detail", "") or "")
        return "view_logs", stage, detail
    if name == "tail_logs":
        stage = str(tool_args.get("stage", "") or "")
        detail = str(tool_args.get("detail", "") or "")
        return "tail_logs", stage, detail
    if name == "inspect_config":
        return "inspect_config", str(tool_args.get("component", "") or ""), ""
    if name == "inspect_dockerfile":
        return "inspect_dockerfile", str(tool_args.get("component", "") or ""), ""
    if name == "inspect_permissions":
        return "inspect_permissions", str(tool_args.get("component", "") or ""), ""
    if name == "modify_config":
        return (
            "modify_config",
            str(tool_args.get("component", "") or ""),
            str(tool_args.get("value", tool_args.get("fix", "")) or ""),
        )
    if name == "add_dependency":
        return (
            "add_dependency",
            str(tool_args.get("component", "") or ""),
            str(tool_args.get("dependency_fix", "") or ""),
        )
    if name == "rerun_pipeline":
        return "rerun_pipeline", "", ""
    if name == "verify_fix":
        return "verify_fix", "", ""

    # ── API-native 6-tool schema ───────────────────────────────────────────
    if name == "read_file":
        return "read_file", str(tool_args.get("path", "") or ""), ""
    if name == "write_file":
        path = str(tool_args.get("path", "") or "")
        content = str(tool_args.get("content", "") or "")
        return "write_file", path, content
    if name == "list_files":
        return "list_files", str(tool_args.get("directory", "") or ""), ""
    if name == "trigger_pipeline":
        return "trigger_pipeline", "", ""

    # ── Shared tools ───────────────────────────────────────────────────────
    if name == "set_hypothesis":
        return "set_hypothesis", "", str(tool_args.get("hypothesis", "") or "")
    if name == "finalize":
        return "finalize", "", ""

    return name.lower(), "", ""


def get_model_action(
    client: Any,
    step: int,
    messages: list[Dict[str, Any]],
) -> Tuple[str, str, str, Dict[str, Any], Optional[str]]:
    try:
        completion = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            tools=_ACTIVE_TOOL_SCHEMAS,
            tool_choice="required",
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            stream=False,
            timeout=HTTP_TIMEOUT_SECONDS,  # Add explicit timeout
        )
    except Exception as exc:
        # Groq (and some other providers) return a 400 BadRequestError when the
        # model emits a malformed tool call (e.g. XML-style <function=...> syntax).
        # The raw generation is attached to the error body under 'failed_generation'.
        # Try to salvage a valid action from it before giving up.
        raw_generation = ""
        try:
            body = exc.response.json() if hasattr(exc, "response") else {}
            err = body.get("error", {})
            raw_generation = err.get("failed_generation", "")
        except Exception:
            pass

        if raw_generation:
            parsed = _parse_xml_tool_call(raw_generation)
            if parsed:
                tool_name, tool_args = parsed
                operation, target, value = _tool_call_to_action_parts(tool_name, tool_args)
                tool_call_id = f"call_recovered_{step}"
                assistant_message: Dict[str, Any] = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_args, ensure_ascii=True, separators=(",", ":")),
                            },
                        }
                    ],
                }
                return operation, target, value, assistant_message, tool_call_id

        # Could not recover — return empty so the retry loop can handle it
        assistant_message = {"role": "assistant", "content": ""}
        return "", "", "", assistant_message, None

    message = completion.choices[0].message
    if message.tool_calls:
        tool_call = message.tool_calls[0]
        tool_name = (tool_call.function.name or "").strip()
        tool_args = _parse_tool_arguments(tool_call.function.arguments)
        operation, target, value = _tool_call_to_action_parts(tool_name, tool_args)
        tool_call_id = tool_call.id or f"call_{step}"
        assistant_message: Dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args, ensure_ascii=True, separators=(",", ":")),
                    },
                }
            ],
        }
        return operation, target, value, assistant_message, tool_call_id

    text = (message.content or "").strip()
    assistant_message = {
        "role": "assistant",
        "content": text,
    }
    # Strict mode: require native tool call structure.
    return "", "", "", assistant_message, None
