"""Replace or extend the human text of a user message with the interceptor output.

Clients resend the full history on every request but never see our changes.
If we only modified the latest message, the improvement would vanish from
history on the next turn and the prompt-cache prefix would change. So every
processed message is remembered by hash and rebuilt identically wherever it
appears again.
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any

from .gate import MARKER_TAG, SYSTEM_REMINDER_RE, human_text
from .structurer import SCHEMA_VERSION

ORIGINAL_TAG = "interceptor_original"


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_block(structure: dict[str, Any]) -> str:
    payload = {k: v for k, v in structure.items() if k != "improved_prompt"}
    return (
        f'<{MARKER_TAG} version="{SCHEMA_VERSION}">\n'
        "Auto-generated structure of the message above. The original text wins on any "
        "contradiction. Surface risky assumptions to the user in your answer.\n"
        f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n</{MARKER_TAG}>"
    )


def render_original(text: str) -> str:
    return (
        f'<{ORIGINAL_TAG} version="{SCHEMA_VERSION}">\n'
        "The request above was rewritten from this message, exactly as the user typed it. "
        "It is the source of truth: if the rewrite contradicts it or drops a detail, follow this.\n"
        f"{text}\n</{ORIGINAL_TAG}>"
    )


def build_texts(mode: str, structure: dict[str, Any], original: str) -> tuple[str, ...]:
    """Text blocks that replace the human text of the message."""
    improved = structure.get("improved_prompt")
    if mode == "rewrite" and isinstance(improved, str) and improved.strip():
        return (improved.strip(), render_original(original))
    return (original, render_block(structure))


def apply_texts(message: dict[str, Any], texts: tuple[str, ...]) -> None:
    """Swap the human-typed text for `texts`, keeping images, system reminders
    and any cache_control breakpoint that was on the replaced text."""
    content = message.get("content")
    kept: list[dict[str, Any]] = []
    cache_control = None
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            kept += [{"type": "text", "text": r} for r in SYSTEM_REMINDER_RE.findall(b.get("text", ""))]
            cache_control = b.get("cache_control", cache_control)
        else:
            kept.append(b)
    new = [{"type": "text", "text": t} for t in texts]
    if cache_control is not None:
        new[-1]["cache_control"] = cache_control
    message["content"] = kept + new


class StructureCache:
    def __init__(self, max_size: int) -> None:
        self._data: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        self._max = max_size

    def get(self, key: str) -> tuple[str, ...] | None:
        value = self._data.get(key)
        if value is not None:
            self._data.move_to_end(key)
        return value

    def put(self, key: str, value: tuple[str, ...]) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)


def reapply_history(messages: list[dict[str, Any]], cache: StructureCache) -> int:
    """Rebuild previously processed user messages. Returns count applied."""
    applied = 0
    for msg in messages:
        text = human_text(msg)
        if text is None:
            continue
        texts = cache.get(text_key(text))
        if texts:
            apply_texts(msg, texts)
            applied += 1
    return applied
