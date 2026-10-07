"""Shared fixtures: a simulated Anthropic API and helpers to drive the interceptor against it.

No test touches the network or needs an API key.
"""
import json

import httpx
from fastapi.testclient import TestClient

from interceptor.app import create_app
from interceptor.config import Config

STRUCTURE = {
    "objective": "Arreglar el pod que no arranca",
    "deliverable": {"type": "analysis", "format": "causa y fix"},
    "context": [{"id": "c1", "kind": "error", "summary": "CrashLoopBackOff", "key_facts": ["exit code 137"]}],
    "constraints": [], "success_criteria": ["el pod queda Running"],
    "assumptions": [{"text": "cluster AKS", "risky": True}],
    "complexity": "moderate",
}
LONG_PROMPT = "mi pod no levanta, mirá esto:\n```\n" + "2026-10-02T10:00:00 error: OOMKilled exit code 137\n" * 20 + "```"
IMPROVED = "Necesito diagnosticar un pod en CrashLoopBackOff (OOMKilled, exit 137). No ejecutes nada."
TOOLS = [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}]

HAIKU, SONNET, OPUS = "claude-haiku-4-5-20251001", "claude-sonnet-5-5", "claude-opus-5-5"
TIERS = [{"name": "simple", "when": {"complexity": ["simple"]}, "model": "haiku"},
         {"name": "moderado", "when": {"complexity": ["moderate"]}, "model": "sonnet"},
         {"name": "complejo", "when": {"complexity": ["complex"]}, "model": "opus"}]

_STRUCTURER, _CLASSIFIER, _AUDITOR = "You are a request structurer", "You classify", "You check whether"


def _system(body):
    return str(body.get("system", ""))


class Upstream:
    """Simulated API. Answers the interpreter calls (structurer, classifier, auditor) and the main model."""

    def __init__(self, structure=STRUCTURE, structurer_status=200, as_tool=True, reject_models=(),
                 classification=None, classifier_status=200, reject_effort=None):
        self.structure = structure
        self.structurer_status = structurer_status
        self.as_tool = as_tool
        self.reject_models = set(reject_models)
        self.reject_effort = reject_effort
        self.classification = classification or {"complexity": "simple", "deliverable": "answer",
                                                 "reason": "pregunta corta"}
        self.classifier_status = classifier_status
        self.calls, self.classified, self.audits = [], [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append({"path": request.url.path, "body": body, "headers": dict(request.headers),
                           "timeout": request.extensions.get("timeout")})
        system = _system(body)
        if system.startswith(_CLASSIFIER):
            self.classified.append(body)
            if self.classifier_status != 200:
                return httpx.Response(self.classifier_status, json={"error": "boom"})
            return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "c1", "name": "submit_classification",
                                                          "input": self.classification}],
                                             "usage": {"input_tokens": 300, "output_tokens": 30}})
        if system.startswith(_AUDITOR):
            self.audits.append(body)
            results = [{"criterion": "el pod queda Running", "met": False, "note": "no lo menciona"}]
            return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "a1", "name": "submit_audit",
                                                          "input": {"results": results}}]})
        if system.startswith(_STRUCTURER):
            if self.structurer_status != 200:
                return httpx.Response(self.structurer_status, json={"error": "boom"})
            if self.as_tool and body.get("tool_choice", {}).get("name") == "submit_structure":
                content = [{"type": "tool_use", "id": "tu1", "name": "submit_structure", "input": self.structure}]
            else:
                content = [{"type": "text", "text": json.dumps(self.structure)}]
            return httpx.Response(200, json={"content": content,
                                             "usage": {"input_tokens": 500, "output_tokens": 150}})
        effort = (body.get("output_config") or {}).get("effort")
        if body.get("model") in self.reject_models or (self.reject_effort and effort == self.reject_effort):
            return httpx.Response(400, json={"type": "error", "error": {"message": "refused"}})
        if body.get("stream"):
            sse = ('event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":900,"output_tokens":1}}}\n\n'
                   'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Subí el limit "}}\n\n'
                   'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"a 512Mi."}}\n\n'
                   'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":42}}\n\n')
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                                         "usage": {"input_tokens": 900, "output_tokens": 42}})


def make(tmp_path, upstream, **overrides):
    """Interceptor wired to the simulated API, with metrics on and no files from the repo."""
    overrides.setdefault("metrics_enabled", True)
    overrides.setdefault("require_tools", False)
    overrides.setdefault("routes_file", None)
    overrides.setdefault("profile_file", None)
    cfg = Config(upstream_url="https://upstream.test", metrics_path=str(tmp_path / "m.jsonl"), **overrides)
    return TestClient(create_app(cfg, transport=httpx.MockTransport(upstream))), tmp_path / "m.jsonl"


def post(client, messages, model="m", headers=None, **body):
    """POST /v1/messages; `messages` may be a plain string for a single user turn."""
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]
    return client.post("/v1/messages", headers=headers,
                       json={"model": model, "max_tokens": 5, "messages": messages, **body})


def main_calls(up):
    """Requests that reached the main model (not the interpreter's own calls)."""
    return [c for c in up.calls if not _system(c["body"]).startswith((_STRUCTURER, _AUDITOR, _CLASSIFIER))]


def structurer_calls(up):
    return [c for c in up.calls if _system(c["body"]).startswith(_STRUCTURER)]


def metrics(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def routes(tmp_path, rules, **extra):
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"rules": rules, **extra}), encoding="utf-8")
    return str(path)


def tool_loop(turn):
    """The same turn after the model used a tool: what the client sends next."""
    return turn + [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]
