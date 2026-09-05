"""Executable privacy controls for Task 6C-1 through 6C-3.

These tests deliberately use recognizable canaries.  Passing requires more than
"the application currently has no body logger": the supported launcher must
disable Uvicorn access logs, the application must emit only a closed structured
event schema, and successful, rejected, malformed, and unexpected-error paths
must not leak the canaries.
"""

from __future__ import annotations

import asyncio
import builtins
from contextlib import contextmanager
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.error import URLError

from fastapi.testclient import TestClient
import pytest
import swisseph as swe
from tests.backend import http_request as _request
from tests.backend import minimal_chart_payload
from tests.backend import unused_local_port as _unused_local_port

if sys.version_info < (3, 11):
    from exceptiongroup import BaseExceptionGroup


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
CANARY_TEXT = "PRIVACY_CANARY_DO_NOT_LOG_6C_734991"
CANARY_LATITUDE = 23.456789
CANARY_LONGITUDE = 123.456789

EXPECTED_EVENT_FIELDS = {
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
    # Ruled 2026-09-03: the exception class name, drawn from a closed allowlist,
    # so an operator can tell one unexpected failure from another.
    "failure_class",
}


def _payload() -> dict:
    return minimal_chart_payload(
        year=1997, month=8, day=17, hour=9, minute=42,
        latitude=CANARY_LATITUDE, longitude=CANARY_LONGITUDE, altitude_m=87,
    )


async def _empty_request():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _ignore_send(_message):
    return None


async def _no_content_app(_scope, _receive, send):
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


@contextmanager
def _real_uvicorn_probe(label: str, *setup: str):
    port = _unused_local_port()
    script = "\n".join((
        "import uvicorn",
        *setup,
        (
            "uvicorn.run(main.app, host='127.0.0.1', "
            f"port={port}, access_log=False, http='h11')"
        ),
    ))
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=BACKEND_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    probe = SimpleNamespace(base_url=f"http://127.0.0.1:{port}", output="")
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(
                    f"{label} server stopped early:\n" + process.stdout.read()
                )
            try:
                if _request(probe.base_url + "/api/health")[0] == 200:
                    break
            except URLError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError(f"{label} server startup timed out")
        yield probe
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        probe.output = process.stdout.read()


def test_structured_event_schema_is_closed_and_discards_attacker_controlled_text():
    from app.privacy_logging import ALLOWED_EVENT_FIELDS, build_request_event

    event = build_request_event(
        request_id=f"server-generated-{CANARY_TEXT}",
        method=f"POST\r\n{CANARY_TEXT}",
        path=f"/attacker/{CANARY_TEXT}",
        status_code=422,
        duration_ms=731.0,
        content_length=f"invalid-{CANARY_TEXT}",
        error_code="request_rejected",
    )
    encoded = json.dumps(event, ensure_ascii=False)

    assert set(event) == EXPECTED_EVENT_FIELDS
    assert set(event) == set(ALLOWED_EVENT_FIELDS)
    assert event["method"] == "OTHER"
    assert event["route"] == "frontend_or_unmatched"
    assert event["request_id"] == "invalid"
    assert event["request_size_bucket"] == "invalid"
    assert CANARY_TEXT not in encoded
    for forbidden_field in (
        "request_body",
        "response_body",
        "query_string",
        "client_ip",
        "user_agent",
        "headers",
        "exception",
        "latitude",
        "longitude",
        "datetime",
    ):
        assert forbidden_field not in event


def test_place_search_uses_fixed_route_label_without_query_text():
    from app.privacy_logging import build_request_event

    event = build_request_event(
        request_id="a" * 32,
        method="POST",
        path="/api/places/search",
        status_code=200,
        duration_ms=5,
        content_length="72",
        error_code=None,
    )

    assert event["route"] == "/api/places/search"
    assert "query" not in event


def test_duration_bucket_rejects_non_finite_values():
    from app import privacy_logging

    assert privacy_logging._duration_bucket(math.nan) == "invalid"
    assert privacy_logging._duration_bucket(math.inf) == "invalid"
    assert privacy_logging._duration_bucket(-math.inf) == "invalid"
    assert privacy_logging._duration_bucket(999.9) == "250_to_999ms"
    assert privacy_logging._duration_bucket(1_000.0) == "gte_1000ms"


