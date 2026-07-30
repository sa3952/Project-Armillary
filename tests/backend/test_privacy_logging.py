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
import io
import inspect
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi.testclient import TestClient
import pytest
import swisseph as swe


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "backend"
LOCAL_LAUNCHER = PROJECT_ROOT / "start.command"
RUN_LOCAL = PROJECT_ROOT / "scripts" / "local" / "run-local.sh"

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
}


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _payload() -> dict:
    return {
        "datetime": {
            "year": 1997,
            "month": 8,
            "day": 17,
            "hour": 9,
            "minute": 42,
            "second": 0,
        },
        "timezone": {"mode": "iana", "iana_name": "Asia/Taipei"},
        "location": {
            "latitude": CANARY_LATITUDE,
            "longitude": CANARY_LONGITUDE,
            "altitude_m": 87,
        },
    }


def _request(url: str, body: bytes | None = None) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = Request(url, data=body, headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


@pytest.mark.local_launcher
def test_supported_launcher_disables_uvicorn_access_log_and_loads_privacy_boundary():
    launcher = RUN_LOCAL.read_text(encoding="utf-8")

    assert "--no-access-log" in launcher
    assert (PROJECT_ROOT / "backend" / "app" / "privacy_logging.py").is_file()


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


def test_event_builder_does_not_depend_on_optimizer_sensitive_assert():
    from app import privacy_logging

    source = inspect.getsource(privacy_logging.build_request_event)
    assert "assert " not in source


def test_completion_event_failures_do_not_escape_the_asgi_boundary(monkeypatch):
    from app import privacy_logging

    class CompletionProbe(BaseException):
        pass

    async def app(_scope, _receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    def fail_builder(**_kwargs):
        raise CompletionProbe("completion-event-canary")

    monkeypatch.setattr(privacy_logging, "build_request_event", fail_builder)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = privacy_logging.PrivacyBoundaryMiddleware(app)
    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/health",
                "headers": [],
            },
            receive,
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
    assert rejected_events[0]["outcome"] == "error"


def test_hostile_scope_header_iterator_is_reduced_without_escaping():
    from app import privacy_logging

    class HeaderProbe(BaseException):
        pass

    class HostileHeaders:
        def __iter__(self):
            raise HeaderProbe("header-iterator-canary")

    captured_events = []

    async def app(_scope, _receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    middleware = privacy_logging.PrivacyBoundaryMiddleware(
        app,
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
            receive,
            send,
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
        asyncio.CancelledError(),
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

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

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
                receive,
                send,
            )
        )

    if isinstance(control_error, BaseExceptionGroup):
        assert privacy_logging._is_process_control_exception(captured.value)
        assert len(captured.value.exceptions) == 1
    else:
        assert captured.value is control_error
    assert captured_events == []


def test_process_control_exception_from_event_emitter_is_not_silenced():
    from app import privacy_logging

    control_error = asyncio.CancelledError()

    async def app(_scope, _receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    def cancelled_emitter(_event):
        raise control_error

    middleware = privacy_logging.PrivacyBoundaryMiddleware(
        app,
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
                receive,
                send,
            )
        )

    assert captured.value is control_error


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

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

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
                receive,
                send,
            )
        )

    assert privacy_logging._is_process_control_exception(captured.value)
    assert sensitive_canary not in repr(captured.value)
    assert all(
        not isinstance(nested, PrivacyProbe)
        for nested in captured.value.exceptions
    )


def test_process_control_policy_discloses_header_and_event_boundary():
    private_policy = (
        PROJECT_ROOT / "docs" / "privacy" / "PRIVACY_LOGGING_POLICY.md"
    )
    public_policy = PROJECT_ROOT / "docs" / "PRIVACY_LOGGING_POLICY.md"
    policy_path = (
        private_policy if private_policy.is_file() else public_policy
    )
    policy = policy_path.read_text(encoding="utf-8")

    if policy.startswith("# Privacy Logging Policy"):
        assert "process-control exception re-raise path" in policy
        assert (
            "does not guarantee that security headers or the completion "
            "event are still\nemitted"
        ) in policy
        assert "重新拋出路徑" not in policy
        assert "不保證補送" not in policy
    else:
        assert "process-control exception重新拋出路徑" in policy


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


