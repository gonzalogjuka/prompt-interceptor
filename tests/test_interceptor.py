import json

import httpx
from fastapi.testclient import TestClient

from interceptor.app import create_app
from interceptor.config import Config
from interceptor.gate import MARKER_TAG, human_text, inspect, should_structure
from interceptor.inject import ORIGINAL_TAG

STRUCTURE = {
    "objective": "Arreglar el pod que no arranca",
    "deliverable": {"type": "analysis", "format": "causa y fix"},
    "context": [{"id": "c1", "kind": "error", "summary": "CrashLoopBackOff", "key_facts": ["exit code 137"]}],
    "constraints": [], "success_criteria": ["el pod queda Running"],
    "assumptions": [{"text": "cluster AKS", "risky": True}],
    "complexity": "moderate",
}
LONG_PROMPT = "mi pod no levanta, mirá esto:\n```\n" + "2026-10-02T10:00:00 error: OOMKilled exit code 137\n" * 20 + "```"


class Upstream:
    def __init__(self, structure=STRUCTURE, structurer_status=200, as_tool=True, reject_models=(),
                 classification=None, classifier_status=200):
        self.classification = classification or {"complexity": "simple", "deliverable": "answer",
                                                 "reason": "pregunta corta"}
        self.classifier_status = classifier_status
        self.classified = []
        self.reject_models = set(reject_models)
        self.calls = []
        self.structure = structure
        self.structurer_status = structurer_status
        self.as_tool = as_tool

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.calls.append({"path": request.url.path, "body": body, "headers": dict(request.headers)})
        if str(body.get("system", "")).startswith("You classify"):
            self.classified.append(body)
            if self.classifier_status != 200:
                return httpx.Response(self.classifier_status, json={"error": "boom"})
            return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "c1", "name": "submit_classification",
                                                          "input": self.classification}],
                                             "usage": {"input_tokens": 300, "output_tokens": 30}})
        if str(body.get("system", "")).startswith("You check whether"):
            self.audits = getattr(self, "audits", []) + [body]
            results = [{"criterion": "el pod queda Running", "met": False, "note": "no lo menciona"}]
            return httpx.Response(200, json={"content": [{"type": "tool_use", "id": "a1", "name": "submit_audit",
                                                          "input": {"results": results}}]})
        if str(body.get("system", "")).startswith("You are a request structurer"):
            if self.structurer_status != 200:
                return httpx.Response(self.structurer_status, json={"error": "boom"})
            if self.as_tool and body.get("tool_choice", {}).get("name") == "submit_structure":
                content = [{"type": "tool_use", "id": "tu1", "name": "submit_structure", "input": self.structure}]
            else:
                content = [{"type": "text", "text": json.dumps(self.structure)}]
            return httpx.Response(200, json={"content": content,
                                             "usage": {"input_tokens": 500, "output_tokens": 150}})
        if body.get("model") in self.reject_models:
            return httpx.Response(400, json={"type": "error", "error": {"message": "context too long"}})
        if body.get("stream"):
            sse = ('event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":900,"output_tokens":1}}}\n\n'
                   'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Subí el limit "}}\n\n'
                   'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"a 512Mi."}}\n\n'
                   'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":42}}\n\n')
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                                         "usage": {"input_tokens": 900, "output_tokens": 42}})


def make(tmp_path, upstream, **overrides):
    overrides.setdefault("metrics_enabled", True)
    overrides.setdefault("require_tools", False)
    overrides.setdefault("routes_file", None)
    overrides.setdefault("profile_file", None)
    cfg = Config(upstream_url="https://upstream.test", metrics_path=str(tmp_path / "m.jsonl"), **overrides)
    return TestClient(create_app(cfg, transport=httpx.MockTransport(upstream))), tmp_path / "m.jsonl"


def main_calls(up):
    return [c for c in up.calls if not str(c["body"].get("system", "")).startswith(
        ("You are a request structurer", "You check whether", "You classify"))]


