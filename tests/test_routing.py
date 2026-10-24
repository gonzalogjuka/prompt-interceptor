"""Who interprets, who answers and with which effort: rules, shadow, override, sticky turn, fallback."""
import pytest

from helpers import HAIKU, IMPROVED, LONG_PROMPT, OPUS, SONNET, STRUCTURE, TIERS, TOOLS, Upstream, main_calls, \
    make, metrics, post, routes, structurer_calls, tool_loop
from interceptor.routing import Router, Rule, find_override

ALL_TO = lambda model: [{"name": "all", "when": {}, "model": model}]  # noqa: E731
INTERPRETERS = {"interpreters": {"moderate": "haiku", "complex": "sonnet"}}


def classified(complexity, deliverable="answer"):
    return {"complexity": complexity, "deliverable": deliverable, "reason": "test"}


# ---- pure matching logic (interceptor/routing.py), no app involved ------------
def test_rule_matches_context_kinds():
    structure = {"context": [{"kind": "error", "summary": "x"}, {"kind": "log", "summary": "y"}]}
    assert Rule("r", "haiku", when={"context_kinds": ["log", "config"]}).matches(structure, "texto", "opus")
    assert not Rule("r", "haiku", when={"context_kinds": ["config"]}).matches(structure, "texto", "opus")
    assert not Rule("r", "haiku", when={"context_kinds": ["config"]}).matches({}, "texto", "opus")


def test_rule_matches_min_and_max_chars():
    rule = Rule("r", "haiku", when={"min_chars": 10, "max_chars": 20})
    assert rule.matches({}, "x" * 15, "opus")
    assert rule.matches({}, "x" * 10, "opus") and rule.matches({}, "x" * 20, "opus")  # bordes inclusivos
    assert not rule.matches({}, "x" * 9, "opus")
    assert not rule.matches({}, "x" * 21, "opus")


def test_rule_matches_from_models():
    rule = Rule("r", "haiku", when={"from_models": ["opus", "sonnet"]})
    assert rule.matches({}, "x", "claude-opus-5-5")
    assert not rule.matches({}, "x", "claude-haiku-4-5-20251001")


def test_rule_with_no_conditions_matches_anything():
    assert Rule("r", "haiku").matches({}, "", "cualquier-modelo")


@pytest.mark.parametrize("text, expected", [
    ("explicame esto #model:sonnet", "sonnet"),
    ("#model:opus al principio", "opus"),
    ("texto #model:sonnet #model:opus", "sonnet"),  # gana el primero
    ("sin override", None),
    ("texto#model:sonnet", None),  # pegado a una palabra: no es un override
    ("##model:sonnet", None),  # precedido por #: no es un override
], ids=["mid-text", "start", "first-wins", "none", "glued-to-word", "double-hash"])
def test_find_override(text, expected):
    assert find_override(text) == expected


def test_router_load_without_file_keeps_legacy_single_rule():
    router = Router.load(None, HAIKU)
    assert [r.when for r in router.rules] == [{"complexity": ["simple"]}]
    assert router.resolve("haiku") == HAIKU


def test_router_load_alias_keys_are_case_insensitive(tmp_path):
    path = routes(tmp_path, [], aliases={"MiModelo": "custom-model-x"})
    router = Router.load(path, HAIKU)
    assert router.resolve("mimodelo") == router.resolve("MiModelo") == "custom-model-x"


def test_router_load_drops_empty_interpreter_entries(tmp_path):
    path = routes(tmp_path, [], interpreters={"moderate": "haiku", "complex": ""})
    router = Router.load(path, HAIKU)
    assert router.interpreters == {"moderate": "haiku"}
    assert router.interpreter_for("complex") is None
    assert router.interpreter_for("simple") is None


