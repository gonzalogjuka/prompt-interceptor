"""The proxy itself: what it forwards untouched, how it rewrites, history, streaming, metrics."""
import pytest

from helpers import IMPROVED, LONG_PROMPT, TOOLS, STRUCTURE, Upstream, main_calls, make, metrics, post, \
    structurer_calls
from interceptor.gate import MARKER_TAG
from interceptor.inject import ORIGINAL_TAG


# ---- untouched ---------------------------------------------------------------
@pytest.mark.parametrize("body, action", [
    ({"messages": [{"role": "user", "content": "gracias"}]}, "passthrough_gate"),
    ({"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                                 "content": LONG_PROMPT}]}]}, "passthrough_non_human_turn"),
], ids=["short-prompt", "tool-loop"])
def test_requests_that_pass_untouched(tmp_path, body, action):
    up = Upstream()
    client, mpath = make(tmp_path, up)
    post(client, **body)
    assert len(up.calls) == 1 and up.calls[0]["body"]["messages"] == body["messages"]
    assert metrics(mpath)[0]["action"] == action


def test_side_requests_without_tools_are_untouched(tmp_path):
    up = Upstream()
    client, mpath = make(tmp_path, up, require_tools=True)
    post(client, LONG_PROMPT, model="claude-haiku-4-5")
    assert len(up.calls) == 1 and up.calls[0]["body"]["messages"][0]["content"] == LONG_PROMPT
    assert metrics(mpath)[0]["action"] == "passthrough_side_request"


def test_cross_session_message_reaches_the_model_unchanged_without_interpreter_calls(tmp_path):
    from test_gate import PEER
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, mpath = make(tmp_path, up, require_tools=True)
    post(client, [{"role": "user", "content": [{"type": "text", "text": PEER}]}], model="claude-opus-5-5",
         tools=TOOLS)
    assert len(up.calls) == 1 and main_calls(up)[0]["body"]["messages"][0]["content"][0]["text"] == PEER
    assert metrics(mpath)[0]["skip_reason"] == "cross_session"


def test_other_endpoints_pass_through(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up)
    r = client.post("/v1/messages/count_tokens", json={"model": "m", "messages": []})
    assert r.status_code == 200 and up.calls[0]["path"] == "/v1/messages/count_tokens"
    assert client.head("/api/hello").status_code == 200


@pytest.mark.parametrize("failure", [{"structurer_status": 500}], ids=["structurer-500"])
def test_fail_open_sends_the_original(tmp_path, failure):
    up = Upstream(**failure)
    client, mpath = make(tmp_path, up)
    assert post(client, LONG_PROMPT).status_code == 200
    assert main_calls(up)[0]["body"]["messages"][0]["content"] == LONG_PROMPT
    assert metrics(mpath)[0]["action"] == "error"


# ---- rewriting ---------------------------------------------------------------
def test_rewrite_puts_improved_prompt_first_and_keeps_original(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, mpath = make(tmp_path, up)
    post(client, LONG_PROMPT)
    sent = main_calls(up)[0]["body"]["messages"][-1]["content"]
    assert sent[0]["text"] == IMPROVED
    assert ORIGINAL_TAG in sent[1]["text"] and LONG_PROMPT in sent[1]["text"]
    rec = metrics(mpath)[0]
    assert rec["action"] == "structured" and rec["mode"] == "rewrite" and rec["risky_assumptions"] == 1


def test_append_mode_keeps_original_first_and_adds_structure(tmp_path):
    up = Upstream()  # no improved_prompt -> append
    client, _ = make(tmp_path, up)
    post(client, LONG_PROMPT)
    sent = main_calls(up)[0]["body"]["messages"][-1]["content"]
    assert sent[0]["text"] == LONG_PROMPT and MARKER_TAG in sent[1]["text"]


def test_rewrite_preserves_reminders_images_and_cache_control(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, _ = make(tmp_path, up)
    img = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
    post(client, [{"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>CLAUDE.md rules</system-reminder>"}, img,
        {"type": "text", "text": LONG_PROMPT, "cache_control": {"type": "ephemeral"}}]}])
    sent = main_calls(up)[0]["body"]["messages"][-1]["content"]
    assert sent[0]["text"] == "<system-reminder>CLAUDE.md rules</system-reminder>" and sent[1] == img
    assert sent[2]["text"] == IMPROVED and sent[-1]["cache_control"] == {"type": "ephemeral"}


def test_trailing_system_message_is_skipped_to_find_user_turn(tmp_path):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, _ = make(tmp_path, up, require_tools=True)
    post(client, [{"role": "user", "content": [{"type": "text", "text": LONG_PROMPT}]},
                  {"role": "system", "content": [{"type": "text", "text": "x" * 42000}]}], tools=TOOLS)
    sent = main_calls(up)[0]["body"]["messages"]
    assert sent[0]["content"][0]["text"] == IMPROVED and len(sent[1]["content"][0]["text"]) == 42000


@pytest.mark.parametrize("improved, system_tail", [(None, False), (IMPROVED, False), (IMPROVED, True)],
                         ids=["append", "rewrite", "rewrite-with-system-tail"])
def test_history_is_rebuilt_identically_next_turn_without_reinterpreting(tmp_path, improved, system_tail):
    """The client resends its ORIGINAL text next turn; the interceptor must rebuild the same bytes
    (stable prefix = the prompt cache keeps working) without paying another interpreter call."""
    up = Upstream(structure={**STRUCTURE, **({"improved_prompt": improved} if improved else {})})
    client, _ = make(tmp_path, up, require_tools=True)
    user1 = {"role": "user", "content": [{"type": "text", "text": LONG_PROMPT}]}
    tail = [{"role": "system", "content": [{"type": "text", "text": "ctx"}]}] if system_tail else []
    post(client, [user1, *tail], tools=TOOLS)
    post(client, [user1, *tail, {"role": "assistant", "content": "resp"},
                  {"role": "user", "content": [{"type": "text", "text": "mostrame lo que recibiste"}]}], tools=TOOLS)
    first, second = (c["body"]["messages"][0]["content"] for c in main_calls(up))
    assert first == second and second != user1["content"]
    assert len(structurer_calls(up)) == 1


# ---- interpreter call --------------------------------------------------------
def test_interpreter_call_is_forced_to_the_tool_and_gets_only_oauth_betas(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up)
    client.post("/v1/messages", headers={"authorization": "Bearer tok",
                                         "anthropic-beta": "context-1m-2025-08-07, oauth-2025-04-20"},
                json={"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": LONG_PROMPT}]})
    call = structurer_calls(up)[0]
    assert call["body"]["tool_choice"] == {"type": "tool", "name": "submit_structure"}
    assert call["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    assert call["headers"]["authorization"] == "Bearer tok"
    assert "context-1m" in main_calls(up)[0]["headers"]["anthropic-beta"]  # main request untouched


def test_text_fallback_when_gateway_ignores_tools(tmp_path):
    up = Upstream(as_tool=False)
    client, mpath = make(tmp_path, up)
    post(client, LONG_PROMPT)
    assert metrics(mpath)[0]["action"] == "structured"


@pytest.mark.parametrize("seconds, read_timeout", [(0, None), (15, 15)], ids=["no-limit", "limit"])
def test_interpreter_timeout(tmp_path, seconds, read_timeout):
    """0 (the default) lets the interpreter finish its work; only connecting is bounded."""
    up = Upstream()
    client, _ = make(tmp_path, up, structurer_timeout=seconds)
    post(client, LONG_PROMPT)
    timeout = structurer_calls(up)[0]["timeout"]
    assert timeout["read"] == read_timeout
    assert timeout["connect"] == (10.0 if seconds == 0 else seconds)


# ---- streaming and metrics ---------------------------------------------------
def test_streaming_relays_and_reads_usage(tmp_path):
    up = Upstream()
    client, mpath = make(tmp_path, up)
    with client.stream("POST", "/v1/messages", json={"model": "m", "max_tokens": 5, "stream": True,
                                                    "messages": [{"role": "user", "content": LONG_PROMPT}]}) as r:
        data = b"".join(r.iter_bytes())
    assert b"message_delta" in data
    assert metrics(mpath)[0]["upstream_usage"] == {"input_tokens": 900, "output_tokens": 42}


def test_metrics_and_audit_are_off_by_default(tmp_path):
    up = Upstream()
    client, mpath = make(tmp_path, up, metrics_enabled=False)
    post(client, LONG_PROMPT)
    assert not mpath.exists() and up.audits == []
