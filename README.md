# prompt-interceptor

A local, Anthropic-compatible proxy that sits between your AI client (Claude Code,
the Anthropic SDKs, anything with a configurable base URL) and the model. It
listens to every request and, without any change in how you work:

- **improves your prompt** — a small model rewrites messy messages into a clear,
  well-ordered request; your original travels next to it as the source of truth
- **verifies the rewrite** — a deterministic check guarantees no exact value
  (names, versions, ids, error codes, sizes) was lost
- **compresses pasted logs** — repeated log lines are collapsed, nothing unique is dropped
- **routes each task to a model** — rules you edit decide Haiku / Sonnet / Opus per
  task, with shadow mode, manual override and automatic fallback
- **audits answers** (optional) — checks each final answer against the success
  criteria extracted from your request, in the background
- **measures everything** — per-request metrics, prompt-cache stats per model, and
  an A/B benchmark harness to prove (or disprove) that it helps

It fails open: if anything inside the interceptor breaks, your original request
goes through untouched.

```
client ──► interceptor ──────────────────────────────────────────► Anthropic API
             │ side task (no tools)? ──► pass through
             │ history: rebuild earlier improved messages (stable prompt cache)
             │ gate: short? ──► not rewritten; still routed by complexity:
             │   continuation ("dale", "seguí") ──► keeps the previous turn's model, no call
             │   otherwise ──► light classifier (Haiku, sees the previous answer) ──► route
             │ compress: collapse repeated log lines
             │ structurer (Haiku, forced tool output): objective, context, constraints,
             │   success criteria, assumptions, complexity, improved prompt
             │ simple? ──► keep your text, just route
             │ verify: exact values kept? add missing / reject rewrite
             │ inject: improved prompt + <interceptor_original>
             │ route: rules / shadow / #model: override, sticky for the whole turn
             └ upstream refused the routed model? ──► retry with the original model
           after the answer: optional background audit against the success criteria
```

## Quick start (Windows)

Double-click, in order:

| File | What it does |
|---|---|
| `1-Iniciar-interceptor.cmd` | creates `.venv`, installs deps, creates `.env`, starts on `:8787` |
| `2-Claude-con-interceptor.cmd` | opens Claude Code routed through the interceptor (that terminal only) |
| `3-Ver-reporte.cmd` | metrics summary (needs `METRICS_ENABLED=true`) |
| `4-Ver-log.cmd` | live log of the background instance |
| `5-Benchmark.cmd` | A/B benchmark (needs `ANTHROPIC_API_KEY` in `.env`) |
| `test.ps1` | tests against a simulated API (no key, no network) |

Run it in the background at every login instead of keeping a window open:

```powershell
.\autostart.ps1 -Install     # start now and at every login (no admin needed)
.\autostart.ps1 -Status
.\autostart.ps1 -Stop
.\autostart.ps1 -Uninstall
```

Linux / Mac (same behavior: loads `config.env` then `.env`):

```bash
./start.sh                       # foreground, log on screen
./autostart.sh --install         # systemd user service: starts at boot, restarts on failure
./autostart.sh --status | --restart | --stop | --uninstall
./claude-interceptor.sh          # Claude Code through the interceptor (that terminal only)
```

The hosted claude.ai chat cannot be intercepted: it does not route through a
configurable endpoint.

## Install on another machine

Everything that defines the interceptor is in the repo (code, `config.env`, `routes.json`,
`perfil.md`). Per machine there is only `.env` (created empty, empty is fine), logs and metrics.

1. **Prerequisites:** git, Python 3.12+, Claude Code logged in, and access to the private repo
   (`gh auth login` with the repo owner's account, then `gh auth setup-git` so `git pull` works
   unattended).
2. **Clone and start in the background:**
   ```bash
   gh repo clone gonzalogjuka/prompt-interceptor && cd prompt-interceptor
   ./autostart.sh --install          # Windows: .\autostart.ps1 -Install
   ```
3. **Point the client at it:** `./claude-interceptor.sh` for one terminal; for every session
   `export ANTHROPIC_BASE_URL=http://127.0.0.1:8787` in `~/.bashrc` (Windows: `setx`); for the
   VS Code extension, `"claudeCode.environmentVariables": [{"name": "ANTHROPIC_BASE_URL",
   "value": "http://127.0.0.1:8787"}]` (with Remote-SSH, in the remote settings). Only do it
   globally with the background service installed: if nothing listens on 8787 the client fails.
4. **Check:** send a prompt and look for a `[context] perfil ...` line in `interceptor.log`.

**Updates:** with `AUTO_UPDATE=true` (default) each background start runs `git pull --ff-only`, so
the daily-improvement PRs you merge reach every machine on its next start (`./autostart.sh
--restart` to take them now). It never blocks: without network or credentials it logs an
`[update]` warning and starts with what it has.

Run the interceptor on the machine where Claude Code runs: with VS Code Remote-SSH or an SSH
terminal into a VM, that is the VM, not the PC you type on.

## Controls inside your messages

| Token | Effect |
|---|---|
| `#model:opus` / `#model:sonnet` / `#model:haiku` / `#model:<full id>` | force the model for this turn (works even with routing off) |
| `#raw` | send this message untouched |

## Model routing

Rules live in `routes.json` (first match wins, no match = the model the client asked for):

```json
{
  "aliases": {"haiku": "claude-haiku-4-5-20251001", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"},
  "rules": [
    {"name": "consultas-simples", "when": {"complexity": ["simple"]}, "model": "haiku"},
    {"name": "codigo-y-acciones-moderadas", "when": {"complexity": ["moderate"], "deliverable": ["code", "action"]}, "model": "sonnet"}
  ]
}
```

