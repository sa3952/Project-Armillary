"""Pure-ASGI request boundary for hosted JSON calculation/search endpoints."""

from __future__ import annotations

import threading
from typing import Final

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


MAX_CHART_REQUEST_BYTES: Final = 16 * 1024
MAX_HOSTED_HEADER_BYTES: Final = 16 * 1024
MAX_HOSTED_REQUESTS_PER_WORKER: Final = 4
_CHART_PATH: Final = "/api/chart"
_BOUNDED_JSON_PATHS: Final = frozenset(
    {
        _CHART_PATH,
        "/api/places/search",
    }
)


class _RequestBodyTooLarge(Exception):
    pass


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
    )


class RequestCapacityBoundary:
    """Reject excess requests inside the privacy/header boundary.

    Uvicorn's ``--limit-concurrency`` rejects below ASGI, so those responses
    cannot receive application security headers or closed telemetry.  Keeping
    the same per-worker cap here preserves bounded concurrency while making
    every rejection traverse the application's reviewed response lifecycle.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_concurrent: int = MAX_HOSTED_REQUESTS_PER_WORKER,
    ) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        self.app = app
        self.max_concurrent = max_concurrent
        self._active = 0
        self._lock = threading.Lock()

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        with self._lock:
            if self._active >= self.max_concurrent:
                accepted = False
            else:
                self._active += 1
                accepted = True
        if not accepted:
            await _error_response(
                503,
                "request_capacity_exhausted",
                "服務目前忙碌，請稍後重試。",
            )(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        finally:
            with self._lock:
                self._active -= 1


def _header_values(scope: Scope, name: bytes) -> list[bytes]:
    return [
        value
        for raw_name, value in scope.get("headers", [])
        if raw_name.lower() == name
    ]


def _header_bytes(scope: Scope) -> int:
    return sum(
        len(name) + len(value) + 4
        for name, value in scope.get("headers", [])
    )


def _content_length(scope: Scope) -> int | None:
    values = _header_values(scope, b"content-length")
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("ambiguous content length")
    try:
        rendered = values[0].decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("non-ASCII content length") from error
    if not rendered.isdecimal():
        raise ValueError("invalid content length")
    return int(rendered, 10)


def _has_conflicting_framing(scope: Scope) -> bool:
    """Reject a body framed by both Content-Length and Transfer-Encoding.

    RFC 9112 forbids the combination.  h11 accepts it and prefers
    Content-Length, and the reverse proxy normalizes it in the supported
    deployment, so this boundary previously relied on a layer it does not
    own.  Rejecting here makes the application's own framing contract
    explicit instead of assumed.
    """

    return bool(
        _header_values(scope, b"content-length")
        and _header_values(scope, b"transfer-encoding")
    )


def _is_supported_json_content_type(scope: Scope) -> bool:
    values = _header_values(scope, b"content-type")
    if len(values) != 1:
        return False
    try:
        rendered = values[0].decode("ascii")
    except UnicodeDecodeError:
        return False
    parts = [part.strip() for part in rendered.split(";")]
    if parts[0].casefold() != "application/json":
        return False
    if len(parts) > 2:
        return False
    for parameter in parts[1:]:
        if "=" not in parameter:
            return False
        name, value = (item.strip() for item in parameter.split("=", maxsplit=1))
        if name.casefold() != "charset":
            return False
        if value.strip('"').casefold() != "utf-8":
            return False
    return True


class ChartRequestBoundary:
    """Limit hosted JSON POST bodies before JSON/Pydantic parsing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = MAX_CHART_REQUEST_BYTES,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in _BOUNDED_JSON_PATHS
        ):
            await self.app(scope, receive, send)
            return

        if _header_bytes(scope) > MAX_HOSTED_HEADER_BYTES:
            await _error_response(
                431,
                "request_headers_too_large",
                "請求標頭超過 16 KiB 上限。",
            )(scope, receive, send)
            return

        if not _is_supported_json_content_type(scope):
            await _error_response(
                415,
                "unsupported_media_type",
                "此端點只接受 UTF-8 application/json。",
            )(scope, receive, send)
            return

        if _has_conflicting_framing(scope):
            await _error_response(
                400,
                "conflicting_request_framing",
                "請求同時使用 Content-Length 與 Transfer-Encoding。",
            )(scope, receive, send)
            return

        try:
            content_length = _content_length(scope)
        except ValueError:
            await _error_response(
                400,
                "invalid_content_length",
                "Content-Length 格式無效或不明確。",
            )(scope, receive, send)
            return
        if (
            content_length is not None
            and content_length > self.max_body_bytes
        ):
            await _error_response(
                413,
                "request_body_too_large",
                "請求內容超過 16 KiB 上限。",
            )(scope, receive, send)
            return

        received_bytes = 0
        response_started = False

        async def receive_with_limit() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] != "http.request":
                return message
            body = message.get("body", b"")
            received_bytes += len(body)
            if received_bytes > self.max_body_bytes:
                raise _RequestBodyTooLarge
            return message

        async def send_with_state(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive_with_limit, send_with_state)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await _error_response(
                413,
                "request_body_too_large",
                "請求內容超過 16 KiB 上限。",
            )(scope, receive, send)