def metrics(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---- gate -------------------------------------------------------------------
def test_gate_skips_short_and_bypass():
    assert should_structure("gracias!", 400)[0] is False
    assert should_structure(LONG_PROMPT + " #raw", 400) == (False, ["bypass_token"])
    assert should_structure(LONG_PROMPT, 400)[0] is True


def test_human_text_ignores_tool_results_and_reminders():
    assert human_text({"role": "user", "content": [{"type": "tool_result", "content": "x"}]}) is None
    msg = {"role": "user", "content": [{"type": "text", "text": "<system-reminder>ruido</system-reminder>hola"}]}
    assert human_text(msg) == "hola"


# ---- proxy ------------------------------------------------------------------
def test_structures_long_prompt_and_forwards(tmp_path):
    up = Upstream()
    client, mpath = make(tmp_path, up)
    r = client.post("/v1/messages", headers={"x-api-key": "k"},
                    json={"model": "claude-opus-5-5", "max_tokens": 10,
                          "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert r.status_code == 200
    sent = main_calls(up)[0]["body"]["messages"][-1]["content"]
    assert sent[0]["text"] == LONG_PROMPT              # original preserved
    assert MARKER_TAG in sent[1]["text"]                # structure appended
    rec = metrics(mpath)[0]
    assert rec["action"] == "structured" and rec["risky_assumptions"] == 1
    assert rec["upstream_usage"]["output_tokens"] == 42


def test_short_prompt_passes_untouched(tmp_path):
    up = Upstream()
    client, mpath = make(tmp_path, up)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": "gracias"}]})
    assert len(up.calls) == 1
    assert up.calls[0]["body"]["messages"][0]["content"] == "gracias"
    assert metrics(mpath)[0]["action"] == "passthrough_gate"


def test_fail_open_on_structurer_error(tmp_path):
    up = Upstream(structurer_status=500)
    client, mpath = make(tmp_path, up)
    r = client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                          "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert r.status_code == 200
    assert main_calls(up)[0]["body"]["messages"][0]["content"] == LONG_PROMPT
    assert metrics(mpath)[0]["action"] == "error"


def test_history_is_reapplied_on_next_turn(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    # next turn: client resends ORIGINAL history (it never saw our change)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5, "messages": [
        {"role": "user", "content": LONG_PROMPT},
        {"role": "assistant", "content": "respuesta"},
        {"role": "user", "content": "ok"}]})
    second = main_calls(up)[1]["body"]["messages"]
    assert MARKER_TAG in second[0]["content"][1]["text"]
    structurer_calls = [c for c in up.calls if c not in main_calls(up)]
    assert len(structurer_calls) == 1  # no re-structuring, cached


def test_routing_enforce_simple(tmp_path):
    up = Upstream(structure={**STRUCTURE, "complexity": "simple"})
    client, mpath = make(tmp_path, up, routing_mode="enforce", model_simple="claude-haiku-4-5-20251001")
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert main_calls(up)[0]["body"]["model"] == "claude-haiku-4-5-20251001"
    assert metrics(mpath)[0]["routed_from"] == "claude-opus-5-5"


def test_streaming_relays_and_reads_usage(tmp_path):
    up = Upstream()
    client, mpath = make(tmp_path, up)
    with client.stream("POST", "/v1/messages", json={"model": "m", "max_tokens": 5, "stream": True,
                                                    "messages": [{"role": "user", "content": LONG_PROMPT}]}) as r:
        data = b"".join(r.iter_bytes())
    assert b"message_delta" in data
    rec = metrics(mpath)[0]
    assert rec["upstream_usage"] == {"input_tokens": 900, "output_tokens": 42}


def test_agent_tool_loop_is_not_intercepted(tmp_path):
    up = Upstream()
    client, mpath = make(tmp_path, up)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5, "messages": [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": LONG_PROMPT}]}]})
    assert len(up.calls) == 1
    assert metrics(mpath)[0]["action"] == "passthrough_non_human_turn"


def test_passthrough_other_endpoints(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up)
    r = client.post("/v1/messages/count_tokens", json={"model": "m", "messages": []})
    assert r.status_code == 200 and up.calls[0]["path"] == "/v1/messages/count_tokens"


# ---- rewrite mode -----------------------------------------------------------
IMPROVED = "Necesito diagnosticar un pod en CrashLoopBackOff (OOMKilled, exit 137). No ejecutes nada."


def test_rewrite_puts_improved_prompt_first_and_keeps_original(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, mpath = make(tmp_path, up)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    sent = main_calls(up)[0]["body"]["messages"][-1]["content"]
    assert sent[0]["text"] == IMPROVED
    assert ORIGINAL_TAG in sent[1]["text"] and LONG_PROMPT in sent[1]["text"]
    assert metrics(mpath)[0]["mode"] == "rewrite"


def test_rewrite_preserves_reminders_images_and_cache_control(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, _ = make(tmp_path, up)
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>CLAUDE.md rules</system-reminder>"},
        img,
        {"type": "text", "text": LONG_PROMPT, "cache_control": {"type": "ephemeral"}}]}]})
    sent = main_calls(up)[0]["body"]["messages"][-1]["content"]
    assert sent[0]["text"] == "<system-reminder>CLAUDE.md rules</system-reminder>"
    assert sent[1] == img
    assert sent[2]["text"] == IMPROVED
    assert sent[-1]["cache_control"] == {"type": "ephemeral"}


def test_rewrite_history_is_rebuilt_identically(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, _ = make(tmp_path, up)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5, "messages": [
        {"role": "user", "content": LONG_PROMPT},
        {"role": "assistant", "content": "respuesta"},
        {"role": "user", "content": "ok"}]})
    first = main_calls(up)[0]["body"]["messages"][0]["content"]
    second = main_calls(up)[1]["body"]["messages"][0]["content"]
    assert first == second  # stable prefix -> prompt cache keeps working


def test_structurer_only_gets_oauth_betas(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up)
    client.post("/v1/messages", headers={"authorization": "Bearer tok",
                                         "anthropic-beta": "context-1m-2025-08-07, oauth-2025-04-20"},
                json={"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": LONG_PROMPT}]})
    structurer = [c for c in up.calls if c not in main_calls(up)][0]["headers"]
    main = main_calls(up)[0]["headers"]
    assert structurer["anthropic-beta"] == "oauth-2025-04-20"
    assert structurer["authorization"] == "Bearer tok"
    assert "context-1m" in main["anthropic-beta"]  # main request untouched


def test_metrics_disabled_by_default(tmp_path):
    up = Upstream()
    client, mpath = make(tmp_path, up, metrics_enabled=False)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert not mpath.exists()


def test_head_passthrough(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up)
    assert client.head("/api/hello").status_code == 200


# ---- robustness -------------------------------------------------------------
def test_structurer_is_forced_to_use_tool(tmp_path):
    up = Upstream()
    client, mpath = make(tmp_path, up)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    req = [c for c in up.calls if c not in main_calls(up)][0]["body"]
    assert req["tool_choice"] == {"type": "tool", "name": "submit_structure"}
    assert metrics(mpath)[0]["action"] == "structured"


def test_text_fallback_when_gateway_ignores_tools(tmp_path):
    up = Upstream(as_tool=False)
    client, mpath = make(tmp_path, up)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert metrics(mpath)[0]["action"] == "structured"


