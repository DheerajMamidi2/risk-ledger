"""FastAPI boundary for the local, paper-only RiskLedger demo."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from risk_ledger.events import EventIntegrityError, VersionConflictError
from risk_ledger.models import ApproveRequest, CreateSessionRequest, RejectRequest
from risk_ledger.scenarios import ScenarioNotFoundError
from risk_ledger.service import (
    ApprovalExpiredError,
    SessionConflictError,
    SessionNotFoundError,
    SessionService,
)

LOGGER = logging.getLogger(__name__)
MAX_MUTATION_BODY_BYTES = 16_384
MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cache-Control": "no-store",
}


def _error(status_code: int, code: str, message: str, **headers: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "error": {"code": code, "message": message}},
        headers=headers,
    )


def _success(data: Any, *, status_code: int = 200) -> JSONResponse:
    if hasattr(data, "model_dump"):
        content = data.model_dump(mode="json")
    elif isinstance(data, tuple):
        content = [item.model_dump(mode="json") for item in data]
    else:
        content = data
    return JSONResponse(status_code=status_code, content={"success": True, "data": content})


class MutationRateLimiter:
    """Small process-local sliding window for a single-user local MVP."""

    def __init__(
        self,
        limit: int,
        *,
        window_seconds: int = 60,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._monotonic = monotonic
        self._buckets: dict[str, tuple[float, ...]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        now = self._monotonic()
        cutoff = now - self._window_seconds
        with self._lock:
            recent = tuple(
                timestamp for timestamp in self._buckets.get(key, ()) if timestamp > cutoff
            )
            if len(recent) >= self._limit:
                retry_after = max(1, int(self._window_seconds - (now - recent[0])))
                self._buckets = {**self._buckets, key: recent}
                return False, retry_after
            self._buckets = {**self._buckets, key: (*recent, now)}
            return True, self._window_seconds


class BoundaryMiddleware:
    """Enforce local CSRF, body, rate and response-header boundaries."""

    def __init__(self, app: ASGIApp, *, mutation_limit: int) -> None:
        self._app = app
        self._limiter = MutationRateLimiter(mutation_limit)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        request_headers = Headers(scope=scope)
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        downstream_receive = receive
        if method in MUTATION_METHODS and path.startswith("/api/"):
            if request_headers.get("x-riskledger-client") != "local-web":
                await _error(
                    403,
                    "FORBIDDEN",
                    "Mutation request was rejected.",
                )(scope, receive, secured_send)
                return
            content_length = request_headers.get("content-length")
            try:
                declared_size = int(content_length) if content_length is not None else 0
            except ValueError:
                await _error(400, "INVALID_REQUEST", "Request validation failed.")(
                    scope, receive, secured_send
                )
                return
            if declared_size < 0:
                await _error(400, "INVALID_REQUEST", "Request validation failed.")(
                    scope, receive, secured_send
                )
                return
            if declared_size > MAX_MUTATION_BODY_BYTES:
                await _error(413, "PAYLOAD_TOO_LARGE", "Request body is too large.")(
                    scope, receive, secured_send
                )
                return
            content_type = request_headers.get("content-type", "").split(";", maxsplit=1)[0]
            if content_type != "application/json":
                await _error(415, "UNSUPPORTED_MEDIA_TYPE", "JSON content is required.")(
                    scope, receive, secured_send
                )
                return

            buffered_messages: tuple[Message, ...] = ()
            received_size = 0
            while True:
                message = await receive()
                buffered_messages = (*buffered_messages, message)
                if message["type"] == "http.disconnect":
                    break
                if message["type"] != "http.request":
                    break
                received_size += len(message.get("body", b""))
                if received_size > MAX_MUTATION_BODY_BYTES:
                    await _error(413, "PAYLOAD_TOO_LARGE", "Request body is too large.")(
                        scope, receive, secured_send
                    )
                    return
                if not message.get("more_body", False):
                    break

            message_iterator = iter(buffered_messages)

            async def replay_receive() -> Message:
                try:
                    return next(message_iterator)
                except StopIteration:
                    return {"type": "http.request", "body": b"", "more_body": False}

            downstream_receive = replay_receive
            client = scope.get("client")
            client_host = client[0] if client else "unknown"
            allowed, retry_after = self._limiter.allow(client_host)
            if not allowed:
                await _error(
                    429,
                    "RATE_LIMITED",
                    "Too many mutation requests.",
                    **{"Retry-After": str(retry_after)},
                )(scope, receive, secured_send)
                return

        await self._app(scope, downstream_receive, secured_send)


def create_app(*, service: SessionService | None = None, mutation_limit: int = 30) -> FastAPI:
    session_service = service or SessionService()
    app = FastAPI(
        title="RiskLedger",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.session_service = session_service
    app.add_middleware(BoundaryMiddleware, mutation_limit=mutation_limit)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["testserver", "localhost", "127.0.0.1", "0.0.0.0", "[::1]"],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return _error(422, "INVALID_REQUEST", "Request validation failed.")

    @app.exception_handler(ScenarioNotFoundError)
    @app.exception_handler(SessionNotFoundError)
    async def not_found_handler(_request: Request, _exc: Exception) -> JSONResponse:
        return _error(404, "NOT_FOUND", "Resource was not found.")

    @app.exception_handler(SessionConflictError)
    @app.exception_handler(VersionConflictError)
    async def conflict_handler(_request: Request, _exc: Exception) -> JSONResponse:
        return _error(409, "CONFLICT", "Request conflicts with current session state.")

    @app.exception_handler(ApprovalExpiredError)
    async def expired_handler(_request: Request, _exc: ApprovalExpiredError) -> JSONResponse:
        return _error(409, "APPROVAL_EXPIRED", "Approval window expired without execution.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return _error(404, "NOT_FOUND", "Resource was not found.")
        return _error(exc.status_code, "REQUEST_REJECTED", "Request was rejected.")

    @app.exception_handler(EventIntegrityError)
    async def integrity_handler(_request: Request, _exc: EventIntegrityError) -> JSONResponse:
        return _error(409, "AUDIT_INTEGRITY_FAILURE", "Audit integrity check failed closed.")

    @app.exception_handler(Exception)
    async def generic_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception("Unhandled RiskLedger request failure", exc_info=exc)
        return _error(500, "INTERNAL_ERROR", "An internal error occurred.")

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return _success(
            {
                "status": "ok",
                "mode": "PAPER_ONLY",
                "dataset_digest": session_service.catalog.dataset_digest,
                "network_execution": False,
            }
        )

    @app.get("/api/scenarios")
    async def scenarios() -> JSONResponse:
        return _success(session_service.catalog.list_summaries())

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: CreateSessionRequest) -> JSONResponse:
        return _success(session_service.create_session(request), status_code=201)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> JSONResponse:
        return _success(session_service.get_session(session_id))

    @app.post("/api/sessions/{session_id}/approve")
    async def approve(session_id: str, request: ApproveRequest) -> JSONResponse:
        return _success(session_service.approve(session_id, request))

    @app.post("/api/sessions/{session_id}/reject")
    async def reject(session_id: str, request: RejectRequest) -> JSONResponse:
        return _success(session_service.reject(session_id, request))

    static_directory = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static_directory), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_directory / "index.html")

    return app