def test_completion_event_failures_do_not_escape_the_asgi_boundary(monkeypatch):
    from app import privacy_logging

    class CompletionProbe(BaseException):
        pass

    def fail_builder(**_kwargs):
        raise CompletionProbe("completion-event-canary")

    monkeypatch.setattr(privacy_logging, "build_request_event", fail_builder)
    sent = []

    async def send(message):
        sent.append(message)

    middleware = privacy_logging.PrivacyBoundaryMiddleware(_no_content_app)
    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/health",
                "headers": [],
            },
            _empty_request,
            send,
        )
    )

    assert sent[0]["status"] == 204


def test_capacity_rejection_stays_inside_headers_and_telemetry_boundary():
    from app.privacy_logging import PrivacyBoundaryMiddleware
    from app.request_limits import RequestCapacityBoundary

    async def exercise():
        release = asyncio.Event()
        entered = 0
        all_entered = asyncio.Event()
        events: list[dict] = []

        async def held_app(_scope, _receive, send):
            nonlocal entered
            entered += 1
            if entered == 2:
                all_entered.set()
            await release.wait()
            await send({
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b""})

        boundary = PrivacyBoundaryMiddleware(
            RequestCapacityBoundary(held_app, max_concurrent=2),
            event_emitter=lambda event: events.append(event) or True,
        )

        async def call():
            messages = []

            async def receive():
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }

            async def send(message):
                messages.append(message)

            await boundary(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/health",
                    "headers": [],
                },
                receive,
                send,
            )
            return messages

        accepted = [asyncio.create_task(call()) for _ in range(2)]
        await asyncio.wait_for(all_entered.wait(), timeout=2)
        rejected = await call()
        release.set()
        await asyncio.gather(*accepted)
        return rejected, events

    rejected, events = asyncio.run(exercise())
    start = rejected[0]
    assert start["status"] == 503
    headers = {
        name.decode().lower(): value.decode()
        for name, value in start["headers"]
    }
    assert "content-security-policy" in headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-request-id"]
    rejected_events = [
        event for event in events if event["status_code"] == 503
    ]
    assert len(rejected_events) == 1
    assert rejected_events[0]["outcome"] == "rejected"
    assert rejected_events[0]["error_code"] == "request_capacity_exhausted"


def test_boundary_reason_producer_and_privacy_consumer_are_closed():
    from app import privacy_logging, request_limits

    for reason in sorted(request_limits.BOUNDARY_REASON_CODES):
        scope = {"type": "http", "state": {}}
        request_limits._mark_boundary_reason(scope, reason)
        assert privacy_logging._boundary_reason(scope) == reason
        event = privacy_logging.build_request_event(
            request_id="a" * 32,
            method="POST",
            path="/api/chart",
            status_code=503 if "capacity" in reason else 400,
            duration_ms=1,
            content_length="1",
            error_code=privacy_logging._boundary_reason(scope),
        )
        assert event["error_code"] == reason
        assert event["outcome"] == "rejected"

    assert privacy_logging._boundary_reason({
        "type": "http",
        "state": {
            request_limits.BOUNDARY_REASON_STATE_KEY: "attacker_text"
        },
    }) is None

    domain_scope = {
        "type": "http",
        "state": {
            privacy_logging.DOMAIN_ERROR_STATE_KEY: "full_ephemeris_required"
        },
    }
    assert privacy_logging._domain_error_reason(domain_scope) == (
        "full_ephemeris_required"
    )
    domain_event = privacy_logging.build_request_event(
        request_id="b" * 32,
        method="POST",
        path="/api/chart",
        status_code=503,
        duration_ms=1,
        content_length="1",
        error_code=privacy_logging._domain_error_reason(domain_scope),
    )
    assert domain_event["outcome"] == "error"


