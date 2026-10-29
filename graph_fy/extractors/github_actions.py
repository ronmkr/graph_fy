"""GitHub Actions workflow extractor.

Extracts GitHub Actions workflow components:
- Workflow definitions & triggers (on: push, pull_request, workflow_call, schedule, etc.)
- Jobs & matrix strategies
- Steps & action invocations (uses: actions/checkout@v4)
- Reusable workflow references
- Secret & environment variable references
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


def _make_id(*parts: str) -> str:
    clean = [re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(p)).strip("_") for p in parts if p]
    return "_".join(clean)


def is_github_workflow(path: Path) -> bool:
    """Return True if path is a GitHub Actions workflow YAML file."""
    posix_path = str(path).replace("\\", "/").lower()
    return (
        "/.github/workflows/" in posix_path or posix_path.startswith(".github/workflows/")
    ) and (posix_path.endswith(".yml") or posix_path.endswith(".yaml"))


def extract_github_workflow(path: Path) -> dict[str, Any]:
    """Extract GitHub Actions workflow into graph nodes and edges."""
    str_path = str(path.resolve())
    file_nid = _make_id(str_path)
    file_name = path.name

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            return {"nodes": nodes, "edges": edges}
    except Exception as exc:
        return {"nodes": nodes, "edges": edges, "error": str(exc)}

    workflow_name = data.get("name") or path.stem
    wf_node: dict[str, Any] = {
        "id": file_nid,
        "label": f"Workflow:{workflow_name}",
        "type": "workflow",
        "file_type": "code",
        "workflow_name": str(workflow_name),
        "source_file": str_path,
        "source_location": "L1",
        "text": f"GitHub Actions Workflow {workflow_name}",
    }
    nodes.append(wf_node)

    # 1. Triggers (`on:`)
    on_triggers = data.get("on") or data.get(True) or []  # PyYAML might parse `on:` as True
    trg_list: list[str] = []
    is_reusable = False

    if isinstance(on_triggers, str):
        trg_list = [on_triggers]
    elif isinstance(on_triggers, list):
        trg_list = [str(t) for t in on_triggers]
    elif isinstance(on_triggers, dict):
        trg_list = [str(t) for t in on_triggers.keys()]

    if "workflow_call" in trg_list:
        is_reusable = True
        wf_node["is_reusable"] = True

    for trg in trg_list:
        trg_nid = _make_id("trigger", str_path, trg)
        nodes.append({
            "id": trg_nid,
            "label": f"Trigger:{trg}",
            "type": "workflow_trigger",
            "file_type": "code",
            "trigger_event": trg,
            "source_file": str_path,
            "source_location": "L1",
            "text": f"Workflow trigger event {trg}",
        })
        edges.append({
            "source": file_nid,
            "target": trg_nid,
            "relation": "triggered_by",
            "confidence": "EXTRACTED",
            "weight": 1.0,
            "source_file": str_path,
            "source_location": "L1",
        })

    # 2. Global env
    global_env = data.get("env") or {}
    workflow_env_vars: list[str] = []
    if isinstance(global_env, dict):
        workflow_env_vars.extend(str(k) for k in global_env.keys())
        for k in global_env.keys():
            k_str = str(k)
            env_nid = _make_id("gha_env", str_path, k_str)
            nodes.append({
                "id": env_nid,
                "label": f"GHA:Env:{k_str}",
                "type": "config_decl",
                "file_type": "code",
                "config_type": "env",
                "config_name": k_str,
                "source_file": str_path,
                "source_location": "L1",
                "text": f"GitHub Actions workflow environment variable {k_str}",
            })
            edges.append({
                "source": file_nid,
                "target": env_nid,
                "relation": "declares_env",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "L1",
            })
    wf_node["env_vars"] = workflow_env_vars

    # 3. Jobs
    jobs = data.get("jobs") or {}
    if isinstance(jobs, dict):
        for job_id, job_data in jobs.items():
            if not isinstance(job_data, dict):
                continue
            job_nid = _make_id("job", str_path, str(job_id))
            job_name = job_data.get("name") or str(job_id)

            job_env = job_data.get("env") or {}
            if isinstance(job_env, dict):
                for k in job_env.keys():
                    k_str = str(k)
                    env_nid = _make_id("gha_env", str_path, str(job_id), k_str)
                    nodes.append({
                        "id": env_nid,
                        "label": f"GHA:Env:{k_str}",
                        "type": "config_decl",
                        "file_type": "code",
                        "config_type": "env",
                        "config_name": k_str,
                        "source_file": str_path,
                        "source_location": "L1",
                        "text": f"GitHub Actions job {job_id} environment variable {k_str}",
                    })
                    edges.append({
                        "source": job_nid,
                        "target": env_nid,
                        "relation": "declares_env",
                        "confidence": "EXTRACTED",
                        "weight": 1.0,
                        "source_file": str_path,
                        "source_location": "L1",
                    })

            # Reusable workflow call job
            uses_call = job_data.get("uses")
            job_node: dict[str, Any] = {
                "id": job_nid,
                "label": f"Job:{job_name}",
                "type": "job",
                "file_type": "code",
                "job_id": str(job_id),
                "source_file": str_path,
                "source_location": "L1",
                "text": f"Workflow job {job_name} ({job_id})",
            }

            # Matrix strategy
            strategy = job_data.get("strategy")
            if isinstance(strategy, dict) and isinstance(strategy.get("matrix"), dict):
                job_node["matrix"] = list(strategy["matrix"].keys())

            nodes.append(job_node)
            edges.append({
                "source": file_nid,
                "target": job_nid,
                "relation": "defines",
                "confidence": "EXTRACTED",
                "weight": 1.0,
                "source_file": str_path,
                "source_location": "L1",
            })

            # Reusable workflow call: e.g. `./.github/workflows/build.yml`
            if uses_call:
                uses_str = str(uses_call).strip()
                reusable_nid = _make_id("reusable_call", uses_str)
                nodes.append({
                    "id": reusable_nid,
                    "label": f"WorkflowCall:{uses_str}",
                    "type": "reusable_workflow_call",
                    "file_type": "code",
                    "target_workflow": uses_str,
                    "source_file": str_path,
                    "source_location": "L1",
                    "text": f"Reusable workflow call {uses_str}",
                })
                edges.append({
                    "source": job_nid,
                    "target": reusable_nid,
                    "relation": "calls_workflow",
                    "confidence": "EXTRACTED",
                    "weight": 1.0,
                    "source_file": str_path,
                    "source_location": "L1",
                })

            # Job dependencies (`needs:`)
            needs = job_data.get("needs") or []
            needs_list: list[str] = [str(needs)] if isinstance(needs, str) else [str(n) for n in needs]
            for needed_job in needs_list:
                dep_job_nid = _make_id("job", str_path, needed_job)
                edges.append({
                    "source": job_nid,
                    "target": dep_job_nid,
                    "relation": "depends_on",
                    "confidence": "EXTRACTED",
                    "weight": 1.0,
                    "source_file": str_path,
                    "source_location": "L1",
                })

            # Steps
            steps = job_data.get("steps") or []
            if isinstance(steps, list):
                for step_idx, step in enumerate(steps, start=1):
                    if not isinstance(step, dict):
                        continue
                    step_name = step.get("name") or f"step_{step_idx}"
                    step_nid = _make_id("step", str_path, str(job_id), str(step_idx))

                    step_uses = step.get("uses")
                    step_run = step.get("run") or ""
                    step_env = step.get("env") or {}
                    step_with = step.get("with") or {}

                    referenced_secrets: list[str] = []
                    referenced_envs: list[str] = []

                    # Scan for ${{ secrets.FOO }} or ${{ env.BAR }}
                    step_str_repr = str(step)
                    for sec_match in re.findall(r"secrets\.([A-Za-z0-9_]+)", step_str_repr):
                        referenced_secrets.append(sec_match)
                    for env_match in re.findall(r"env\.([A-Za-z0-9_]+)", step_str_repr):
                        referenced_envs.append(env_match)

                    if isinstance(step_env, dict):
                        referenced_envs.extend(str(k) for k in step_env.keys())

                    step_node: dict[str, Any] = {
                        "id": step_nid,
                        "label": f"Step:{step_name}",
                        "type": "workflow_step",
                        "file_type": "code",
                        "step_index": step_idx,
                        "secrets": list(dict.fromkeys(referenced_secrets)),
                        "env_vars": list(dict.fromkeys(referenced_envs)),
                        "source_file": str_path,
                        "source_location": f"L{step_idx}",
                        "text": f"Step {step_name}: {step_run[:100] if step_run else step_uses or ''}",
                    }
                    if step_run:
                        step_node["run"] = str(step_run)

                    nodes.append(step_node)
                    edges.append({
                        "source": job_nid,
                        "target": step_nid,
                        "relation": "has_step",
                        "confidence": "EXTRACTED",
                        "weight": 1.0,
                        "source_file": str_path,
                        "source_location": f"L{step_idx}",
                    })

                    # Action reference (`uses: actions/checkout@v4`)
                    if step_uses:
                        uses_str = str(step_uses).strip()
                        action_ref = uses_str
                        pin = ""
                        if "@" in uses_str:
                            action_ref, pin = uses_str.split("@", 1)

                        is_sha_pinned = bool(re.match(r"^[0-9a-fA-F]{40}$", pin))

                        act_nid = _make_id("action", action_ref)
                        nodes.append({
                            "id": act_nid,
                            "label": f"Action:{action_ref}",
                            "type": "action",
                            "file_type": "code",
                            "action_name": action_ref,
                            "pinned_version": pin,
                            "is_sha_pinned": is_sha_pinned,
                            "source_file": str_path,
                            "source_location": f"L{step_idx}",
                            "text": f"GitHub Action {action_ref} (pinned: {pin or 'none'})",
                        })
                        edges.append({
                            "source": step_nid,
                            "target": act_nid,
                            "relation": "uses_action",
                            "confidence": "EXTRACTED",
                            "weight": 1.0,
                            "pinned_version": pin,
                            "is_sha_pinned": is_sha_pinned,
                            "source_file": str_path,
                            "source_location": f"L{step_idx}",
                        })

                    # Referenced secrets and env vars
                    for sec in set(referenced_secrets):
                        sec_nid = _make_id("secret", sec)
                        nodes.append({
                            "id": sec_nid,
                            "label": f"Secret:{sec}",
                            "type": "secret_ref",
                            "file_type": "code",
                            "secret_name": sec,
                            "source_file": str_path,
                            "source_location": f"L{step_idx}",
                            "text": f"Secret {sec}",
                        })
                        edges.append({
                            "source": step_nid,
                            "target": sec_nid,
                            "relation": "references_secret",
                            "confidence": "EXTRACTED",
                            "weight": 1.0,
                            "source_file": str_path,
                            "source_location": f"L{step_idx}",
                        })

                    for ev in set(referenced_envs):
                        ev_nid = _make_id("env", ev)
                        nodes.append({
                            "id": ev_nid,
                            "label": f"Env:{ev}",
                            "type": "env_ref",
                            "file_type": "code",
                            "env_name": ev,
                            "source_file": str_path,
                            "source_location": f"L{step_idx}",
                            "text": f"Environment variable {ev}",
                        })
                        edges.append({
                            "source": step_nid,
                            "target": ev_nid,
                            "relation": "references_env",
                            "confidence": "EXTRACTED",
                            "weight": 1.0,
                            "source_file": str_path,
                            "source_location": f"L{step_idx}",
                        })

    return {"nodes": nodes, "edges": edges}
