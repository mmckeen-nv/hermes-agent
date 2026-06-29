"""Adaptive iteration-budget extension helpers.

This module keeps the turn-extension decision small and testable.  The agent
loop calls it only when the current tool-call budget is exhausted; memory
providers such as Daystrom DML/DCN can inspect the compact run state and decide
whether to grant another bounded slice.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional


_GRANT_DECISIONS = {"grant", "extend", "continue", "needs_more_turns", "incomplete"}
_DENY_DECISIONS = {"deny", "stop", "terminate", "complete", "noise", "no_extend"}

_COMPLETION_RE = re.compile(
    r"\b(?:done|completed|complete|merged|pushed|opened pr|all tests pass(?:ed)?|validation passed|no .* pending)\b",
    re.IGNORECASE,
)
_INCOMPLETE_RE = re.compile(
    r"\b(?:not complete|isn't complete|is not complete|still need|next step|remaining|pending|blocked|todo|failing|failed|error|traceback|rerun|fix|implement|continue|in progress)\b",
    re.IGNORECASE,
)
_NOISE_RE = re.compile(
    r"\b(?:same error|repeated|looping|no progress|identical|stuck|thrash(?:ing)?|noise)\b",
    re.IGNORECASE,
)


def normalize_extension_decision(raw: Any) -> Dict[str, Any]:
    """Normalize provider return values into a stable decision dictionary."""
    if raw is None:
        return {}
    if isinstance(raw, bool):
        return {"decision": "grant" if raw else "deny", "reason_codes": ["boolean_provider_result"]}
    if isinstance(raw, str):
        raw = {"decision": raw}
    if not isinstance(raw, Mapping):
        return {"decision": "deny", "reason_codes": ["invalid_provider_result"]}

    decision = str(raw.get("decision") or raw.get("action") or "").strip().lower().replace("-", "_")
    if decision in _GRANT_DECISIONS:
        decision = "grant"
    elif decision in _DENY_DECISIONS:
        decision = "deny"
    else:
        decision = "deny"

    reason_codes = raw.get("reason_codes") or raw.get("reasons") or []
    if isinstance(reason_codes, str):
        reason_codes = [reason_codes]
    elif not isinstance(reason_codes, list):
        reason_codes = [str(reason_codes)] if reason_codes else []

    normalized: Dict[str, Any] = {
        "decision": decision,
        "reason_codes": [str(r) for r in reason_codes if str(r).strip()],
    }
    for key in ("extension", "extend_by", "confidence", "source", "summary"):
        if key in raw:
            normalized[key] = raw[key]
    return normalized


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _recent_messages(messages: Iterable[Mapping[str, Any]], *, limit: int = 12) -> List[Dict[str, Any]]:
    selected = list(messages)[-limit:]
    compact: List[Dict[str, Any]] = []
    for msg in selected:
        role = str(msg.get("role") or "")
        tool_calls = msg.get("tool_calls") or []
        compact.append({
            "role": role,
            "text": _message_text(msg)[-1200:],
            "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
            "tool_name": str(msg.get("name") or ""),
        })
    return compact


def build_iteration_extension_state(
    *,
    messages: List[Dict[str, Any]],
    user_message: str,
    prefetch_context: str = "",
    session_id: str = "",
    api_call_count: int = 0,
    budget_used: int = 0,
    budget_max: int = 0,
    hard_cap: int = 0,
) -> Dict[str, Any]:
    """Build a compact, non-secret run state for a provider decision."""
    recent = _recent_messages(messages)
    recent_text = "\n".join(m.get("text", "") for m in recent if m.get("text"))[-6000:]
    recent_tool_calls = sum(int(m.get("tool_call_count") or 0) for m in recent)
    recent_tool_results = sum(1 for m in recent if m.get("role") == "tool")
    last_assistant = next((m for m in reversed(recent) if m.get("role") == "assistant"), {})
    return {
        "schema_version": "hermes.iteration_extension.v1",
        "session_id": session_id,
        "user_message": (user_message or "")[-2000:],
        "recent_messages": recent,
        "recent_text": recent_text,
        "prefetch_context": (prefetch_context or "")[-4000:],
        "api_call_count": int(api_call_count or 0),
        "budget_used": int(budget_used or 0),
        "budget_max": int(budget_max or 0),
        "hard_cap": int(hard_cap or 0),
        "recent_tool_calls": recent_tool_calls,
        "recent_tool_results": recent_tool_results,
        "last_assistant_text": str(last_assistant.get("text") or "")[-1200:],
    }


def heuristic_iteration_extension_decision(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Conservative local fallback when no cognition provider can decide.

    This intentionally denies obvious completion/noise and grants only when the
    compact run state contains unresolved-work signals plus evidence of active
    tool work.  It is not a replacement for DML/DCN; it is a safe fallback.
    """
    text = "\n".join(
        str(state.get(k) or "")
        for k in ("user_message", "recent_text", "prefetch_context", "last_assistant_text")
    )
    incomplete_text = re.sub(r"\bno [^.?!\n]{0,80}\bpending\b", "", text, flags=re.IGNORECASE)
    if _NOISE_RE.search(text):
        return {"decision": "deny", "reason_codes": ["noise_or_loop_signal"], "source": "local_heuristic"}
    if _COMPLETION_RE.search(text) and not _INCOMPLETE_RE.search(incomplete_text):
        return {"decision": "deny", "reason_codes": ["completion_signal"], "source": "local_heuristic"}
    if int(state.get("recent_tool_calls") or 0) <= 0 and int(state.get("recent_tool_results") or 0) <= 0:
        return {"decision": "deny", "reason_codes": ["no_recent_tool_work"], "source": "local_heuristic"}
    if _INCOMPLETE_RE.search(text):
        return {"decision": "grant", "reason_codes": ["incomplete_signal", "recent_tool_work"], "source": "local_heuristic"}
    return {"decision": "deny", "reason_codes": ["insufficient_incomplete_signal"], "source": "local_heuristic"}


__all__ = [
    "build_iteration_extension_state",
    "heuristic_iteration_extension_decision",
    "normalize_extension_decision",
]
