"""Append-only JSONL metrics. One line per intercepted request."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class MetricsSink:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


class NullSink:
    def write(self, record: dict[str, Any]) -> None:
        pass


class SSEUsageScanner:
    """Reads usage counters, answer text and stop reason out of a Messages API
    event stream as it passes through to the client."""

    def __init__(self, keep_text: bool = False) -> None:
        self._buf = ""
        self.usage: dict[str, int] = {}
        self.stop_reason: str | None = None
        self.keep_text = keep_text
        self._text: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self._text)

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk.decode("utf-8", errors="ignore")
        *lines, self._buf = self._buf.split("\n")
        for line in lines:
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            usage = event.get("usage") or (event.get("message") or {}).get("usage")
            if isinstance(usage, dict):
                for k, v in usage.items():
                    if isinstance(v, int):
                        self.usage[k] = v
            delta = event.get("delta") or {}
            if event.get("type") == "message_delta" and delta.get("stop_reason"):
                self.stop_reason = delta["stop_reason"]
            elif self.keep_text and delta.get("type") == "text_delta":
                self._text.append(delta.get("text", ""))