def test_reminder_mentioning_marker_does_not_hide_human_text():
    msg = {"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>notas sobre <interceptor_structure></system-reminder>"},
        {"type": "text", "text": LONG_PROMPT}]}
    assert inspect(msg) == (LONG_PROMPT, "ok")


def test_skip_reason_is_recorded(tmp_path):
    up = Upstream()
    client, mpath = make(tmp_path, up)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5, "messages": [
        {"role": "user", "content": "hola"}, {"role": "assistant", "content": "{"}]})
    assert metrics(mpath)[0]["skip_reason"] == "last_role=assistant"


# ---- Claude Code request shapes ---------------------------------------------
TOOLS = [{"name": "Bash", "description": "run", "input_schema": {"type": "object"}}]


def test_trailing_system_message_is_skipped_to_find_user_turn(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, mpath = make(tmp_path, up, require_tools=True)
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5, "tools": TOOLS, "messages": [
        {"role": "user", "content": [{"type": "text", "text": LONG_PROMPT}]},
        {"role": "system", "content": [{"type": "text", "text": "x" * 42000}]}]})
    sent = main_calls(up)[0]["body"]["messages"]
    assert sent[0]["content"][0]["text"] == IMPROVED
    assert sent[1]["role"] == "system" and len(sent[1]["content"][0]["text"]) == 42000
    assert metrics(mpath)[0]["action"] == "structured"


def test_side_requests_without_tools_are_untouched(tmp_path):
    up = Upstream()
    client, mpath = make(tmp_path, up, require_tools=True)
    client.post("/v1/messages", json={"model": "claude-haiku-4-5", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert len(up.calls) == 1  # no structurer call
    assert up.calls[0]["body"]["messages"][0]["content"] == LONG_PROMPT
    assert metrics(mpath)[0]["action"] == "passthrough_side_request"


def test_main_loop_history_rebuilt_across_turns_with_system_tail(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, _ = make(tmp_path, up, require_tools=True)
    sys_msg = {"role": "system", "content": [{"type": "text", "text": "ctx"}]}
    user1 = {"role": "user", "content": [{"type": "text", "text": LONG_PROMPT}]}
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5, "tools": TOOLS,
                                      "messages": [user1, sys_msg]})
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5, "tools": TOOLS, "messages": [
        user1, sys_msg, {"role": "assistant", "content": "resp"},
        {"role": "user", "content": [{"type": "text", "text": "mostrame lo que recibiste"}]}]})
    first = main_calls(up)[0]["body"]["messages"][0]["content"]
    second = main_calls(up)[1]["body"]["messages"][0]["content"]
    assert first == second and second[0]["text"] == IMPROVED


# ---- verifier ---------------------------------------------------------------
from interceptor.verify import check, extract_facts  # noqa: E402

POD = """no levanta, no ejecutes nada
Name:         api-backend-7d9f8b6c4-x2kqp
Node:         aks-nodepool1-31718369-vmss000002
    Image:          myregistry.azurecr.io/api-backend:2.4.1
      Reason:       OOMKilled
      Exit Code:    137
      memory:  256Mi
  2026-10-05T14:02:11Z  Warning  BackOff  kubelet
la 2.3.0 andaba bien"""


def test_extract_facts_finds_identifiers_and_skips_timestamps():
    facts = extract_facts(POD)
    for f in ["api-backend-7d9f8b6c4-x2kqp", "aks-nodepool1-31718369-vmss000002",
              "myregistry.azurecr.io/api-backend:2.4.1", "OOMKilled", "137", "256Mi", "2.3.0"]:
        assert f in facts
    assert not any(f.startswith("2026-10-05") for f in facts)


def test_check_appends_missing_facts():
    improved = "Pod OOMKilled, exit 137, limit 256Mi, 2.3.0 andaba. Imagen myregistry.azurecr.io/api-backend:2.4.1"
    fixed, rep = check(POD, improved)
    assert fixed.startswith(improved) and "api-backend-7d9f8b6c4-x2kqp" in fixed
    assert rep.added and not rep.rejected


def test_check_rejects_rewrite_that_lost_most_facts():
    fixed, rep = check(POD, "Tengo un problema con un pod, ayudame.")
    assert fixed is None and rep.rejected


def test_rejected_rewrite_falls_back_to_append_mode(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": "Tengo un problema con un pod."})
    client, mpath = make(tmp_path, up)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": POD + "\n" + "x" * 400}]})
    sent = main_calls(up)[0]["body"]["messages"][0]["content"]
    assert sent[0]["text"].startswith(POD)          # original first
    assert MARKER_TAG in sent[1]["text"]            # structure block
    rec = metrics(mpath)[0]
    assert rec["mode"] == "append" and rec["verify_rejected"] is True


# ---- routing ----------------------------------------------------------------
def routes(tmp_path, rules):
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"rules": rules}), encoding="utf-8")
    return str(path)


def test_enforce_routes_by_rule(tmp_path):
    rf = routes(tmp_path, [{"name": "code", "when": {"complexity": ["moderate"], "deliverable": ["analysis"]},
                            "model": "sonnet"}])
    up = Upstream()
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=rf)
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert main_calls(up)[0]["body"]["model"] == "claude-sonnet-5-5"
    rec = metrics(mpath)[0]
    assert rec["route_rule"] == "code" and rec["routed_from"] == "claude-opus-5-5"


def test_shadow_reports_but_does_not_change(tmp_path):
    rf = routes(tmp_path, [{"name": "all", "when": {}, "model": "haiku"}])
    up = Upstream()
    client, mpath = make(tmp_path, up, routing_mode="shadow", routes_file=rf)
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert main_calls(up)[0]["body"]["model"] == "claude-opus-5-5"
    assert metrics(mpath)[0]["route_shadow"] == "claude-haiku-4-5-20251001"


