"""Anthropic-compatible proxy that improves prompts and routes them per task.

Point any client that accepts a base URL at this service:
    ANTHROPIC_BASE_URL=http://localhost:8787
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .audit import audit
from .classify import classify
from .compress import compress
from .config import Config
from .context import extract, load_profile
from .gate import (find_target, human_texts, inspect, is_continuation, preview,
                   previous_assistant_text, shape, should_structure)
from .inject import StructureCache, apply_texts, build_texts, reapply_history, render_block, text_key
from .metrics import MetricsSink, NullSink, SSEUsageScanner
from .routing import Router, find_override
from .structurer import structure
from .verify import check

log = logging.getLogger("interceptor")

_DROP_REQ = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding"}
_DROP_RESP = {"content-length", "content-encoding", "transfer-encoding", "connection"}
_AUTH_HEADERS = ("x-api-key", "authorization", "anthropic-version", "anthropic-beta")


def _fwd_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQ}


def _drop_betas(headers: dict[str, str], prefixes: tuple[str, ...]) -> dict[str, str]:
    out = {k: v for k, v in headers.items() if k.lower() != "anthropic-beta"}
    raw = next((v for k, v in headers.items() if k.lower() == "anthropic-beta"), "")
    betas = [b.strip() for b in raw.split(",") if b.strip() and not b.strip().startswith(prefixes)]
    if betas:
        out["anthropic-beta"] = ",".join(betas)
    return out


# modelos sin adaptive thinking ni effort, y con salida maxima de 64k
_NO_ADAPTIVE = ("claude-haiku-4",)
_NO_ADAPTIVE_MAX_TOKENS = 64000


def _fit_model(payload: dict[str, Any]) -> dict[str, Any]:
    """Copia del body sin los campos del modelo original que el modelo ruteado rechaza con 400."""
    if not str(payload.get("model", "")).startswith(_NO_ADAPTIVE):
        return payload
    out = dict(payload)
    if (out.get("thinking") or {}).get("type") == "adaptive":
        out.pop("thinking")
        cm = out.get("context_management")
        if isinstance(cm, dict):
            # clear_thinking exige thinking activo
            edits = [e for e in cm.get("edits", []) if not str(e.get("type", "")).startswith("clear_thinking")]
            if edits:
                out["context_management"] = {**cm, "edits": edits}
            else:
                out.pop("context_management")
    if isinstance(out.get("output_config"), dict):
        rest = {k: v for k, v in out["output_config"].items() if k != "effort"}
        if rest:
            out["output_config"] = rest
        else:
            out.pop("output_config")
    # sin mensajes role=system a mitad de conversacion: su texto pasa al system de arriba
    # (sin cache_control, para no pasar el limite de breakpoints); el effort por turno se descarta
    mid = [m for m in out.get("messages", []) if isinstance(m, dict) and m.get("role") == "system"]
    if mid:
        system = out.get("system") or []
        blocks = [{"type": "text", "text": system}] if isinstance(system, str) else list(system)
        for m in mid:
            content = m.get("content")
            if isinstance(content, str):
                blocks.append({"type": "text", "text": content})
            else:
                blocks += [{k: v for k, v in b.items() if k != "cache_control"} for b in content or []]
        out["system"] = blocks
        out["messages"] = [m for m in out["messages"] if not (isinstance(m, dict) and m.get("role") == "system")]
    if isinstance(out.get("max_tokens"), int):
        out["max_tokens"] = min(out["max_tokens"], _NO_ADAPTIVE_MAX_TOKENS)
    return out


def _resp_headers(resp: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in resp.headers.items() if k.lower() not in _DROP_RESP}


def _short(model: Any) -> str:
    return str(model).replace("claude-", "")


def create_app(cfg: Config | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(None, connect=10.0))
    cache = StructureCache(cfg.cache_size)
    sticky = StructureCache(cfg.cache_size)  # turn anchor -> routed model ("" = routing disabled)
    shadow_sticky = StructureCache(cfg.cache_size)  # same, for decisions shadow mode only logged
    sticky_effort = StructureCache(cfg.cache_size)  # turn anchor -> effort set by the matching rule
    criteria = StructureCache(cfg.cache_size)  # turn anchor -> success criteria (for the audit)
    background: set[asyncio.Task] = set()
    metrics = MetricsSink(cfg.metrics_path) if cfg.metrics_enabled else NullSink()
    router = Router.load(cfg.routes_file, cfg.model_simple)
    profile = load_profile(cfg.profile_file) if cfg.context else ""
    # 0 = the interpreter takes as long as it needs; only connecting is bounded
    interpreter_timeout = cfg.structurer_timeout or httpx.Timeout(None, connect=10.0)
    log.info("modo=%s | verify=%s | compress=%s | audit=%s | routing=%s (%d reglas, %s) | "
             "clasificar cortos=%s | solo requests con tools=%s | contexto=%s | perfil=%s | intérpretes=%s",
             cfg.inject_mode, cfg.verify, cfg.compress, cfg.audit, cfg.routing_mode, len(router.rules),
             router.source, cfg.classify_short, cfg.require_tools, cfg.context,
             f"{len(profile)} chars" if profile else "no",
             ", ".join(f"{k}={_short(router.resolve(v))}" for k, v in router.interpreters.items()) or "no")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await client.aclose()

    app = FastAPI(title="prompt-interceptor", version="0.2.0", lifespan=lifespan)

    def structurer_headers(request: Request) -> dict[str, str]:
        if cfg.api_key:
            return {"x-api-key": cfg.api_key, "anthropic-version": "2023-06-01",
                    "content-type": "application/json"}
        h = {k: v for k, v in request.headers.items() if k.lower() in _AUTH_HEADERS}
        # Only keep auth-related betas: feature betas the client uses for its main model
        # (e.g. 1M context) may be unavailable for the structurer model and get a 400.
        betas = [b.strip() for b in h.pop("anthropic-beta", "").split(",") if b.strip().startswith("oauth")]
        if betas:
            h["anthropic-beta"] = ",".join(betas)
        h.setdefault("anthropic-version", "2023-06-01")
        h["content-type"] = "application/json"
        return h

    # ---- routing --------------------------------------------------------------
    def route(body: dict[str, Any], record: dict[str, Any], model: str, how: str, why: str,
              anchor_key: str | None) -> None:
        current = body.get("model")
        if not model or model == current:
            if how == "override":
                log.info("[route] override %s | ya era %s, sin cambio", why, _short(current))
            return
        history = record.get("history_chars", 0)
        if how != "override" and history > cfg.route_max_history_chars:
            # switching models drops the prompt cache of the whole conversation
            record.update(route_skipped="history_too_large", route_rule=why)
            log.info("[route] %s | regla=%s | se mantiene %s: historial de %d chars, cambiar de modelo "
                     "perdería la cache", how, why, _short(current), history)
            return
        if how == "shadow":
            if anchor_key:
                shadow_sticky.put(anchor_key, model)
            record.update(route_shadow=model, route_rule=why)
            log.info("[route] shadow | regla=%s | %s -> %s (no aplicado)", why, _short(current), _short(model))
            return
        record.update(routed_from=current, route_rule=why, route_how=how)
        body["model"] = model
        if anchor_key:
            sticky.put(anchor_key, model)
        log.info("[route] %s | %s | %s -> %s", how, why, _short(current), _short(model))

    def set_effort(body: dict[str, Any], record: dict[str, Any], effort: str, why: str,
                   anchor_key: str | None, quiet: bool = False) -> None:
        if str(body.get("model", "")).startswith(_NO_ADAPTIVE):
            return  # these models take no effort parameter
        current = (body.get("output_config") or {}).get("effort")
        if current == effort:
            return
        if cfg.routing_mode == "shadow":
            record.update(effort_shadow=effort)
            log.info("[effort] shadow | regla=%s | %s -> %s (no aplicado)", why, current or "default", effort)
            return
        body["output_config"] = {**(body.get("output_config") or {}), "effort": effort}
        record.update(effort_from=current or "default", effort=effort)
        if anchor_key:
            sticky_effort.put(anchor_key, effort)
        if not quiet:
            log.info("[effort] regla=%s | %s -> %s", why, current or "default", effort)

    def apply_match(body: dict[str, Any], record: dict[str, Any], structure: dict[str, Any], text: str,
                    requested: str, anchor_key: str | None) -> None:
        """Responder decision: model and, if the rule says so, effort."""
        match = router.match(structure, text, requested)
        if not match:
            return
        rule, model = match
        route(body, record, model, cfg.routing_mode, rule.name, anchor_key)
        if rule.effort:
            set_effort(body, record, rule.effort, rule.name, anchor_key)

    def apply_sticky(body: dict[str, Any], record: dict[str, Any], anchor_key: str | None) -> None:
        model = sticky.get(anchor_key) if anchor_key else None
        if model and model != body.get("model"):
            record.update(routed_from=body.get("model"), route_how="sticky")
            body["model"] = model
            log.debug("[route] sticky | %s", _short(model))
        effort = sticky_effort.get(anchor_key) if anchor_key else None
        if effort:
            set_effort(body, record, effort, "sticky", None, quiet=True)

    def inherit(body: dict[str, Any], record: dict[str, Any], anchor_key: str | None,
                prev_key: str | None) -> None:
        """A pure continuation ("dale", "seguí") keeps the model of the previous turn."""
        current = body.get("model")
        store = shadow_sticky if cfg.routing_mode == "shadow" else sticky
        model = store.get(prev_key) if prev_key else None
        if not model or model == current:
            log.info("[route] continuación | sigue en %s", _short(current))
            return
        if anchor_key:
            store.put(anchor_key, model)
        if cfg.routing_mode == "shadow":
            record.update(route_shadow=model, route_rule="continuación")
            log.info("[route] shadow | continuación | heredaría %s del turno anterior (no aplicado)", _short(model))
            return
        record.update(routed_from=current, route_how="inherit", route_rule="continuación")
        body["model"] = model
        log.info("[route] continuación | hereda %s del turno anterior", _short(model))

    # ---- pipeline -------------------------------------------------------------
    async def process(body: dict[str, Any], request: Request, record: dict[str, Any]) -> dict[str, Any]:
        original = copy.deepcopy(body)
        try:
            messages = body.get("messages") or []
            requested = str(body.get("model", "?"))
            tag = f"{_short(requested)} tools={len(body.get('tools') or [])}"
            if cfg.require_tools and not body.get("tools"):
                # client side tasks (titles, summaries, quota checks): leave untouched
                record.update(action="passthrough_side_request", skip_reason="no_tools")
                log.info("[side] %s | %d msgs | tarea interna del cliente, no se toca", tag, len(messages))
                return body

            record["history_chars"] = len(json.dumps(messages, ensure_ascii=False))
            humans = human_texts(messages)  # before reapply: clients resend the original text
            anchor_key = text_key(f"{len(humans)}:{humans[-1]}") if humans else None
            prev_key = text_key(f"{len(humans) - 1}:{humans[-2]}") if len(humans) > 1 else None
            record["anchor_key"] = anchor_key
            record["history_reapplied"] = reapply_history(messages, cache)
            if record["history_reapplied"]:
                log.info("[history] %d mensaje(s) anteriores reconstruidos", record["history_reapplied"])

            idx = find_target(messages)
            last = messages[idx] if idx is not None else None
            text, why = inspect(last) if last else (None, "no_messages")
            if text is None:
                # continuation of a turn (tool loop etc.): keep the model chosen for the turn
                record.update(action="passthrough_non_human_turn", skip_reason=why)
                apply_sticky(body, record, anchor_key)
                level = logging.DEBUG if why == "tool_result_turn" else logging.INFO
                log.log(level, "[skip] %s | %s | %s | %d msgs", why, tag, shape(last), len(messages))
                return body

            ctx = (extract(body, messages, idx, cfg.context_max_chars, cfg.context_turns, profile)
                   if cfg.context else None)

            def log_context(res: Any) -> None:
                if ctx is None:
                    return
                record.update(context_instructions_chars=len(ctx.instructions), context_turns=ctx.turns,
                              context_environment=bool(ctx.environment),
                              structurer_cache_read_tokens=res.cache_read_tokens,
                              structurer_cache_write_tokens=res.cache_write_tokens)
                log.info("[context] %s | cache leída %d / escrita %d tokens | entrada %d tokens", ctx.summary(),
                         res.cache_read_tokens, res.cache_write_tokens, res.input_tokens)

            override = find_override(text)
            ok, reasons = should_structure(text, cfg.min_chars)
            record.update(gate_reasons=reasons, prompt_chars=len(text))
            if not ok:
                record["action"] = "passthrough_gate"
                log.info("[passthrough] %d chars, no se reescribe (%s) | %s | %d msgs | \"%s\"",
                         len(text), ",".join(reasons), tag, len(messages), preview(text))
                if override:
                    route(body, record, router.resolve(override), "override", f"#model:{override}", anchor_key)
                    return body
                if (reasons == ["bypass_token"] or not cfg.classify_short
                        or cfg.routing_mode not in ("shadow", "enforce")):
                    return body
                if is_continuation(text):
                    record["action"] = "continuation"
                    inherit(body, record, anchor_key, prev_key)
                    return body
                res = await classify(
                    client, upstream_url=cfg.upstream_url, headers=structurer_headers(request),
                    model=cfg.structurer_model, text=text,
                    previous_answer=previous_assistant_text(messages, idx), timeout=interpreter_timeout,
                    context=ctx,
                )
                record.update(action="classified_short", complexity=res.complexity, deliverable=res.deliverable,
                              classify_reason=res.reason, structurer_latency_ms=res.latency_ms,
                              structurer_input_tokens=res.input_tokens, structurer_output_tokens=res.output_tokens)
                log.info("[classify] %d chars | %s/%s | haiku %d ms | %s | \"%s\" | %s",
                         len(text), res.complexity, res.deliverable, res.latency_ms, tag, preview(text), res.reason)
                log_context(res)
                apply_match(body, record, res.structure, text, requested, anchor_key)
                return body

            source = text
            if cfg.compress:
                compressed, collapsed = compress(text, cfg.compress_min_chars)
                if collapsed:
                    source = (f"[interceptor: {collapsed} repeated log lines collapsed; each kept line shows "
                              f"its count and time span]\n{compressed}")
                    record.update(compressed_from=len(text), compressed_to=len(source), collapsed_lines=collapsed)
                    log.info("[compress] %d -> %d chars | %d líneas de log repetidas colapsadas",
                             len(text), len(source), collapsed)

            def pick_route(structure: dict[str, Any]) -> None:
                if override:
                    route(body, record, router.resolve(override), "override", f"#model:{override}", anchor_key)
                elif cfg.routing_mode in ("shadow", "enforce"):
                    apply_match(body, record, structure, text, requested, anchor_key)

            def keep(structure: dict[str, Any], who: str, latency_ms: int) -> dict[str, Any]:
                # a rewrite costs more than it helps on short, self-contained asks
                record["action"] = "classified_only"
                if source != text:
                    cache.put(text_key(text), (source,))
                    apply_texts(last, (source,))
                log.info("[keep] %d chars | simple/%s | sin reescribir | %s %d ms | %s | %d msgs | \"%s\"",
                         len(text), (structure.get("deliverable") or {}).get("type"), who, latency_ms, tag,
                         len(messages), preview(text))
                pick_route(structure)
                return body

            interpreter = cfg.structurer_model
            pre = None
            if router.interpreters:
                # interpreter decision: a cheap classification picks the model that rewrites
                try:
                    pre = await classify(
                        client, upstream_url=cfg.upstream_url, headers=structurer_headers(request),
                        model=cfg.structurer_model, text=source,
                        previous_answer=previous_assistant_text(messages, idx), timeout=interpreter_timeout,
                        context=ctx,
                    )
                except Exception as exc:  # no classification: the default interpreter still rewrites
                    record["classify_error"] = f"{type(exc).__name__}: {exc}"[:300]
                    log.warning("[interpret] no se pudo clasificar (%s) | interpreta %s", exc,
                                _short(cfg.structurer_model))
            if pre is not None:
                record.update(complexity=pre.complexity, deliverable=pre.deliverable, classify_reason=pre.reason,
                              classify_latency_ms=pre.latency_ms)
                if cfg.skip_simple and pre.complexity == "simple":
                    log_context(pre)
                    return keep(pre.structure, _short(cfg.structurer_model), pre.latency_ms)
                interpreter = router.interpreter_for(pre.complexity) or cfg.structurer_model
                log.info("[interpret] %s/%s -> %s | %s", pre.complexity, pre.deliverable, _short(interpreter),
                         pre.reason)
            record["interpreter_model"] = interpreter

            result = await structure(
                client, upstream_url=cfg.upstream_url, headers=structurer_headers(request),
                model=interpreter, text=source, timeout=interpreter_timeout, context=ctx,
            )
            log_context(result)
            if anchor_key and result.structure.get("success_criteria"):
                criteria.put(anchor_key, list(result.structure["success_criteria"]))
            complexity = result.structure.get("complexity")
            deliverable = (result.structure.get("deliverable") or {}).get("type")
            record.update(complexity=complexity, deliverable=deliverable,
                          structurer_latency_ms=result.latency_ms,
                          structurer_input_tokens=result.input_tokens,
                          structurer_output_tokens=result.output_tokens)

            if cfg.skip_simple and complexity == "simple":
                return keep(result.structure, _short(interpreter), result.latency_ms)

            texts = build_texts(cfg.inject_mode, result.structure, source)
            mode = "rewrite" if texts[0] != source else "append"

            if mode == "rewrite" and cfg.verify:
                fixed, rep = check(text, texts[0])
                record.update(verify_total=rep.total, verify_present=rep.present,
                              verify_added=len(rep.added), verify_rejected=rep.rejected)
                if fixed is None:
                    texts, mode = (source, render_block(result.structure)), "append"
                    log.info("[verify] la reescritura perdió %d de %d datos exactos -> se usa tu original + estructura",
                             rep.total - rep.present, rep.total)
                else:
                    texts = (fixed, texts[1])
                    if rep.added:
                        log.info("[verify] %d/%d datos exactos presentes, %d agregados a la reescritura",
                                 rep.present, rep.total, len(rep.added))
                    elif rep.total:
                        log.info("[verify] %d/%d datos exactos presentes", rep.present, rep.total)

            cache.put(text_key(text), texts)
            apply_texts(last, texts)
            record.update(
                action="structured",
                mode=mode,
                risky_assumptions=sum(1 for a in result.structure.get("assumptions", [])
                                      if isinstance(a, dict) and a.get("risky")),
                injected_chars=sum(len(t) for t in texts),
            )
            log.info("[%s] %d chars -> %d chars | %s/%s | %d riesgosos | %s %d ms | %s | %d msgs | \"%s\"",
                     mode, len(text), record["injected_chars"], complexity, deliverable,
                     record["risky_assumptions"], _short(interpreter), result.latency_ms, tag, len(messages),
                     preview(text))
            pick_route(result.structure)
            return body
        except Exception as exc:  # fail open: never break the user's request
            record.update(action="error", error=f"{type(exc).__name__}: {exc}"[:500])
            log.warning("[error] mensaje enviado sin cambios: %s", exc)
            if not cfg.fail_open:
                raise
            return original

    # ---- audit ----------------------------------------------------------------
    def schedule_audit(record: dict[str, Any], answer: str, stop_reason: str | None,
                       headers: dict[str, str]) -> None:
        key = record.get("anchor_key")
        if not (cfg.audit and key and answer.strip() and stop_reason == "end_turn"):
            return
        wanted = criteria.get(key)
        if not wanted:
            return
        criteria.put(key, [])  # one audit per turn

        async def run() -> None:
            try:
                results = await audit(client, upstream_url=cfg.upstream_url, headers=headers,
                                      model=cfg.structurer_model, criteria=wanted, answer=answer,
                                      timeout=interpreter_timeout)
                met = sum(1 for r in results if r.get("met"))
                missing = [r.get("criterion", "?") for r in results if not r.get("met")]
                log.info("[audit] %d/%d criterios cumplidos%s", met, len(results),
                         (" | falta: " + "; ".join(missing)) if missing else "")
                metrics.write({"type": "audit", "anchor_key": key, "met": met,
                               "total": len(results), "results": results})
            except Exception as exc:
                log.warning("[audit] no se pudo auditar: %s", exc)

        task = asyncio.create_task(run())
        background.add(task)
        task.add_done_callback(background.discard)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        started = time.perf_counter()
        body = await request.json()
        record: dict[str, Any] = {"path": "/v1/messages", "model_requested": body.get("model"),
                                  "stream": bool(body.get("stream"))}
        client_output_config = copy.deepcopy(body.get("output_config"))
        try:
            body = await process(body, request, record)
        except Exception as exc:
            metrics.write(record)
            return JSONResponse({"type": "error", "error": {"type": "interceptor_error",
                                                            "message": str(exc)}}, status_code=502)
        url = f"{cfg.upstream_url}/v1/messages"
        if request.url.query:
            url += f"?{request.url.query}"

        async def send(payload: dict[str, Any]) -> httpx.Response:
            headers = _fwd_headers(request)
            if record.get("routed_from") and payload.get("model") != record["routed_from"]:
                # el beta de 1M es del modelo original: el ruteado (p. ej. Haiku) lo rechaza con 400.
                # solo se rutea con historial chico, asi que no hace falta; el fallback lo recupera
                headers = _drop_betas(headers, ("context-1m",))
                payload = _fit_model(payload)
            req = client.build_request("POST", url, headers=headers, json=payload)
            return await client.send(req, stream=True)

        upstream = await send(body)
        if upstream.status_code >= 400 and (record.get("routed_from") or record.get("effort_from")):
            # the routed model or effort was refused (context size, beta, params...): hand back the original
            detail = (await upstream.aread())[:300].decode("utf-8", "ignore")
            await upstream.aclose()
            fallback = record.get("routed_from") or body.get("model")
            log.warning("[route] %s rechazó el request (%d): %s | vuelvo a %s con el effort del cliente",
                        _short(body.get("model")), upstream.status_code, detail, _short(fallback))
            body["model"] = fallback
            if client_output_config is None:
                body.pop("output_config", None)
            else:
                body["output_config"] = client_output_config
            record["route_fallback"] = True
            if record.get("anchor_key"):
                sticky.put(record["anchor_key"], "")  # stop routing this turn
                sticky_effort.put(record["anchor_key"], "")
            upstream = await send(body)
        record.update(model_used=body.get("model"), upstream_status=upstream.status_code)
        auditing = cfg.audit and bool(record.get("anchor_key")) and upstream.status_code == 200
        audit_headers = structurer_headers(request) if auditing else {}

        if body.get("stream"):
            scanner = SSEUsageScanner(keep_text=auditing)

            async def relay():
                try:
                    async for chunk in upstream.aiter_bytes():
                        scanner.feed(chunk)
                        yield chunk
                finally:
                    await upstream.aclose()
                    record.update(upstream_usage=scanner.usage, stop_reason=scanner.stop_reason,
                                  total_ms=int((time.perf_counter() - started) * 1000))
                    metrics.write(record)
                    if auditing:
                        schedule_audit(record, scanner.text, scanner.stop_reason, audit_headers)

            return StreamingResponse(relay(), status_code=upstream.status_code,
                                     headers=_resp_headers(upstream))

        content = await upstream.aread()
        await upstream.aclose()
        try:
            data = json.loads(content)
            record.update(upstream_usage=data.get("usage", {}), stop_reason=data.get("stop_reason"))
            if auditing:
                answer = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                schedule_audit(record, answer, data.get("stop_reason"), audit_headers)
        except (ValueError, AttributeError):
            pass
        record["total_ms"] = int((time.perf_counter() - started) * 1000)
        metrics.write(record)
        return Response(content=content, status_code=upstream.status_code,
                        headers=_resp_headers(upstream))

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def passthrough(path: str, request: Request) -> Response:
        url = f"{cfg.upstream_url}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        upstream_req = client.build_request(request.method, url, headers=_fwd_headers(request),
                                            content=await request.body())
        upstream = await client.send(upstream_req, stream=True)

        async def relay():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(relay(), status_code=upstream.status_code,
                                 headers=_resp_headers(upstream))

    return app


def setup_logging() -> None:
    import os
    from logging.handlers import RotatingFileHandler

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    root.handlers.clear()
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    if os.getenv("LOG_FILE"):
        file_handler = RotatingFileHandler(os.environ["LOG_FILE"], maxBytes=5_000_000, backupCount=3,
                                           encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # uvicorn already logs each request


def main() -> None:
    import os
    import uvicorn

    setup_logging()
    uvicorn.run(create_app(), host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8787")),
                log_config=None)


if __name__ == "__main__":
    main()
