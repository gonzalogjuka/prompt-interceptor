"""What the interpreter knows besides the message, taken from the request itself.

Claude Code sends the CLAUDE.md files and the memory index of the project it runs in on every
request, plus the environment (working directory, platform, date) and the whole conversation.
Handing that to the structurer and the classifier lets them read the message the way the main
model will: the user's vocabulary, conventions, projects, and what "that" refers to. Nothing to
configure: wherever the client runs, the context of that project comes with the request.
perfil.md adds what should travel with the interceptor itself (other PCs, clients without CLAUDE.md).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .gate import inspect

_REMINDER_RE = re.compile(r"<system-reminder>(.*?)</system-reminder>", re.DOTALL)
# reminders worth keeping: instruction files and memory, not tool or plan-mode chatter
_INSTRUCTION_HINTS = ("CLAUDE.md", "MEMORY.md", "AGENTS.md", "CLAUDE.local.md")
_ENV_RE = re.compile(r"^#+\s*Environment\b.*?(?=^#+\s|\Z)", re.DOTALL | re.MULTILINE | re.IGNORECASE)
_DATE_RE = re.compile(r"^.*\b(Today's date|current date)\b.*$", re.MULTILINE | re.IGNORECASE)
# a client without instruction files (SDK, other tools): its own system prompt is the context
_SMALL_SYSTEM_CHARS = 8000


def _texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]


def _clip(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


@dataclass
class RequestContext:
    profile: str = ""       # perfil.md: the user's context that travels with the interceptor
    instructions: str = ""  # CLAUDE.md files, memory index (or a small client system prompt)
    environment: str = ""   # working directory, platform, date
    conversation: str = ""  # last exchanges before the message
    turns: int = 0

    @property
    def stable(self) -> str:
        """The part that stays the same for a whole session: sent cached."""
        parts = []
        if self.profile:
            parts.append(f"<user_profile>\n{self.profile}\n</user_profile>")
        if self.instructions:
            parts.append(f"<user_instructions>\n{self.instructions}\n</user_instructions>")
        if self.environment:
            parts.append(f"<environment>\n{self.environment}\n</environment>")
        return "\n".join(parts)

    def summary(self) -> str:
        """One-line description for the log, so every prompt shows what the interpreter saw."""
        bits = [f"perfil {len(self.profile) / 1000:.1f}k"] if self.profile else []
        bits.append(f"instrucciones {len(self.instructions) / 1000:.1f}k" if self.instructions else "sin instrucciones")
        if self.environment:
            bits.append("entorno")
        bits.append(f"{self.turns} msgs de conversación" if self.turns else "sin conversación")
        return " + ".join(bits)


def load_profile(path: str | None) -> str:
    """perfil.md lives in the interceptor folder, so it goes wherever the interceptor goes."""
    if not path or not Path(path).is_file():
        return ""
    return Path(path).read_text(encoding="utf-8").strip()


def extract(body: dict[str, Any], messages: list[dict[str, Any]], target: int,
            max_chars: int, max_turns: int, profile: str = "") -> RequestContext:
    """Build the context of the message at index `target` from the request body."""
    ctx = RequestContext(profile=profile)
    seen: set[str] = set()
    instructions: list[str] = []
    for m in messages[: target + 1]:
        if m.get("role") != "user":
            continue
        for t in _texts(m.get("content")):
            for r in _REMINDER_RE.findall(t):
                r = r.strip()
                if r not in seen and any(h in r for h in _INSTRUCTION_HINTS):
                    seen.add(r)
                    instructions.append(r)

    system = "\n".join(_texts(body.get("system")))
    if not instructions and system and len(system) <= _SMALL_SYSTEM_CHARS:
        instructions.append(system)
    ctx.instructions = _clip("\n\n".join(instructions), max_chars)

    env = _ENV_RE.search(system)
    env_text = env.group(0).strip() if env else ""
    date = _DATE_RE.search(system)
    if date and date.group(0) not in env_text:
        env_text = f"{env_text}\n{date.group(0).strip()}".strip()
    ctx.environment = _clip(env_text, 2000)

    lines: list[str] = []
    for m in reversed(messages[:target]):
        if len(lines) >= max_turns:
            break
        role = m.get("role")
        if role == "user":
            text, _ = inspect(m)  # only what the human typed: no tool results, markers or peers
        elif role == "assistant":
            text = "\n".join(_texts(m.get("content"))).strip()
        else:
            continue
        if text:
            who = "user" if role == "user" else "assistant"
            lines.append(f"<{who}>\n{_clip(text, 800)}\n</{who}>")
    ctx.turns = len(lines)
    ctx.conversation = "\n".join(reversed(lines))
    return ctx
