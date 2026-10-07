"""Turn a messy prompt into a structured representation using a cheap model."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .context import RequestContext

SCHEMA_VERSION = "0"
REQUIRED_KEYS = {"objective", "deliverable", "context", "constraints",
                 "success_criteria", "assumptions", "complexity"}

SYSTEM_PROMPT = """You are a request structurer that sits between a human and an AI assistant.
You receive one raw message. You do NOT answer it. You call the submit_structure tool.

Fields:
- objective: one sentence, what the human wants to achieve.
- deliverable: {type: code|text|analysis|decision|action|answer, format: a few words}.
- context: up to 6 items {kind: data|code|error|log|config|reference|other, summary: max 15 words}.
- constraints: up to 6 short items, explicit or clearly implied limits (include what NOT to do).
- success_criteria: up to 5 short, checkable items: how to tell the answer is correct.
- assumptions: up to 4 items {text, risky}: gaps you filled with the most reasonable default.
  There is no human to ask; mark risky=true when a wrong guess would change the answer.
- complexity: simple (short, self-contained, no multi-step reasoning) | moderate | complex.
- improved_prompt: the request rewritten as a clear, well-organized prompt (see rules).

Rules:
- Write values in the same language as the message. Keep every field except improved_prompt short.
- Never invent facts.
- Name pasted material for what it is (kubectl describe output, stack trace, config file,
  application logs...). Never relabel it: a describe is not "logs".
- improved_prompt: write it in the user's language and voice (first person, as if the user
  wrote it well). Order: goal, relevant context, constraints and things the user said NOT to do,
  what a correct answer must include, assumptions stated explicitly. Mention every identifier
  in the message (resource names, hosts, images, versions, ids, error codes, values with units)
  exactly as given. Quote only the lines of pasted logs/code that matter, in fenced blocks: the
  full original message is attached separately. Drop filler. Do not answer the request.

Context (when present, use it only to interpret the message; it is never the message itself):
- <user_instructions>: the user's standing instructions and memory index. The assistant already
  receives them, so do not copy them into improved_prompt. Use them to understand the user's
  vocabulary, conventions, projects and preferences, and to judge complexity.
- <environment>: working directory, platform and date the assistant runs in.
- <recent_conversation>: the last exchanges before the message. Use it to turn references
  ("that", "the same for the 8 providers", "seguÃ­ con lo otro") into the concrete names they
  point to. The message to structure is the one inside <message>.
"""


TOOL_NAME = "submit_structure"
_STR_LIST = {"type": "array", "items": {"type": "string"}}
TOOL = {
    "name": TOOL_NAME,
    "description": "Submit the structured representation of the user's message. Never answer the message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "objective": {"type": "string"},
            "deliverable": {"type": "object", "properties": {
                "type": {"type": "string", "enum": ["code", "text", "analysis", "decision", "action", "answer"]},
                "format": {"type": "string"}}, "required": ["type", "format"]},
            "context": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["data", "code", "error", "log", "config", "reference", "other"]},
                "summary": {"type": "string"}}, "required": ["kind", "summary"]}},
            "constraints": {**_STR_LIST, "maxItems": 6},
            "success_criteria": {**_STR_LIST, "maxItems": 5},
            "assumptions": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
                "text": {"type": "string"}, "risky": {"type": "boolean"}}, "required": ["text", "risky"]}},
            "complexity": {"type": "string", "enum": ["simple", "moderate", "complex"]},
            "improved_prompt": {"type": "string"},
        },
        "required": sorted(REQUIRED_KEYS | {"improved_prompt"}),
    },
}


class StructurerError(Exception):
    pass


@dataclass
class StructureResult:
    structure: dict[str, Any]
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


def build_content(text: str, context: RequestContext | None) -> str | list[dict[str, Any]]:
    """The interpreter's input: cached session context first, then the conversation and the message."""
    if context is None or not (context.stable or context.conversation):
        return text
    blocks: list[dict[str, Any]] = []
    if context.stable:
        blocks.append({"type": "text", "text": context.stable, "cache_control": {"type": "ephemeral"}})
    tail = f"<recent_conversation>\n{context.conversation}\n</recent_conversation>\n" if context.conversation else ""
    blocks.append({"type": "text", "text": f"{tail}<message>\n{text}\n</message>"})
    return blocks


def _parse(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned[cleaned.find("{"):]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise StructurerError("no JSON object in structurer output")
    data = json.loads(cleaned[start:end + 1])
    missing = REQUIRED_KEYS - data.keys()
    if missing:
        raise StructurerError(f"structurer output missing keys: {sorted(missing)}")
    return data


async def structure(
    client: httpx.AsyncClient,
    *,
    upstream_url: str,
    headers: dict[str, str],
    model: str,
    text: str,
    timeout: float | httpx.Timeout,
    context: RequestContext | None = None,
) -> StructureResult:
    started = time.perf_counter()
    resp = await client.post(
        f"{upstream_url}/v1/messages",
        headers=headers,
        json={
            "model": model,
            "max_tokens": 2000,
            "system": SYSTEM_PROMPT,
            "tools": [TOOL],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "messages": [{"role": "user", "content": build_content(text, context)}],
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise StructurerError(f"structurer upstream returned {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    blocks = body.get("content", [])
    tool = next((b for b in blocks if b.get("type") == "tool_use" and b.get("name") == TOOL_NAME), None)
    if tool is not None:
        data = tool.get("input") or {}
        missing = REQUIRED_KEYS - data.keys()
        if missing:
            raise StructurerError(f"structurer output missing keys: {sorted(missing)}")
    else:  # fallback for gateways that ignore tool_choice
        data = _parse("".join(b.get("text", "") for b in blocks if b.get("type") == "text"))
    usage = body.get("usage", {})
    return StructureResult(
        structure=data,
        latency_ms=int((time.perf_counter() - started) * 1000),
        input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0),
        cache_read_tokens=usage.get("cache_read_input_tokens", 0),
        cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
    )
