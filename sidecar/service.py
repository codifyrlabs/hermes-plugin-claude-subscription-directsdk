# sidecar/service.py
"""OpenAI-compatible API over the plugin. Logs metadata only; never prompts, completions, headers or keys."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import directsdk

from .ledger import LedgerCorrupt
from .runtime import Runtime

logger = logging.getLogger("claude-sidecar")

STATUS = {"unauthorized": 401, "invalid_request": 400, "model_not_allowed": 400, "paused": 503,
          "ledger_unreadable": 503, "logged_out": 503, "claude_missing": 503, "monthly_cap": 429,
          "busy": 429, "upstream_error": 502}


def _error(kind: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"type": kind, "message": message}}, status_code=STATUS[kind])


def _dump(obj: Any) -> dict:
    data = obj.model_dump()
    data.pop("_response", None)
    return data


def _cost(usage: Any) -> Any:
    if isinstance(usage, dict) and isinstance(usage.get("native_cost"), dict):
        return usage["native_cost"].get("total_cost_usd")
    return None


def _tokens(usage: Any) -> tuple[Any, Any]:
    if not isinstance(usage, dict):
        return None, None
    return usage.get("prompt_tokens"), usage.get("completion_tokens")


def _classify(exc: BaseException) -> str:
    if isinstance(exc, directsdk.ClaudeCodeLoggedOut):
        return "logged_out"
    if isinstance(exc, directsdk.ClaudeCodeMissing):
        return "claude_missing"
    if isinstance(exc, ValueError):
        return "invalid_request"
    return "upstream_error"


_END = object()
DISCONNECT_POLL_SECONDS = 0.25


async def _until_disconnected(request: Request) -> None:
    while not await request.is_disconnected():
        await asyncio.sleep(DISCONNECT_POLL_SECONDS)


async def _prime(result: Any) -> Any:
    try:
        return await result.__anext__()
    except StopAsyncIteration:
        return _END
    except BaseException:
        await result.aclose()
        raise


class _SettlingStreamingResponse(StreamingResponse):
    def __init__(self, content: Any, settle: Callable[[str, Any], Awaitable[None]], **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._settle = settle

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._settle("client_disconnected", None)


def create_api_app(rt: Runtime) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(token.encode(), rt.config.api_key.encode())

    def finish(model: str, stream: bool, outcome: str, usage: Any, started: float) -> None:
        # The slot is released whatever the ledger write raises (disk full, read-only, EACCES).
        try:
            cost = _cost(usage)
            prompt, completion = _tokens(usage)
            duration_ms = int((time.monotonic() - started) * 1000)
            try:
                rt.ledger.add(cost, {"model": model, "stream": stream, "outcome": outcome, "duration_ms": duration_ms})
            except Exception as exc:  # noqa: BLE001 - logged by type only; the request itself already ran
                logger.error("ledger write failed error_type=%s", type(exc).__name__)
            logger.info("request model=%s stream=%s outcome=%s cost_usd=%s prompt_tokens=%s completion_tokens=%s "
                        "duration_ms=%d", model, stream, outcome, cost, prompt, completion, duration_ms)
        finally:
            rt.gate.release()

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models(request: Request):
        if not authorized(request):
            return _error("unauthorized", "missing or invalid bearer token")
        return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "claude-subscription"}
                                           for m in sorted(rt.config.allowed_models)]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        if not authorized(request):
            logger.info("request refused outcome=unauthorized")
            return _error("unauthorized", "missing or invalid bearer token")
        try:
            body = await request.json()
        except ValueError:
            return _error("invalid_request", "body must be JSON")
        if not isinstance(body, dict):
            return _error("invalid_request", "body must be a JSON object")
        model = body.get("model")
        if model not in rt.config.allowed_models:
            return _error("model_not_allowed", "model is not on the sidecar allowlist")
        try:
            blocked = rt.ledger.blocked_reason()
        except LedgerCorrupt:
            logger.error("request refused outcome=ledger_unreadable")
            return _error("ledger_unreadable", "spend ledger is unreadable; fix it in the panel host")
        if blocked == "paused":
            return _error("paused", "sidecar is paused")
        if blocked == "monthly_cap":
            return _error("monthly_cap", "monthly spend cap reached")
        if not rt.gate.try_acquire(model):
            return _error("busy", "a request is already in flight")
        stream = bool(body.get("stream"))
        started = time.monotonic()

        async def start() -> tuple[Any, Any]:
            result = await rt.client.chat.completions.create(**body)
            # The plugin's stream is lazy: logged-out, missing-binary and bad-request errors only surface on the
            # first chunk. Pull it before committing to a 200, so the caller gets a real status and can fall back.
            return result, (await _prime(result) if stream else None)

        async def abandon(work: asyncio.Task) -> Any:
            """Cancel the plugin call (directsdk kills claude's process group); return its usage if it finished."""
            work.cancel()
            (outcome,) = await asyncio.gather(work, return_exceptions=True)
            if isinstance(outcome, BaseException):
                return None
            result, _ = outcome
            if stream:
                await result.aclose()
                return None
            try:
                return _dump(result).get("usage")
            except Exception:  # noqa: BLE001 - cost is simply unknown then
                return None

        # uvicorn never cancels a handler when an HTTP/1.1 client goes away, so watch for it: otherwise an abandoned
        # request keeps claude running (and the slot held) until request_timeout.
        work = asyncio.create_task(start())
        watch = asyncio.create_task(_until_disconnected(request))
        try:
            await asyncio.wait({work, watch}, return_when=asyncio.FIRST_COMPLETED)
            if not work.done() and watch.exception() is not None:
                await asyncio.wait({work})
        except asyncio.CancelledError:
            # Server shutdown cancelled the handler: stop claude and free the slot before unwinding.
            watch.cancel()
            finish(model, stream, "cancelled", await abandon(work), started)
            raise
        watch.cancel()
        if not work.done():
            finish(model, stream, "client_disconnected", await abandon(work), started)
            logger.info("request abandoned outcome=client_disconnected")
            return JSONResponse({"error": {"type": "client_disconnected", "message": "client disconnected"}},
                                status_code=499)
        try:
            result, first = work.result()
        except Exception as exc:  # noqa: BLE001 - mapped to a typed error, logged by type only
            kind = _classify(exc)
            finish(model, stream, kind, None, started)
            logger.info("request error_type=%s", type(exc).__name__)
            message = str(exc)[:200] if kind == "invalid_request" else kind.replace("_", " ")
            return _error(kind, message)
        if not stream:
            outcome, usage = "error", None
            try:
                data = _dump(result)
                usage, outcome = data.get("usage"), "ok"
            finally:
                finish(model, False, outcome, usage, started)
            return JSONResponse(data)

        settled = False

        async def settle(outcome: str, usage: Any) -> None:
            nonlocal settled
            if settled:
                return
            settled = True
            try:
                if outcome != "ok":
                    await result.aclose()
            finally:
                finish(model, True, outcome, usage, started)

        async def events():
            usage, outcome = None, "error"

            def sse(chunk: Any) -> str:
                nonlocal usage
                data = _dump(chunk)
                if data.get("usage"):
                    usage = data["usage"]
                return f"data: {json.dumps(data)}\n\n"

            try:
                if first is not _END:
                    yield sse(first)
                    async for chunk in result:
                        yield sse(chunk)
                yield "data: [DONE]\n\n"
                outcome = "ok"
            except (GeneratorExit, asyncio.CancelledError):
                outcome = "client_disconnected"
                raise
            except Exception as exc:  # noqa: BLE001 - reported in-band; headers are already sent
                outcome = _classify(exc)
                logger.info("stream error_type=%s", type(exc).__name__)
                yield f"data: {json.dumps({'error': {'type': outcome, 'message': outcome.replace('_', ' ')}})}\n\n"
            finally:
                await settle(outcome, usage)

        # The claude process is already running, so the slot must be settled even if the client goes away
        # before the body iterator is ever started (an unstarted generator never runs its finally).
        return _SettlingStreamingResponse(events(), settle, media_type="text/event-stream")

    return app