def test_manual_override_even_on_short_prompt_and_routing_off(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up)  # routing off
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": "explicame esto #model:sonnet"}]})
    assert up.calls[0]["body"]["model"] == "claude-sonnet-5-5"


def test_routed_model_sticks_for_the_tool_loop_of_the_turn(tmp_path):
    rf = routes(tmp_path, [{"name": "all", "when": {}, "model": "sonnet"}])
    up = Upstream()
    client, _ = make(tmp_path, up, routing_mode="enforce", routes_file=rf, require_tools=True)
    user1 = {"role": "user", "content": [{"type": "text", "text": LONG_PROMPT}]}
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5, "tools": TOOLS,
                                      "messages": [user1]})
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5, "tools": TOOLS, "messages": [
        user1,
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}]})
    assert [c["body"]["model"] for c in main_calls(up)] == ["claude-sonnet-5-5", "claude-sonnet-5-5"]


def test_fallback_to_original_model_when_routed_model_refuses(tmp_path):
    rf = routes(tmp_path, [{"name": "all", "when": {}, "model": "haiku"}])
    up = Upstream(reject_models={"claude-haiku-4-5-20251001"})
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=rf)
    r = client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                          "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert r.status_code == 200
    assert [c["body"]["model"] for c in main_calls(up)] == ["claude-haiku-4-5-20251001", "claude-opus-5-5"]
    rec = metrics(mpath)[0]
    assert rec["route_fallback"] is True and rec["model_used"] == "claude-opus-5-5"


def test_routed_model_does_not_get_the_1m_context_beta(tmp_path):
    rf = routes(tmp_path, [{"name": "all", "when": {}, "model": "haiku"}])
    up = Upstream(reject_models={"claude-haiku-4-5-20251001"})
    client, _ = make(tmp_path, up, routing_mode="enforce", routes_file=rf)
    client.post("/v1/messages", headers={"anthropic-beta": "context-1m-2025-08-07, oauth-2025-04-20"},
                json={"model": "claude-opus-5-5", "max_tokens": 5,
                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    routed, fallback = main_calls(up)
    assert routed["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert "context-1m" in fallback["headers"]["anthropic-beta"]  # el original recupera su beta


def test_body_fitted_for_haiku_and_restored_on_fallback(tmp_path):
    rf = routes(tmp_path, [{"name": "all", "when": {}, "model": "haiku"}])
    up = Upstream(reject_models={"claude-haiku-4-5-20251001"})
    client, _ = make(tmp_path, up, routing_mode="enforce", routes_file=rf)
    client.post("/v1/messages", json={
        "model": "claude-opus-5-5", "max_tokens": 128000, "thinking": {"type": "adaptive"},
        "context_management": {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]},
        "output_config": {"effort": "high"}, "system": "base",
        "messages": [{"role": "user", "content": LONG_PROMPT},
                     {"role": "system", "content": [{"type": "text", "text": "env", "cache_control": {"type": "ephemeral"}}],
                      "output_config": {"effort": "high"}}]})
    routed, fallback = (c["body"] for c in main_calls(up))
    assert routed["max_tokens"] == 64000
    assert [m["role"] for m in routed["messages"]] == ["user"]
    assert routed["system"] == [{"type": "text", "text": "base"}, {"type": "text", "text": "env"}]
    assert fallback["messages"][1]["role"] == "system" and fallback["system"] == "base"
    assert not {"thinking", "context_management", "output_config"} & routed.keys()
    assert fallback["thinking"] == {"type": "adaptive"} and fallback["max_tokens"] == 128000
    assert fallback["output_config"] == {"effort": "high"} and "context_management" in fallback


def test_body_untouched_when_routed_to_sonnet(tmp_path):
    rf = routes(tmp_path, [{"name": "all", "when": {}, "model": "sonnet"}])
    up = Upstream()
    client, _ = make(tmp_path, up, routing_mode="enforce", routes_file=rf)
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 128000,
                                      "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"},
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    body = main_calls(up)[0]["body"]
    assert body["thinking"] == {"type": "adaptive"} and body["max_tokens"] == 128000


# ---- refinements from real-world testing --------------------------------------
from interceptor.gate import preview  # noqa: E402


def test_simple_requests_are_not_rewritten_but_still_routed(tmp_path):
    rf = routes(tmp_path, [{"name": "simple", "when": {"complexity": ["simple"]}, "model": "haiku"}])
    up = Upstream(structure={**STRUCTURE, "complexity": "simple", "improved_prompt": IMPROVED})
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=rf)
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    sent = main_calls(up)[0]["body"]
    assert sent["messages"][0]["content"] == LONG_PROMPT          # text untouched
    assert sent["model"] == "claude-haiku-4-5-20251001"            # but routed
    assert metrics(mpath)[0]["action"] == "classified_only"


def test_simple_requests_rewritten_when_skip_simple_disabled(tmp_path):
    up = Upstream(structure={**STRUCTURE, "complexity": "simple", "improved_prompt": IMPROVED})
    client, _ = make(tmp_path, up, skip_simple=False)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert main_calls(up)[0]["body"]["messages"][0]["content"][0]["text"] == IMPROVED


