"""Privacy-safe, allowlist-only application telemetry.

Birth date/time and precise coordinates are the application's most sensitive
inputs.  This module therefore never serializes a Request, response, exception,
header value, URL, query string, client address, or user agent.  Callers may
only supply values that are reduced to a closed vocabulary before emission.

The output is operational telemetry, not an audit trail.  Delivery is
best-effort: a broken logging sink must never break a calculation request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sys
import time
import uuid

if sys.version_info < (3, 11):
    # Directly declared for the supported Python 3.10 floor. Import failure is
    # fatal: silently losing mixed-group cancellation semantics would violate
    # the privacy boundary's process-control contract.
    from exceptiongroup import BaseExceptionGroup  # type: ignore[import-not-found]
from collections.abc import Callable, Mapping
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .request_limits import BOUNDARY_REASON_STATE_KEY


EVENT_SCHEMA_VERSION: Final = "privacy-request-event-v1"
EVENT_NAME: Final = "http_request_completed"

ALLOWED_EVENT_FIELDS: Final = (
    "event_schema_version",
    "event",
    "request_id",
    "route",
    "method",
    "status_code",
    "duration_bucket",
    "request_size_bucket",
    "outcome",
    "error_code",
)

_ALLOWED_METHODS: Final = frozenset({"GET", "POST", "HEAD", "OPTIONS"})
_ALLOWED_ROUTES: Final = frozenset(
    {
        "/api/chart",
        "/api/client-config",
        "/api/health",
        "/api/places/search",
        "/api/runtime-health",
        "/openapi.json",
    }
)
_ALLOWED_ERROR_CODES: Final = frozenset(
    {
        "compute_capacity_exhausted",
        "conflicting_request_framing",
        "empty_request_body",
        "invalid_content_length",
        "request_body_too_large",
        "request_capacity_exhausted",
        "request_headers_too_large",
        "request_rejected",
        "server_error",
        "internal_server_error",
        "unsupported_media_type",
        "unsupported_method",
        "unclassified",
    }
)
_BOUNDARY_REJECTION_CODES: Final = _ALLOWED_ERROR_CODES - {
    "server_error", "internal_server_error", "unclassified"
}
_ALLOWED_DURATION_BUCKETS: Final = frozenset(
    {
        "invalid",
        "lt_10ms",
        "10_to_49ms",
        "50_to_249ms",
        "250_to_999ms",
        "gte_1000ms",
    }
)
_ALLOWED_SIZE_BUCKETS: Final = frozenset(
    {
        "absent",
        "invalid",
        "empty",
        "1_to_1024b",
        "1025_to_16384b",
        "16385_to_131072b",
        "gt_131072b",
    }
)
_ALLOWED_OUTCOMES: Final = frozenset({"success", "rejected", "error", "unknown"})
_REQUEST_ID_PATTERN: Final = re.compile(r"^[0-9a-f]{32}$")
_PROCESS_CONTROL_EXCEPTIONS: Final = (
    asyncio.CancelledError,
    KeyboardInterrupt,
    SystemExit,
    GeneratorExit,
)

_SECURITY_HEADERS: Final = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; object-src 'none'; "
        "frame-ancestors 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}

_SECURITY_LOGGER = logging.getLogger("classical_astrology.privacy")
_SECURITY_LOGGER.setLevel(logging.INFO)
_SECURITY_LOGGER.propagate = False

if not any(
    getattr(handler, "_classical_astrology_privacy_handler", False)
    for handler in _SECURITY_LOGGER.handlers
):
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _handler._classical_astrology_privacy_handler = True  # type: ignore[attr-defined]
    _SECURITY_LOGGER.addHandler(_handler)


def _safe_method(value: object) -> str:
    if isinstance(value, str):
        normalized = value.upper()
        if normalized in _ALLOWED_METHODS:
            return normalized
    return "OTHER"


def _safe_route(value: object) -> str:
    if isinstance(value, str) and value in _ALLOWED_ROUTES:
        return value
    return "frontend_or_unmatched"


def _safe_status_code(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
        return value
    return 0


def _duration_bucket(value: object) -> str:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        return "invalid"
    if value < 10:
        return "lt_10ms"
    if value < 50:
        return "10_to_49ms"
    if value < 250:
        return "50_to_249ms"
    if value < 1_000:
        return "250_to_999ms"
    return "gte_1000ms"


def _request_size_bucket(value: object) -> str:
    if value is None:
        return "absent"
    if not isinstance(value, str):
        return "invalid"
    try:
        size = int(value, 10)
    except (TypeError, ValueError):
        return "invalid"
    if size < 0:
        return "invalid"
    if size == 0:
        return "empty"
    if size <= 1_024:
        return "1_to_1024b"
    if size <= 16_384:
        return "1025_to_16384b"
    if size <= 131_072:
        return "16385_to_131072b"
    return "gt_131072b"


def _outcome(status_code: int, error_code: str | None) -> str:
    # A response can fail after its HTTP status has already been sent.  Keep the
    # wire status truthful while allowing telemetry to record the completed
    # operation as an error.
    if error_code in {"internal_server_error", "server_error"}:
        return "error"
    if error_code in _BOUNDARY_REJECTION_CODES:
        return "rejected"
    if 200 <= status_code <= 399:
        return "success"
    if 400 <= status_code <= 499:
        return "rejected"
    if 500 <= status_code <= 599:
        return "error"
    return "unknown"


def _safe_error_code(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in _ALLOWED_ERROR_CODES:
        return value
    return "unclassified"


def _boundary_reason(scope: Scope) -> str | None:
    state = scope.get("state")
    if not isinstance(state, Mapping):
        return None
    value = state.get(BOUNDARY_REASON_STATE_KEY)
    if isinstance(value, str) and value in _BOUNDARY_REJECTION_CODES:
        return value
    return None


def _safe_request_id(value: object) -> str:
    if isinstance(value, str) and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return "invalid"


def _is_process_control_exception(error: BaseException) -> bool:
    """Keep cancellation and interpreter-control signals outside containment."""

    if isinstance(error, BaseExceptionGroup):
        return any(
            _is_process_control_exception(nested)
            for nested in error.exceptions
        )
    return isinstance(error, _PROCESS_CONTROL_EXCEPTIONS)


def _raise_if_process_control(error: BaseException) -> None:
    if isinstance(error, BaseExceptionGroup):
        control_subgroup = error.subgroup(_PROCESS_CONTROL_EXCEPTIONS)
        if control_subgroup is not None:
            raise control_subgroup
        return
    if isinstance(error, _PROCESS_CONTROL_EXCEPTIONS):
        raise error


def build_request_event(
    *,
    request_id: str,
    method: object,
    path: object,
    status_code: object,
    duration_ms: object,
    content_length: object,
    error_code: object,
) -> dict:
    """Build one event using only server-generated or reduced values.

    ``request_id`` is retained only when it matches the server-generated UUID
    hex format.  External request IDs are deliberately ignored at the HTTP
    middleware call site, and this reducer fails closed if that invariant later
    regresses.
    """

    safe_status = _safe_status_code(status_code)
    safe_error = _safe_error_code(error_code)
    event = {
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "event": EVENT_NAME,
        "request_id": _safe_request_id(request_id),
        "route": _safe_route(path),
        "method": _safe_method(method),
        "status_code": safe_status,
        "duration_bucket": _duration_bucket(duration_ms),
        "request_size_bucket": _request_size_bucket(content_length),
        "outcome": _outcome(safe_status, safe_error),
        "error_code": safe_error,
    }
    return event


def _event_is_safe(event: object) -> bool:
    if not isinstance(event, dict) or tuple(event) != ALLOWED_EVENT_FIELDS:
        return False
    return (
        event["event_schema_version"] == EVENT_SCHEMA_VERSION
        and event["event"] == EVENT_NAME
        and isinstance(event["request_id"], str)
        and (
            event["request_id"] == "invalid"
            or bool(_REQUEST_ID_PATTERN.fullmatch(event["request_id"]))
        )
        and event["route"] in _ALLOWED_ROUTES | {"frontend_or_unmatched"}
        and event["method"] in _ALLOWED_METHODS | {"OTHER"}
        and isinstance(event["status_code"], int)
        and not isinstance(event["status_code"], bool)
        and 0 <= event["status_code"] <= 599
        and event["duration_bucket"] in _ALLOWED_DURATION_BUCKETS
        and event["request_size_bucket"] in _ALLOWED_SIZE_BUCKETS
        and event["outcome"] in _ALLOWED_OUTCOMES
        and (
            event["error_code"] is None
            or event["error_code"] in _ALLOWED_ERROR_CODES
        )
    )


def emit_security_event(event: dict) -> bool:
    """Emit an already-sanitized event without affecting request processing."""

    try:
        if not _event_is_safe(event):
            return False
        payload = {field: event[field] for field in ALLOWED_EVENT_FIELDS}
        _SECURITY_LOGGER.info(
            "PRIVACY_EVENT %s",
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        )
    except BaseException as error:
        _raise_if_process_control(error)
        # Do not log the logging failure: fallback logging could reintroduce an
        # uncontrolled sink or exception representation.  Operational metrics
        # may report aggregate sink health only after a deployment is designed.
        return False
    return True


def _content_length_from_scope(scope: Scope) -> str | None:
    """Return only the raw Content-Length value for later numeric bucketing."""

    try:
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name.lower() == b"content-length":
                return raw_value.decode("ascii", errors="replace")
    except BaseException as error:
        _raise_if_process_control(error)
        return None
    return None


def _apply_security_headers(
    message: Message,
    request_id: str,
    additional_response_headers: Mapping[str, str],
) -> None:
    headers = MutableHeaders(scope=message)
    for name, value in _SECURITY_HEADERS.items():
        headers[name] = value
    for name, value in additional_response_headers.items():
        headers[name] = value
    headers["X-Request-ID"] = request_id


def _error_response(error_code: str) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code": error_code,
                "message": (
                    "伺服器無法完成本次計算。請重新嘗試；若持續發生，"
                    "請回報 request ID。"
                ),
            }
        },
    )


def _is_excluded_event_path(
    path: object,
    excluded_event_routes: frozenset[str],
) -> bool:
    if not isinstance(path, str):
        return False
    return any(
        path == route or path.startswith(f"{route}/")
        for route in excluded_event_routes
    )


class PrivacyBoundaryMiddleware:
    """ASGI-level privacy, error-containment, and response-header boundary.

    The low-level ASGI shape is intentional.  Function-style FastAPI middleware
    receives a response as soon as ``http.response.start`` is available, so it
    cannot contain exceptions later raised by a streaming iterator or response
    background task.  This wrapper awaits the whole downstream ASGI call, emits
    exactly one event after it finishes, and never sends raw exception text to a
    fallback logger.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        event_emitter: Callable[[dict], bool] = emit_security_event,
        excluded_event_routes: frozenset[str] = frozenset(),
        additional_response_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.app = app
        self.event_emitter = event_emitter
        self.excluded_event_routes = excluded_event_routes
        self.additional_response_headers = dict(
            additional_response_headers or {}
        )

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        response_started = False
        response_complete = False
        status_code = 0
        error_code: str | None = None
        process_control_exception = False

        async def send_with_boundary(message: Message) -> None:
            nonlocal response_started, response_complete, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = _safe_status_code(message.get("status"))
                _apply_security_headers(
                    message,
                    request_id,
                    self.additional_response_headers,
                )
            elif message["type"] == "http.response.body":
                response_complete = not message.get("more_body", False)
            await send(message)

        try:
            await self.app(scope, receive, send_with_boundary)
            if not response_started:
                error_code = "internal_server_error"
                try:
                    await _error_response(error_code)(
                        scope,
                        receive,
                        send_with_boundary,
                    )
                except BaseException as error:
                    _raise_if_process_control(error)
                    # A failed ASGI send has no safe fallback sink.  The
                    # allowlisted completion event below remains best-effort.
                    pass
        except BaseException as error:
            if _is_process_control_exception(error):
                process_control_exception = True
                _raise_if_process_control(error)
            # Do not re-raise into ServerErrorMiddleware/Uvicorn: their default
            # error logger includes exception text and tracebacks.  Before
            # response start we can still replace the response with a stable
            # generic 500.  Afterwards the HTTP status cannot be changed.
            error_code = "internal_server_error"
            if not response_started:
                try:
                    await _error_response(error_code)(
                        scope,
                        receive,
                        send_with_boundary,
                    )
                except BaseException as response_error:
                    _raise_if_process_control(response_error)
                    pass
            elif not response_complete:
                try:
                    await send({"type": "http.response.body", "body": b""})
                except BaseException as send_error:
                    _raise_if_process_control(send_error)
                    pass
        finally:
            if (
                not process_control_exception
                and not _is_excluded_event_path(
                    scope.get("path"),
                    self.excluded_event_routes,
                )
            ):
                if error_code is None:
                    boundary_reason = _boundary_reason(scope)
                    if boundary_reason is not None:
                        error_code = boundary_reason
                    elif 400 <= status_code <= 499:
                        error_code = "request_rejected"
                    elif status_code >= 500:
                        error_code = "server_error"

                try:
                    event = build_request_event(
                        request_id=request_id,
                        method=scope.get("method"),
                        # ASGI scope path excludes the query string.  Unknown/static
                        # paths are reduced to a fixed label.
                        path=scope.get("path"),
                        status_code=status_code,
                        duration_ms=(time.perf_counter() - started) * 1_000,
                        content_length=_content_length_from_scope(scope),
                        error_code=error_code,
                    )
                    self.event_emitter(event)
                except BaseException as event_error:
                    _raise_if_process_control(event_error)
                    # Completion telemetry is best-effort.  A reviewed future
                    # reducer or sink must not expose its exception to Uvicorn.
                    pass
