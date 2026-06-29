"""Human-facing Daystrom DML configuration/status report helpers.

The goal is operator relief: a user should be able to ask Hermes what DML
knows about itself without spelunking through config.yaml, logs, or Python
objects.  This module is intentionally secret-safe and read-only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from agent.dml_preflight import run_daystrom_dml_preflight


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _check(label: str, enabled: bool, detail: str = "") -> str:
    suffix = f" — {detail}" if detail else ""
    return f"{'✅' if enabled else '❌'} {label}{suffix}"


def _warn(label: str, detail: str = "") -> str:
    suffix = f" — {detail}" if detail else ""
    return f"⚠️ {label}{suffix}"


def _cfg(config: Dict[str, Any], *keys: str) -> Any:
    node: Any = config
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _active_provider_names(memory_manager: Any = None, agent: Any = None) -> List[str]:
    manager = memory_manager or getattr(agent, "_memory_manager", None)
    out: List[str] = []
    for provider in getattr(manager, "providers", []) or []:
        out.append(str(getattr(provider, "name", provider.__class__.__name__) or provider.__class__.__name__))
    return out


def _runtime_agent(source_agent: Any = None, running_agents: Iterable[Any] | None = None) -> Any:
    if source_agent is not None:
        return source_agent
    for candidate in running_agents or []:
        if candidate is not None and candidate is not False:
            # Gateway uses a sentinel object before the real AIAgent exists.  A
            # real agent has a memory manager; sentinel objects do not.
            if getattr(candidate, "_memory_manager", None) is not None:
                return candidate
    return None


def build_dml_status_report(
    *,
    config: Dict[str, Any] | None,
    hermes_home: str | Path,
    agent: Any = None,
    memory_manager: Any = None,
    running_agents: Iterable[Any] | None = None,
) -> str:
    """Return a compact, secret-safe operator report for ``/dml-help``.

    The report combines static config, available Python hooks, startup-preflight
    findings, and any live runtime provider state available in the current
    gateway/session process.
    """
    cfg: Dict[str, Any] = dict(config or {})
    memory_cfg = dict(_cfg(cfg, "memory") or {})
    dml_cfg = dict(memory_cfg.get("daystrom_dml") or {})
    agent_cfg = dict(_cfg(cfg, "agent") or {})
    provider = str(memory_cfg.get("provider") or "").strip() or "unset"
    runtime = _runtime_agent(agent, running_agents)
    manager = memory_manager or getattr(runtime, "_memory_manager", None)

    is_dml_provider = provider == "daystrom_dml"
    policy = str(dml_cfg.get("retrieval_policy") or "unset")
    dcn_mode = str(_cfg(dml_cfg, "dcn", "mode") or "unset")
    extension_policy = str(agent_cfg.get("max_turns_extension_policy") or "unset")
    active_providers = _active_provider_names(manager)

    lines: List[str] = [
        "🧠 **Daystrom DML help / status**",
        "",
        "This command is read-only and secret-safe. It shows what Hermes can tell from config plus any live runtime hooks.",
        "",
        "**Core state**",
        _check("Memory provider is Daystrom DML", is_dml_provider, f"memory.provider={provider}"),
        _check("DML config block exists", bool(dml_cfg), "memory.daystrom_dml" if dml_cfg else "missing"),
        _check("Retrieval policy is always-on", policy == "always", f"retrieval_policy={policy}"),
        _check("Synchronous turn persistence enabled", _as_bool(dml_cfg.get("sync_turns")), f"sync_turns={dml_cfg.get('sync_turns', 'unset')}"),
        _check("Semantic memory enabled", _as_bool(dml_cfg.get("enable_memory")), f"enable_memory={dml_cfg.get('enable_memory', 'unset')}"),
        _check("DCN active-read cognition enabled", dcn_mode == "active_read", f"dcn.mode={dcn_mode}"),
        "",
        "**Flexible iterations**",
        _check("Auto-extension enabled", _as_bool(agent_cfg.get("max_turns_auto_extend")), f"max_turns_auto_extend={agent_cfg.get('max_turns_auto_extend', 'unset')}"),
        _check("Extension policy uses cognition", extension_policy == "cognition", f"max_turns_extension_policy={extension_policy}"),
        _check("Extension chunk configured", int(agent_cfg.get("max_turns_extension") or 0) > 0 if str(agent_cfg.get("max_turns_extension") or "").isdigit() else False, f"max_turns_extension={agent_cfg.get('max_turns_extension', 'unset')}"),
        _check("Hard cap configured", int(agent_cfg.get("max_turns_hard_cap") or 0) >= int(agent_cfg.get("max_turns") or 1) if str(agent_cfg.get("max_turns_hard_cap") or "").isdigit() else False, f"max_turns={agent_cfg.get('max_turns', 'unset')}, hard_cap={agent_cfg.get('max_turns_hard_cap', 'unset')}"),
        "",
        "**Runtime hooks**",
        _check("Memory manager is initialized", manager is not None),
        _check("daystrom_dml provider is active at runtime", "daystrom_dml" in active_providers, f"active={active_providers or 'none visible'}"),
        _check("Memory manager can decide iteration extension", callable(getattr(manager, "decide_iteration_extension", None)) if manager is not None else False),
    ]

    dml_provider = None
    for provider_obj in getattr(manager, "providers", []) or []:
        if getattr(provider_obj, "name", "") == "daystrom_dml":
            dml_provider = provider_obj
            break
    lines.append(_check("DML provider exposes iteration-extension hook", callable(getattr(dml_provider, "decide_iteration_extension", None)) if dml_provider is not None else False))

    issues, ok = run_daystrom_dml_preflight(
        config=cfg,
        hermes_home=hermes_home,
        agent=runtime,
        memory_manager=manager,
    )
    if is_dml_provider:
        lines.extend(["", "**Preflight**"])
        if issues:
            lines.append(_warn(f"{len(issues)} issue(s) found"))
            for issue in issues[:10]:
                lines.append(f"- {issue}")
            if len(issues) > 10:
                lines.append(f"- …and {len(issues) - 10} more")
        else:
            lines.append(f"✅ Preflight clean ({len(ok)} checks passed)")
    else:
        lines.extend(["", "**Preflight**", "ℹ️ Skipped because this profile is not configured for `memory.provider: daystrom_dml`."])

    lines.extend([
        "",
        "**Useful actions**",
        "- `/status` — current session/gateway state",
        "- `/profile` — active profile and Hermes home",
        "- `/restart` — restart gateway after config/code changes",
        "- `hermes config edit` — advanced config editing from a shell",
    ])
    return "\n".join(lines)


__all__ = ["build_dml_status_report"]