def test_chart_request_does_not_use_python_file_write_apis(monkeypatch):
    """Fail if request handling adds an application-level persistence path.

    This guard covers Python writes in the application request path.  It does
    not claim secure memory erasure, and it does not cover future proxies,
    hosting platforms, browser downloads, or native-library behavior.
    """

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
    assert response.json() == {
        "detail": {
            "code": "swiss_ephemeris_error",
            "message": "天文計算失敗；伺服器端星曆資料可能無法使用。",
        }
    }
    assert CANARY_TEXT not in response.text
    assert str(CANARY_LATITUDE) not in response.text
    assert str(CANARY_LONGITUDE) not in response.text


def test_real_uvicorn_unexpected_error_does_not_emit_traceback_or_canary():
    port = _unused_local_port()
    script = "\n".join(
        (
            "import uvicorn",
            "from app import main",
            "def fail(_request):",
            f"    raise RuntimeError({CANARY_TEXT!r})",
            "main._compute_chart_locked = fail",
            (
                "uvicorn.run(main.app, host='127.0.0.1', "
                f"port={port}, access_log=False)"
            ),
        )
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=BACKEND_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise AssertionError(
                    "unexpected-error server stopped early:\n"
                    + process.stdout.read()
                )
            try:
                status, _ = _request(f"http://127.0.0.1:{port}/api/health")
                if status == 200:
                    break
            except URLError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("unexpected-error server startup timed out")
            time.sleep(0.05)

        error_status, body = _request(
            f"http://127.0.0.1:{port}/api/chart",
            json.dumps(_payload()).encode(),
        )
        assert error_status == 500
        assert json.loads(body)["detail"]["code"] == "internal_server_error"
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        output = process.stdout.read()

    assert CANARY_TEXT not in output
    assert "Traceback (most recent call last)" not in output
    assert "RuntimeError" not in output
    assert "internal_server_error" in output


def test_real_uvicorn_non_control_base_exception_is_contained():
    base_canary = "PRIVACY_BASE_EXCEPTION_CANARY_6C_734991"
    port = _unused_local_port()
    script = "\n".join(
        (
            "import uvicorn",
            "from starlette.routing import Route",
            "from app import main",
            "class PrivacyProbe(BaseException):",
            "    pass",
            "async def fail(_request):",
            f"    raise PrivacyProbe({base_canary!r})",
            (
                "main.app.router.routes.insert("
                "0, Route('/__privacy_base_exception', fail))"
            ),
            (
                "uvicorn.run(main.app, host='127.0.0.1', "
                f"port={port}, access_log=False)"
            ),
        )
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=BACKEND_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise AssertionError(
                    "base-exception server stopped early:\n"
                    + process.stdout.read()
                )
            try:
                status, _ = _request(f"http://127.0.0.1:{port}/api/health")
                if status == 200:
                    break
            except URLError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("base-exception server startup timed out")
            time.sleep(0.05)

        error_status, body = _request(
            f"http://127.0.0.1:{port}/__privacy_base_exception"
        )
        assert error_status == 500
        assert json.loads(body)["detail"]["code"] == "internal_server_error"
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        output = process.stdout.read()

    assert base_canary not in output
    assert "Traceback (most recent call last)" not in output
    assert "PrivacyProbe" not in output
    assert "internal_server_error" in output


def test_real_uvicorn_event_emitter_failure_is_isolated():
    emitter_canary = "PRIVACY_EMITTER_FAILURE_CANARY_6C_734991"
    port = _unused_local_port()
    script = "\n".join(
        (
            "import uvicorn",
            "from app import main",
            "def fail_emitter(_event):",
            f"    raise OSError({emitter_canary!r})",
            "main.emit_security_event = fail_emitter",
            (
                "uvicorn.run(main.app, host='127.0.0.1', "
                f"port={port}, access_log=False)"
            ),
        )
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=BACKEND_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise AssertionError(
                    "emitter-failure server stopped early:\n"
                    + process.stdout.read()
                )
            try:
                status, body = _request(
                    f"http://127.0.0.1:{port}/api/health"
                )
                if status == 200:
                    assert json.loads(body)["status"] == "ok"
                    break
            except URLError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("emitter-failure server startup timed out")
            time.sleep(0.05)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        output = process.stdout.read()

    assert emitter_canary not in output
    assert "Traceback (most recent call last)" not in output
    assert "OSError" not in output


def test_real_uvicorn_post_start_errors_are_contained_and_reported():
    """Cover errors raised after ``http.response.start``.

    A function-style FastAPI middleware returns as soon as response headers are
    available.  Streaming iterators and background tasks can fail afterwards,
    so this must be exercised through the real ASGI server rather than only
    TestClient's ordinary pre-response exception path.
    """

    stream_canary = "PRIVACY_STREAM_CANARY_6C_734991"
    background_canary = "PRIVACY_BACKGROUND_CANARY_6C_734991"
    port = _unused_local_port()
    script = "\n".join(
        (
            "import uvicorn",
            "from fastapi.responses import PlainTextResponse, StreamingResponse",
            "from starlette.background import BackgroundTask",
            "from starlette.routing import Route",
            "from app import main",
            "async def broken_stream():",
            "    yield b'ok'",
            f"    raise RuntimeError({stream_canary!r})",
            "async def stream_route(_request):",
            "    return StreamingResponse(broken_stream())",
            "def broken_background():",
            f"    raise RuntimeError({background_canary!r})",
            "async def background_route(_request):",
            (
                "    return PlainTextResponse("
                "b'ok', background=BackgroundTask(broken_background))"
            ),
            (
                "main.app.router.routes.insert("
                "0, Route('/__privacy_stream', stream_route))"
            ),
            (
                "main.app.router.routes.insert("
                "0, Route('/__privacy_background', background_route))"
            ),
            (
                "uvicorn.run(main.app, host='127.0.0.1', "
                f"port={port}, access_log=False)"
            ),
        )
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=BACKEND_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise AssertionError(
                    "post-start-error server stopped early:\n"
                    + process.stdout.read()
                )
            try:
                status, _ = _request(f"http://127.0.0.1:{port}/api/health")
                if status == 200:
                    break
            except URLError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("post-start-error server startup timed out")
            time.sleep(0.05)

        stream_status, stream_body = _request(
            f"http://127.0.0.1:{port}/__privacy_stream"
        )
        background_status, background_body = _request(
            f"http://127.0.0.1:{port}/__privacy_background"
        )
        assert stream_status == 200
        assert stream_body == b"ok"
        assert background_status == 200
        assert background_body == b"ok"
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        output = process.stdout.read()

    assert stream_canary not in output
    assert background_canary not in output
    assert "Traceback (most recent call last)" not in output
    assert "RuntimeError" not in output

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


@pytest.mark.local_launcher
def test_real_launcher_logs_are_body_free_for_success_rejection_and_malformed_json():
    port = _unused_local_port()
    base_url = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [str(LOCAL_LAUNCHER), "--no-open", "--port", str(port)],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                raise AssertionError(
                    "privacy self-test server stopped early:\n"
                    + process.stdout.read()
                )
            try:
                status, _ = _request(
                    f"{base_url}/api/health?private={CANARY_TEXT}"
                )
                if status == 200:
                    break
            except URLError:
                pass
            if time.monotonic() >= deadline:
                raise AssertionError("privacy self-test server startup timed out")
            time.sleep(0.05)

        success_status, _ = _request(
            f"{base_url}/api/chart",
            json.dumps(_payload()).encode(),
        )
        rejected_payload = _payload()
        rejected_payload["timezone"] = {
            "mode": "iana",
            "iana_name": CANARY_TEXT,
        }
        rejected_status, _ = _request(
            f"{base_url}/api/chart",
            json.dumps(rejected_payload).encode(),
        )
        malformed_status, _ = _request(
            f"{base_url}/api/chart",
            f'{{"private":"{CANARY_TEXT}",'.encode(),
        )

        assert success_status == 200
        assert rejected_status == 422
        assert malformed_status == 422
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        output = process.stdout.read()

    for canary in (
        CANARY_TEXT,
        str(CANARY_LATITUDE),
        str(CANARY_LONGITUDE),
    ):
        assert canary not in output
    assert '"POST /api/chart HTTP/' not in output

    records = []
    for line in output.splitlines():
        marker = "PRIVACY_EVENT "
        if marker in line:
            records.append(json.loads(line.split(marker, 1)[1]))

    assert len(records) >= 4
    assert {record["route"] for record in records} >= {
        "/api/health",
        "/api/chart",
    }
    assert {record["status_code"] for record in records} >= {200, 422}
    assert all(set(record) == EXPECTED_EVENT_FIELDS for record in records)
    assert all(record["request_id"] for record in records)