def test_boundary_reason_is_single_assignment_and_preserves_first_producer():
    from app import privacy_logging, request_limits

    scope = {"type": "http", "state": {}}
    request_limits._mark_boundary_reason(scope, "request_body_too_large")
    with pytest.raises(RuntimeError, match="already assigned"):
        request_limits._mark_boundary_reason(scope, "compute_capacity_exhausted")

    assert privacy_logging._boundary_reason(scope) == "request_body_too_large"


def test_hostile_scope_header_iterator_is_reduced_without_escaping():
    from app import privacy_logging

    class HeaderProbe(BaseException):
        pass

    class HostileHeaders:
        def __iter__(self):
            raise HeaderProbe("header-iterator-canary")

    captured_events = []

    middleware = privacy_logging.PrivacyBoundaryMiddleware(
        _no_content_app,
        event_emitter=lambda event: captured_events.append(event) or True,
    )
    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/health",
                "headers": HostileHeaders(),
            },
            _empty_request,
            _ignore_send,
        )
    )

    assert captured_events[-1]["request_size_bucket"] == "absent"


def test_process_control_base_exceptions_remain_propagating():
    from app import privacy_logging

    assert privacy_logging._is_process_control_exception(
        asyncio.CancelledError()
    )
    assert privacy_logging._is_process_control_exception(KeyboardInterrupt())
    assert privacy_logging._is_process_control_exception(SystemExit())
    assert privacy_logging._is_process_control_exception(GeneratorExit())

    class PrivacyProbe(BaseException):
        pass

    assert not privacy_logging._is_process_control_exception(PrivacyProbe())
    assert privacy_logging._is_process_control_exception(
        BaseExceptionGroup(
            "cancelled task group",
            [asyncio.CancelledError()],
        )
    )
    assert not privacy_logging._is_process_control_exception(
        BaseExceptionGroup(
            "non-control task group",
            [PrivacyProbe()],
        )
    )


@pytest.mark.parametrize(
    "control_error",
    [
        asyncio.CancelledError("middleware-cancellation-canary"),
        BaseExceptionGroup(
            "cancelled task group",
            [asyncio.CancelledError()],
        ),
    ],
)
def test_process_control_exceptions_propagate_through_middleware(control_error):
    from app import privacy_logging

    captured_events = []

    async def app(_scope, _receive, _send):
        raise control_error

    middleware = privacy_logging.PrivacyBoundaryMiddleware(
        app,
        event_emitter=lambda event: captured_events.append(event) or True,
    )
    with pytest.raises(BaseException) as captured:
        asyncio.run(
            middleware(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/health",
                    "headers": [],
                },
                _empty_request,
                _ignore_send,
            )
        )

    if isinstance(control_error, BaseExceptionGroup):
        assert privacy_logging._is_process_control_exception(captured.value)
        assert len(captured.value.exceptions) == 1
    else:
        # Python 3.10 asyncio.run re-raises cancellation as a fresh
        # CancelledError with empty args.  The invariant at this boundary is
        # that cancellation remains process control and is not converted into
        # an ordinary application failure or swallowed by privacy telemetry.
        assert isinstance(captured.value, asyncio.CancelledError)
    assert captured_events == []


def test_process_control_exception_from_event_emitter_is_not_silenced():
    from app import privacy_logging

    control_error = asyncio.CancelledError("event-emitter-cancellation-canary")

    def cancelled_emitter(_event):
        raise control_error

    middleware = privacy_logging.PrivacyBoundaryMiddleware(
        _no_content_app,
        event_emitter=cancelled_emitter,
    )
    with pytest.raises(asyncio.CancelledError) as captured:
        asyncio.run(
            middleware(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/health",
                    "headers": [],
                },
                _empty_request,
                _ignore_send,
            )
        )

    assert isinstance(captured.value, asyncio.CancelledError)


