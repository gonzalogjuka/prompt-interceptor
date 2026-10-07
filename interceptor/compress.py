"""Deterministic context compression for pasted logs.

Collapses log lines that repeat once timestamps are ignored, keeping the first
occurrence annotated with the count and the time span. Only lines that look
like log lines (a timestamp or a log level) are candidates, so code and prose
are never touched, and lines that differ in anything but the timestamp are
kept, so no identifier is lost.
"""
from __future__ import annotations

import re
from collections import Counter

MIN_REPEATS = 3

_TS = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b"
)
_LEVEL = re.compile(r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|FATAL|CRITICAL|Warning|Error|Normal)\b")


def _is_log_line(line: str) -> bool:
    return len(line.strip()) >= 20 and bool(_TS.search(line) or _LEVEL.search(line))


def compress(text: str, min_chars: int = 3000) -> tuple[str, int]:
    """Return (text, collapsed_line_count). Unchanged when nothing repeats."""
    if len(text) < min_chars:
        return text, 0
    lines = text.split("\n")
    norm = [_TS.sub("<ts>", ln).strip() if _is_log_line(ln) else None for ln in lines]
    counts = Counter(n for n in norm if n)
    span_first: dict[str, str] = {}
    span_last: dict[str, str] = {}
    for ln, n in zip(lines, norm):
        if n and counts[n] >= MIN_REPEATS:
            m = _TS.search(ln)
            if m:
                span_first.setdefault(n, m.group())
                span_last[n] = m.group()

    out: list[str] = []
    seen: set[str] = set()
    collapsed = 0
    for ln, n in zip(lines, norm):
        if not n or counts[n] < MIN_REPEATS:
            out.append(ln)
            continue
        if n in seen:
            collapsed += 1
            continue
        seen.add(n)
        first, last = span_first.get(n), span_last.get(n)
        span = f", {first} -> {last}" if first and last and first != last else ""
        out.append(f"{ln}  [x{counts[n]}{span}]")
    if not collapsed:
        return text, 0
    return "\n".join(out), collapsed