# ---- who answers: rules ------------------------------------------------------
def test_enforce_routes_by_rule_and_keeps_text_of_simple_requests(tmp_path):
    rules = [{"name": "simple", "when": {"complexity": ["simple"]}, "model": "haiku"},
             {"name": "code", "when": {"complexity": ["moderate"], "deliverable": ["analysis"]}, "model": "sonnet"}]
    up = Upstream()  # STRUCTURE: moderate/analysis
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, rules))
    post(client, LONG_PROMPT, model=OPUS)
    assert main_calls(up)[0]["body"]["model"] == SONNET
    assert metrics(mpath)[0]["route_rule"] == "code" and metrics(mpath)[0]["routed_from"] == OPUS

    up = Upstream(structure={**STRUCTURE, "complexity": "simple", "improved_prompt": IMPROVED})
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, rules))
    post(client, LONG_PROMPT, model=OPUS)
    sent = main_calls(up)[0]["body"]
    assert sent["model"] == HAIKU and sent["messages"][0]["content"] == LONG_PROMPT  # routed, not rewritten
    assert metrics(mpath)[-1]["action"] == "classified_only"  # same tmp_path: [0] is the first request


def test_simple_requests_rewritten_when_skip_simple_disabled(tmp_path):
    up = Upstream(structure={**STRUCTURE, "complexity": "simple", "improved_prompt": IMPROVED})
    client, _ = make(tmp_path, up, skip_simple=False)
    post(client, LONG_PROMPT)
    assert main_calls(up)[0]["body"]["messages"][0]["content"][0]["text"] == IMPROVED


def test_without_routes_file_simple_goes_to_model_simple(tmp_path):
    up = Upstream(structure={**STRUCTURE, "complexity": "simple"})
    client, _ = make(tmp_path, up, routing_mode="enforce", model_simple=HAIKU)
    post(client, LONG_PROMPT, model=OPUS)
    assert main_calls(up)[0]["body"]["model"] == HAIKU


def test_shadow_reports_model_and_effort_but_changes_nothing(tmp_path):
    rules = [{"name": "all", "when": {}, "model": "haiku", "effort": "low"}]
    up = Upstream()
    client, mpath = make(tmp_path, up, routing_mode="shadow", routes_file=routes(tmp_path, rules))
    post(client, LONG_PROMPT, model=OPUS, output_config={"effort": "high"})
    sent = main_calls(up)[0]["body"]
    assert sent["model"] == OPUS and sent["output_config"] == {"effort": "high"}
    rec = metrics(mpath)[0]
    assert rec["route_shadow"] == HAIKU and rec["effort_shadow"] == "low"


@pytest.mark.parametrize("history_limit", [100_000, 10], ids=["normal", "history-too-large"])
def test_manual_override_works_with_routing_off_and_any_history(tmp_path, history_limit):
    up = Upstream()
    client, _ = make(tmp_path, up, route_max_history_chars=history_limit)  # routing off
    post(client, "explicame esto #model:sonnet", model=OPUS)
    assert up.calls[0]["body"]["model"] == SONNET


def test_routing_skipped_when_history_is_large(tmp_path):
    up = Upstream()
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, ALL_TO("haiku")),
                         route_max_history_chars=1000)
    post(client, [{"role": "user", "content": "x" * 2000}, {"role": "assistant", "content": "ok"},
                  {"role": "user", "content": LONG_PROMPT}], model=OPUS)
    assert main_calls(up)[0]["body"]["model"] == OPUS
    assert metrics(mpath)[0]["route_skipped"] == "history_too_large"


def test_routed_model_and_effort_stick_for_the_whole_turn(tmp_path):
    rules = [{"name": "all", "when": {}, "model": "sonnet", "effort": "medium"}]
    up = Upstream()
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, rules),
                         require_tools=True)
    turn = [{"role": "user", "content": [{"type": "text", "text": LONG_PROMPT}]}]
    for messages in (turn, tool_loop(turn)):
        post(client, messages, model=OPUS, tools=TOOLS, output_config={"effort": "xhigh"})
    assert [(c["body"]["model"], c["body"]["output_config"]) for c in main_calls(up)] == \
        [(SONNET, {"effort": "medium"})] * 2
    assert metrics(mpath)[0]["effort_from"] == "xhigh"


