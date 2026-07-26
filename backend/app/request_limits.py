"""Pure-ASGI request boundary for the hosted chart endpoint."""

from __future__ import annotations

from typing import Final

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


MAX_CHART_REQUEST_BYTES: Final = 16 * 1024
_CHART_PATH: Final = "/api/chart"


class _RequestBodyTooLarge(Exception):
    pass


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
    )


def _header_values(scope: Scope, name: bytes) -> list[bytes]:
    return [
        value
        for raw_name, value in scope.get("headers", [])
        if raw_name.lower() == name
    ]


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
    """Limit only hosted ``POST /api/chart`` before JSON/Pydantic parsing."""

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
            or scope.get("path") != _CHART_PATH
        ):
            await self.app(scope, receive, send)
            return

        if not _is_supported_json_content_type(scope):
            await _error_response(
                415,
                "unsupported_media_type",
                "此端點只接受 UTF-8 application/json。",
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
