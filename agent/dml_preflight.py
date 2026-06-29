"""Startup preflight checks for Daystrom DML-backed Hermes profiles.

The checks are intentionally non-mutating and secret-safe.  They catch the
class of partial deployments where the DML plugin/config is present but the
Hermes core hooks, launcher paths, or profile settings needed for full DML/DCN
operation are missing.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _expand_path(raw: Any, *, hermes_home: Path) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    text = text.replace("%LOCALAPPDATA%", os.environ.get("LOCALAPPDATA", ""))
    text = os.path.expandvars(os.path.expanduser(text))
    path = Path(text)
    if not path.is_absolute():
        path = hermes_home / path
    return path


def _missing_path(label: str, path: Path | None, issues: List[str], ok: List[str]) -> None:
    if path is None:
        issues.append(f"missing configured {label}")
        return
    if not path.exists():
        issues.append(f"{label} does not exist: {path}")
    else:
        ok.append(f"{label} exists")


def _provider_names(memory_manager: Any) -> Iterable[str]:
    for provider in getattr(memory_manager, "providers", []) or []:
        yield str(getattr(provider, "name", provider.__class__.__name__) or provider.__class__.__name__)


def run_daystrom_dml_preflight(
    *,
    config: Dict[str, Any],
    hermes_home: str | Path,
    agent: Any = None,
    memory_manager: Any = None,
) -> Tuple[List[str], List[str]]:
    """Return ``(issues, ok)`` for a Daystrom DML startup preflight.

    Empty ``issues`` means the profile has the expected DML startup surface.
    The function never reads secrets or dumps whole config blocks.
    """
    issues: List[str] = []
    ok: List[str] = []
    cfg = config or {}
    memory_cfg = dict(cfg.get("memory") or {})
    provider_name = str(memory_cfg.get("provider") or "").strip()
    if provider_name != "daystrom_dml":
        return issues, ok

    hermes_home_path = Path(hermes_home).expanduser()
    dml_cfg = dict(memory_cfg.get("daystrom_dml") or {})
    if not dml_cfg:
        issues.append("memory.provider is daystrom_dml but memory.daystrom_dml config is missing")
        return issues, ok

    for key, expected in {
        "retrieval_policy": "always",
        "sync_turns": True,
        "enable_memory": True,
    }.items():
        value = dml_cfg.get(key)
        if isinstance(expected, bool):
            if not _as_bool(value):
                issues.append(f"memory.daystrom_dml.{key} is not enabled")
            else:
                ok.append(f"memory.daystrom_dml.{key} enabled")
        elif str(value or "").strip().lower() != expected:
            issues.append(f"memory.daystrom_dml.{key} is {value!r}, expected {expected!r}")
        else:
            ok.append(f"memory.daystrom_dml.{key}={expected}")

    dcn_cfg = dict(dml_cfg.get("dcn") or {})
    if str(dcn_cfg.get("mode") or "").strip().lower() != "active_read":
        issues.append("memory.daystrom_dml.dcn.mode is not active_read")
    else:
        ok.append("memory.daystrom_dml.dcn.mode=active_read")

    for key in ("integration_dir", "launcher", "venv_python", "source_dir", "storage_dir", "config_path"):
        _missing_path(f"memory.daystrom_dml.{key}", _expand_path(dml_cfg.get(key), hermes_home=hermes_home_path), issues, ok)

    agent_cfg = dict(cfg.get("agent") or {})
    if str(agent_cfg.get("max_turns_extension_policy") or "").strip().lower() == "cognition":
        ok.append("agent.max_turns_extension_policy=cognition")
        if not _as_bool(agent_cfg.get("max_turns_auto_extend")):
            issues.append("agent.max_turns_auto_extend is not enabled")
        else:
            ok.append("agent.max_turns_auto_extend enabled")
        try:
            if int(agent_cfg.get("max_turns_extension") or 0) < 1:
                issues.append("agent.max_turns_extension is not a positive integer")
            else:
                ok.append("agent.max_turns_extension positive")
        except Exception:
            issues.append("agent.max_turns_extension is not an integer")
        try:
            if int(agent_cfg.get("max_turns_hard_cap") or 0) < int(agent_cfg.get("max_turns") or 1):
                issues.append("agent.max_turns_hard_cap is below agent.max_turns")
            else:
                ok.append("agent.max_turns_hard_cap covers max_turns")
        except Exception:
            issues.append("agent.max_turns_hard_cap is not an integer")
    elif any(k in agent_cfg for k in ("max_turns_auto_extend", "max_turns_extension", "max_turns_hard_cap")):
        issues.append("agent iteration extension keys are present but policy is not cognition")

    try:
        from agent.iteration_extension import build_iteration_extension_state

        state = build_iteration_extension_state(
            messages=[],
            user_message="dml preflight",
            prefetch_context="",
            session_id="dml-preflight",
            api_call_count=0,
            budget_used=0,
            budget_max=int(agent_cfg.get("max_turns") or 1),
            hard_cap=int(agent_cfg.get("max_turns_hard_cap") or 0),
        )
        if state.get("schema_version") == "hermes.iteration_extension.v1":
            ok.append("iteration extension contract available")
        else:
            issues.append("iteration extension contract schema is missing or unexpected")
    except Exception as exc:
        issues.append(f"iteration extension contract unavailable: {type(exc).__name__}: {exc}")

    try:
        from agent.context_compressor import ContextCompressor

        sig = inspect.signature(ContextCompressor.__init__)
        if "dml_first_enabled" in sig.parameters:
            ok.append("ContextCompressor supports dml_first_enabled")
        else:
            issues.append("ContextCompressor lacks dml_first_enabled support")
    except Exception as exc:
        issues.append(f"ContextCompressor preflight failed: {type(exc).__name__}: {exc}")

    manager = memory_manager or getattr(agent, "_memory_manager", None)
    if manager is None:
        issues.append("memory manager is not initialized")
    else:
        providers = list(_provider_names(manager))
        if "daystrom_dml" not in providers:
            issues.append(f"daystrom_dml provider is not active; active providers={providers}")
        else:
            ok.append("daystrom_dml provider active")
        if not callable(getattr(manager, "decide_iteration_extension", None)):
            issues.append("memory manager lacks decide_iteration_extension hook")
        else:
            ok.append("memory manager iteration-extension hook available")
        provider_objs = getattr(manager, "providers", []) or []
        dml_provider = next((p for p in provider_objs if getattr(p, "name", "") == "daystrom_dml"), None)
        if dml_provider is not None and callable(getattr(dml_provider, "decide_iteration_extension", None)):
            ok.append("daystrom_dml provider iteration-extension hook available")
        else:
            issues.append("daystrom_dml provider lacks decide_iteration_extension hook")

    return issues, ok


__all__ = ["run_daystrom_dml_preflight"]