Conditions (all must hold; lists are OR): `complexity`, `deliverable`,
`context_kinds`, `min_chars`, `max_chars`, `from_models`.

Two decisions per prompt, both logged:
- **Who interprets** — `"interpreters": {"moderate": "haiku", "complex": "sonnet"}`: a light
  classification decides the complexity first, then that model rewrites the prompt
  (`[interpret]` in the log). Simple prompts are never rewritten. Without `interpreters`,
  `STRUCTURER_MODEL` rewrites everything.
- **Who answers, and how hard** — a rule's `model`, plus optional `"effort"`
  (`low|medium|high|xhigh|max`) kept for the whole turn (`[effort]` in the log). Without
  `effort`, the client's effort is kept. If the model refuses it, the request is retried with
  the client's model and effort.

**The model depends on complexity, never on length.** Long messages get complexity from the
structurer; short ones from a light classifier call that also sees the end of the previous
answer, so "do that for the 8 providers" is judged by the work it implies. Pure continuations
("ok", "dale, hacelo", "seguí") skip the call and keep the model of the previous turn, so a
complex task is not handed to a cheaper model just because you said "go ahead".
`MIN_CHARS` only decides whether a message is worth rewriting.

`ROUTING_MODE`:
- `off` — never reroute (manual `#model:` still works)
- `shadow` — log what the rules would do, change nothing. **Start here.**
- `enforce` — apply the rules

Handover control:
- **Sticky per turn**: the model picked for your message is kept for every tool-loop
  request of that turn, so one task never switches models halfway.
- **Fallback**: if the routed model rejects the request (context too long, unsupported
  beta or parameter), it is retried with the original model and routing stops for that turn.
- **Cache guard**: prompt caches are per model, so switching models re-sends the whole
  conversation uncached. Above `ROUTE_MAX_HISTORY_CHARS` the rules are not applied.
  `report.ps1` shows cache reads/writes per model so you can tune this with data.

## Benchmark

```powershell
.\bench.ps1                          # 8 sample tasks in bench/tasks.jsonl
.\bench.ps1 --min-chars 200          # also structure short messy prompts
.\bench.ps1 --routing enforce        # measure routing too
.\bench.ps1 --only pod-oom,acr-pull
```

Each task runs twice (direct and through the interceptor). Reported per task:
deterministic checks (`must_include` / `must_not_include` regexes), latency,
total tokens including the structurer, and a blind verdict from a judge model.
Add your own real prompts to `bench/tasks.jsonl`: the sample set only shows the format.

## Configuration

Everything that defines the interceptor travels with the repo: `config.env` (settings, no
secrets), `routes.json` (who interprets / who answers) and `perfil.md` (your context for the
interpreter). `.env` stays on each PC (git-ignored): it holds `ANTHROPIC_API_KEY` and overrides
any value of `config.env`. On a new PC: clone, run `1-Iniciar-interceptor.cmd`.

All options are documented in `.env.example`. The ones you will touch most:

| Variable | Default | |
|---|---|---|
| `ROUTING_MODE` | `off` | `off` / `shadow` / `enforce` |
| `MIN_CHARS` | `400` | below this, prompts are not rewritten (they are still classified for routing) |
| `CLASSIFY_SHORT` | `true` | with routing on, classify short messages by complexity (light Haiku call) |
| `INJECT_MODE` | `rewrite` | `rewrite` or `append` (original first + JSON structure) |
| `SKIP_SIMPLE` | `true` | simple requests are classified and routed, not rewritten |
| `CONTEXT` | `true` | the interpreter sees your CLAUDE.md files, memory index, environment and the last `CONTEXT_TURNS` messages (cached) |
| `VERIFY` / `COMPRESS` | `true` | exact-value check / log compression |
| `AUDIT` | `false` | background check of answers vs success criteria |
| `METRICS_ENABLED` | `false` | JSONL metrics for `report.ps1` |
| `REQUIRE_TOOLS` | `true` | only touch agent main-loop requests; set `false` for plain SDK use |
| `ANTHROPIC_API_KEY` | empty | structurer/audit key; empty reuses the client's credentials |

## Reading the log

```
[side]        client side task (title, summary): untouched
[skip]        not a human turn (tool loop, client marker, another session's message) — reason shown
[passthrough] too short to be worth rewriting
[classify]    short message classified by complexity (and the reason)
[interpret]   long message: complexity -> model that rewrites it
[effort]      effort set by the matching rule (client's -> rule's)
[context]     what the interpreter saw: instructions, environment, conversation, cache read
[compress]    N repeated log lines collapsed
[keep]        simple request: text untouched, only classified/routed
[verify]      exact values kept / added / rewrite rejected
[rewrite]     improved prompt sent, original attached
[append]      original sent first, structure attached (verify rejected the rewrite)
[route]       shadow / enforce / override / continuation decision, cache guard, fallback
[history]     earlier improved messages rebuilt for a stable prompt cache
[audit]       criteria met by the final answer
[error]       something failed inside the interceptor; request sent unchanged
```

## Tests

```powershell
.\test.ps1     # or: pip install -r requirements-dev.txt && pytest -q
```

All tests run against a mocked upstream; no API key needed.

## Design rules

- **Fail open.** The interceptor must never break a request.
- **Never lose the original.** Rewrites are additions; the user's text is always attached.
- **Deterministic checks over model judgment** wherever possible (gate, verify, compress, routing rules).
- **Pay only when it helps.** Trivial prompts, side tasks and tool loops cost nothing extra.
- **Measure before believing.** Shadow mode, metrics, audit and the benchmark exist so
  every change can be justified with data.

## Status

Working end to end with Claude Code on Windows. Open questions the benchmark is meant
to answer: whether rewriting improves answers for strong models, the right `MIN_CHARS`,
and whether routing saves money once cache effects are counted.