def test_client_markers_are_not_treated_as_human_text():
    for marker in ["[Request interrupted by user]", "[Request interrupted by user for tool use]",
                   "<command-name>/model</command-name>", "<local-command-stdout>Set model</local-command-stdout>",
                   "[SUGGESTION MODE: Suggest what the user might naturally type next]\n" + LONG_PROMPT]:
        assert inspect({"role": "user", "content": marker}) == (None, "client_marker")
    assert inspect({"role": "user", "content": "[1, 2, 3] ordename esto"})[1] == "ok"


def test_cross_session_messages_are_not_touched(tmp_path):
    peer = ("Another Claude session sent a message:\n<cross-session-message from=\"uds:x\" "
            "from-name=\"infrastructure-23\" from-mode=\"bypass\">\n" + LONG_PROMPT + "\n</cross-session-message>")
    notice = "[Cross-session delivery notice] Your message to another session was held by that session."
    for text in [peer, notice]:
        assert inspect({"role": "user", "content": [{"type": "text", "text": text}]}) == (None, "cross_session")
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, mpath = make(tmp_path, up, require_tools=True)
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5, "tools": TOOLS,
                                      "messages": [{"role": "user", "content": [{"type": "text", "text": peer}]}]})
    assert main_calls(up)[0]["body"]["messages"][0]["content"][0]["text"] == peer
    assert len(up.calls) == 1  # no structurer/classifier call
    assert metrics(mpath)[0]["skip_reason"] == "cross_session"


# ---- interpreter context ------------------------------------------------------
CC_SYSTEM = [{"type": "text", "text": "You are Claude Code.\n# Tools\nmuchas reglas\n# Environment\n"
                                      " - Primary working directory: C:\\Repos\\Infra\n - Platform: win32\n"
                                      "# Otra seccion\nnada"},
             {"type": "text", "text": "Today's date is 2026-10-07."}]
CLAUDE_MD = ("<system-reminder>\nContents of C:\\Users\\g\\.claude\\CLAUDE.md:\nResponder en español. "
             "Prueba valida = deploy desde la UI de CM.\n</system-reminder>")
PLAN_NOISE = "<system-reminder>Plan mode is active.</system-reminder>"


def cc_messages(final):
    return [{"role": "user", "content": [{"type": "text", "text": CLAUDE_MD}, {"type": "text", "text": PLAN_NOISE},
                                         {"type": "text", "text": "armame el modelo de OCI"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Listo el modelo OCI, falta probarlo."},
                                              {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
            {"role": "user", "content": [{"type": "text", "text": final}]}]


def test_context_extracts_instructions_environment_and_conversation():
    from interceptor.context import extract
    msgs = cc_messages(LONG_PROMPT)
    ctx = extract({"system": CC_SYSTEM}, msgs, len(msgs) - 1, max_chars=30000, max_turns=6)
    assert "Prueba valida = deploy desde la UI de CM." in ctx.instructions
    assert "Plan mode" not in ctx.instructions
    assert "C:\\Repos\\Infra" in ctx.environment and "2026-10-07" in ctx.environment
    assert "muchas reglas" not in ctx.environment and "Otra seccion" not in ctx.environment
    assert "armame el modelo de OCI" in ctx.conversation and "falta probarlo" in ctx.conversation
    assert "tool_result" not in ctx.conversation and ctx.turns == 2
    assert LONG_PROMPT not in ctx.conversation  # the message itself is not context


def test_small_client_system_prompt_is_the_context_without_instruction_files():
    from interceptor.context import extract
    msgs = [{"role": "user", "content": "hola"}]
    assert extract({"system": "Sos el asistente de soporte de Cybele."}, msgs, 0, 30000, 6).instructions \
        == "Sos el asistente de soporte de Cybele."


def test_structurer_receives_cached_context_and_conversation(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, mpath = make(tmp_path, up, require_tools=True)
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5, "tools": TOOLS,
                                      "system": CC_SYSTEM, "messages": cc_messages(LONG_PROMPT)})
    sent = [c for c in up.calls if c not in main_calls(up)][0]["body"]["messages"][0]["content"]
    assert sent[0]["cache_control"] == {"type": "ephemeral"}
    assert "<user_instructions>" in sent[0]["text"] and "deploy desde la UI de CM" in sent[0]["text"]
    assert "<recent_conversation>" in sent[1]["text"] and "falta probarlo" in sent[1]["text"]
    assert sent[1]["text"].endswith(f"<message>\n{LONG_PROMPT}\n</message>")
    rec = metrics(mpath)[0]
    assert rec["context_turns"] == 2 and rec["context_instructions_chars"] > 0


def test_classifier_receives_cached_context(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up, require_tools=True, routing_mode="enforce", routes_file=routes(tmp_path, TIERS))
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5, "tools": TOOLS,
                                      "system": CC_SYSTEM, "messages": cc_messages("¿y para AWS?")})
    sent = up.classified[0]["messages"][0]["content"]
    assert sent[0]["cache_control"] == {"type": "ephemeral"} and "deploy desde la UI de CM" in sent[0]["text"]
    assert sent[1]["text"].endswith("<request>\n¿y para AWS?\n</request>")


def test_context_can_be_turned_off(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, _ = make(tmp_path, up, require_tools=True, context=False)
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5, "tools": TOOLS,
                                      "system": CC_SYSTEM, "messages": cc_messages(LONG_PROMPT)})
    sent = [c for c in up.calls if c not in main_calls(up)][0]["body"]["messages"][0]["content"]
    assert sent == LONG_PROMPT


def test_profile_travels_in_the_cached_context(tmp_path):
    perfil = tmp_path / "perfil.md"
    perfil.write_text("Gonzalo, infraestructura en Cybele. Español, directo.", encoding="utf-8")
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, _ = make(tmp_path, up, profile_file=str(perfil))
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    sent = [c for c in up.calls if c not in main_calls(up)][0]["body"]["messages"][0]["content"]
    assert sent[0]["text"].startswith("<user_profile>\nGonzalo, infraestructura en Cybele.")
    assert sent[0]["cache_control"] == {"type": "ephemeral"}