def test_mixed_exception_group_propagates_only_process_control_subgroup():
    from app import privacy_logging

    class PrivacyProbe(BaseException):
        pass

    sensitive_canary = "MIXED_BASE_EXCEPTION_PRIVACY_CANARY"
    mixed_error = BaseExceptionGroup(
        "mixed task group",
        [
            asyncio.CancelledError(),
            PrivacyProbe(sensitive_canary),
        ],
    )

    async def app(_scope, _receive, _send):
        raise mixed_error

    middleware = privacy_logging.PrivacyBoundaryMiddleware(app)
    with pytest.raises(BaseExceptionGroup) as captured:
        asyncio.run(
            middleware(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/health",
                    "headers": [],
                },
                _empty_request,
                _ignore_send,
            )
        )

    assert privacy_logging._is_process_control_exception(captured.value)
    assert sensitive_canary not in repr(captured.value)
    assert all(
        not isinstance(nested, PrivacyProbe)
        for nested in captured.value.exceptions
    )


def test_logging_sink_failure_cannot_break_request_processing(monkeypatch):
    from app import privacy_logging

    event = privacy_logging.build_request_event(
        request_id="0123456789abcdef0123456789abcdef",
        method="POST",
        path="/api/chart",
        status_code=200,
        duration_ms=10.0,
        content_length="512",
        error_code=None,
    )

    def fail_sink(*_args, **_kwargs):
        raise OSError("simulated unavailable logging sink")

    monkeypatch.setattr(privacy_logging._SECURITY_LOGGER, "info", fail_sink)
    assert privacy_logging.emit_security_event(event) is False


def test_emitter_rejects_direct_unsanitized_event(capsys):
    from app import privacy_logging

    event = privacy_logging.build_request_event(
        request_id="0123456789abcdef0123456789abcdef",
        method="POST",
        path="/api/chart",
        status_code=200,
        duration_ms=10.0,
        content_length="512",
        error_code=None,
    )
    event["route"] = f"/api/chart?private={CANARY_TEXT}"

    assert privacy_logging.emit_security_event(event) is False
    assert CANARY_TEXT not in capsys.readouterr().err


def test_caller_supplied_request_id_is_ignored(monkeypatch):
    from app import main as app_main

    captured_events: list[dict] = []
    monkeypatch.setattr(
        app_main,
        "emit_security_event",
        lambda event: captured_events.append(event) or True,
    )

    with TestClient(app_main.app) as client:
        response = client.get(
            "/api/health",
            headers={"X-Request-ID": CANARY_TEXT},
        )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != CANARY_TEXT
    assert response.headers["Content-Security-Policy"].startswith(
        "default-src 'self'"
    )
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Permissions-Policy"] == (
        "camera=(), microphone=(), geolocation=()"
    )
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Cross-Origin-Embedder-Policy"] == "require-corp"
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert response.headers["Cache-Control"] == "no-store"
    assert captured_events[-1]["request_id"] == response.headers["X-Request-ID"]
    assert CANARY_TEXT not in json.dumps(captured_events, ensure_ascii=False)


def test_public_cache_policy_separates_api_html_and_versioned_assets():
    from app.main import create_app
    from app.settings import AppProfile, AppSettings

    application = create_app(AppSettings(profile=AppProfile.PUBLIC))
    with TestClient(application) as client:
        api = client.get("/api/health")
        html = client.get("/zh-TW/features")
        versioned = client.get("/zh-TW/page.css?v=release-test")
        false_version_substring = client.get(
            "/zh-TW/page.css?preview=release-test"
        )
        empty_version = client.get("/zh-TW/page.css?v=")
        unversioned = client.get("/zh-TW/favicon.svg")
        missing = client.get("/missing.css?v=release-test")

    assert api.headers["cache-control"] == "no-store"
    assert html.headers["cache-control"] == "no-cache"
    assert versioned.headers["cache-control"] == (
        "public, max-age=31536000, immutable"
    )
    assert false_version_substring.headers["cache-control"] == (
        "public, max-age=300"
    )
    assert empty_version.headers["cache-control"] == "public, max-age=300"
    assert unversioned.headers["cache-control"] == "public, max-age=300"
    assert missing.headers["cache-control"] == "no-store"