# ---- fallback when the routed model or effort is refused ----------------------
def test_refused_routed_model_falls_back_with_the_original_body_and_betas(tmp_path):
    up = Upstream(reject_models={HAIKU})
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, ALL_TO("haiku")))
    r = post(client, [{"role": "user", "content": LONG_PROMPT},
                      {"role": "system", "content": [{"type": "text", "text": "env",
                                                      "cache_control": {"type": "ephemeral"}}],
                       "output_config": {"effort": "high"}}],
             model=OPUS, max_tokens=128000, thinking={"type": "adaptive"}, output_config={"effort": "high"},
             context_management={"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}, system="base",
             headers={"anthropic-beta": "context-1m-2025-08-07, oauth-2025-04-20"})
    assert r.status_code == 200
    routed, fallback = main_calls(up)
    # routed to Haiku: body fitted (no adaptive thinking/effort, 64k max, mid system merged), no 1M beta
    rb = routed["body"]
    assert rb["model"] == HAIKU and rb["max_tokens"] == 64000
    assert [m["role"] for m in rb["messages"]] == ["user"]
    assert rb["system"] == [{"type": "text", "text": "base"}, {"type": "text", "text": "env"}]
    assert not {"thinking", "context_management", "output_config"} & rb.keys()
    assert routed["headers"]["anthropic-beta"] == "oauth-2025-04-20"
    # fallback: the client's request exactly as it was
    fb = fallback["body"]
    assert fb["model"] == OPUS and fb["max_tokens"] == 128000 and fb["thinking"] == {"type": "adaptive"}
    assert fb["messages"][1]["role"] == "system" and fb["system"] == "base"
    assert fb["output_config"] == {"effort": "high"} and "context_management" in fb
    assert "context-1m" in fallback["headers"]["anthropic-beta"]
    rec = metrics(mpath)[0]
    assert rec["route_fallback"] is True and rec["model_used"] == OPUS


def test_body_untouched_when_routed_to_a_model_with_adaptive_thinking(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, ALL_TO("sonnet")))
    post(client, LONG_PROMPT, model=OPUS, max_tokens=128000, thinking={"type": "adaptive"},
         output_config={"effort": "high"})
    body = main_calls(up)[0]["body"]
    assert body["thinking"] == {"type": "adaptive"} and body["max_tokens"] == 128000
    assert body["output_config"] == {"effort": "high"}


def test_refused_effort_falls_back_to_the_clients(tmp_path):
    rules = [{"name": "moderado", "when": {"complexity": ["moderate"]}, "model": OPUS, "effort": "medium"}]
    up = Upstream(classification=classified("moderate"), reject_effort="medium")
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, rules))
    assert post(client, "explicame esto", model=OPUS, output_config={"effort": "xhigh"}).status_code == 200
    assert [c["body"]["output_config"] for c in main_calls(up)] == [{"effort": "medium"}, {"effort": "xhigh"}]
    assert metrics(mpath)[0]["route_fallback"] is True


def test_effort_is_not_sent_to_models_without_it(tmp_path):
    rules = [{"name": "simple", "when": {"complexity": ["simple"]}, "model": "haiku", "effort": "low"}]
    up = Upstream()
    client, _ = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, rules))
    post(client, "que hora es en UTC?", model=OPUS)
    sent = main_calls(up)[0]["body"]
    assert sent["model"] == HAIKU and "output_config" not in sent


# ---- short messages: classified by complexity, not by length -------------------
@pytest.mark.parametrize("complexity, requested, expected", [
    ("complex", SONNET, OPUS), ("simple", OPUS, HAIKU)], ids=["short-but-complex", "short-and-simple"])
def test_short_message_is_routed_by_the_work_it_implies(tmp_path, complexity, requested, expected):
    up = Upstream(classification=classified(complexity))
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, TIERS))
    short = "rediseñá la arquitectura del pipeline de VDI"
    post(client, short, model=requested)
    sent = main_calls(up)[0]["body"]
    assert sent["model"] == expected and sent["messages"][0]["content"] == short  # never rewritten
    rec = metrics(mpath)[0]
    assert rec["action"] == "classified_short" and rec["complexity"] == complexity


@pytest.mark.parametrize("routing, text", [("off", "que es un configmap?"), ("enforce", "que es un configmap? #raw")],
                         ids=["routing-off", "raw"])
def test_no_classification(tmp_path, routing, text):
    up = Upstream()
    client, _ = make(tmp_path, up, routing_mode=routing, routes_file=routes(tmp_path, TIERS))
    post(client, text)
    assert up.classified == []


