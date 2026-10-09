"""API-native tool schemas for the CI/CD WebSocket agent.

Six tools that map 1:1 to the WebSocket protocol commands exposed by cicd_api.py.
These replace the previous 10 abstract operations (view_logs, inspect_config,
inspect_dockerfile, inspect_permissions, modify_config, add_dependency,
rerun_pipeline, verify_fix, set_hypothesis, finalize) with direct API calls.

Tool → WebSocket command mapping:
  read_file          → {"type": "read_file",   "path": "..."}
  write_file         → {"type": "write_file",  "path": "...", "content": "..."}
  list_files         → {"type": "list_files",  "directory": ""}
  trigger_pipeline   → {"type": "trigger_pipeline"}  [push events returned async]
  set_hypothesis     → local (no network; instructs agent to declare root cause)
  finalize           → local (no network; ends episode and requests scoring)
"""

from typing import Any, Dict, List

API_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of any file in the workspace — source files, "
                "Dockerfile, docker-compose.yml, requirements.txt, migration SQL, "
                "logging config, etc. Use this to inspect both configuration and code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Relative file path within the workspace. Examples: "
                            "'Dockerfile', 'docker-compose.yml', "
                            "'services/api/requirements.txt', "
                            "'services/api/logging_config.py', "
                            "'db/migrations/001_init.sql'"
                        ),
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write or overwrite a file in the workspace to apply a fix. "
                "Changes persist and will be picked up by the next pipeline run. "
                "Provide the complete new content of the file — partial writes are not supported."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to the file to write.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete new content for the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files and sub-directories in the workspace. "
                "Use this to discover the project structure and locate relevant files "
                "before reading them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": (
                            "Sub-directory to list (empty string for workspace root). "
                            "Examples: '', 'services/api', 'db/migrations', 'tests'"
                        ),
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trigger_pipeline",
            "description": (
                "Trigger a CI/CD pipeline run. Stage events (stage_started, "
                "stage_completed, pipeline_done) are pushed back over the WebSocket "
                "connection in real time — you do not need to poll for status. "
                "Call this after applying fixes to verify they resolved the failure."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_hypothesis",
            "description": (
                "Declare your current theory about the root cause of the pipeline failure. "
                "A correct hypothesis scores positively and unlocks the fix workflow. "
                "An incorrect one scores negatively — do not repeat a hypothesis that "
                "already scored negatively. Call this only AFTER you have read the "
                "relevant files and stage logs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hypothesis": {
                        "type": "string",
                        "description": (
                            "One or two sentences describing the root cause and the "
                            "specific file / line that needs to be changed."
                        ),
                    }
                },
                "required": ["hypothesis"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize",
            "description": (
                "End the episode and request final scoring. Only call this AFTER "
                "the pipeline has passed (pipeline_done with status='passed'). "
                "Calling finalize before the pipeline passes will score 0."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]
