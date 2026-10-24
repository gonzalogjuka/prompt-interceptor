"""What counts as a human turn, what is worth rewriting, and other pure text decisions."""
import pytest

from helpers import LONG_PROMPT
from interceptor.gate import (find_target, human_texts, inspect, is_continuation, preview,
                              previous_assistant_text, shape, should_structure)

PEER = ("Another Claude session sent a message:\n<cross-session-message from=\"uds:x\" "
        "from-name=\"infrastructure-23\" from-mode=\"bypass\">\n" + LONG_PROMPT + "\n</cross-session-message>")


def text(t):
    return {"role": "user", "content": [{"type": "text", "text": t}]}


@pytest.mark.parametrize("message, expected", [
    # human text, with client context around it
    (text("<system-reminder>ruido</system-reminder>hola"), ("hola", "ok")),
    ({"role": "user", "content": [{"type": "text", "text": "<system-reminder>notas sobre <interceptor_structure>"
                                                          "</system-reminder>"},
                                  {"type": "text", "text": LONG_PROMPT}]}, (LONG_PROMPT, "ok")),
    ({"role": "user", "content": "[1, 2, 3] ordename esto"}, ("[1, 2, 3] ordename esto", "ok")),
    # not typed by the human
    ({"role": "user", "content": [{"type": "tool_result", "content": "x"}]}, (None, "tool_result_turn")),
    ({"role": "assistant", "content": "{"}, (None, "last_role=assistant")),
    (text("<system-reminder>solo contexto</system-reminder>"), (None, "no_human_text")),
    (text("<interceptor_original>ya procesado</interceptor_original>"), (None, "already_processed")),
    ({"role": "user", "content": "[Request interrupted by user]"}, (None, "client_marker")),
    ({"role": "user", "content": "[Request interrupted by user for tool use]"}, (None, "client_marker")),
    ({"role": "user", "content": "<command-name>/model</command-name>"}, (None, "client_marker")),
    ({"role": "user", "content": "<local-command-stdout>Set model</local-command-stdout>"}, (None, "client_marker")),
    ({"role": "user", "content": "[SUGGESTION MODE: Suggest what the user might naturally type next]\n" + LONG_PROMPT},
     (None, "client_marker")),
    (text(PEER), (None, "cross_session")),
    (text("[Cross-session delivery notice] Your message to another session was held by that session."),
     (None, "cross_session")),
], ids=["reminder-stripped", "reminder-mentions-marker", "brackets-are-human", "tool-result", "assistant",
        "only-reminders", "already-processed", "interrupted", "interrupted-tool", "slash-command", "command-output",
        "suggestion-mode", "peer-session", "delivery-notice"])
def test_inspect_decides_what_is_human_text(message, expected):
    assert inspect(message) == expected


def test_should_structure_needs_length_or_signals_and_honors_raw():
    assert should_structure("gracias!", 400)[0] is False
    assert should_structure(LONG_PROMPT + " #raw", 400) == (False, ["bypass_token"])
    assert should_structure(LONG_PROMPT, 400)[0] is True


@pytest.mark.parametrize("t", ["ok", "dale", "Dale!", "sí", "seguí", "ok seguí", "dale, hacelo", "continuá",
                               "gracias", "go on"])
def test_continuations(t):
    assert is_continuation(t)


@pytest.mark.parametrize("t", ["ok pero cambiá el puerto a 8080", "dale pero sin helm",
                               "rediseñá la arquitectura del pipeline", "okey y como lo pruebo?",
                               "si podés agregale tests"])
def test_not_continuations(t):
    assert not is_continuation(t)


def test_preview_is_single_line_and_bounded():
    p = preview("linea uno\n\n  linea dos " + "x" * 200)
    assert "\n" not in p and len(p) == 60 and p.endswith("…")


def test_find_target_skips_trailing_system_entries():
    turn = [{"role": "user", "content": "hola"}, {"role": "assistant", "content": "ok"}]
    assert find_target(turn) == 1
    # Claude Code appends role=system entries after the user's own turn
    assert find_target(turn + [{"role": "system", "content": "env info"}]) == 1
    assert find_target(turn + [{"role": "system", "content": "a"}, {"role": "system", "content": "b"}]) == 1


def test_find_target_is_none_when_nothing_but_system_entries():
    assert find_target([]) is None
    assert find_target([{"role": "system", "content": "a"}]) is None


def test_previous_assistant_text_finds_the_last_one_before_the_given_index():
    messages = [
        {"role": "user", "content": "primero"},
        {"role": "assistant", "content": "primera respuesta"},
        {"role": "user", "content": "segundo"},
        {"role": "assistant", "content": [{"type": "text", "text": "segunda "}, {"type": "text", "text": "respuesta"}]},
        {"role": "user", "content": "tercero"},
    ]
    assert previous_assistant_text(messages, 4) == "segunda \nrespuesta"
    assert previous_assistant_text(messages, 2) == "primera respuesta"


def test_previous_assistant_text_is_empty_when_there_is_none_before():
    messages = [{"role": "user", "content": "primero"}, {"role": "assistant", "content": "x"}]
    assert previous_assistant_text(messages, 0) == ""
    assert previous_assistant_text([], 0) == ""


@pytest.mark.parametrize("message, expected", [
    (None, "none"),
    ({"role": "user", "content": "hola"}, "user[str:4]"),
    ({"role": "user", "content": [{"type": "text", "text": "hola"}]}, "user[text:4]"),
    ({"role": "user", "content": [{"type": "text", "text": "<system-reminder>x</system-reminder>"}]},
     "user[reminder:36]"),
    ({"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}, "user[tool_result]"),
    ({"role": "user", "content": ["not a dict"]}, "user[?]"),
    ({"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "tool_result", "content": "b"}]},
     "user[text:1, tool_result]"),
], ids=["none", "str-content", "text-block", "reminder-block", "tool-result", "non-dict-block", "mixed-blocks"])
def test_shape_describes_a_message_for_logs(message, expected):
    assert shape(message) == expected


def test_human_texts_collects_only_human_typed_user_messages_in_order():
    messages = [
        {"role": "user", "content": "primero"},
        {"role": "assistant", "content": "respuesta"},
        {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
        {"role": "user", "content": "<interceptor_original>ya procesado</interceptor_original>"},
        {"role": "user", "content": "segundo"},
    ]
    assert human_texts(messages) == ["primero", "segundo"]


def test_human_texts_is_empty_for_no_user_messages():
    assert human_texts([{"role": "assistant", "content": "x"}]) == []
