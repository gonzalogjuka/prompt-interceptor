"""A/B benchmark: the same tasks sent raw and through the interceptor.

For each task it measures latency and tokens of both paths (the interceptor
path includes the structurer call), runs the task's deterministic checks
(must_include / must_not_include regexes) on both answers, and asks a judge
model to pick the better answer blind (order randomized).

Needs ANTHROPIC_API_KEY. Usage:
    python bench/run_bench.py                       # all tasks, defaults
    python bench/run_bench.py --only pod-oom,acr-pull --model claude-sonnet-5-5
    python bench/run_bench.py --routing enforce     # also measure routing
    python bench/run_bench.py --min-chars 200       # also structure short messy prompts
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from interceptor.app import create_app  # noqa: E402
from interceptor.config import Config  # noqa: E402

JUDGE_SYSTEM = """You compare two answers to the same user request. Pick the one that better serves the user:
correct, complete for what was asked, respects every constraint the user stated, no invented facts.
Length and formatting are not merits by themselves. If they are equally good, say tie. Call submit_verdict."""
JUDGE_TOOL = {
    "name": "submit_verdict",
    "description": "Submit which answer is better.",
    "input_schema": {"type": "object", "properties": {
        "winner": {"type": "string", "enum": ["A", "B", "tie"]},
        "reason": {"type": "string"}}, "required": ["winner", "reason"]},
}


async def ask(client: httpx.AsyncClient, url: str, headers: dict, model: str, prompt: str,
              max_tokens: int) -> dict:
    started = time.perf_counter()
    r = await client.post(url, headers=headers, json={
        "model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})
    latency = int((time.perf_counter() - started) * 1000)
    r.raise_for_status()
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return {"text": text, "usage": data.get("usage", {}), "model": data.get("model"), "latency_ms": latency}


def run_checks(task: dict, text: str) -> dict:
    inc = [bool(re.search(p, text, re.S)) for p in task.get("must_include", [])]
    exc = [not re.search(p, text, re.S) for p in task.get("must_not_include", [])]
    return {"passed": sum(inc) + sum(exc), "total": len(inc) + len(exc)}


async def judge(client: httpx.AsyncClient, url: str, headers: dict, model: str, prompt: str,
                raw: str, intercepted: str) -> dict:
    flip = random.random() < 0.5
    a, b = (intercepted, raw) if flip else (raw, intercepted)
    r = await client.post(url, headers=headers, json={
        "model": model, "max_tokens": 600, "system": JUDGE_SYSTEM, "tools": [JUDGE_TOOL],
        "tool_choice": {"type": "tool", "name": "submit_verdict"},
        "messages": [{"role": "user", "content":
                      f"<request>\n{prompt}\n</request>\n<answer_A>\n{a}\n</answer_A>\n<answer_B>\n{b}\n</answer_B>"}]})
    r.raise_for_status()
    for block in r.json().get("content", []):
        if block.get("type") == "tool_use":
            v = block["input"]
            w = v.get("winner")
            winner = "tie" if w == "tie" else ("interceptor" if (w == "A") == flip else "raw")
            return {"winner": winner, "reason": v.get("reason", "")}
    return {"winner": "error", "reason": "no verdict"}


def tokens(usage: dict) -> int:
    return sum(usage.get(k, 0) or 0 for k in
               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))


async def run(args, transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key and transport is None:
        sys.exit("Falta ANTHROPIC_API_KEY (el benchmark llama a la API directamente).")
    tasks = [json.loads(line) for line in Path(args.tasks).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.only:
        wanted = set(args.only.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    metrics_path = out / "interceptor_metrics.jsonl"
    metrics_path.unlink(missing_ok=True)

    cfg = Config(upstream_url=args.upstream, api_key=key or None, require_tools=False, min_chars=args.min_chars,
                 routing_mode=args.routing, routes_file=args.routes, profile_file=args.profile,
                 metrics_enabled=True, metrics_path=str(metrics_path), audit=False)
    app = create_app(cfg, transport=transport)
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    upstream_url = f"{args.upstream}/v1/messages"
    results = []
    async with httpx.AsyncClient(transport=transport, timeout=300) as upstream, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://interceptor",
                              timeout=300) as proxy:
        for i, task in enumerate(tasks, 1):
            print(f"[{i}/{len(tasks)}] {task['id']} ...", flush=True)
            raw = await ask(upstream, upstream_url, headers, args.model, task["prompt"], args.max_tokens)
            seen = len(metrics_path.read_text(encoding="utf-8").splitlines()) if metrics_path.exists() else 0
            inter = await ask(proxy, "/v1/messages", headers, args.model, task["prompt"], args.max_tokens)
            lines = metrics_path.read_text(encoding="utf-8").splitlines()[seen:] if metrics_path.exists() else []
            rec = json.loads(lines[-1]) if lines else {}
            verdict = await judge(upstream, upstream_url, headers, args.judge, task["prompt"],
                                  raw["text"], inter["text"])
            results.append({
                "id": task["id"],
                "raw": {"latency_ms": raw["latency_ms"], "tokens": tokens(raw["usage"]),
                        "checks": run_checks(task, raw["text"]), "model": raw["model"]},
                "interceptor": {"latency_ms": inter["latency_ms"],
                                "tokens": tokens(inter["usage"]) + rec.get("structurer_input_tokens", 0)
                                + rec.get("structurer_output_tokens", 0),
                                "structurer_tokens": rec.get("structurer_input_tokens", 0)
                                + rec.get("structurer_output_tokens", 0),
                                "checks": run_checks(task, inter["text"]), "model": inter["model"],
                                "action": rec.get("action"), "mode": rec.get("mode")},
                "verdict": verdict,
                "answers": {"raw": raw["text"], "interceptor": inter["text"]},
            })
    (out / "results.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results),
                                       encoding="utf-8")
    summarize(results)
    print(f"\nDetalle completo (respuestas y motivos del juez): {out / 'results.jsonl'}")
    return results


def summarize(results: list[dict]) -> None:
    print(f"\n{'task':<24}{'acción':<20}{'checks raw/int':>16}{'lat ms raw/int':>18}{'tokens raw/int':>18}  juez")
    for r in results:
        a, b = r["raw"], r["interceptor"]
        print(f"{r['id']:<24}{str(b.get('action')):<20}"
              f"{a['checks']['passed']}/{a['checks']['total']} vs {b['checks']['passed']}/{b['checks']['total']}".rjust(16)
              + f"{a['latency_ms']:>9}/{b['latency_ms']:<8}{a['tokens']:>9}/{b['tokens']:<8}  {r['verdict']['winner']}")
    wins = sum(r["verdict"]["winner"] == "interceptor" for r in results)
    losses = sum(r["verdict"]["winner"] == "raw" for r in results)
    ties = sum(r["verdict"]["winner"] == "tie" for r in results)
    cr = sum(r["raw"]["checks"]["passed"] for r in results), sum(r["raw"]["checks"]["total"] for r in results)
    ci = sum(r["interceptor"]["checks"]["passed"] for r in results), \
        sum(r["interceptor"]["checks"]["total"] for r in results)
    lat_r = statistics.median(r["raw"]["latency_ms"] for r in results)
    lat_i = statistics.median(r["interceptor"]["latency_ms"] for r in results)
    tok_r = sum(r["raw"]["tokens"] for r in results)
    tok_i = sum(r["interceptor"]["tokens"] for r in results)
    print(f"\nJuez: interceptor gana {wins}, empata {ties}, pierde {losses} (de {len(results)})")
    print(f"Checks: raw {cr[0]}/{cr[1]} | interceptor {ci[0]}/{ci[1]}")
    print(f"Latencia mediana: raw {lat_r:.0f} ms | interceptor {lat_i:.0f} ms")
    print(f"Tokens totales (incluye Haiku): raw {tok_r} | interceptor {tok_i} ({100 * (tok_i - tok_r) / max(tok_r, 1):+.0f}%)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tasks", default=str(Path(__file__).with_name("tasks.jsonl")))
    p.add_argument("--only", default="", help="ids separados por coma")
    p.add_argument("--model", default="claude-opus-5-5")
    p.add_argument("--judge", default="claude-sonnet-5-5")
    p.add_argument("--routing", default="off", choices=["off", "shadow", "enforce"])
    p.add_argument("--max-tokens", type=int, default=2000)
    p.add_argument("--min-chars", type=int, default=400,
                   help="umbral del filtro (MIN_CHARS); probá 200 para incluir pedidos cortos y desordenados")
    p.add_argument("--upstream", default="https://api.anthropic.com")
    p.add_argument("--routes", default=str(ROOT / "routes.json"), help="interpretes y reglas a medir")
    p.add_argument("--profile", default=str(ROOT / "perfil.md"), help="perfil que ve el interprete")
    p.add_argument("--out", default=str(Path(__file__).with_name("results")))
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
