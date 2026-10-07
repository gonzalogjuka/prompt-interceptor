"""Pick a model per task from user-editable rules.

routes.json:
{
  "aliases": {"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"},
  "interpreters": {"moderate": "haiku", "complex": "sonnet"},
  "rules": [
    {"name": "simple-answers", "when": {"complexity": ["simple"]}, "model": "haiku"},
    {"name": "moderate", "when": {"complexity": ["moderate"]}, "model": "sonnet", "effort": "medium"}
  ]
}

Two decisions per prompt:
- interpreters: which model rewrites the prompt, by complexity (a cheap classifier call decides
  the complexity first). Missing = STRUCTURER_MODEL. Simple prompts are never rewritten.
- rules: which model answers, and optionally with which effort (low|medium|high|xhigh|max).
  A rule matches when ALL its conditions hold (lists are OR inside one condition).
  Conditions: complexity, deliverable, context_kinds, min_chars, max_chars, from_models.
  First matching rule wins. No match = keep the model and effort the client asked for.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}
OVERRIDE_RE = re.compile(r"(?<![\w#])#model:([A-Za-z0-9_.\-\[\]]+)")


@dataclass
class Rule:
    name: str
    model: str
    when: dict[str, Any] = field(default_factory=dict)
    effort: str | None = None

    def matches(self, structure: dict[str, Any], text: str, requested: str) -> bool:
        w = self.when
        if "complexity" in w and structure.get("complexity") not in w["complexity"]:
            return False
        deliverable = (structure.get("deliverable") or {}).get("type")
        if "deliverable" in w and deliverable not in w["deliverable"]:
            return False
        if "context_kinds" in w:
            kinds = {c.get("kind") for c in structure.get("context") or [] if isinstance(c, dict)}
            if not kinds & set(w["context_kinds"]):
                return False
        if "min_chars" in w and len(text) < w["min_chars"]:
            return False
        if "max_chars" in w and len(text) > w["max_chars"]:
            return False
        if "from_models" in w and not any(m in requested for m in w["from_models"]):
            return False
        return True


@dataclass
class Router:
    rules: list[Rule]
    aliases: dict[str, str]
    source: str = "builtin"
    interpreters: dict[str, str] = field(default_factory=dict)  # complexity -> interpreter model

    @classmethod
    def load(cls, path: str | None, fallback_model: str) -> "Router":
        if path and Path(path).is_file():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            aliases = {**DEFAULT_ALIASES, **{k.lower(): v for k, v in data.get("aliases", {}).items()}}
            rules = [Rule(name=r.get("name", f"rule{i}"), model=r["model"], when=r.get("when", {}),
                          effort=r.get("effort"))
                     for i, r in enumerate(data.get("rules", []))]
            interpreters = {k: v for k, v in (data.get("interpreters") or {}).items() if v}
            return cls(rules=rules, aliases=aliases, source=str(path), interpreters=interpreters)
        # no file: single legacy rule so ROUTING_MODE=enforce keeps its old meaning
        return cls(rules=[Rule("simple", fallback_model, {"complexity": ["simple"]})],
                   aliases=dict(DEFAULT_ALIASES))

    def resolve(self, name: str) -> str:
        return self.aliases.get(name.lower(), name)

    def interpreter_for(self, complexity: str | None) -> str | None:
        """Model that rewrites a prompt of this complexity; None = the default structurer model."""
        name = self.interpreters.get(complexity or "")
        return self.resolve(name) if name else None

    def match(self, structure: dict[str, Any], text: str, requested: str) -> tuple[Rule, str] | None:
        for rule in self.rules:
            if rule.matches(structure, text, requested):
                return rule, self.resolve(rule.model)
        return None


def find_override(text: str) -> str | None:
    m = OVERRIDE_RE.search(text)
    return m.group(1) if m else None