def test_chart_request_does_not_use_python_file_write_apis(monkeypatch):

    from app import main as app_main

    observed_writes: list[str] = []
    original_builtin_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open

    def guarded_stream_open(original):
        def open_without_writes(file, mode="r", *args, **kwargs):
            if any(flag in str(mode) for flag in ("w", "a", "x", "+")):
                observed_writes.append(str(file))
                raise AssertionError(f"request attempted file persistence: {file}")
            return original(file, mode, *args, **kwargs)

        return open_without_writes

    def guarded_os_open(path, flags, *args, **kwargs):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        if flags & write_flags:
            observed_writes.append(str(path))
            raise AssertionError(f"request attempted os.open persistence: {path}")
        return original_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_stream_open(original_builtin_open))
    monkeypatch.setattr(io, "open", guarded_stream_open(original_io_open))
    monkeypatch.setattr(os, "open", guarded_os_open)

    with TestClient(app_main.app) as client:
        response = client.post("/api/chart", json=_payload())

    assert response.status_code == 200
    assert observed_writes == []


def test_backend_has_no_third_party_telemetry_or_persistence_dependency():
    guard_path = PROJECT_ROOT / "scripts" / "verification" / "verify_privacy_dependencies.py"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.verification.verify_privacy_dependencies",
            "--check",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PRIVACY DEPENDENCY CHECK PASSED" in completed.stdout


def test_unexpected_exception_returns_generic_error_and_logs_no_exception_text(
    monkeypatch,
):
    from app import main as app_main

    captured_events: list[dict] = []

    def fail_with_sensitive_exception(_request):
        raise RuntimeError(
            f"{CANARY_TEXT}:{CANARY_LATITUDE}:{CANARY_LONGITUDE}"
        )

    monkeypatch.setattr(app_main, "_compute_chart_locked", fail_with_sensitive_exception)
    monkeypatch.setattr(
        app_main,
        "emit_security_event",
        lambda event: captured_events.append(event) or True,
    )

    with TestClient(app_main.app, raise_server_exceptions=False) as client:
        response = client.post("/api/chart", json=_payload())

    assert response.status_code == 500
    assert response.json() == {
        "detail": {
            "code": "internal_server_error",
            "message": "伺服器無法完成本次計算。請重新嘗試；若持續發生，請回報 request ID。",
        }
    }
    assert CANARY_TEXT not in response.text
    assert str(CANARY_LATITUDE) not in response.text
    assert str(CANARY_LONGITUDE) not in response.text
    assert captured_events[-1]["error_code"] == "internal_server_error"
    assert CANARY_TEXT not in json.dumps(captured_events, ensure_ascii=False)


def test_swisseph_error_response_does_not_expose_exception_text(monkeypatch):
    from app import main as app_main

    def fail_with_sensitive_swisseph_error(_request):
        raise swe.Error(f"{CANARY_TEXT}:{CANARY_LATITUDE}:{CANARY_LONGITUDE}")

    monkeypatch.setattr(
        app_main,
        "_compute_chart_locked",
        fail_with_sensitive_swisseph_error,
    )

    with TestClient(app_main.app, raise_server_exceptions=False) as client:
        response = client.post("/api/chart", json=_payload())

    assert response.status_code == 500
    assert response.json() == {"detail": {"code": "swiss_ephemeris_error"}}
    assert CANARY_TEXT not in response.text
    assert str(CANARY_LATITUDE) not in response.text
    assert str(CANARY_LONGITUDE) not in response.text


def test_real_uvicorn_unexpected_error_does_not_emit_traceback_or_canary():
    with _real_uvicorn_probe(
        "unexpected-error",
        "from app import main",
        "def fail(_request):",
        f"    raise RuntimeError({CANARY_TEXT!r})",
        "main._compute_chart_locked = fail",
    ) as probe:
        error_status, body = _request(
            probe.base_url + "/api/chart",
            json.dumps(_payload()).encode(),
        )
        assert error_status == 500
        assert json.loads(body)["detail"]["code"] == "internal_server_error"
    output = probe.output
    assert CANARY_TEXT not in output
    assert "Traceback (most recent call last)" not in output
    assert "internal_server_error" in output
    # Sebastian ruled that an unexpected failure may carry the exception class
    # name and nothing else, so an operator can tell one 500 from another.  The
    # message, the traceback and every request-derived value stay out; the
    # neighbouring probe raises a class the allowlist does not contain and
    # proves that such a name is reduced to `unclassified` rather than emitted.
    assert '"failure_class":"RuntimeError"' in output


