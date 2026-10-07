"""Decide whether a user message is worth structuring.

Structuring costs a model call, so trivial prompts ("thanks", "run the tests")
must pass through untouched. The gate is deterministic and cheap.
"""
from __future__ import annotations

import re
from typing import Any

MARKER_TAG = "interceptor_structure"
MARKER_PREFIX = "<interceptor_"  # any block we generated
BYPASS_TOKEN = "#raw"

# Text the client itself puts in the user role: interruption markers, slash-command echoes,
# the prompt-suggestion side task (it carries tools, so the no-tools [side] check misses it).
CLIENT_MARKER_RE = re.compile(
    r"^\[Request (interrupted|cancelled)[^\]]*\]$|^<(command-name|command-message|local-command-\w+)>"
    r"|^\[SUGGESTION MODE:",
    re.IGNORECASE,
)
SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
# Messages another Claude session sends (and the client's delivery notices): not typed by the
# human, so rewriting them changes what a peer said. They keep the turn's model, untouched.
CROSS_SESSION_RE = re.compile(r"<cross-session-message\b|^\[Cross-session delivery notice\]", re.M)
_SIGNALS: dict[str, re.Pattern[str]] = {
    "code_block": re.compile(r"```"),
    "stack_trace": re.compile(r"Traceback \(most recent call last\)|^\s+at \S+\(.*\)$|Exception:|panic:", re.M),
    "error_line": re.compile(r"\b(error|failed|fatal|denied|refused|timeout)\b", re.I),
    "log_lines": re.compile(r"^\S*\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", re.M),
    "structured_data": re.compile(r"^\s*[\[{]|^\s*[\w.-]+:\s+\S", re.M),
}


def inspect(message: dict[str, Any]) -> tuple[str | None, str]:
    """Return (human_text, reason). human_text is None when this message is not a
    human turn worth looking at; reason says why, for logging."""
    if message.get("role") != "user":
        return None, f"last_role={message.get('role')}"
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None, "tool_result_turn"
        text = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        return None, f"content_type={type(content).__name__}"
    # strip client-injected context BEFORE looking for our markers: a reminder that
    # happens to mention them must not hide the human text
    text = SYSTEM_REMINDER_RE.sub("", text).strip()
    if MARKER_PREFIX in text:
        return None, "already_processed"
    if not text:
        return None, "no_human_text"
    if CLIENT_MARKER_RE.search(text):
        return None, "client_marker"
    if CROSS_SESSION_RE.search(text):
        return None, "cross_session"
    return text, "ok"


def preview(text: str, n: int = 60) -> str:
    """One-line excerpt for logs."""
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[: n - 1] + "…"


def find_target(messages: list[dict[str, Any]]) -> int | None:
    """Index of the message to inspect: the last one, skipping trailing role=system
    entries some clients (e.g. Claude Code) append after the user's turn."""
    i = len(messages) - 1
    while i >= 0 and messages[i].get("role") == "system":
        i -= 1
    return i if i >= 0 else None


# Pure continuations: the work is whatever the previous turn was about.
_CONTINUATION_RE = re.compile(
    r"^(ok(ey|a|i)?|dale|si|sí|yes|yep|listo|perfecto|genial|joya|bien|de una|gracias|thanks|"
    r"segu[ií]|continu[aá]|continue|go( on)?|proceed|hacelo|metele|aplicalo|avanz[aá]|adelante|"
    r"hace(lo)? nomas|do it)\b[\s!.,]*(y?\s*(segu[ií]|continu[aá]|hacelo|avanz[aá]|metele|dale))?[\s!.,]*$",
    re.IGNORECASE,
)


def is_continuation(text: str) -> bool:
    """True for short acknowledgments that just tell the assistant to go on."""
    return len(text) <= 40 and bool(_CONTINUATION_RE.match(text.strip()))


def human_texts(messages: list[dict[str, Any]]) -> list[str]:
    """Texts of all human-typed user messages, in order (tool results and markers excluded)."""
    out = []
    for m in messages:
        if m.get("role") == "user":
            text, _ = inspect(m)
            if text:
                out.append(text)
    return out


def previous_assistant_text(messages: list[dict[str, Any]], before: int) -> str:
    """Text of the last assistant message before index `before` (for classifier context)."""
    for m in reversed(messages[:before]):
        if m.get("role") == "assistant":
            content = m.get("content")
            if isinstance(content, str):
                return content
            return "\n".join(b.get("text", "") for b in content or []
                             if isinstance(b, dict) and b.get("type") == "text")
    return ""


def last_human(messages: list[dict[str, Any]]) -> str | None:
    """Text of the most recent human-typed user message (skips tool results and
    system entries). Used as the anchor of the current turn."""
    for m in reversed(messages):
        if m.get("role") == "user":
            text, _ = inspect(m)
            if text:
                return text
    return None


def human_text(message: dict[str, Any]) -> str | None:
    return inspect(message)[0]


def shape(message: dict[str, Any] | None) -> str:
    """Compact description of a message for logs: role[type:chars,...]."""
    if not message:
        return "none"
    content = message.get("content")
    if isinstance(content, str):
        return f"{message.get('role')}[str:{len(content)}]"
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            parts.append("?")
        elif b.get("type") == "text":
            t = b.get("text", "")
            tag = "reminder" if t.lstrip().startswith("<system-reminder>") else "text"
            parts.append(f"{tag}:{len(t)}")
        else:
            parts.append(str(b.get("type")))
    return f"{message.get('role')}[{', '.join(parts)}]"


def should_structure(text: str, min_chars: int) -> tuple[bool, list[str]]:
    """Return (decision, reasons)."""
    if BYPASS_TOKEN in text:
        return False, ["bypass_token"]
    reasons = [name for name, rx in _SIGNALS.items() if rx.search(text)]
    if len(text) >= min_chars:
        reasons.insert(0, "length")
    elif len(reasons) < 2:
        return False, reasons or ["too_short"]
    return True, reasons
