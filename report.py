"""Summarize metrics.jsonl (enable with METRICS_ENABLED=true).

Answers: what did the interceptor do, what did it cost, how is the prompt cache
doing per model (the price of switching models), and do answers meet criteria.
"""
import json
import sys
from collections import Counter, defaultdict

path = sys.argv[1] if len(sys.argv) > 1 else "metrics.jsonl"
rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
reqs = [r for r in rows if r.get("type") != "audit"]
audits = [r for r in rows if r.get("type") == "audit"]


def section(title):
    print(f"\n== {title} ==")


section(f"requests: {len(reqs)}")
for k, v in Counter(r.get("action", "?") for r in reqs).most_common():
    print(f"  {k:<30} {v}")

s = [r for r in reqs if r.get("structurer_input_tokens")]
if s:
    section("Haiku (structurer + classifier)")
    lat = sorted(r.get("structurer_latency_ms", 0) for r in s)
    print(f"  calls: {len(s)} | tokens in/out: {sum(r['structurer_input_tokens'] for r in s)}"
          f"/{sum(r.get('structurer_output_tokens', 0) for r in s)}")
    print(f"  latency p50/p90/max ms: {lat[len(lat) // 2]}/{lat[int(len(lat) * 0.9)]}/{lat[-1]}")
    print(f"  complexity: {dict(Counter(r.get('complexity') for r in s))}")
    print(f"  short messages classified: {sum(1 for r in s if r.get('action') == 'classified_short')}"
          f" | continuations (no Haiku call): {sum(1 for r in reqs if r.get('action') == 'continuation')}")
    v = [r for r in s if "verify_total" in r]
    if v:
        print(f"  verify: {sum(r['verify_present'] for r in v)}/{sum(r['verify_total'] for r in v)} facts kept, "
              f"{sum(r['verify_added'] for r in v)} added, {sum(1 for r in v if r['verify_rejected'])} rewrites rejected")

c = [r for r in reqs if r.get("collapsed_lines")]
if c:
    section("compression")
    print(f"  messages: {len(c)} | chars {sum(r['compressed_from'] for r in c)} -> {sum(r['compressed_to'] for r in c)}"
          f" | log lines collapsed: {sum(r['collapsed_lines'] for r in c)}")

section("routing")
print(f"  applied: {sum(1 for r in reqs if r.get('route_how') in ('enforce', 'override'))}"
      f" | sticky (tool loop): {sum(1 for r in reqs if r.get('route_how') == 'sticky')}"
      f" | shadow: {sum(1 for r in reqs if r.get('route_shadow'))}"
      f" | skipped for cache: {sum(1 for r in reqs if r.get('route_skipped'))}"
      f" | fallbacks: {sum(1 for r in reqs if r.get('route_fallback'))}")
for (src, dst), n in Counter((r.get("routed_from"), r.get("model_used")) for r in reqs
                             if r.get("routed_from")).most_common():
    print(f"  {src} -> {dst}: {n}")

per_model = defaultdict(Counter)
for r in reqs:
    u = r.get("upstream_usage") or {}
    m = per_model[r.get("model_used", "?")]
    m["requests"] += 1
    for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"):
        m[k] += u.get(k, 0) or 0
if per_model:
    section("prompt cache per model (switching models re-creates the cache)")
    print(f"  {'model':<28}{'reqs':>6}{'input':>12}{'cache read':>12}{'cache write':>12}{'output':>10}{'hit %':>7}")
    for model, m in per_model.items():
        total_in = m["input_tokens"] + m["cache_read_input_tokens"] + m["cache_creation_input_tokens"]
        hit = 100 * m["cache_read_input_tokens"] / total_in if total_in else 0
        print(f"  {str(model):<28}{m['requests']:>6}{m['input_tokens']:>12}{m['cache_read_input_tokens']:>12}"
              f"{m['cache_creation_input_tokens']:>12}{m['output_tokens']:>10}{hit:>6.0f}%")

if audits:
    section("audit (answers vs success criteria)")
    met, total = sum(a["met"] for a in audits), sum(a["total"] for a in audits)
    pct = f" ({100 * met / total:.0f}%)" if total else ""
    print(f"  turns audited: {len(audits)} | criteria met: {met}/{total}{pct}")
    misses = Counter(r.get("criterion") for a in audits for r in a.get("results", []) if not r.get("met"))
    for crit, n in misses.most_common(5):
        print(f"  missed x{n}: {crit}")

errors = [r for r in reqs if r.get("action") == "error"]
if errors:
    section("last errors")
    for r in errors[-3:]:
        print(f"  {r.get('ts')}  {r.get('error')}")
