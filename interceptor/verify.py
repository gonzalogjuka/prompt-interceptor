"""Deterministic check that a rewrite kept the exact values of the original.

No model involved: we extract identifier-like tokens from the original (names,
versions, ids, error codes, values with units) and look for them verbatim in
the rewrite. Missing ones are appended; if the rewrite lost most of a
fact-dense message, it is rejected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_APPEND = 25          # missing facts we are willing to list explicitly
SMALL_MESSAGE_FACTS = 60  # above this, the message is log-like: never reject, just list
MIN_COVERAGE = 0.5        # below this (on non log-like messages) the rewrite is rejected

_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/@-]*[A-Za-z0-9_]")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?Z?)?$")
_VERSION = re.compile(r"^v?\d+\.\d+(\.\d+)*$")
_CAMEL = re.compile(r"[a-z][A-Z]|[A-Z]{2,}[a-z]")
_UNIT = re.compile(r"^\d+(\.\d+)?(Ki|Mi|Gi|Ti|KB|MB|GB|TB|ms|s|m|h|%|m?cpu)$")


def _is_fact(tok: str) -> bool:
    if len(tok) < 3 or _TIMESTAMP.match(tok):
        return False
    if _VERSION.match(tok) or _UNIT.match(tok):
        return True
    if tok.isdigit():
        return len(tok) >= 3  # exit codes, ports, ids; skip tiny counters
    has_digit = any(c.isdigit() for c in tok)
    has_alpha = any(c.isalpha() for c in tok)
    if has_digit and has_alpha and len(tok) >= 4:
        return True
    if any(c in tok for c in "-_./:@") and len(tok) >= 6:
        return True
    return len(tok) >= 6 and bool(_CAMEL.search(tok))


def extract_facts(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for tok in _TOKEN.findall(text):
        if _is_fact(tok):
            seen.setdefault(tok, None)
    return list(seen)


@dataclass
class VerifyReport:
    total: int = 0
    present: int = 0
    added: list[str] = field(default_factory=list)
    rejected: bool = False

    @property
    def coverage(self) -> float:
        return self.present / self.total if self.total else 1.0


def check(original: str, improved: str) -> tuple[str | None, VerifyReport]:
    """Return (fixed_improved, report). fixed_improved is None when the rewrite
    should be rejected in favor of the original."""
    facts = extract_facts(original)
    missing = [f for f in facts if f not in improved]
    report = VerifyReport(total=len(facts), present=len(facts) - len(missing))
    if not missing:
        return improved, report
    if len(facts) <= SMALL_MESSAGE_FACTS and report.coverage < MIN_COVERAGE:
        report.rejected = True
        return None, report
    report.added = missing[:MAX_APPEND]
    extra = len(missing) - len(report.added)
    listed = ", ".join(f"`{f}`" for f in report.added)
    tail = f" (+{extra} more, see the original message)" if extra > 0 else ""
    fixed = f"{improved}\n\nExact values from the original message not mentioned above: {listed}{tail}"
    return fixed, report
