"""What counts as a human turn, what is worth rewriting, and other pure text decisions."""
import pytest

from helpers import LONG_PROMPT
from interceptor.gate import inspect, is_continuation, preview, should_structure

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