# ---- interpreter and responder per complexity --------------------------------
def interp_routes(tmp_path, rules=None):
    p = tmp_path / "interp.json"
    p.write_text(json.dumps({"interpreters": {"moderate": "haiku", "complex": "sonnet"},
                             "rules": rules or []}), encoding="utf-8")
    return str(p)


def structurer_calls(up):
    return [c["body"] for c in up.calls if str(c["body"].get("system", "")).startswith("You are a request structurer")]


def test_complex_prompt_is_interpreted_by_the_stronger_model(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED},
                  classification={"complexity": "complex", "deliverable": "analysis", "reason": "multi-paso"})
    client, mpath = make(tmp_path, up, routes_file=interp_routes(tmp_path))
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert len(up.classified) == 1
    assert structurer_calls(up)[0]["model"] == "claude-sonnet-5-5"
    assert metrics(mpath)[0]["interpreter_model"] == "claude-sonnet-5-5"


def test_simple_long_prompt_skips_the_interpreter_entirely(tmp_path):
    up = Upstream(classification={"complexity": "simple", "deliverable": "answer", "reason": "lookup"})
    client, mpath = make(tmp_path, up, routes_file=interp_routes(tmp_path))
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert structurer_calls(up) == []
    assert main_calls(up)[0]["body"]["messages"][0]["content"] == LONG_PROMPT
    assert metrics(mpath)[0]["action"] == "classified_only"


def test_classifier_failure_still_rewrites_with_the_default_interpreter(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED}, classifier_status=500)
    client, mpath = make(tmp_path, up, routes_file=interp_routes(tmp_path))
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert structurer_calls(up)[0]["model"] == "claude-haiku-4-5-20251001"
    rec = metrics(mpath)[0]
    assert rec["action"] == "structured" and "classify_error" in rec


def test_without_interpreters_the_structurer_model_is_used_without_classifying(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, _ = make(tmp_path, up)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert up.classified == [] and structurer_calls(up)[0]["model"] == "claude-haiku-4-5-20251001"


EFFORT_RULES = [{"name": "moderado", "when": {"complexity": ["moderate"]}, "model": "claude-opus-5-5",
                 "effort": "medium"}]


def test_rule_sets_the_effort_and_keeps_it_for_the_whole_turn(tmp_path):
    up = Upstream(classification={"complexity": "moderate", "deliverable": "answer", "reason": "explicar"})
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, EFFORT_RULES))
    turn = [{"role": "user", "content": "explicame el lock file"}]
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                      "output_config": {"effort": "xhigh"}, "messages": turn})
    assert main_calls(up)[0]["body"]["output_config"] == {"effort": "medium"}
    assert metrics(mpath)[0]["effort_from"] == "xhigh"
    loop = turn + [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}]
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                      "output_config": {"effort": "xhigh"}, "messages": loop})
    assert main_calls(up)[1]["body"]["output_config"] == {"effort": "medium"}


def test_effort_is_not_sent_to_models_without_it(tmp_path):
    rules = [{"name": "simple", "when": {"complexity": ["simple"]}, "model": "haiku", "effort": "low"}]
    up = Upstream()
    client, _ = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, rules))
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": "que hora es en UTC?"}]})
    sent = main_calls(up)[0]["body"]
    assert sent["model"] == "claude-haiku-4-5-20251001" and "output_config" not in sent


def test_refused_effort_falls_back_to_the_clients(tmp_path):
    class Refuse(Upstream):
        def __call__(self, request):
            body = json.loads(request.content)
            if (body.get("output_config") or {}).get("effort") == "medium":
                self.calls.append({"path": request.url.path, "body": body, "headers": {}})
                return httpx.Response(400, json={"type": "error", "error": {"message": "bad effort"}})
            return super().__call__(request)
    up = Refuse(classification={"complexity": "moderate", "deliverable": "answer", "reason": "x"})
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, EFFORT_RULES))
    r = client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                          "output_config": {"effort": "xhigh"},
                                          "messages": [{"role": "user", "content": "explicame esto"}]})
    assert r.status_code == 200
    assert main_calls(up)[-1]["body"]["output_config"] == {"effort": "xhigh"}
    assert metrics(mpath)[0]["route_fallback"] is True


def test_preview_is_single_line_and_bounded():
    p = preview("linea uno\n\n  linea dos " + "x" * 200)
    assert "\n" not in p and len(p) == 60 and p.endswith("…")


# ---- compression, cache guard, audit ---------------------------------------------
import time as _time  # noqa: E402

from interceptor.compress import compress  # noqa: E402
from interceptor.metrics import SSEUsageScanner  # noqa: E402

BIG_LOG = "el api se cae:\n" + "\n".join(
    f"2026-10-05T14:{i // 60:02d}:{i % 60:02d}Z ERROR pool exhausted acquiring conn db-prod-01:5432"
    for i in range(200)) + "\n2026-10-05T14:05:01Z FATAL OOM in worker-7f3a"


def test_compress_collapses_repeats_and_keeps_unique_lines():
    out, n = compress(BIG_LOG)
    assert n == 199 and "[x200" in out and "FATAL OOM in worker-7f3a" in out
    assert len(out) < len(BIG_LOG) / 10


def test_compress_leaves_code_and_short_text_alone():
    code = "def f():\n" + "    return None\n" * 50
    assert compress(code, min_chars=10) == (code, 0)
    assert compress("hola", min_chars=3000) == ("hola", 0)


