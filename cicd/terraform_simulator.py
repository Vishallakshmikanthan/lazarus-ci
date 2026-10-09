from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Tuple


def has_terraform_config(workspace_path: str) -> bool:
    return len(_tf_files(workspace_path)) > 0


def simulate_terraform_command(workspace_path: str, command_text: str) -> Tuple[int, str, str]:
    cmd = command_text.lower()
    if "terraform init" in cmd:
        return _terraform_init(workspace_path)
    if "terraform plan" in cmd:
        return _terraform_plan(workspace_path)
    if "terraform apply" in cmd:
        return _terraform_apply(workspace_path)
    return 0, "Terraform command skipped (not init/plan/apply).", ""


def simulate_terraform_pipeline(workspace_path: str) -> Tuple[int, str, str]:
    logs: List[str] = []
    errs: List[str] = []
    for phase, fn in (("init", _terraform_init), ("plan", _terraform_plan), ("apply", _terraform_apply)):
        code, out, err = fn(workspace_path)
        logs.append(f"## Terraform {phase}\n{out}")
        if err:
            errs.append(err)
        if code != 0:
            return code, "\n".join(logs), "\n".join(errs)
    return 0, "\n".join(logs), "\n".join(errs)


def _terraform_init(workspace_path: str) -> Tuple[int, str, str]:
    _ensure_tf_dirs(workspace_path)
    provider = _provider_name(workspace_path)
    if provider == "invalidcorp":
        return 1, (
            "Initializing the backend...\n"
            "Initializing provider plugins...\n"
        ), (
            "Error: Failed to query available provider packages\n"
            "Could not retrieve the list of available versions for provider invalidcorp/mock: "
            "provider registry registry.terraform.io does not have a provider named invalidcorp/mock"
        )
    lock_path = os.path.join(workspace_path, ".terraform", "providers.lock.json")
    with open(lock_path, "w", encoding="utf-8") as f:
        json.dump({"provider": provider, "initialized_at": time.time()}, f)
    return 0, (
        "Initializing the backend...\n"
        "Successfully configured the backend \"local\"!\n"
        "Initializing provider plugins...\n"
        f"- Finding hashicorp/{provider} versions matching \">= 1.0.0\"...\n"
        f"- Installing hashicorp/{provider} v5.0.0...\n"
        f"- Installed hashicorp/{provider} v5.0.0 (signed by HashiCorp)\n"
        "Terraform has been successfully initialized!"
    ), ""


def _terraform_plan(workspace_path: str) -> Tuple[int, str, str]:
    missing = _missing_required_variables(workspace_path)
    if missing:
        return 1, "", (
            "Error: No value for required variable\n"
            + "\n".join([f"  on infra/variables.tf line {idx + 1}: variable \"{name}\" is required" for idx, name in enumerate(missing)])
        )
    resources = _resource_descriptors(workspace_path)
    if not resources:
        return 0, "No changes. Infrastructure is up-to-date.", ""
    body = ["Terraform used the selected providers to generate the following execution plan:"]
    for resource in resources:
        body.append(f"  # {resource} will be created")
        body.append(f"  + resource \"{resource}\" {{ ... }}")
    body.append("")
    body.append(f"Plan: {len(resources)} to add, 0 to change, 0 to destroy.")
    return 0, "\n".join(body), ""


def _terraform_apply(workspace_path: str) -> Tuple[int, str, str]:
    if _permission_error_requested(workspace_path):
        return 1, "", (
            "Error: AccessDenied: User is not authorized to perform this action\n"
            "  with provider aws, on infra/main.tf line 1\n"
            "Apply failed due to insufficient IAM permissions."
        )
    resources = _resource_descriptors(workspace_path)
    logs: List[str] = []
    for resource in resources:
        logs.append(f"{resource}: Creating...")
        logs.append(f"{resource}: Creation complete after 1s [id=sim-{abs(hash(resource)) % 10000}]")
    state = {
        "version": 4,
        "terraform_version": "1.8.5",
        "resources": resources,
        "updated_at": time.time(),
    }
    state_path = os.path.join(workspace_path, ".terraform", "terraform.tfstate")
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    logs.append("")
    logs.append(f"Apply complete! Resources: {len(resources)} added, 0 changed, 0 destroyed.")
    return 0, "\n".join(logs), ""


def _tf_files(workspace_path: str) -> List[str]:
    files: List[str] = []
    for root, dirs, filenames in os.walk(workspace_path):
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "__pycache__", ".terraform"}]
        for name in filenames:
            if name.endswith(".tf"):
                files.append(os.path.join(root, name))
    return files


def _read_all_tf(workspace_path: str) -> str:
    parts: List[str] = []
    for path in _tf_files(workspace_path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                parts.append(f.read())
        except OSError:
            continue
    return "\n".join(parts)


def _provider_name(workspace_path: str) -> str:
    tf = _read_all_tf(workspace_path)
    match = re.search(r'provider\s+"([^"]+)"', tf)
    if match:
        return match.group(1)
    return "aws"


def _missing_required_variables(workspace_path: str) -> List[str]:
    tf = _read_all_tf(workspace_path)
    required = re.findall(r'variable\s+"([^"]+)"\s*\{[^}]*\}', tf, flags=re.DOTALL)
    tfvars = _read_tfvars(workspace_path)
    missing: List[str] = []
    for name in required:
        block_match = re.search(rf'variable\s+"{re.escape(name)}"\s*\{{([^}}]*)\}}', tf, flags=re.DOTALL)
        block = block_match.group(1) if block_match else ""
        has_default = "default" in block
        if not has_default and name not in tfvars:
            missing.append(name)
    return missing


def _read_tfvars(workspace_path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for root, dirs, files in os.walk(workspace_path):
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "__pycache__", ".terraform"}]
        for name in files:
            if not name.endswith(".tfvars"):
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        values[key.strip()] = value.strip().strip('"')
            except OSError:
                continue
    return values


def _resource_descriptors(workspace_path: str) -> List[str]:
    tf = _read_all_tf(workspace_path)
    matches = re.findall(r'resource\s+"([^"]+)"\s+"([^"]+)"', tf)
    return [f"{rtype}.{name}" for rtype, name in matches]


def _permission_error_requested(workspace_path: str) -> bool:
    tf = _read_all_tf(workspace_path).lower()
    return "simulate_permission_denied" in tf or "permission_denied = true" in tf


def _ensure_tf_dirs(workspace_path: str) -> None:
    os.makedirs(os.path.join(workspace_path, ".terraform"), exist_ok=True)
