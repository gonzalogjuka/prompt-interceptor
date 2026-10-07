"""What the interpreter sees besides the message: instructions, memory, environment, conversation, profile."""
import pytest

from helpers import IMPROVED, LONG_PROMPT, STRUCTURE, TIERS, TOOLS, Upstream, make, metrics, post, routes, \
    structurer_calls
from interceptor.context import extract

CC_SYSTEM = [{"type": "text", "text": "You are Claude Code.\n# Tools\nmuchas reglas\n# Environment\n"
                                      " - Primary working directory: C:\\Repos\\Infra\n - Platform: win32\n"
                                      "# Otra seccion\nnada"},
             {"type": "text", "text": "Today's date is 2026-10-07."}]
CLAUDE_MD = ("<system-reminder>\nContents of C:\\Users\\g\\.claude\\CLAUDE.md:\nResponder en español. "
             "Prueba valida = deploy desde la UI de CM.\n</system-reminder>")
PLAN_NOISE = "<system-reminder>Plan mode is active.</system-reminder>"


def cc_messages(final):
    """A Claude Code conversation: instructions in reminders, a tool loop, then the new message."""
    return [{"role": "user", "content": [{"type": "text", "text": CLAUDE_MD}, {"type": "text", "text": PLAN_NOISE},
                                         {"type": "text", "text": "armame el modelo de OCI"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Listo el modelo OCI, falta probarlo."},
                                              {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
            {"role": "user", "content": [{"type": "text", "text": final}]}]


def test_extract_takes_instructions_environment_and_conversation():
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
    ctx = extract({"system": "Sos el asistente de soporte de Cybele."}, [{"role": "user", "content": "hola"}],
                  0, 30000, 6)
    assert ctx.instructions == "Sos el asistente de soporte de Cybele."


def test_interpreter_gets_cached_context_then_conversation_then_message(tmp_path):
    perfil = tmp_path / "perfil.md"
    perfil.write_text("Gonzalo, infraestructura en Cybele.", encoding="utf-8")
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, mpath = make(tmp_path, up, require_tools=True, profile_file=str(perfil))
    post(client, cc_messages(LONG_PROMPT), model="claude-opus-5-5", tools=TOOLS, system=CC_SYSTEM)
    cached, tail = structurer_calls(up)[0]["body"]["messages"][0]["content"]
    assert cached["cache_control"] == {"type": "ephemeral"}
    assert cached["text"].startswith("<user_profile>\nGonzalo, infraestructura en Cybele.")
    assert "<user_instructions>" in cached["text"] and "deploy desde la UI de CM" in cached["text"]
    assert "<environment>" in cached["text"]
    assert "<recent_conversation>" in tail["text"] and "falta probarlo" in tail["text"]
    assert tail["text"].endswith(f"<message>\n{LONG_PROMPT}\n</message>")
    rec = metrics(mpath)[0]
    assert rec["context_turns"] == 2 and rec["context_instructions_chars"] > 0


def test_classifier_gets_the_same_cached_context(tmp_path):
    up = Upstream()
    client, _ = make(tmp_path, up, require_tools=True, routing_mode="enforce", routes_file=routes(tmp_path, TIERS))
    post(client, cc_messages("¿y para AWS?"), model="claude-opus-5-5", tools=TOOLS, system=CC_SYSTEM)
    cached, request = up.classified[0]["messages"][0]["content"]
    assert cached["cache_control"] == {"type": "ephemeral"} and "deploy desde la UI de CM" in cached["text"]
    assert request["text"].endswith("<request>\n¿y para AWS?\n</request>")


@pytest.mark.parametrize("overrides", [{"context": False}, {"profile_file": None}], ids=["context-off", "nothing"])
def test_without_context_the_interpreter_gets_only_the_message(tmp_path, overrides):
    up = Upstream(structure={**STRUCTURE, "improved_prompt": IMPROVED})
    client, _ = make(tmp_path, up, **overrides)
    post(client, LONG_PROMPT)  # plain request: no system prompt, no history, no profile
    assert structurer_calls(up)[0]["body"]["messages"][0]["content"] == LONG_PROMPT
