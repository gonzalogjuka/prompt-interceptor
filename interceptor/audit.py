"""Post-hoc audit: did the final answer of a turn meet the success criteria?

Runs in the background after the answer has been streamed to the user, so it
never adds latency. The result is logged (and written to metrics) as a signal
for tuning and benchmarking; it does not retry or alter the answer.
"""
from __future__ import annotations

from typing import Any

import httpx

MAX_ANSWER_CHARS = 20000
TOOL_NAME = "submit_audit"
SYSTEM_PROMPT = """You check whether an AI assistant's final answer meets a list of success criteria.
Judge each criterion strictly from the answer text: met=true only if the answer clearly satisfies it.
Keep each note under 15 words, in the same language as the criteria. Call the submit_audit tool."""
TOOL = {
    "name": TOOL_NAME,
    "description": "Submit the per-criterion verdict.",
    "input_schema": {
        "type": "object",
        "properties": {"results": {"type": "array", "items": {"type": "object", "properties": {
            "criterion": {"type": "string"}, "met": {"type": "boolean"}, "note": {"type": "string"}},
            "required": ["criterion", "met", "note"]}}},
        "required": ["results"],
    },
}


async def audit(
    client: httpx.AsyncClient,
    *,
    upstream_url: str,
    headers: dict[str, str],
    model: str,
    criteria: list[str],
    answer: str,
    timeout: float | httpx.Timeout,
) -> list[dict[str, Any]]:
    numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(criteria))
    resp = await client.post(
        f"{upstream_url}/v1/messages",
        headers=headers,
        json={
            "model": model,
            "max_tokens": 1000,
            "system": SYSTEM_PROMPT,
            "tools": [TOOL],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
            "messages": [{"role": "user", "content":
                          f"<criteria>\n{numbered}\n</criteria>\n<answer>\n{answer[:MAX_ANSWER_CHARS]}\n</answer>"}],
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    for block in resp.json().get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
            return list((block.get("input") or {}).get("results") or [])
    return []
