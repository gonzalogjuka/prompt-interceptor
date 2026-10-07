"""Configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    upstream_url: str = "https://api.anthropic.com"
    api_key: str | None = None  # used for the structurer call; falls back to client auth
    structurer_model: str = "claude-haiku-4-5-20251001"
    structurer_timeout: float = 0  # seconds; 0 = no limit, the interpreter finishes its work
    min_chars: int = 400  # below this, prompts pass through untouched
    inject_mode: str = "rewrite"  # rewrite | append
    skip_simple: bool = True  # simple requests: route only, leave the text untouched
    routing_mode: str = "off"  # off | shadow | enforce
    routes_file: str | None = "routes.json"
    verify: bool = True  # deterministic check that the rewrite kept exact values
    compress: bool = True  # collapse repeated log lines in large pasted messages
    compress_min_chars: int = 3000
    route_max_history_chars: int = 100_000  # above this, don't switch models (cache loss)
    classify_short: bool = True  # with routing on, classify short messages too (not by length)
    audit: bool = False  # background check of the final answer against success criteria
    require_tools: bool = True  # only touch agent main-loop requests (they carry tools)
    model_simple: str = "claude-haiku-4-5-20251001"
    context: bool = True  # give the interpreter the instructions, memory, environment and conversation
    context_max_chars: int = 30_000  # cap for instructions + memory (sent cached)
    context_turns: int = 6  # previous messages of the conversation the interpreter sees
    profile_file: str | None = "perfil.md"  # user context that travels with the interceptor
    metrics_enabled: bool = False
    metrics_path: str = "./metrics.jsonl"
    cache_size: int = 512
    fail_open: bool = True  # on any interceptor error, forward the original request

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            upstream_url=os.getenv("UPSTREAM_URL", cls.upstream_url).rstrip("/"),
            api_key=os.getenv("ANTHROPIC_API_KEY") or None,
            structurer_model=os.getenv("STRUCTURER_MODEL", cls.structurer_model),
            structurer_timeout=float(os.getenv("STRUCTURER_TIMEOUT", cls.structurer_timeout)),
            min_chars=int(os.getenv("MIN_CHARS", cls.min_chars)),
            inject_mode=os.getenv("INJECT_MODE", cls.inject_mode).lower(),
            skip_simple=_bool("SKIP_SIMPLE", cls.skip_simple),
            routing_mode=os.getenv("ROUTING_MODE", cls.routing_mode).lower(),
            routes_file=os.getenv("ROUTES_FILE", cls.routes_file) or None,
            verify=_bool("VERIFY", cls.verify),
            compress=_bool("COMPRESS", cls.compress),
            compress_min_chars=int(os.getenv("COMPRESS_MIN_CHARS", cls.compress_min_chars)),
            route_max_history_chars=int(os.getenv("ROUTE_MAX_HISTORY_CHARS", cls.route_max_history_chars)),
            audit=_bool("AUDIT", cls.audit),
            classify_short=_bool("CLASSIFY_SHORT", cls.classify_short),
            require_tools=_bool("REQUIRE_TOOLS", cls.require_tools),
            model_simple=os.getenv("MODEL_SIMPLE", cls.model_simple),
            context=_bool("CONTEXT", cls.context),
            context_max_chars=int(os.getenv("CONTEXT_MAX_CHARS", cls.context_max_chars)),
            context_turns=int(os.getenv("CONTEXT_TURNS", cls.context_turns)),
            profile_file=os.getenv("PROFILE_FILE", cls.profile_file) or None,
            metrics_enabled=_bool("METRICS_ENABLED", cls.metrics_enabled),
            metrics_path=os.getenv("METRICS_PATH", cls.metrics_path),
            cache_size=int(os.getenv("CACHE_SIZE", cls.cache_size)),
            fail_open=_bool("FAIL_OPEN", cls.fail_open),
        )
