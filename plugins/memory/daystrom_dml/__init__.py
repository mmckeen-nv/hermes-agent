"""Daystrom DML memory provider for Hermes/Citizen Snips.

This provider intentionally uses the Hermes-owned Daystrom DML launcher,
source tree, venv, and runtime store. It does not route model inference
through DML; it only contributes memory recall and DPM/personality overlay
context, then mirrors completed turns back into DML.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.memory_provider import MemoryProvider
from hermes_constants import get_default_hermes_root, get_hermes_home
from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)

def _default_integration_dir() -> Path:
    """Resolve the Hermes-owned Daystrom DML handoff for default/profile homes."""
    candidates = [
        get_default_hermes_root() / "hermes-agent" / "integrations" / "daystrom-dml",
        get_hermes_home() / "hermes-agent" / "integrations" / "daystrom-dml",
        get_hermes_home() / "integrations" / "daystrom-dml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _resolve_path(value: Any, fallback: Path) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    raw = raw.replace("%LOCALAPPDATA%", os.environ.get("LOCALAPPDATA", ""))
    raw = os.path.expandvars(os.path.expanduser(raw))
    path = Path(raw)
    if not path.is_absolute():
        path = get_default_hermes_root() / path
    return path
_DCN_MODES = {"disabled", "observe_only", "active_read", "active_learn"}
_DCN_DECISIONS = {"legacy", "overlay_only", "retrieve", "suppress_overlay"}


def _clean_text(value: str, limit: int = 4000) -> str:
    text = " ".join((value or "").split())
    return text[:limit].rstrip()


_MEMORY_CONTEXT_RE = re.compile(
    r"<\s*memory-context\s*>[\s\S]*?</\s*memory-context\s*>",
    re.IGNORECASE,
)
_DAYSTROM_BLOCK_RE = re.compile(
    r"^=== Daystrom (?:Personality Matrix Overlay|DML Active Continuity|DML Retrieved Memory) ===[\s\S]*?(?=^=== |\Z)",
    re.MULTILINE,
)
_ROLE_PREFIX_RE = re.compile(r"^(?:(?:user|assistant):\s*){2,}", re.IGNORECASE)
_ANY_ROLE_PREFIX_RE = re.compile(r"\b(?:(?:user|assistant):\s*){2,}", re.IGNORECASE)
_SCAFFOLD_RE = re.compile(
    r"^(?:i(?:'|’)ll|i am|i’m|let me|reading|checking|inspecting)\b",
    re.IGNORECASE,
)
_SMOKE_TEST_RE = re.compile(
    r"\b(?:smoke[- ]?test|self[- ]?test|test record|pre[- ]?fix|completed snips_?2 turn|completed citizen snips turn)\b",
    re.IGNORECASE,
)
_TOOL_EXHAUST_RE = re.compile(
    r"\b(?:chunk id:|wall time:|process exited|original token count|output:|"
    r"functions\.exec_command|multi_tool_use\.parallel|apply_patch|namespace web|"
    r"^total output lines:|\[truncated\])\b",
    re.IGNORECASE | re.MULTILINE,
)
_DISCORD_PREFIX_RE = re.compile(r"\[[A-Za-z0-9_.-]{2,32}\]\s*")
_STRUCTURED_FIELD_RE = re.compile(
    r"(?P<key>current_focus|memory_policy|next_action|next_step|task)\s*=\s*(?P<value>.*?)(?=;\s*(?:current_focus|memory_policy|next_action|next_step|task|last_confirmed_status)\s*=|\s*\|\s*(?:thread|state)\s*:|\Z)",
    re.IGNORECASE,
)
_LOG_STATUS_RE = re.compile(
    r"\b(?:gateway received sigterm|pytest passed|py_compile|traceback|"
    r"ran pytest|tests? passed|smoke hygiene|chunk id:|process exited|"
    r"wall time:|original token count|```)\b",
    re.IGNORECASE,
)
_ITERATION_INCOMPLETE_RE = re.compile(
    r"\b(?:not complete|isn't complete|is not complete|still need|next step|remaining|pending|blocked|failing|failed|error|traceback|rerun|fix|implement|continue|in progress)\b",
    re.IGNORECASE,
)
_ITERATION_COMPLETE_RE = re.compile(
    r"\b(?:done|completed|complete|merged|pushed|opened pr|all tests pass(?:ed)?|validation passed|no .* pending)\b",
    re.IGNORECASE,
)
_ITERATION_NOISE_RE = re.compile(
    r"\b(?:same error|repeated|looping|no progress|identical|stuck|thrash(?:ing)?|noise)\b",
    re.IGNORECASE,
)
_DURABLE_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("preference", re.compile(r"\b(?:i|we|mark)\s+(?:prefers?|likes?|wants?|needs?|expects?)\b|\bplease\s+(?:prefer|use|keep|make|remember|avoid)\b|\bmore\s+[^.!?]{1,80}\bless\s+|\bless\s+[^.!?]{1,80}\bmore\s+|\btoo\s+(?:mechanical|rigid|formal|verbose|terse)\b", re.IGNORECASE)),
    ("identity", re.compile(r"\b(?:assistant identity|citizen snips|snips_?2|you are citizen snips)\b", re.IGNORECASE)),
    ("constraint", re.compile(r"\b(?:do not|don't|never|always|must|should|explicit(?:ly)? instructed|current-turn instructions?)\b", re.IGNORECASE)),
    ("artifact", re.compile(r"\b(?:changed|updated|added|implemented|fixed|created|wrote|patched|configured|wired|compiled)\b.*(?:/[^\s]+|\b[\w.-]+\.(?:py|yaml|yml|json|md|sh|txt)\b)", re.IGNORECASE)),
    ("validation", re.compile(r"\b(?:test|pytest|py_compile|validation|smoke script|compile)\b.*\b(?:pass(?:ed)?|fail(?:ed)?|error|blocked|ran|run)\b", re.IGNORECASE)),
    ("blocker", re.compile(r"\b(?:blocker|blocked|failure|bug|regression|root cause|workaround|resolved|fix)\b", re.IGNORECASE)),
    ("checkpoint", re.compile(r"\b(?:current task|active task|next action|remaining|checkpoint|handoff|resume|continuity)\b", re.IGNORECASE)),
)
_SENSITIVE_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|authorization|bearer)\b\s*[:=]\s*\S+"
)
_TRANSCRIPT_RESIDUE_RE = re.compile(
    r"(?:\bCompleted\s+(?:Snips_?2|Citizen Snips)\s+turn\b|"
    r"\b(?:User|Assistant):\s|"
    r"\bassistant:\s*\|\s*assistant:\b|"
    r"===\s*Daystrom\s+(?:DML|Personality)|"
    r"<\s*memory-context\s*>|"
    r"\b(?:tool_calls?|functions\.|multi_tool_use\.|Chunk ID:|Process exited)\b)",
    re.IGNORECASE,
)
_SYSTEM_WRAPPER_RE = re.compile(
    r"(?:\[System note:|"
    r"\[Internal memory context:|"
    r"Your previous turn in this session was interrupted by a gateway shutdown|"
    r"The conversation history below is intact|"
    r"unfinished tool result|"
    r"address the user's new message below|"
    r"\[IMPORTANT:\s*Background process|"
    r"Background process proc_|"
    r"completed \(exit code|"
    r"Command:\s*codex exec|"
    r"Output:\s*\n?\s*\[… output truncated|"
    r"tokens used|"
    r"--output-last-message)",
    re.IGNORECASE,
)
_PERSONALITY_OVERLAY_RE = re.compile(
    r"(?:"
    r"===\s*(?:Daystrom\s+)?Personality Matrix(?:\s+Overlay)?\s*===|"
    r"\bIdentity:\s*Citizen Snips\.\s*Preferences:|"
    r"\bConstraint:\s*Current-turn instructions override the DPM overlay\b|"
    r"\bDPM overlay\b"
    r")",
    re.IGNORECASE,
)
_SUMMARY_PREFACE_RE = re.compile(
    r"(?is)^\s*(?:here\s+is\s+a\s+summary(?:\s+of\s+the\s+content)?(?:\s+in\s+\d+\s+(?:tokens?|characters?)\s+or\s+less)?\s*:?|summary\s*:)\s*",
)
_SUMMARY_PROMPT_RESIDUE_RE = re.compile(
    r"(?:"
    r"\b(?:please|go ahead and|kindly)\s+(?:provide|paste|share)\b.{0,120}\b(?:text|content|original text)\b|"
    r"\bi['’]?m\s+(?:ready|waiting)\b.{0,120}\b(?:summari[sz]e|condense)\b|"
    r"\b(?:summari[sz]e|condense)\b.{0,80}\b(?:256|characters?\s+or\s+less|character\s+limit)\b|"
    r"\bthere\s+is\s+no\s+content\s+provided\b"
    r")",
    re.IGNORECASE,
)
_MEMORY_REHYDRATION_RE = re.compile(
    r"(?:"
    r"\bre[- ]?hydrat(?:e|ion)\b|"
    r"\b(?:resume|restore|recover|reload|rebuild|reconstruct)\b.{0,80}\b(?:context|memory|state|continuity|thread|session)\b|"
    r"\b(?:context|memory|state|continuity|thread|session)\b.{0,80}\b(?:resume|restore|recover|reload|rebuild|reconstruct)\b|"
    r"\b(?:lost|missing|dropped|forgot|forget|compacted|compressed|truncated)\b.{0,80}\b(?:context|memory|state|continuity|thread|session)\b|"
    r"\b(?:after|from|following|because of)\b.{0,40}\b(?:compaction|compression|context loss|context reset)\b|"
    r"\b(?:system|memory)[- ]?context\b|"
    r"<\s*memory-context\s*>|"
    r"\bcompaction\b"
    r")",
    re.IGNORECASE,
)
_EXPLICIT_MEMORY_RECALL_RE = re.compile(
    r"(?:"
    r"\b(?:what|where|when|why|how)\b.{0,80}\b(?:did|do|had)\s+we\s+(?:decide|agree|choose|settle|plan|say)\b|"
    r"\b(?:what|where|when|why|how)\b.{0,80}\b(?:did|do|had)\s+(?:i|you|mark)\s+(?:decide|agree|choose|settle|plan|say|ask|tell)\b|"
    r"\b(?:recall|remember|remind me|look up|retrieve)\b.{0,80}\b(?:memory|memories|decision|decisions|context|what we|what i|what you|yesterday|earlier|last time|previously)\b|"
    r"\b(?:what did we|what was the|what were the)\b.{0,80}\b(?:yesterday|earlier|last time|previously)\b"
    r")",
    re.IGNORECASE,
)
_LONG_HORIZON_CONTINUATION_RE = re.compile(
    r"(?:"
    r"\b(?:continue|resume|pick up|carry on|keep going)\b.{0,80}\b(?:long[- ]?(?:running|horizon)|multi[- ]?(?:turn|session|day)|setup|migration|implementation|project|task|workstream)\b|"
    r"\b(?:continue|resume|pick up|carry on|keep going)\b.{0,80}\b(?:where we left off|from yesterday|from last time|the previous task|that task)\b|"
    r"\b(?:long[- ]?(?:running|horizon)|multi[- ]?(?:turn|session|day))\b.{0,80}\b(?:continue|resume|task|setup|project)\b"
    r")",
    re.IGNORECASE,
)


def _strip_summary_preface(value: str) -> str:
    text = value or ""
    # A few old memories captured the summarizer instruction wrapper rather
    # than the memory.  Store and render the memory itself, not the wrapper.
    for _ in range(3):
        stripped = _SUMMARY_PREFACE_RE.sub("", text).strip()
        if stripped == text.strip():
            break
        text = stripped
    return text.strip()


def _contains_system_wrapper(value: str) -> bool:
    return bool(_SYSTEM_WRAPPER_RE.search(value or ""))


def _contains_personality_overlay(value: str) -> bool:
    return bool(_PERSONALITY_OVERLAY_RE.search(value or ""))


def _should_inject_dml_memory(query: str) -> bool:
    """Gate high-token DML continuity/retrieval to explicit memory needs."""
    text = _clean_text(query, 1600)
    if not text:
        return False
    return bool(
        _MEMORY_REHYDRATION_RE.search(text)
        or _EXPLICIT_MEMORY_RECALL_RE.search(text)
        or _LONG_HORIZON_CONTINUATION_RE.search(text)
    )


def _strip_system_wrapper_notes(value: str) -> str:
    """Drop gateway/system wrapper notes before DML writeback or injection."""
    text = value or ""
    if not text:
        return ""
    kept = [line for line in text.splitlines() if not _contains_system_wrapper(line)]
    return "\n".join(kept)


def _strip_recent_channel_context(value: str) -> str:
    """Keep only the current gateway message when a Recent channel messages prelude is present."""

    text = value or ""
    match = re.search(r"(?is)\[New message\]\s*(.*)$", text)
    if match:
        return match.group(1)
    return text


def _strip_injected_context(value: str) -> str:
    """Remove API-time memory injection from text before writing DML handoffs."""
    text = _strip_recent_channel_context(value or "")
    text = _SUMMARY_PREFACE_RE.sub("", text)
    text = _MEMORY_CONTEXT_RE.sub(" ", text)
    text = _strip_system_wrapper_notes(text)
    text = _DAYSTROM_BLOCK_RE.sub(" ", text)
    text = _ANY_ROLE_PREFIX_RE.sub("", text)
    return text


def _looks_like_transcript_residue(value: str) -> bool:
    """Return True for text that should never enter DML as semantic memory."""
    text = value or ""
    if _contains_system_wrapper(text):
        return True
    if _TRANSCRIPT_RESIDUE_RE.search(text):
        return True
    # Raw dialogue/checkpoint blobs often contain multiple role labels even if
    # they do not start with repeated prefixes. DML should store state, not chat.
    role_labels = len(re.findall(r"\b(?:user|assistant):", text, flags=re.IGNORECASE))
    return role_labels >= 2


def _safe_memory_text(value: str, *, limit: int = 700) -> str:
    """Compact text to a DML-safe semantic fragment, rejecting transcript residue."""
    text = _redact_sensitive(_fit_sentence_boundary(_clean_text(_strip_injected_context(value), limit), limit))
    if not text or _looks_like_transcript_residue(text):
        return ""
    return text


def _strip_dialogue_noise(value: str) -> str:
    text = _strip_injected_context(value or "")
    text = _DISCORD_PREFIX_RE.sub("", text)
    text = re.sub(r"\b(?:user|assistant):\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:thread|state)\s*:\s*[^|;]*\|?", " ", text, flags=re.IGNORECASE)
    text = text.replace("Citizen Snips durable turn memory.", " ")
    text = re.sub(r"\b(?:User signal|Assistant outcome)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bRemember\s*:\s*", "", text, flags=re.IGNORECASE)
    return _clean_text(text, 1200)


def _semantic_value(value: str, *, limit: int = 220) -> str:
    if _contains_system_wrapper(value) or _contains_personality_overlay(value):
        return ""
    text = _strip_summary_preface(_strip_dialogue_noise(value))
    if not text or _LOG_STATUS_RE.search(text) or _TOOL_EXHAUST_RE.search(text):
        return ""
    if _SUMMARY_PROMPT_RESIDUE_RE.search(text):
        return ""
    text = re.split(r"```|<tool output>|Gateway received SIGTERM:?", text, maxsplit=1, flags=re.IGNORECASE)[0]
    text = _fit_sentence_boundary(text.strip(" -;|"), limit)
    if not text or _looks_like_transcript_residue(text):
        return ""
    return _redact_sensitive(text)


def _semantic_label_value(label: str, value: str, *, limit: int = 220) -> str:
    if _contains_system_wrapper(value) or _contains_personality_overlay(value):
        return ""
    safe = _semantic_value(value, limit=limit)
    if not safe:
        return ""
    if label == "Memory policy":
        safe = re.split(
            r"\s*\|\s*task\s*:|\s*;\s*task\s*=|\s*;\s*(?:current_focus|next_action|next_step)\s*=",
            safe,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" -;|")
    elif label == "Current focus" and re.search(r"\boffload\b.*\bcodex\b|\bcodex\b.*\boffload\b", safe, re.IGNORECASE):
        safe = "tighten DML continuity formatting through Codex-offloaded implementation."
    return safe


def _append_semantic_bullet(result: List[str], label: str, value: str, *, limit: int = 220) -> None:
    safe = _semantic_label_value(label, value, limit=limit)
    if safe:
        result.append(f"- {label}: {safe}")


def _bullet_parts(line: str) -> Tuple[str, str]:
    match = re.match(r"^-\s*([^:]+):\s*(.*)$", line.strip())
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip()


def _dedupe_equivalence_key(label: str, value: str) -> str:
    text = value.lower()
    text = re.sub(r"\.\.\.\s*(?:\[truncated\])?", " ", text)
    text = re.sub(r"\b(?:please|can you|could you|great|thanks)\b", " ", text)
    text = re.sub(r"\W+", " ", text).strip()
    if label in {"Current focus", "Memory policy"}:
        return label.lower()
    return f"{label.lower()}:{text}"


def _prefer_semantic_bullet(existing: str, candidate: str) -> str:
    _, old_value = _bullet_parts(existing)
    _, new_value = _bullet_parts(candidate)

    def score(value: str) -> Tuple[int, int]:
        ellipsis_penalty = -1 if "..." in value or "[truncated]" in value.lower() else 0
        terminal_bonus = 1 if re.search(r"[.!?]$", value.strip()) else 0
        return (ellipsis_penalty + terminal_bonus, len(value))

    return candidate if score(new_value) > score(old_value) else existing


def _dedupe_semantic_bullets(candidates: List[str], *, limit: int) -> List[str]:
    ordered_keys: List[str] = []
    chosen: Dict[str, str] = {}
    for candidate in candidates:
        label, value = _bullet_parts(candidate)
        if not label:
            key = re.sub(r"\W+", " ", candidate).strip().lower()
        else:
            key = _dedupe_equivalence_key(label, value)
        if key in chosen:
            chosen[key] = _prefer_semantic_bullet(chosen[key], candidate)
            continue
        ordered_keys.append(key)
        chosen[key] = candidate
    return [chosen[key] for key in ordered_keys[:limit]]


def _semantic_memory_bullets(text: str, meta: Optional[Dict[str, Any]] = None, *, limit: int = 6) -> List[str]:
    """Render arbitrary DML recall as compact semantic bullets only."""
    raw = str(text or "")
    meta = meta if isinstance(meta, dict) else {}
    bullets: List[str] = []

    existing_label, existing_value = _bullet_parts(raw)
    if existing_label in {"Current focus", "Memory policy", "Preference", "Next step", "Memory"}:
        _append_semantic_bullet(bullets, existing_label, existing_value, limit=320 if existing_label == "Memory policy" else 220)
        return bullets

    structured = raw
    if re.search(r"\|\s*state\s*:", structured, re.IGNORECASE):
        structured = re.split(r"\|\s*state\s*:", structured, maxsplit=1, flags=re.IGNORECASE)[1]
    for match in _STRUCTURED_FIELD_RE.finditer(structured):
        key = match.group("key").lower()
        value = match.group("value")
        if key == "current_focus":
            _append_semantic_bullet(bullets, "Current focus", value)
        elif key == "memory_policy":
            _append_semantic_bullet(bullets, "Memory policy", value, limit=320)
        elif key in {"next_action", "next_step", "task"}:
            _append_semantic_bullet(bullets, "Next step", value)

    if bullets:
        return _dedupe_semantic_bullets(bullets, limit=limit)

    memory_class = str(meta.get("memory_class") or "").lower()
    safe = _semantic_value(raw, limit=300)
    if not safe:
        return []
    if memory_class == "preference" or re.search(r"\b(?:prefers?|likes?|wants?)\b", safe, re.IGNORECASE):
        return [f"- Preference: {safe}"]
    if memory_class == "constraint" or re.search(r"\b(?:never|always|do not|don't|must|should)\b", safe, re.IGNORECASE):
        return [f"- Memory policy: {safe}"]
    if memory_class in {"checkpoint", "blocker", "artifact"}:
        return [f"- Current focus: {safe}"]
    return [f"- Memory: {safe}"]


def _dedupe_memory_blocks(blocks: List[str]) -> str:
    """Join memory blocks while removing repeated bullet/state lines across lanes."""
    seen_line_index: Dict[str, Tuple[int, int]] = {}
    output: List[str] = []
    for block in blocks:
        kept: List[str] = []
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                kept.append(line)
                continue
            if stripped.startswith("- "):
                label, value = _bullet_parts(stripped)
                key = _dedupe_equivalence_key(label, value) if label else re.sub(r"\W+", " ", stripped).strip().lower()
                if key in seen_line_index:
                    block_idx, line_idx = seen_line_index[key]
                    existing = kept[line_idx].strip() if block_idx == len(output) else output[block_idx].splitlines()[line_idx].strip()
                    if _prefer_semantic_bullet(existing, stripped) == stripped:
                        if block_idx == len(output):
                            kept[line_idx] = line
                        else:
                            previous_lines = output[block_idx].splitlines()
                            previous_lines[line_idx] = line
                            output[block_idx] = "\n".join(previous_lines)
                    continue
                seen_line_index[key] = (len(output), len(kept))
            kept.append(line)
        compact = "\n".join(kept).strip()
        if compact and not compact.endswith("==="):
            output.append(compact)
    return "\n\n".join(output)


def _sentenceish_fragments(text: str, *, limit: int = 4) -> List[str]:
    cleaned = _clean_text(_strip_injected_context(text), 2400)
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+|\n+", cleaned)
    fragments: List[str] = []
    seen: set[str] = set()
    for part in parts:
        line = _ROLE_PREFIX_RE.sub("", part).strip(" -\t")
        if not line:
            continue
        if _TOOL_EXHAUST_RE.search(line):
            continue
        key = re.sub(r"\W+", " ", line).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        fragments.append(line)
        if len(fragments) >= limit:
            break
    return fragments


def _fit_sentence_boundary(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: max(1, limit)].rstrip()
    sentence_cut = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    if sentence_cut >= max(40, int(limit * 0.55)):
        return cut[: sentence_cut + 1].rstrip()
    word_cut = cut.rfind(" ")
    if word_cut >= max(20, int(limit * 0.45)):
        return cut[:word_cut].rstrip()
    return cut.rstrip(" .,;:-")


def _redact_sensitive(text: str) -> str:
    return _SENSITIVE_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", text or "")


def _dpm_preference_text(user_content: str, hygiene: Dict[str, Any]) -> str:
    """Extract a clean current-user preference for the personality graph.

    DML turn memories are summaries; DPM should learn from the user's actual
    correction/preference, not from replay wrappers like "User signal:" or
    assistant outcomes.  Keep this conservative and hygienic.
    """
    memory_class = str(hygiene.get("memory_class") or "").lower()
    if memory_class not in {"preference", "identity", "constraint"}:
        return ""
    text = _semantic_value(user_content, limit=360)
    if not text:
        return ""
    if not any(pattern.search(text) for klass, pattern in _DURABLE_PATTERNS if klass in {"preference", "identity", "constraint"}):
        return ""
    return text


def _classify_turn_memory(user_content: str, assistant_content: str) -> Dict[str, Any]:
    raw_user = _strip_injected_context(user_content)
    raw_assistant = _strip_injected_context(assistant_content)
    combined = _clean_text(f"{raw_user}\n{raw_assistant}", 5000)
    if not combined:
        return {"keep": False, "score": 0.0, "memory_class": "empty", "reasons": ["empty"]}
    if _looks_like_transcript_residue(combined):
        return {"keep": False, "score": 0.0, "memory_class": "transcript_residue", "reasons": ["transcript_residue"]}

    score = 0.0
    classes: List[str] = []
    reasons: List[str] = []
    for memory_class, pattern in _DURABLE_PATTERNS:
        if pattern.search(combined):
            classes.append(memory_class)
            score += 0.24
    if classes:
        reasons.append("durable_signal")

    user_fragments = _sentenceish_fragments(raw_user, limit=3)
    assistant_fragments = _sentenceish_fragments(raw_assistant, limit=4)
    if user_fragments:
        score += 0.08
    if assistant_fragments:
        score += 0.06

    noise_hits = 0
    if _SMOKE_TEST_RE.search(combined):
        noise_hits += 3
        reasons.append("smoke_or_self_test")
    if _MEMORY_CONTEXT_RE.search(user_content or "") or _MEMORY_CONTEXT_RE.search(assistant_content or ""):
        noise_hits += 2
        reasons.append("injected_memory_context")
    if _DAYSTROM_BLOCK_RE.search(user_content or "") or _DAYSTROM_BLOCK_RE.search(assistant_content or ""):
        noise_hits += 2
        reasons.append("daystrom_context_block")
    if _TOOL_EXHAUST_RE.search(combined):
        noise_hits += 1
        reasons.append("tool_output_boilerplate")
    role_prefix_count = len(re.findall(r"\b(?:user|assistant):\s*\|?\s*(?:user|assistant):", combined, flags=re.IGNORECASE))
    if role_prefix_count:
        noise_hits += min(3, role_prefix_count)
        reasons.append("repeated_role_prefix")
    scaffold_lines = sum(1 for line in combined.splitlines() if _SCAFFOLD_RE.match(line.strip()))
    if scaffold_lines:
        noise_hits += min(2, scaffold_lines)
        reasons.append("assistant_scaffolding")
    score -= noise_hits * 0.16

    memory_class = classes[0] if classes else "low_signal"
    if not classes:
        reasons.append("no_durable_signal")
    if "smoke_or_self_test" in reasons:
        keep = False
    else:
        keep = bool(classes) and score >= 0.28

    summary_parts: List[str] = []
    if user_fragments:
        summary_parts.append("User signal: " + _fit_sentence_boundary(" ".join(user_fragments), 360))
    if assistant_fragments and memory_class in {"artifact", "validation", "blocker", "checkpoint"}:
        summary_parts.append("Assistant outcome: " + _fit_sentence_boundary(" ".join(assistant_fragments), 520))
    summary = _safe_memory_text(" ".join(summary_parts), limit=700)
    if not summary:
        keep = False
    return {
        "keep": keep,
        "score": round(max(0.0, min(1.0, score)), 3),
        "memory_class": memory_class,
        "reasons": reasons[:8],
        "summary": summary,
    }


def _handoff_fragment(role: str, content: str, *, limit: int = 900) -> str:
    text = _strip_injected_context(content)
    lines: List[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = _ROLE_PREFIX_RE.sub("", raw).strip()
        if not line:
            continue
        # Drop obvious progress/scaffolding chatter from the checkpoint. The
        # actual completed result is still captured via sync_turn/ingest.
        if role == "assistant" and _SCAFFOLD_RE.match(line):
            continue
        if _looks_like_transcript_residue(line) or _TOOL_EXHAUST_RE.search(line):
            continue
        key = re.sub(r"\W+", " ", line).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
    return _safe_memory_text(" ".join(lines), limit=limit)


def _continuity_state_from_messages(messages: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    """Build compact state/task/next-action fields, never a transcript tail."""
    latest_user = ""
    latest_assistant = ""
    for msg in reversed(messages[-20:]):
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        fragment = _handoff_fragment(str(role), content, limit=360)
        if not fragment:
            continue
        if role == "user" and not latest_user:
            latest_user = fragment
        elif role == "assistant" and not latest_assistant:
            latest_assistant = fragment
        if latest_user and latest_assistant:
            break

    state_parts = []
    if latest_user:
        state_parts.append("current_focus=" + _fit_sentence_boundary(latest_user, 220))
    if latest_assistant:
        state_parts.append("last_confirmed_status=" + _fit_sentence_boundary(latest_assistant, 260))
    state_parts.append("memory_policy=store compact semantic state only; never store transcripts, DML blocks, tool logs, or role-prefixed dialogue")
    state = _safe_memory_text("; ".join(state_parts), limit=700)
    task = _safe_memory_text(latest_user or "Maintain Citizen Snips continuity using compact DML state.", limit=220)
    next_action = "Continue from compact DML state and retrieve only relevant durable facts."
    return state, task, next_action


class DaystromDMLProvider(MemoryProvider):
    """Hermes external-memory provider backed by the local Daystrom DML store."""

    def __init__(self) -> None:
        self._cfg = self._load_provider_config()
        integration_dir = _resolve_path(self._cfg.get("integration_dir"), _default_integration_dir())
        self.integration_dir = integration_dir
        self.launcher = _resolve_path(self._cfg.get("launcher"), integration_dir / "bin" / "hermes-dml-memory")
        self.venv_python = _resolve_path(self._cfg.get("venv_python"), integration_dir / ".venv-dml" / "bin" / "python")
        self.source_dir = _resolve_path(self._cfg.get("source_dir"), integration_dir / "source")
        self.store_dir = _resolve_path(self._cfg.get("storage_dir"), integration_dir / "stores" / "hermes-runtime-store")
        self.config_path = _resolve_path(self._cfg.get("config_path"), integration_dir / "source" / "openclaw-wrapper" / "config" / "dml_gpu_only.yaml")
        self.tenant_id = str(self._cfg.get("tenant_id") or "openclaw")
        self.client_id = self._cfg.get("client_id") or "snips2"
        self.top_k = int(self._cfg.get("top_k") or 6)
        self.timeout = float(self._cfg.get("timeout_seconds") or 20)
        self.max_context_chars = int(self._cfg.get("max_context_chars") or 5000)
        self.sync_turns = bool(self._cfg.get("sync_turns", True))
        self.enable_personality = bool(self._cfg.get("enable_personality", True))
        self.enable_memory = bool(self._cfg.get("enable_memory", True))
        self.retrieval_policy = str(
            self._cfg.get("retrieval_policy")
            or self._cfg.get("memory_retrieval")
            or "always"
        ).strip().lower().replace("-", "_")
        self.dcn_requested_mode = self._configured_dcn_mode()
        self.dcn_promotion = self._load_dcn_promotion()
        self.dcn_promotion_gate_reason = self._promotion_gate_reason(self.dcn_promotion)
        self.dcn_mode = self._effective_dcn_mode()
        self.no_require_gpu = bool(self._cfg.get("no_require_gpu", True))
        self._session_id = ""
        self._thread_id = ""
        self._chat_id = ""
        self._relationship_id = str(self._cfg.get("relationship_id") or "relationship:mark-snips2")
        self._project_id = str(self._cfg.get("project_id") or "project:snips2")
        self._last_sync_key = ""
        self._lock = threading.Lock()
        self._dcn_observations: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "daystrom_dml"

    def _load_provider_config(self) -> Dict[str, Any]:
        try:
            cfg = load_config()
            block = cfg_get(cfg, "memory", "daystrom_dml") or {}
            return block if isinstance(block, dict) else {}
        except Exception:
            return {}

    def _dcn_config(self) -> Dict[str, Any]:
        dcn_cfg = self._cfg.get("dcn") if isinstance(self._cfg.get("dcn"), dict) else {}
        assert isinstance(dcn_cfg, dict)
        return dcn_cfg

    def _configured_dcn_mode(self) -> str:
        env_mode = os.environ.get("DAYSTROM_DCN_MODE")
        dcn_cfg = self._dcn_config()
        mode = str(env_mode or dcn_cfg.get("mode") or self._cfg.get("dcn_mode") or "disabled").strip().lower().replace("-", "_")
        if mode not in _DCN_MODES:
            raise ValueError(f"Invalid Daystrom DCN mode {mode!r}; expected one of {sorted(_DCN_MODES)}")
        return mode

    def _load_dcn_promotion(self) -> Dict[str, Any]:
        raw_env = os.environ.get("DAYSTROM_DCN_PROMOTION_EVIDENCE")
        if raw_env:
            try:
                payload = json.loads(raw_env)
            except json.JSONDecodeError:
                return {}
            return payload if isinstance(payload, dict) else {}
        dcn_cfg = self._dcn_config()
        promotion = dcn_cfg.get("promotion") or dcn_cfg.get("promotion_evidence")
        return promotion if isinstance(promotion, dict) else {}

    def _promotion_gate_reason(self, promotion: Dict[str, Any]) -> str:
        if self.dcn_requested_mode != "active_learn":
            return "not_requested"
        checkpoint_id = str(promotion.get("checkpoint_id") or "").strip()
        rollback_command = str(promotion.get("rollback_command") or "")
        eval_raw = promotion.get("eval")
        hygiene_raw = promotion.get("hygiene")
        eval_evidence = eval_raw if isinstance(eval_raw, dict) else {}
        hygiene = hygiene_raw if isinstance(hygiene_raw, dict) else {}
        runtime_mode = str(promotion.get("runtime_mode") or promotion.get("target_mode") or "").strip().lower().replace("-", "_")
        if promotion.get("promoted") is not True:
            return "promotion_missing"
        if runtime_mode != "active_learn":
            return "runtime_mode_not_active_learn"
        if not checkpoint_id:
            return "checkpoint_missing"
        if checkpoint_id not in rollback_command:
            return "rollback_command_missing_checkpoint"
        if eval_evidence.get("passed") is not True:
            return "eval_not_passed"
        if hygiene.get("passed") is not True:
            return "hygiene_not_passed"
        return "ok"

    def _effective_dcn_mode(self) -> str:
        if self.dcn_requested_mode == "active_learn" and self.dcn_promotion_gate_reason != "ok":
            logger.warning(
                "Daystrom DCN active_learn requested without valid governed promotion evidence; falling back to active_read: %s",
                self.dcn_promotion_gate_reason,
            )
            return "active_read"
        return self.dcn_requested_mode

    def is_available(self) -> bool:
        return self.launcher.exists() and os.access(self.launcher, os.X_OK) and self.store_dir.exists()

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = self._shape_session_id(session_id, kwargs)
        self._thread_id = str(kwargs.get("thread_id") or "")
        self._chat_id = str(kwargs.get("chat_id") or "")
        # Validate best-effort. Never block agent startup on DML rough edges.
        try:
            self._run_cli(["health"], timeout=min(self.timeout, 15))
        except Exception as exc:
            logger.warning("Daystrom DML health check failed during init: %s", exc)

    def _shape_session_id(self, session_id: str, kwargs: Dict[str, Any]) -> str:
        thread = str(kwargs.get("thread_id") or "").strip()
        chat = str(kwargs.get("chat_id") or "").strip()
        if thread:
            return f"snips2-discord-{thread}"
        if chat:
            return f"snips2-discord-{chat}"
        return session_id or "snips2-hermes-default"

    def system_prompt_block(self) -> str:
        return (
            "Daystrom DML memory/personality provider is active for Citizen Snips. "
            "Use injected DML context as potentially relevant recall, subordinate to current user instructions. "
            "The DML inference/frontier pipeline is intentionally not active."
        )

    def _observe_dcn(self, query: str, *, session_id: str, should_inject_memory: bool) -> None:
        if self.dcn_mode != "observe_only":
            return
        event = {
            "event": "dcn.observe",
            "mode": self.dcn_mode,
            "query_hash": hashlib.sha256(query.encode("utf-8", errors="ignore")).hexdigest()[:16],
            "query_chars": len(query),
            "session_id": session_id,
            "would_apply_dpm": bool(self.enable_personality),
            "would_inject_dml": bool(self.enable_memory and should_inject_memory),
            "would_call_resume": bool(self.enable_memory and should_inject_memory),
            "would_call_retrieve": bool(self.enable_memory and should_inject_memory),
        }
        self._record_dcn_event(event)
        logger.info("Daystrom DCN observe-only: %s", json.dumps(event, sort_keys=True))

    def _record_dcn_event(self, event: Dict[str, Any]) -> None:
        self._dcn_observations.append(event)
        self._dcn_observations = self._dcn_observations[-50:]

    def _dcn_policy_decision(self, query: str) -> Dict[str, Any]:
        """Return deterministic active-read gates for DPM and DML.

        Phase 9 keeps this in-process and rules-first. The method is small and
        intentionally overridable in smokes so DCN failure paths are testable
        without changing plugin globals.
        """
        text = _clean_text(query, 1600).lower()
        if not text:
            return {"decision": "legacy", "reason_codes": ["empty_query"]}
        contradiction_terms = (
            "don't use personality",
            "do not use personality",
            "ignore personality",
            "override personality",
            "contradicts your personality",
            "stale personality",
            "that preference is wrong",
            "not my preference",
            "forget that preference",
            "current-turn contradiction",
        )
        if any(term in text for term in contradiction_terms):
            return {"decision": "suppress_overlay", "reason_codes": ["current_turn_contradiction", "suppress_dpm"]}
        if _should_inject_dml_memory(query):
            return {"decision": "retrieve", "reason_codes": ["long_horizon_or_resume", "retrieve_dml"]}
        return {"decision": "overlay_only", "reason_codes": ["casual_short_turn", "no_dml_retrieval"]}

    def _active_read_gates(self, query: str, *, session_id: str) -> Dict[str, Any]:
        try:
            decision = self._dcn_policy_decision(query)
            name = str(decision.get("decision") or "").strip().lower()
            if name not in _DCN_DECISIONS:
                raise ValueError(f"unknown DCN decision {name!r}")
            include_dpm = bool(self.enable_personality and name in {"overlay_only", "retrieve", "legacy"})
            retrieve_dml = bool(self.enable_memory and name == "retrieve")
            event = {
                "event": "dcn.active_learn" if self.dcn_mode == "active_learn" else "dcn.active_read",
                "mode": self.dcn_mode,
                "requested_mode": self.dcn_requested_mode,
                "decision": name,
                "query_hash": hashlib.sha256(query.encode("utf-8", errors="ignore")).hexdigest()[:16],
                "query_chars": len(query),
                "session_id": session_id,
                "include_dpm": include_dpm,
                "retrieve_dml": retrieve_dml,
                "reason_codes": list(decision.get("reason_codes") or []),
            }
            if self.dcn_mode == "active_learn":
                event.update({
                    "promotion_id": str(self.dcn_promotion.get("promotion_id") or ""),
                    "checkpoint_id": str(self.dcn_promotion.get("checkpoint_id") or ""),
                    "promotion_gate": self.dcn_promotion_gate_reason,
                })
            self._record_dcn_event(event)
            logger.info("Daystrom DCN active-read: %s", json.dumps(event, sort_keys=True))
            return {"fallback": False, "include_dpm": include_dpm, "retrieve_dml": retrieve_dml, "event": event}
        except Exception as exc:
            should_inject_memory = _should_inject_dml_memory(query)
            event = {
                "event": "dcn.active_read_fallback",
                "mode": self.dcn_mode,
                "requested_mode": self.dcn_requested_mode,
                "fallback": True,
                "reason": exc.__class__.__name__,
                "query_hash": hashlib.sha256(query.encode("utf-8", errors="ignore")).hexdigest()[:16],
                "query_chars": len(query),
                "session_id": session_id,
                "include_dpm": bool(self.enable_personality),
                "retrieve_dml": bool(self.enable_memory and should_inject_memory),
            }
            self._record_dcn_event(event)
            logger.warning("Daystrom DCN active-read fallback: %s", json.dumps(event, sort_keys=True))
            return {"fallback": True, "include_dpm": event["include_dpm"], "retrieve_dml": event["retrieve_dml"], "event": event}

    def dcn_observations(self) -> List[Dict[str, Any]]:
        return list(self._dcn_observations)

    def decide_iteration_extension(self, run_state: Dict[str, Any]) -> Dict[str, Any]:
        """Use DML/DCN recall context to decide whether Hermes should grant more turns."""
        if not self.enable_memory:
            return {"decision": "deny", "reason_codes": ["dml_memory_disabled"]}
        text_parts = [
            str(run_state.get("user_message") or ""),
            str(run_state.get("recent_text") or ""),
            str(run_state.get("prefetch_context") or ""),
            str(run_state.get("last_assistant_text") or ""),
        ]
        combined = _clean_text("\n".join(text_parts), 8000)
        incomplete_combined = re.sub(r"\bno [^.?!\n]{0,80}\bpending\b", "", combined, flags=re.IGNORECASE)
        if _ITERATION_NOISE_RE.search(combined):
            return {"decision": "deny", "reason_codes": ["dcn_noise_or_loop_signal"], "source": "daystrom_dml"}
        if _ITERATION_COMPLETE_RE.search(combined) and not _ITERATION_INCOMPLETE_RE.search(incomplete_combined):
            return {"decision": "deny", "reason_codes": ["dcn_completion_signal"], "source": "daystrom_dml"}
        recent_work = int(run_state.get("recent_tool_calls") or 0) + int(run_state.get("recent_tool_results") or 0)
        if recent_work <= 0:
            return {"decision": "deny", "reason_codes": ["dcn_no_recent_tool_work"], "source": "daystrom_dml"}

        recall_text = ""
        try:
            query = _clean_text(
                "iteration budget exhausted; decide if current Hermes task is incomplete or noisy: "
                + str(run_state.get("user_message") or "")
                + " "
                + str(run_state.get("recent_text") or ""),
                1200,
            )
            payload = self._run_cli([
                "retrieve",
                "--query", query,
                "--top-k", "4",
                "--tenant-id", self.tenant_id,
                "--session-id", str(run_state.get("session_id") or self._session_id),
                "--ground-truth-policy", "never",
                "--no-reform-memory",
            ], timeout=min(self.timeout, 6))
            recall_text = _clean_text(str(payload.get("raw_context") or ""), 4000)
        except Exception as exc:
            logger.debug("Daystrom DML iteration-extension recall failed: %s", exc)

        evidence = _clean_text(combined + "\n" + recall_text, 10000)
        incomplete_evidence = re.sub(r"\bno [^.?!\n]{0,80}\bpending\b", "", evidence, flags=re.IGNORECASE)
        if _ITERATION_NOISE_RE.search(evidence):
            return {"decision": "deny", "reason_codes": ["dcn_recall_noise_or_loop_signal"], "source": "daystrom_dml"}
        if _ITERATION_INCOMPLETE_RE.search(incomplete_evidence):
            return {
                "decision": "grant",
                "extend_by": 30,
                "reason_codes": ["dcn_incomplete_signal", "dml_recall_checked", "recent_tool_work"],
                "source": "daystrom_dml",
            }
        return {"decision": "deny", "reason_codes": ["dcn_insufficient_incomplete_signal"], "source": "daystrom_dml"}

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        query = _clean_text(query, 1200)
        if not query:
            return ""
        effective_session = self._session_id or session_id or "snips2-hermes-default"
        if self.dcn_mode in {"active_read", "active_learn"}:
            gates = self._active_read_gates(query, session_id=effective_session)
            include_dpm = bool(gates.get("include_dpm"))
            retrieve_dml = bool(gates.get("retrieve_dml"))
        else:
            should_inject_memory = _should_inject_dml_memory(query)
            self._observe_dcn(query, session_id=effective_session, should_inject_memory=should_inject_memory)
            include_dpm = bool(self.enable_personality)
            retrieve_dml = bool(self.enable_memory and should_inject_memory)
        if self.enable_memory and self.retrieval_policy in {"always", "full", "eager", "ungated"}:
            retrieve_dml = True
        blocks: List[str] = []
        if include_dpm:
            overlay = self._personality_overlay(query)
            block = self._format_personality_overlay(overlay)
            if block:
                blocks.append(block)
        if retrieve_dml:
            # Resume preserves active continuity even when retrieval confidence is low.
            resume_block = self._resume_block(effective_session)
            if resume_block:
                blocks.append(resume_block)
            retrieve_block = self._retrieve_block(query, effective_session)
            if retrieve_block:
                blocks.append(retrieve_block)
        return self._fit(_dedupe_memory_blocks(blocks), self.max_context_chars)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if not self.sync_turns:
            return
        user = _clean_text(user_content, 1800)
        assistant = _clean_text(assistant_content, 2200)
        if not user or not assistant:
            return
        hygiene = _classify_turn_memory(user_content, assistant_content)
        if not hygiene.get("keep"):
            logger.debug("Daystrom DML sync_turn skipped low-value turn: %s", hygiene)
            return
        summary = str(hygiene.get("summary") or "").strip()
        key = f"{hash(summary)}:{hygiene.get('memory_class')}"
        with self._lock:
            if key == self._last_sync_key:
                return
            self._last_sync_key = key
        effective_session = self._session_id or session_id or "snips2-hermes-default"
        text = f"Citizen Snips durable turn memory. {summary}"
        meta = {
            "source": "hermes-memory-provider",
            "phase": "completed-turn",
            "provider": "daystrom_dml",
            "memory_class": hygiene.get("memory_class"),
            "hygiene_score": hygiene.get("score"),
            "hygiene_reasons": hygiene.get("reasons"),
            "summary_source": "hermes_daystrom_hygiene_v1",
        }
        if self._thread_id:
            meta["thread_id"] = self._thread_id
        if self._chat_id:
            meta["chat_id"] = self._chat_id
        try:
            self._run_cli([
                "ingest",
                "--kind", "observation",
                "--session-id", effective_session,
                "--tenant-id", self.tenant_id,
                "--client-id", self.client_id,
                "--summary-policy", "cheap",
                "--filter-noise",
                "--meta", json.dumps(meta, separators=(",", ":")),
                "--text", text,
            ], timeout=self.timeout)
            dpm_text = _dpm_preference_text(user_content, hygiene)
            evo_meta = {
                "source": "hermes-dpm-evolution-sync",
                "channel": "discord" if self._chat_id else "hermes",
                "thread_id": self._thread_id,
                "chat_id": self._chat_id,
                "task_type": hygiene.get("memory_class"),
                "feedback_source": "turn_heuristic",
            }
            self._run_cli([
                "dpm-observe",
                "--source-id", "hermes-dpm-evolution-sync",
                "--meta", json.dumps(evo_meta, separators=(",", ":")),
                "--prompt", user,
                "--response", assistant,
            ], timeout=self.timeout)
            if dpm_text:
                dpm_meta = {
                    **meta,
                    "source": "hermes-dpm-sync-turn",
                    "dpm_preference": True,
                    "dpm_scope": "relationship",
                    "memory_class": "preference",
                    "summary_source": "hermes_daystrom_dpm_sync_v1",
                }
                self._run_cli([
                    "ingest",
                    "--kind", "preference",
                    "--session-id", effective_session,
                    "--tenant-id", self.tenant_id,
                    "--client-id", self.client_id,
                    "--summary-policy", "cheap",
                    "--no-filter-noise",
                    "--meta", json.dumps(dpm_meta, separators=(",", ":")),
                    "--text", dpm_text,
                ], timeout=self.timeout)
        except Exception as exc:
            logger.debug("Daystrom DML sync_turn failed: %s", exc)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        # Retrieval is synchronous and capped by timeout in prefetch(); no background worker yet.
        return None

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        if not self.sync_turns:
            return ""
        try:
            state, task, next_action = _continuity_state_from_messages(messages)
            if not state:
                return ""
            self._run_cli([
                "handoff",
                "--thread", self._thread_id or self._session_id or "snips2-hermes",
                "--state", state,
                "--task", task,
                "--next-action", next_action,
                "--session-id", self._session_id or "snips2-hermes-default",
                "--tenant-id", self.tenant_id,
                "--client-id", self.client_id,
            ], timeout=self.timeout)
            return (
                "## Daystrom DML Continuity Checkpoint\n"
                "Durable state was written to the memory lattice before context compaction.\n\n"
                f"## Active Task\n{task}\n\n"
                f"## Active State\n{state}\n\n"
                f"## Remaining Work\n{next_action}\n\n"
                "## Memory Policy\nUse DML recall/resume as the continuity source; do not treat this checkpoint as new user input. "
                "Do not preserve summarizer wrappers such as 'Here is a summary' as memory."
            )
        except Exception:
            return ""

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def _run_cli(self, args: List[str], *, timeout: Optional[float] = None) -> Dict[str, Any]:
        cmd = [str(self.launcher)]
        if self.no_require_gpu:
            cmd.append("--no-require-gpu")
        cmd.extend(["--storage-dir", str(self.store_dir), "--config-path", str(self.config_path)])
        cmd.extend(args)
        env = os.environ.copy()
        env.setdefault("DAYSTROM_DPM_RELATIONSHIP_ID", self._relationship_id)
        env.setdefault("DAYSTROM_DPM_PROJECT_ID", self._project_id)
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout or self.timeout,
            env=env,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"DML command failed rc={proc.returncode}: {proc.stderr.strip() or proc.stdout[:500]}")
        return self._parse_json_output(proc.stdout)

    @staticmethod
    def _parse_json_output(stdout: str) -> Dict[str, Any]:
        text = stdout or ""
        start = text.find("{")
        if start < 0:
            return {"raw": text}
        return json.loads(text[start:])

    def _resume_block(self, session_id: str) -> str:
        try:
            data = self._run_cli(["resume", "--session-id", session_id, "--tenant-id", self.tenant_id], timeout=self.timeout)
        except Exception:
            return ""
        raw = self._safe_context_from_payload(data)
        if not raw:
            return ""
        return "=== Daystrom DML Active Continuity ===\n" + self._fit(raw, 900)

    def _retrieve_block(self, query: str, session_id: str) -> str:
        try:
            data = self._run_cli([
                "retrieve",
                "--query", query,
                "--session-id", session_id,
                "--tenant-id", self.tenant_id,
                "--top-k", str(self.top_k),
                "--ground-truth-policy", "never",
                "--no-reform-memory",
            ], timeout=self.timeout)
        except Exception:
            return ""
        raw = self._safe_context_from_payload(data)
        if not raw:
            return ""
        return "=== Daystrom DML Retrieved Memory ===\n" + self._fit(raw, 1400)

    def _safe_context_from_payload(self, data: Dict[str, Any]) -> str:
        candidates: List[str] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            if meta.get("memory_state") == "quarantined":
                continue
            text = str(meta.get("summary") or item.get("summary") or item.get("text") or "")
            candidates.extend(_semantic_memory_bullets(text, meta, limit=self.top_k))
        if not candidates:
            raw = str(data.get("raw_context") or "")
            candidates.extend(_semantic_memory_bullets(raw, limit=self.top_k))
            if not candidates:
                for line in raw.splitlines():
                    candidates.extend(_semantic_memory_bullets(line, limit=self.top_k))
        result = _dedupe_semantic_bullets(candidates, limit=self.top_k)
        return "\n".join(result)

    def _personality_overlay(self, prompt: str) -> Optional[Dict[str, Any]]:
        if not (self.venv_python.exists() and self.source_dir.exists()):
            return None
        code = r'''
import json, os, sys
from pathlib import Path
source = Path(os.environ["DAYSTROM_DML_SOURCE"])
sys.path.insert(0, str(source / "dml_core"))
from daystrom_dml.dml_adapter import DMLAdapter
adapter = DMLAdapter(
    config_path=os.environ.get("DAYSTROM_DML_CONFIG"),
    config_overrides={
        "storage_dir": os.environ["DAYSTROM_DML_STORE"],
        "dml.agentic_mode.enabled": True,
        "strict_llm_required": False,
        "dpm": {
            "enable": True,
            "mode": os.environ.get("DAYSTROM_DPM_MODE", "active-write"),
            "preference_graph_path": str(Path(os.environ["DAYSTROM_DML_STORE"]) / "dpm_preference_graph.json"),
            "relationship_id": os.environ.get("DAYSTROM_DPM_RELATIONSHIP_ID", "relationship:mark-snips2"),
            "project_id": os.environ.get("DAYSTROM_DPM_PROJECT_ID", "project:snips2"),
            "token_budget": int(os.environ.get("DAYSTROM_DPM_TOKEN_BUDGET", "80")),
        },
    },
)
overlay = adapter.personality_overlay(
    prompt=os.environ.get("DAYSTROM_DML_PROMPT", ""),
    thread_id=os.environ.get("DAYSTROM_DML_THREAD_ID") or None,
    relationship_id=os.environ.get("DAYSTROM_DPM_RELATIONSHIP_ID", "relationship:mark-snips2"),
    project_id=os.environ.get("DAYSTROM_DPM_PROJECT_ID", "project:snips2"),
)
print(json.dumps(overlay or {}))
'''
        env = os.environ.copy()
        env.update({
            "DAYSTROM_DML_SOURCE": str(self.source_dir),
            "DAYSTROM_DML_STORE": str(self.store_dir),
            "DAYSTROM_DML_CONFIG": str(self.config_path),
            "DAYSTROM_DML_PROMPT": prompt,
            "DAYSTROM_DML_THREAD_ID": self._thread_id,
            "DAYSTROM_DPM_RELATIONSHIP_ID": self._relationship_id,
            "DAYSTROM_DPM_PROJECT_ID": self._project_id,
        })
        try:
            proc = subprocess.run(
                [str(self.venv_python), "-c", code],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=min(self.timeout, 12),
                env=env,
            )
            if proc.returncode != 0:
                logger.debug("Daystrom DPM overlay failed: %s", proc.stderr.strip())
                return None
            start = proc.stdout.find("{")
            if start < 0:
                return None
            data = json.loads(proc.stdout[start:])
            return data if isinstance(data, dict) and data else None
        except Exception as exc:
            logger.debug("Daystrom DPM overlay exception: %s", exc)
            return None

    def _format_personality_overlay(self, overlay: Optional[Dict[str, Any]]) -> str:
        if not overlay:
            return ""
        body = overlay.get("overlay") if isinstance(overlay.get("overlay"), dict) else {}
        rendered = str(body.get("rendered_text") or body.get("persona_summary") or "").strip()
        directives = body.get("style_directives") if isinstance(body.get("style_directives"), list) else []
        do_not = body.get("do_not_do") if isinstance(body.get("do_not_do"), list) else []
        lines = ["=== Daystrom Personality Matrix Overlay ==="]
        if rendered:
            lines.append(rendered)
        for directive in directives[:4]:
            if directive and str(directive) not in rendered:
                lines.append(f"- {directive}")
        for item in do_not[:3]:
            lines.append(f"- Constraint: {item}")
        lines.append("- Constraint: Explicit current-turn user instructions override this overlay.")
        return self._fit("\n".join(lines), 1200)

    @staticmethod
    def _fit(text: str, limit: int) -> str:
        fitted = _fit_sentence_boundary(text, limit)
        if len((text or "").strip()) <= limit:
            return fitted
        return fitted + "\n... [truncated]"


def register(ctx) -> None:
    ctx.register_memory_provider(DaystromDMLProvider())
