"""Lightweight complexity classifier for routing.

Used for messages too short to be worth rewriting: we still want to know how
demanding they are, so the right model answers. The structurer already does
this for long messages; this is the cheap path (tiny output, small prompt).
The end of the previous assistant message is included so that requests like
"fix that" are judged by what they refer to, not by their length.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .context import RequestContext

MAX_CONTEXT_CHARS = 1500
TOOL_NAME = "submit_classification"
SYSTEM_PROMPT = """You classify how demanding the user's latest request is for an AI assistant, so it can be
sent to the right model. You do NOT answer it. The previous assistant message is given only to understand
what the request refers to ("do it", "fix that", "why?").

complexity:
- simple: factual or definition question, short explanation, small lookup, trivial edit or formatting.
- moderate: some reasoning, a self-contained piece of code or config, single-step debugging with the info given.
- complex: multi-step reasoning, design or architecture, debugging across components, large or risky changes,
  long agentic work.
Judge the work the request implies, not the length of the message. When <user_instructions> and
<environment> are given, use them to understand the user's projects and conventions (what a step really
involves for this user); they are context, not the request. Call submit_classification."""
TOOL = {
    "name": TOOL_NAME,
    "description": "Submit the classification of the user's latest request.",
    "input_schema": {
        "type": "object",
        "properties": {
            "complexity": {"type": "string", "enum": ["simple", "moderate", "complex"]},
            "deliverable": {"type": "string", "enum": ["code", "text", "analysis", "decision", "action", "answer"]},
            "reason": {"type": "string"},
        },
        "required": ["complexity", "deliverable", "reason"],
    },
}


class ClassifyError(Exception):
    pass


@dataclass
class ClassifyResult:
    complexity: str
    deliverable: str
    reason: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def structure(self) -> dict[str, Any]:
        """Same shape the router reads from the full structurer."""
        return {"complexity": self.complexity, "deliverable": {"type": self.deliverable}, "context": []}


async def classify(
    client: httpx.AsyncClient,
    *,
    upstream_url: str,
    headers: dict[str, str],
    model: str,
    text: str,
    previous_answer: str,
    timeout: float | httpx.Timeout,
    context: RequestContext | None = None,
) -> ClassifyResult:
    started = time.perf_counter()
    previous = previous_answer[-MAX_CONTEXT_CHARS:].strip()
    request = (f"<previous_assistant_message>\n{previous}\n</previous_assistant_message>\n" if previous else "")
    request += f"<request>\n{text}\n</request>"
    content: str | list[dict[str, Any]] = request
    if context is not None and context.stable:
        # same session context as the structurer, cached; the request itself goes after it
        content = [{"type": "text", "text": context.stable, "cache_control": {"type": "ephemeral"}},
                   {"type": "text", "text": request}]
    resp = await client.post(
        f"{upstream_url}/v1/messages",
        headers=headers,
        json={
            "model": model,
            "max_tokens": 200,
            "system": SYSTEM_PROMPT,
            "tools": [TOOL],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "messages": [{"role": "user", "content": content}],
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise ClassifyError(f"classifier upstream returned {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    tool = next((b for b in body.get("content", [])
                 if b.get("type") == "tool_use" and b.get("name") == TOOL_NAME), None)
    data = (tool or {}).get("input") or {}
    if data.get("complexity") not in ("simple", "moderate", "complex"):
        raise ClassifyError("classifier returned no valid complexity")
    usage = body.get("usage", {})
    return ClassifyResult(
        complexity=data["complexity"],
        deliverable=data.get("deliverable", "answer"),
        reason=str(data.get("reason", ""))[:200],
        latency_ms=int((time.perf_counter() - started) * 1000),
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
    )