def test_compressed_text_is_what_structurer_and_model_receive(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": "db-prod-01:5432 worker-7f3a pool exhausted"})
    client, mpath = make(tmp_path, up)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": BIG_LOG}]})
    structurer_input = [c for c in up.calls if c not in main_calls(up)][0]["body"]["messages"][0]["content"]
    assert "[x200" in structurer_input and len(structurer_input) < len(BIG_LOG) / 5
    original_block = main_calls(up)[0]["body"]["messages"][0]["content"][1]["text"]
    assert "[x200" in original_block
    assert metrics(mpath)[0]["collapsed_lines"] == 199


def test_routing_skipped_when_history_is_large(tmp_path):
    rf = routes(tmp_path, [{"name": "all", "when": {}, "model": "haiku"}])
    up = Upstream()
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=rf, route_max_history_chars=1000)
    history = [{"role": "user", "content": "x" * 2000}, {"role": "assistant", "content": "ok"},
               {"role": "user", "content": LONG_PROMPT}]
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5, "messages": history})
    assert main_calls(up)[0]["body"]["model"] == "claude-opus-5-5"
    assert metrics(mpath)[0]["route_skipped"] == "history_too_large"


def test_override_ignores_history_size(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up, route_max_history_chars=10)
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": "hola #model:haiku"}]})
    assert up.calls[0]["body"]["model"] == "claude-haiku-4-5-20251001"


def test_sse_scanner_reads_text_and_stop_reason():
    sc = SSEUsageScanner(keep_text=True)
    sc.feed(b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ho"}}\n')
    sc.feed(b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"la"}}\ndata: {"type":"mess')
    sc.feed(b'age_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n')
    assert sc.text == "hola" and sc.stop_reason == "end_turn" and sc.usage["output_tokens"] == 3


def _wait_for(pred, timeout=2.0):
    end = _time.time() + timeout
    while _time.time() < end:
        if pred():
            return True
        _time.sleep(0.02)
    return False


def test_audit_runs_in_background_after_streamed_answer(tmp_path):
    up = Upstream()
    cfg = Config(upstream_url="https://upstream.test", metrics_path=str(tmp_path / "m.jsonl"),
                 metrics_enabled=True, require_tools=False, routes_file=None, audit=True)
    with TestClient(create_app(cfg, transport=httpx.MockTransport(up))) as client:
        with client.stream("POST", "/v1/messages", json={"model": "m", "max_tokens": 5, "stream": True,
                                                        "messages": [{"role": "user", "content": LONG_PROMPT}]}) as r:
            b"".join(r.iter_bytes())
        assert _wait_for(lambda: getattr(up, "audits", None))
        assert _wait_for(lambda: any(m.get("type") == "audit" for m in metrics(tmp_path / "m.jsonl")))
    audit_req = up.audits[0]["messages"][0]["content"]
    assert "el pod queda Running" in audit_req and "Subí el limit a 512Mi." in audit_req
    rec = [m for m in metrics(tmp_path / "m.jsonl") if m.get("type") == "audit"][0]
    assert rec["met"] == 0 and rec["total"] == 1


def test_audit_is_off_by_default(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up)
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": LONG_PROMPT}]})
    assert not getattr(up, "audits", None)


# ---- benchmark harness --------------------------------------------------------
def test_benchmark_harness_runs_end_to_end(tmp_path):
    import asyncio
    import sys
    import types
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
    import run_bench

    def handler(req):
        b = json.loads(req.content)
        system = str(b.get("system", ""))
        if system.startswith("You are a request structurer"):
            s = {**STRUCTURE, "improved_prompt": b["messages"][0]["content"]}
            return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "submit_structure", "input": s}],
                                             "usage": {"input_tokens": 700, "output_tokens": 250}})
        if system.startswith("You compare"):
            return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "submit_verdict",
                                                          "input": {"winner": "A", "reason": "x"}}]})
        return httpx.Response(200, json={"model": b["model"], "content": [{"type": "text", "text": "limit memoria 2.4.1"}],
                                         "usage": {"input_tokens": 300, "output_tokens": 400}})

    args = types.SimpleNamespace(tasks=str(Path(run_bench.__file__).with_name("tasks.jsonl")), only="pod-oom",
                                 model="m", judge="j", routing="off", max_tokens=100, min_chars=400,
                                 upstream="https://api.test", out=str(tmp_path / "out"))
    results = asyncio.run(run_bench.run(args, transport=httpx.MockTransport(handler)))
    r = results[0]
    assert r["interceptor"]["action"] == "structured" and r["interceptor"]["structurer_tokens"] == 950
    assert r["raw"]["checks"] == {"passed": 3, "total": 3}
    assert r["verdict"]["winner"] in ("raw", "interceptor")
    assert (tmp_path / "out" / "results.jsonl").exists()


# ---- routing by complexity, not by length ---------------------------------------
from interceptor.gate import is_continuation  # noqa: E402

TIERS = [{"name": "simple", "when": {"complexity": ["simple"]}, "model": "haiku"},
         {"name": "moderado", "when": {"complexity": ["moderate"]}, "model": "sonnet"},
         {"name": "complejo", "when": {"complexity": ["complex"]}, "model": "opus"}]


