"""Shared fault type definitions used by both injector and runner."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class FaultMetadata:
    fault_type: str
    affected_files: List[str]
    injected_at_commit_sha: str = ""
    expected_fail_stage: str = ""
    description: str = ""
    keywords: List[str] = field(default_factory=list)
    affected_apps: List[str] = field(default_factory=list)
    cascade_faults: List[str] = field(default_factory=list)
    red_herring: str = ""


FAULT_TYPES: List[str] = [
    "merge_conflict",
    "dependency_conflict",
    "docker_order",
    "flaky_test",
    "missing_permission",
    "secret_exposure",
    "env_drift",
    "invalid_database_url",
    "empty_secret_key",
    "missing_pythonpath",
    "circular_import_runtime",
    "missing_package_init",
    "none_config_runtime",
    "log_pii_leak",
    "log_disabled",
    "bad_migration_sql",
    "schema_drift",
    "terraform_invalid_provider",
    "terraform_missing_variable",
    "terraform_permission_denied",
]

FAULT_STAGE_MAP: Dict[str, str] = {
    "merge_conflict":      "build",
    "dependency_conflict": "build",
    "docker_order":        "build",
    "flaky_test":          "test",
    "missing_permission":  "deploy",
    "secret_exposure":     "build",
    "env_drift":           "deploy",
    "invalid_database_url": "deploy",
    "empty_secret_key":     "deploy",
    "missing_pythonpath":   "deploy",
    "circular_import_runtime": "deploy",
    "missing_package_init": "deploy",
    "none_config_runtime":  "deploy",
    "log_pii_leak":        "build",
    "log_disabled":        "build",
    "bad_migration_sql":   "build",
    "schema_drift":        "deploy",
    "terraform_invalid_provider": "deploy",
    "terraform_missing_variable": "deploy",
    "terraform_permission_denied": "deploy",
}

FAULT_KEYWORDS: Dict[str, List[str]] = {
    "bad_migration_sql":   ["sql", "syntax", "migration"],
    "schema_drift":        ["schema", "mismatch", "column"],
    "merge_conflict":      ["merge", "conflict", "markers", "routes"],
    "dependency_conflict": ["dependency", "incompatible", "requests", "urllib3", "pip", "version"],
    "docker_order":        ["docker", "order", "copy", "install", "layer", "dockerfile", "before", "missing", "file", "not found"],
    "flaky_test":          ["flaky", "test", "intermittent", "timing", "random", "fail"],
    "missing_permission":  ["permission", "network", "deploy", "compose", "missing"],
    "secret_exposure":     ["secret", "credential", "api_key", "hardcoded", "exposed", "scan"],
    "env_drift":           ["environment", "variable", "compose", "port", "invalid", "deploy"],
    "invalid_database_url": ["database_url", "env", "port", "connection", "runtime", "db"],
    "empty_secret_key":     ["secret_key", "empty", "session", "runtime", "config"],
    "missing_pythonpath":   ["pythonpath", "venv", "module", "import", "runtime", "path"],
    "circular_import_runtime": ["circular", "import", "lazy", "request", "runtime"],
    "missing_package_init": ["__init__", "package", "module", "runtime", "import"],
    "none_config_runtime":  ["none", "config", "runtime", "attribute", "request"],
    "log_pii_leak":        ["logging", "pii", "credential", "token", "secret", "leak", "routes"],
    "log_disabled":        ["logging", "level", "critical", "disabled", "silent", "log_level"],
    "terraform_invalid_provider": ["terraform", "provider", "registry", "invalid"],
    "terraform_missing_variable": ["terraform", "variable", "tfvars", "plan"],
    "terraform_permission_denied": ["terraform", "permission", "iam", "accessdenied", "apply"],
}

FAULT_AFFECTED_APPS: Dict[str, List[str]] = {}
