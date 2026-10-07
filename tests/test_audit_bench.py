"""Observability: usage read from the stream, background audit of answers, and the A/B benchmark."""
import asyncio
import json
import sys
import time
import types
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from helpers import LONG_PROMPT, STRUCTURE, Upstream, metrics
from interceptor.app import create_app
from interceptor.config import Config
from interceptor.metrics import SSEUsageScanner

ROOT = Path(__file__).resolve().parents[1]


def test_sse_scanner_reads_text_and_stop_reason_across_chunks():
    sc = SSEUsageScanner(keep_text=True)
    sc.feed(b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ho"}}\n')
    sc.feed(b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"la"}}\ndata: {"type":"mess')
    sc.feed(b'age_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}\n')
    assert sc.text == "hola" and sc.stop_reason == "end_turn" and sc.usage["output_tokens"] == 3


def _wait_for(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_audit_runs_in_background_after_a_streamed_answer(tmp_path):
    up = Upstream()
    mpath = tmp_path / "m.jsonl"
    cfg = Config(upstream_url="https://upstream.test", metrics_path=str(mpath), metrics_enabled=True,
                 require_tools=False, routes_file=None, profile_file=None, audit=True)
    with TestClient(create_app(cfg, transport=httpx.MockTransport(up))) as client:  # lifespan keeps tasks alive
        with client.stream("POST", "/v1/messages", json={"model": "m", "max_tokens": 5, "stream": True,
                                                        "messages": [{"role": "user", "content": LONG_PROMPT}]}) as r:
            b"".join(r.iter_bytes())
        assert _wait_for(lambda: up.audits)
        assert _wait_for(lambda: any(m.get("type") == "audit" for m in metrics(mpath)))
    audit_req = up.audits[0]["messages"][0]["content"]
    assert "el pod queda Running" in audit_req and "Subí el limit a 512Mi." in audit_req
    rec = [m for m in metrics(mpath) if m.get("type") == "audit"][0]
    assert rec["met"] == 0 and rec["total"] == 1


def test_benchmark_harness_runs_end_to_end(tmp_path):
    sys.path.insert(0, str(ROOT / "bench"))
    import run_bench

    def handler(req):
        b = json.loads(req.content)
        system = str(b.get("system", ""))
        if system.startswith("You are a request structurer"):
            s = {**STRUCTURE, "improved_prompt": b["messages"][0]["content"]}
            return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "submit_structure", "input": s}],
                                             "usage": {"input_tokens": 700, "output_tokens": 250}})
        if system.startswith("You compare"):
            return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "submit_verdict",
                                                          "input": {"winner": "A", "reason": "x"}}]})
        return httpx.Response(200, json={"model": b["model"], "content": [{"type": "text", "text": "limit memoria 2.4.1"}],
                                         "usage": {"input_tokens": 300, "output_tokens": 400}})

    # own (empty) routes and no profile: the test must not depend on the repo's routes.json / perfil.md
    args = types.SimpleNamespace(tasks=str(ROOT / "bench" / "tasks.jsonl"), only="pod-oom", model="m", judge="j",
                                 routing="off", max_tokens=100, min_chars=400, upstream="https://api.test",
                                 out=str(tmp_path / "out"), routes=None, profile=None)
    r = asyncio.run(run_bench.run(args, transport=httpx.MockTransport(handler)))[0]
    assert r["interceptor"]["action"] == "structured" and r["interceptor"]["structurer_tokens"] == 950
    assert r["raw"]["checks"] == {"passed": 3, "total": 3}
    assert r["verdict"]["winner"] in ("raw", "interceptor")
    assert (tmp_path / "out" / "results.jsonl").exists()