def test_short_but_complex_message_goes_to_the_strong_model(tmp_path):
    up = Upstream(classification={"complexity": "complex", "deliverable": "analysis", "reason": "diseño"})
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, TIERS))
    short = "rediseñá la arquitectura del pipeline de VDI"
    client.post("/v1/messages", json={"model": "claude-sonnet-5-5", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": short}]})
    sent = main_calls(up)[0]["body"]
    assert sent["model"] == "claude-opus-5-5"
    assert sent["messages"][0]["content"] == short         # short messages are never rewritten
    rec = metrics(mpath)[0]
    assert rec["action"] == "classified_short" and rec["complexity"] == "complex"


def test_short_simple_message_goes_to_the_cheap_model(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, TIERS))
    client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": "que es un configmap?"}]})
    assert main_calls(up)[0]["body"]["model"] == "claude-haiku-4-5-20251001"
    assert len(up.classified) == 1


def test_no_classification_when_routing_is_off_or_raw(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up, routes_file=routes(tmp_path, TIERS))  # routing off
    client.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                      "messages": [{"role": "user", "content": "que es un configmap?"}]})
    up2 = Upstream()
    client2, _ = make(tmp_path, up2, routing_mode="enforce", routes_file=routes(tmp_path, TIERS))
    client2.post("/v1/messages", json={"model": "m", "max_tokens": 5,
                                       "messages": [{"role": "user", "content": "que es un configmap? #raw"}]})
    assert up.classified == [] and up2.classified == []


def test_classifier_sees_the_previous_answer(tmp_path):
    up = Upstream(classification={"complexity": "complex", "deliverable": "code", "reason": "refactor grande"})
    client, _ = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, TIERS))
    client.post("/v1/messages", json={"model": "claude-sonnet-5-5", "max_tokens": 5, "messages": [
        {"role": "user", "content": "como organizo el terraform?"},
        {"role": "assistant", "content": [{"type": "text", "text": "Te propongo separar en módulos por cloud."}]},
        {"role": "user", "content": "hacé eso para los 8 providers"}]})
    sent = up.classified[0]["messages"][0]["content"]
    assert "Te propongo separar en módulos por cloud." in sent and "hacé eso para los 8 providers" in sent


def test_continuation_inherits_previous_turn_model_without_classifying(tmp_path):
    up = Upstream(classification={"complexity": "complex", "deliverable": "action", "reason": "migración"})
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, TIERS))
    turn1 = {"role": "user", "content": "migrá el cluster a la nueva vnet"}
    client.post("/v1/messages", json={"model": "claude-sonnet-5-5", "max_tokens": 5, "messages": [turn1]})
    client.post("/v1/messages", json={"model": "claude-sonnet-5-5", "max_tokens": 5, "messages": [
        turn1, {"role": "assistant", "content": "Este es el plan: ..."}, {"role": "user", "content": "dale, hacelo"}]})
    assert [c["body"]["model"] for c in main_calls(up)] == ["claude-opus-5-5", "claude-opus-5-5"]
    assert len(up.classified) == 1                          # "dale, hacelo" was not sent to Haiku
    rec = metrics(mpath)[1]
    assert rec["action"] == "continuation" and rec["route_how"] == "inherit"


def test_continuation_in_shadow_only_reports(tmp_path):
    up = Upstream(classification={"complexity": "complex", "deliverable": "action", "reason": "x"})
    client, mpath = make(tmp_path, up, routing_mode="shadow", routes_file=routes(tmp_path, TIERS))
    turn1 = {"role": "user", "content": "migrá el cluster a la nueva vnet"}
    client.post("/v1/messages", json={"model": "claude-sonnet-5-5", "max_tokens": 5, "messages": [turn1]})
    client.post("/v1/messages", json={"model": "claude-sonnet-5-5", "max_tokens": 5, "messages": [
        turn1, {"role": "assistant", "content": "plan"}, {"role": "user", "content": "ok seguí"}]})
    assert [c["body"]["model"] for c in main_calls(up)] == ["claude-sonnet-5-5", "claude-sonnet-5-5"]
    assert metrics(mpath)[1]["route_shadow"] == "claude-opus-5-5"


def test_same_short_text_in_different_turns_does_not_collide(tmp_path):
    up = Upstream(classification={"complexity": "complex", "deliverable": "action", "reason": "x"})
    client, _ = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, TIERS))
    # conversation A routes turn "ok" to opus; conversation B's first turn is also "ok" but must not inherit
    a1 = {"role": "user", "content": "migrá el cluster a la nueva vnet"}
    client.post("/v1/messages", json={"model": "claude-sonnet-5-5", "max_tokens": 5, "messages": [a1]})
    client.post("/v1/messages", json={"model": "claude-sonnet-5-5", "max_tokens": 5, "messages": [
        a1, {"role": "assistant", "content": "plan"}, {"role": "user", "content": "ok"}]})
    client.post("/v1/messages", json={"model": "claude-sonnet-5-5", "max_tokens": 5, "messages": [
        {"role": "user", "content": "ok"}]})
    assert main_calls(up)[-1]["body"]["model"] == "claude-sonnet-5-5"


def test_classifier_failure_fails_open(tmp_path):
    up = Upstream(classifier_status=500)
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, TIERS))
    r = client.post("/v1/messages", json={"model": "claude-opus-5-5", "max_tokens": 5,
                                          "messages": [{"role": "user", "content": "que es un configmap?"}]})
    assert r.status_code == 200 and main_calls(up)[0]["body"]["model"] == "claude-opus-5-5"
    assert metrics(mpath)[0]["action"] == "error"


def test_continuation_detection():
    for t in ["ok", "dale", "Dale!", "sí", "seguí", "ok seguí", "dale, hacelo", "continuá", "gracias", "go on"]:
        assert is_continuation(t), t
    for t in ["ok pero cambiá el puerto a 8080", "dale pero sin helm", "rediseñá la arquitectura del pipeline",
              "okey y como lo pruebo?", "si podés agregale tests"]:
        assert not is_continuation(t), t