def test_classifier_sees_the_previous_answer(tmp_path):
    up = Upstream(classification=classified("complex", "code"))
    client, _ = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, TIERS))
    post(client, [{"role": "user", "content": "como organizo el terraform?"},
                  {"role": "assistant", "content": [{"type": "text", "text": "Te propongo separar en módulos por cloud."}]},
                  {"role": "user", "content": "hacé eso para los 8 providers"}], model=SONNET)
    sent = up.classified[0]["messages"][0]["content"]
    assert "Te propongo separar en módulos por cloud." in sent and "hacé eso para los 8 providers" in sent


def test_classifier_failure_keeps_the_requested_model(tmp_path):
    up = Upstream(classifier_status=500)
    client, mpath = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, TIERS))
    assert post(client, "que es un configmap?", model=OPUS).status_code == 200
    assert main_calls(up)[0]["body"]["model"] == OPUS and metrics(mpath)[0]["action"] == "error"


# ---- continuations ("dale", "seguí") -----------------------------------------
TURN1 = {"role": "user", "content": "migrá el cluster a la nueva vnet"}


@pytest.mark.parametrize("mode, expected, field", [("enforce", OPUS, "route_how"), ("shadow", SONNET, "route_shadow")])
def test_continuation_inherits_previous_turn_model_without_classifying(tmp_path, mode, expected, field):
    up = Upstream(classification=classified("complex", "action"))
    client, mpath = make(tmp_path, up, routing_mode=mode, routes_file=routes(tmp_path, TIERS))
    post(client, [TURN1], model=SONNET)
    post(client, [TURN1, {"role": "assistant", "content": "plan"}, {"role": "user", "content": "dale, hacelo"}],
         model=SONNET)
    assert [c["body"]["model"] for c in main_calls(up)] == [expected] * 2
    assert len(up.classified) == 1  # "dale, hacelo" was not sent to the classifier
    rec = metrics(mpath)[1]
    assert rec["action"] == "continuation"
    assert rec[field] == ("inherit" if mode == "enforce" else OPUS)


def test_same_short_text_in_different_conversations_does_not_collide(tmp_path):
    up = Upstream(classification=classified("complex", "action"))
    client, _ = make(tmp_path, up, routing_mode="enforce", routes_file=routes(tmp_path, TIERS))
    post(client, [TURN1], model=SONNET)
    post(client, [TURN1, {"role": "assistant", "content": "plan"}, {"role": "user", "content": "ok"}], model=SONNET)
    post(client, "ok", model=SONNET)  # another conversation whose first turn is also "ok"
    assert main_calls(up)[-1]["body"]["model"] == SONNET


# ---- who interprets ----------------------------------------------------------
@pytest.mark.parametrize("complexity, interpreter", [("moderate", HAIKU), ("complex", SONNET)])
def test_interpreter_is_chosen_by_complexity(tmp_path, complexity, interpreter):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED}, classification=classified(complexity))
    client, mpath = make(tmp_path, up, routes_file=routes(tmp_path, [], **INTERPRETERS))
    post(client, LONG_PROMPT, model=OPUS)
    assert len(up.classified) == 1 and structurer_calls(up)[0]["body"]["model"] == interpreter
    assert metrics(mpath)[0]["interpreter_model"] == interpreter


def test_simple_long_prompt_skips_the_interpreter_entirely(tmp_path):
    up = Upstream(classification=classified("simple"))
    client, mpath = make(tmp_path, up, routes_file=routes(tmp_path, [], **INTERPRETERS))
    post(client, LONG_PROMPT, model=OPUS)
    assert structurer_calls(up) == []
    assert main_calls(up)[0]["body"]["messages"][0]["content"] == LONG_PROMPT
    assert metrics(mpath)[0]["action"] == "classified_only"


@pytest.mark.parametrize("routes_extra, classifier_status, classified_calls",
                         [(None, 200, 0), (INTERPRETERS, 500, 1)], ids=["no-interpreters", "classifier-failed"])
def test_default_interpreter_rewrites(tmp_path, routes_extra, classifier_status, classified_calls):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED}, classifier_status=classifier_status)
    client, mpath = make(tmp_path, up, routes_file=routes(tmp_path, [], **(routes_extra or {})))
    post(client, LONG_PROMPT)
    assert len(up.classified) == classified_calls
    assert structurer_calls(up)[0]["body"]["model"] == HAIKU
    assert metrics(mpath)[0]["action"] == "structured"
