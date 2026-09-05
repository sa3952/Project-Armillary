"""Pure-ASGI request boundary for hosted JSON calculation/search endpoints."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import threading
from typing import Final, Mapping

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


MAX_CHART_REQUEST_BYTES: Final = 16 * 1024
MAX_HOSTED_API_HEADER_BYTES: Final = 16 * 1024
# Separate compute and general pools so expensive JSON work cannot starve a
# normal page bootstrap; both remain inside the reviewed ASGI response boundary.
MAX_HOSTED_REQUESTS_PER_WORKER: Final = 32
MAX_HOSTED_COMPUTE_REQUESTS_PER_WORKER: Final = 4
# One retry contract for both general and compute admission refusal.
RETRY_AFTER_SECONDS: Final = 5
# Bound the gap between body chunks independently of edge buffering.
CHUNK_RECEIVE_TIMEOUT_SECONDS: Final = 10.0
API_PATH_PREFIX: Final = "/api/"
_CHART_PATH: Final = "/api/chart"
# Body-bounded JSON routes and high-cost compute routes are distinct sets.
_BOUNDED_JSON_PATHS: Final = frozenset(
    {
        _CHART_PATH,
        "/api/places/search",
    }
)
_COMPUTE_PATHS: Final = frozenset({_CHART_PATH})
BOUNDARY_REASON_STATE_KEY: Final = "armillary.boundary_reason.v1"
COMPUTE_ENTERED_STATE_KEY: Final = "armillary.compute_entered.v1"

# One vocabulary owns every reason this boundary can place in the request
# scope.  Call sites still name the reason they observed; this set makes an
# unregistered new reason fail at the producer instead of disappearing later
# in telemetry or public documentation.
BOUNDARY_REASON_CODES: Final = frozenset({
    "undeclared_host",
    "unknown_api_path",
    "unsupported_method",
    "request_capacity_exhausted",
    "compute_capacity_exhausted",
    "request_headers_too_large",
    "unsupported_media_type",
    "conflicting_request_framing",
    "invalid_content_length",
    "request_body_too_large",
    "empty_request_body",
    "request_body_read_timeout",
})


def _mark_boundary_reason(scope: Scope, reason: str) -> None:
    if reason not in BOUNDARY_REASON_CODES:
        raise RuntimeError(f"unregistered request boundary reason: {reason}")
    state = scope.get("state")
    if not isinstance(state, dict):
        state = {}
        scope["state"] = state
    if BOUNDARY_REASON_STATE_KEY in state:
        raise RuntimeError("request boundary reason was already assigned")
    state[BOUNDARY_REASON_STATE_KEY] = reason


def mark_compute_entered(scope: Scope) -> None:
    state = scope.get("state")
    if not isinstance(state, dict):
        state = {}
        scope["state"] = state
    state[COMPUTE_ENTERED_STATE_KEY] = True


def _compute_entered(scope: Scope) -> bool:
    state = scope.get("state")
    return isinstance(state, dict) and state.get(COMPUTE_ENTERED_STATE_KEY) is True


class DeclaredHostBoundary:
    """Refuse a request that names a host this deployment does not answer for.

    Admission belongs to the proxy, which strips `Authorization` before
    forwarding; the application therefore has no authorization of its own and
    that is deliberate.  What it can do cheaply is notice that it was reached by
    a route the deployment never declared — the shape a published container port
    produces, where a scanner sends the address as the host.  A caller who can
    reach the socket can also set this header, so this catches the accident, not
    the attacker, and the difference is stated rather than implied.
    """

    def __init__(self, app: ASGIApp, *, expected_host: str | None) -> None:
        self.app = app
        self.expected_host = (expected_host or "").strip().lower() or None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.expected_host is not None and scope.get("type") == "http":
            headers = dict(scope.get("headers") or ())
            raw = headers.get(b"host", b"").decode("latin-1")
            host = raw.split(":", 1)[0].strip().lower()
            if host != self.expected_host:
                _mark_boundary_reason(scope, "undeclared_host")
                await _error_response(
                    404,
                    "undeclared_host",
                    "此服務不回應這個主機名稱。",
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


class ApiMethodBoundary:

    def __init__(
        self,
        app: ASGIApp,
        *,
        route_methods: Mapping[str, frozenset[str]],
    ) -> None:
        self.app = app
        self.route_methods = dict(route_methods)

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") == "http":
            path = scope.get("path")
            method = scope.get("method")
            # An ASGI scope always carries a path, but the type says otherwise;
            # treating a missing one as "no declared methods" keeps the boundary
            # closed rather than crashing inside the lookup.
            methods = (
                self.route_methods.get(path) if isinstance(path, str) else None
            )
            # An unregistered /api path used to fall through to the frontend
            # asset mount, which answered 405 with no Allow header and a third
            # body shape.  The API boundary answers for the API prefix.
            if (
                methods is None
                and isinstance(path, str)
                and path.startswith(API_PATH_PREFIX)
            ):
                _mark_boundary_reason(scope, "unknown_api_path")
                await _error_response(
                    404,
                    "unknown_api_path",
                    "此服務沒有這個 API 路徑。",
                )(scope, receive, send)
                return
            if methods is not None and method not in methods:
                _mark_boundary_reason(scope, "unsupported_method")
                response = _error_response(
                    405,
                    "unsupported_method",
                    "此路徑不接受這個 HTTP 方法。",
                )
                response.headers["Allow"] = ", ".join(sorted(methods))
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    retry_after_seconds: int | None = None,
) -> JSONResponse:
    headers = (
        {"Retry-After": str(retry_after_seconds)}
        if retry_after_seconds is not None
        else None
    )
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
        headers=headers,
    )


class RequestCapacityBoundary:

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_concurrent: int = MAX_HOSTED_REQUESTS_PER_WORKER,
        max_compute_concurrent: int = MAX_HOSTED_COMPUTE_REQUESTS_PER_WORKER,
    ) -> None:
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be positive")
        if max_compute_concurrent <= 0:
            raise ValueError("max_compute_concurrent must be positive")
        self.app = app
        self.max_concurrent = max_concurrent
        self.max_compute_concurrent = max_compute_concurrent
        self._active = 0
        self._active_compute = 0
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

        compute = scope.get("path") in _COMPUTE_PATHS
        with self._lock:
            if compute:
                accepted = self._active_compute < self.max_compute_concurrent
                if accepted:
                    self._active_compute += 1
            else:
                accepted = self._active < self.max_concurrent
                if accepted:
                    self._active += 1
        if not accepted:
            error_code = (
                "compute_capacity_exhausted"
                if compute
                else "request_capacity_exhausted"
            )
            _mark_boundary_reason(
                scope,
                error_code,
            )
            await _error_response(
                503,
                error_code,
                "服務目前忙碌，請稍後重試。",
                retry_after_seconds=RETRY_AFTER_SECONDS,
            )(scope, receive, send)
            return

        try:
            if compute:
                await self._run_compute(scope, receive, send)
            else:
                await self.app(scope, receive, send)
        finally:
            with self._lock:
                if compute:
                    self._active_compute -= 1
                else:
                    self._active -= 1

    async def _run_compute(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=1)
        disconnected = asyncio.Event()

        async def pump() -> None:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected.set()
                    if not queue.full():
                        queue.put_nowait(message)
                    return
                await queue.put(message)

        async def receive_brokered() -> Message:
            if disconnected.is_set() and queue.empty():
                return {"type": "http.disconnect"}
            return await queue.get()

        pump_task = asyncio.create_task(pump())
        app_task: asyncio.Future[None] = asyncio.ensure_future(
            self.app(scope, receive_brokered, send)
        )
        try:
            done, _pending = await asyncio.wait(
                {pump_task, app_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if app_task in done:
                await app_task
                return
            error = pump_task.exception()
            if error is not None:
                app_task.cancel()
                with suppress(asyncio.CancelledError):
                    await app_task
                raise error
            if not _compute_entered(scope):
                app_task.cancel()
                with suppress(asyncio.CancelledError):
                    await app_task
                return
            await app_task
        finally:
            for task in (pump_task, app_task):
                if not task.done():
                    task.cancel()
            for task in (pump_task, app_task):
                with suppress(asyncio.CancelledError):
                    await task


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

        if _header_bytes(scope) > MAX_HOSTED_API_HEADER_BYTES:
            _mark_boundary_reason(scope, "request_headers_too_large")
            await _error_response(
                431,
                "request_headers_too_large",
                "請求標頭超過 16 KiB 上限。",
            )(scope, receive, send)
            return

        if not _is_supported_json_content_type(scope):
            _mark_boundary_reason(scope, "unsupported_media_type")
            await _error_response(
                415,
                "unsupported_media_type",
                "此端點只接受 UTF-8 application/json。",
            )(scope, receive, send)
            return

        if _has_conflicting_framing(scope):
            _mark_boundary_reason(scope, "conflicting_request_framing")
            await _error_response(
                400,
                "conflicting_request_framing",
                "請求同時使用 Content-Length 與 Transfer-Encoding。",
            )(scope, receive, send)
            return

        try:
            content_length = _content_length(scope)
        except ValueError:
            _mark_boundary_reason(scope, "invalid_content_length")
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
            _mark_boundary_reason(scope, "request_body_too_large")
            await _error_response(
                413,
                "request_body_too_large",
                "請求內容超過 16 KiB 上限。",
            )(scope, receive, send)
            return
        if content_length == 0:
            _mark_boundary_reason(scope, "empty_request_body")
            # A declared empty body reaches Pydantic as a JSON parse failure and
            # comes back as a 422 listing every missing field, which describes
            # the schema rather than the actual fault.  The body framing is this
            # boundary's own contract, so it answers for it here.
            await _error_response(
                400,
                "empty_request_body",
                "此端點需要 JSON 請求內容，但 Content-Length 為 0。",
            )(scope, receive, send)
            return

        # Read the complete, already bounded JSON body before entering the
        # application.  This prevents an application that responds before
        # consuming its whole receive stream from starting a 2xx response and
        # only then discovering a late oversized chunk.
        buffered: list[Message] = []
        received_bytes = 0
        while True:
            try:
                message = await asyncio.wait_for(
                    receive(), timeout=CHUNK_RECEIVE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                _mark_boundary_reason(scope, "request_body_read_timeout")
                await _error_response(
                    408,
                    "request_body_read_timeout",
                    "讀取請求內容逾時。",
                )(scope, receive, send)
                return
            if message["type"] != "http.request":
                buffered.append(message)
                break
            received_bytes += len(message.get("body", b""))
            if received_bytes > self.max_body_bytes:
                _mark_boundary_reason(scope, "request_body_too_large")
                await _error_response(
                    413,
                    "request_body_too_large",
                    "請求內容超過 16 KiB 上限。",
                )(scope, receive, send)
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break

        async def receive_buffered() -> Message:
            if buffered:
                return buffered.pop(0)
            # Once the bounded body has been replayed, preserve the real ASGI
            # receive channel.  Fabricating disconnect here made every normal
            # request look abandoned to downstream admission control.
            return await receive()

        await self.app(scope, receive_buffered, send)