def test_real_uvicorn_non_control_base_exception_is_contained():
    base_canary = "PRIVACY_BASE_EXCEPTION_CANARY_6C_734991"
    with _real_uvicorn_probe(
        "base-exception",
        "from starlette.routing import Route",
        "from app import main",
        "class PrivacyProbe(BaseException): pass",
        "async def fail(_request):",
        f"    raise PrivacyProbe({base_canary!r})",
        "main.app.router.routes.insert(0, Route('/__privacy_base_exception', fail))",
    ) as probe:
        error_status, body = _request(
            probe.base_url + "/__privacy_base_exception"
        )
        assert error_status == 500
        assert json.loads(body)["detail"]["code"] == "internal_server_error"
    output = probe.output
    assert base_canary not in output
    assert "Traceback (most recent call last)" not in output
    # The closed allowlist doing its job: an exception class this service does
    # not declare never reaches the log under its own name.
    assert "PrivacyProbe" not in output
    assert '"failure_class":"unclassified"' in output
    assert "internal_server_error" in output


def test_real_uvicorn_event_emitter_failure_is_isolated():
    emitter_canary = "PRIVACY_EMITTER_FAILURE_CANARY_6C_734991"
    with _real_uvicorn_probe(
        "emitter-failure",
        "from app import main",
        "def fail_emitter(_event):",
        f"    raise OSError({emitter_canary!r})",
        "main.emit_security_event = fail_emitter",
    ) as probe:
        status, body = _request(probe.base_url + "/api/health")
        assert status == 200
        assert json.loads(body)["status"] == "ok"
    output = probe.output
    assert emitter_canary not in output
    assert "Traceback (most recent call last)" not in output
    assert "OSError" not in output


def test_real_uvicorn_post_start_errors_are_contained_and_reported():
    stream_canary = "PRIVACY_STREAM_CANARY_6C_734991"
    background_canary = "PRIVACY_BACKGROUND_CANARY_6C_734991"
    with _real_uvicorn_probe(
        "post-start-error",
        "from fastapi.responses import PlainTextResponse, StreamingResponse",
        "from starlette.background import BackgroundTask",
        "from starlette.routing import Route",
        "from app import main",
        "async def broken_stream():",
        "    yield b'ok'",
        f"    raise RuntimeError({stream_canary!r})",
        "async def stream_route(_request): return StreamingResponse(broken_stream())",
        "def broken_background():",
        f"    raise RuntimeError({background_canary!r})",
        "async def background_route(_request):",
        "    return PlainTextResponse(b'ok', background=BackgroundTask(broken_background))",
        "main.app.router.routes.insert(0, Route('/__privacy_stream', stream_route))",
        "main.app.router.routes.insert(0, Route('/__privacy_background', background_route))",
    ) as probe:
        stream_status, stream_body = _request(
            probe.base_url + "/__privacy_stream"
        )
        background_status, background_body = _request(
            probe.base_url + "/__privacy_background"
        )
        assert stream_status == 200
        assert stream_body == b"ok"
        assert background_status == 200
        assert background_body == b"ok"
    output = probe.output
    assert stream_canary not in output
    assert background_canary not in output
    assert "Traceback (most recent call last)" not in output
    # The message and the traceback stay out; the class name is what the ruling
    # permits, and it is the only part of an exception that cannot carry a
    # birth time or a coordinate.
    assert 'RuntimeError' not in output.replace('"failure_class":"RuntimeError"', '')

    records = []
    for line in output.splitlines():
        marker = "PRIVACY_EVENT "
        if marker in line:
            records.append(json.loads(line.split(marker, 1)[1]))
    post_start_failures = [
        record
        for record in records
        if record["route"] == "frontend_or_unmatched"
        and record["error_code"] == "internal_server_error"
    ]
    assert len(post_start_failures) == 2
    assert all(record["status_code"] == 200 for record in post_start_failures)
    assert all(record["outcome"] == "error" for record in post_start_failures)
