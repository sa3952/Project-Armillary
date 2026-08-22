from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient


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
            "latitude": 24.1477,
            "longitude": 120.6736,
            "altitude_m": 80,
        },
    }


def _hosted_client():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    return TestClient(create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA)))


def test_hosted_chart_rejects_missing_or_wrong_content_type():
    with _hosted_client() as client:
        missing = client.post("/api/chart", content=b"{}")
        wrong = client.post(
            "/api/chart",
            content=b"{}",
            headers={"Content-Type": "text/plain"},
        )

    assert missing.status_code == 415
    assert wrong.status_code == 415
    assert missing.json()["detail"]["code"] == "unsupported_media_type"
    assert wrong.json()["detail"]["code"] == "unsupported_media_type"


def test_hosted_chart_accepts_json_with_utf8_charset():
    body = json.dumps(_payload()).encode()
    with _hosted_client() as client:
        response = client.post(
            "/api/chart",
            content=body,
            headers={"Content-Type": "application/json; charset=UTF-8"},
        )

    assert response.status_code == 200


def test_hosted_chart_rejects_known_oversized_body_before_schema_parse():
    body = b"{" + b" " * (16 * 1024) + b"}"
    with _hosted_client() as client:
        response = client.post(
            "/api/chart",
            content=body,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "request_body_too_large"


def test_hosted_place_search_rejects_known_oversized_body_before_schema_parse():
    body = b'{"query":"' + b"x" * (16 * 1024) + b'"}'
    with _hosted_client() as client:
        response = client.post(
            "/api/places/search",
            content=body,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "request_body_too_large"


def test_hosted_chart_rejects_ambiguous_or_non_utf8_media_type():
    with _hosted_client() as client:
        duplicate_charset = client.post(
            "/api/chart",
            content=b"{}",
            headers={
                "Content-Type": (
                    "application/json; charset=utf-8; charset=utf-8"
                )
            },
        )
        non_utf8 = client.post(
            "/api/chart",
            content=b"{}",
            headers={"Content-Type": "application/json; charset=latin-1"},
        )

    assert duplicate_charset.status_code == 415
    assert non_utf8.status_code == 415


def test_local_profile_does_not_silently_acquire_hosted_request_policy():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    local_app = create_app(AppSettings(profile=AppProfile.LOCAL))
    with TestClient(local_app) as client:
        response = client.post(
            "/api/chart",
            content=json.dumps(_payload()).encode(),
            headers={"Content-Type": "text/plain"},
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "model_attributes_type"


def test_chunked_body_limit_catches_forged_small_content_length():
    from app.request_limits import ChartRequestBoundary

    downstream_called = False

    async def downstream(_scope, receive, send):
        nonlocal downstream_called
        downstream_called = True
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if not message.get("more_body", False):
                break
        await send(
            {"type": "http.response.start", "status": 204, "headers": []}
        )
        await send({"type": "http.response.body", "body": b""})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chart",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", b"2"),
        ],
    }
    chunks = iter(
        [
            {"type": "http.request", "body": b"x" * 10_000, "more_body": True},
            {"type": "http.request", "body": b"x" * 7_000, "more_body": False},
        ]
    )
    sent = []

    async def receive():
        return next(chunks)

    async def send(message):
        sent.append(message)

    asyncio.run(ChartRequestBoundary(downstream)(scope, receive, send))

    assert not downstream_called
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["detail"]["code"] == "request_body_too_large"


def test_streamed_body_limit_survives_the_assembled_fastapi_stack():
    """The framework must not translate the boundary's overflow into a 400."""
    from app.main import create_app
    from app.settings import AppProfile, AppSettings

    application = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/chart",
        "raw_path": b"/api/chart",
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
        "headers": [(b"content-type", b"application/json")],
        "state": {},
    }
    messages = iter([
        {"type": "http.request", "body": b"x" * 10_000, "more_body": True},
        {"type": "http.request", "body": b"x" * 7_000, "more_body": False},
    ])
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    asyncio.run(application(scope, receive, send))
    starts = [item for item in sent if item["type"] == "http.response.start"]
    bodies = [item for item in sent if item["type"] == "http.response.body"]
    assert [item["status"] for item in starts] == [413]
    assert json.loads(bodies[-1]["body"])["detail"]["code"] == "request_body_too_large"


def test_capacity_boundary_wraps_unsupported_api_method_boundary():
    """A cheap 405 still occupies and releases an application capacity slot."""
    from app.main import create_app
    from app.request_limits import ApiMethodBoundary, RequestCapacityBoundary
    from app.settings import AppProfile, AppSettings

    application = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    application.middleware_stack = application.build_middleware_stack()
    current = application.middleware_stack
    seen = []
    while hasattr(current, "app"):
        if isinstance(current, (RequestCapacityBoundary, ApiMethodBoundary)):
            seen.append(type(current))
        current = current.app
    assert seen == [RequestCapacityBoundary, ApiMethodBoundary]


def test_known_oversized_content_length_never_calls_downstream():
    from app.request_limits import ChartRequestBoundary

    downstream_called = False

    async def downstream(_scope, _receive, _send):
        nonlocal downstream_called
        downstream_called = True

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chart",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", b"16385"),
        ],
    }
    sent = []

    async def receive():
        raise AssertionError("known oversized body must be rejected before receive")

    async def send(message):
        sent.append(message)

    asyncio.run(ChartRequestBoundary(downstream)(scope, receive, send))

    assert not downstream_called
    assert sent[0]["status"] == 413


def test_duplicate_content_length_fails_closed():
    from app.request_limits import ChartRequestBoundary

    downstream_called = False

    async def downstream(_scope, _receive, _send):
        nonlocal downstream_called
        downstream_called = True

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chart",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", b"2"),
            (b"content-length", b"2"),
        ],
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(ChartRequestBoundary(downstream)(scope, receive, send))

    assert not downstream_called
    assert sent[0]["status"] == 400
    assert json.loads(sent[1]["body"])["detail"]["code"] == (
        "invalid_content_length"
    )


def test_request_boundary_preserves_client_disconnect():
    from app.request_limits import ChartRequestBoundary

    observed = []

    async def downstream(_scope, receive, _send):
        observed.append(await receive())

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chart",
        "headers": [(b"content-type", b"application/json")],
    }

    async def receive():
        return {"type": "http.disconnect"}

    async def send(_message):
        raise AssertionError("disconnect must not fabricate a response")

    asyncio.run(ChartRequestBoundary(downstream)(scope, receive, send))

    assert observed == [{"type": "http.disconnect"}]


def test_request_boundary_never_starts_second_response_after_late_oversize():
    from app.request_limits import ChartRequestBoundary

    sent = []

    async def downstream(_scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )
        await receive()

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/chart",
        "headers": [(b"content-type", b"application/json")],
    }

    async def receive():
        return {
            "type": "http.request",
            "body": b"x" * 17,
            "more_body": False,
        }

    async def send(message):
        sent.append(message)

    asyncio.run(
        ChartRequestBoundary(downstream, max_body_bytes=16)(
            scope,
            receive,
            send,
        )
    )

    response_starts = [
        message for message in sent if message["type"] == "http.response.start"
    ]
    assert len(response_starts) == 1
    assert response_starts[0]["status"] == 413


def test_hosted_chart_rejects_a_declared_empty_body_as_a_framing_fault():
    """An empty body is a framing fault, not twelve missing fields.

    Content-Length: 0 previously passed this boundary and failed inside
    Pydantic, so the caller got a 422 enumerating every required field.  That
    answer describes the schema instead of the actual fault, and it leaks the
    shape of the request model to a caller who sent nothing at all.  Body
    framing is this boundary's own contract, so it answers here.
    """
    with _hosted_client() as client:
        response = client.post(
            "/api/chart",
            content=b"",
            headers={"Content-Type": "application/json", "Content-Length": "0"},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "empty_request_body"


def test_compute_capacity_refusal_is_bounded_rather_than_an_open_ended_stall():
    """Overload must degrade into refusals, not into a wedged process.

    _COMPUTE_LOCK serialises every calculation, which is required for
    correctness: FastAPI runs sync endpoints on a threadpool and the Swiss
    ephemeris C library keeps its topocentre and ayanamsa as process-global
    state, so concurrent threads would quietly contaminate each other's
    results.  The lock is right.  The unbounded wait in front of it was not:
    a few expensive requests could hold every other caller indefinitely.
    """
    import threading

    from app import main

    assert main._COMPUTE_LOCK_WAIT_TIMEOUT_SECONDS > 0

    # Hold the lock from another thread, exactly as a long calculation would.
    main._COMPUTE_LOCK.acquire()
    try:
        with pytest.raises(main.ComputeCapacityExhaustedError):
            # Shorten the budget so the test does not actually wait 20 seconds;
            # the behaviour under test is "refuses after the budget", not the
            # specific number.
            original = main._COMPUTE_LOCK_WAIT_TIMEOUT_SECONDS
            main._COMPUTE_LOCK_WAIT_TIMEOUT_SECONDS = 0.05
            try:
                main.compute_chart(object())
            finally:
                main._COMPUTE_LOCK_WAIT_TIMEOUT_SECONDS = original
    finally:
        main._COMPUTE_LOCK.release()

    # The lock is released again for the next caller: a refusal must not leave
    # the lock held, which would turn a transient overload into a permanent one.
    assert main._COMPUTE_LOCK.acquire(timeout=1.0)
    main._COMPUTE_LOCK.release()
    assert threading.active_count() >= 1


# --------------------------------------------------------------------------
# FPI-2026-08-06-E-014
# --------------------------------------------------------------------------

def _compose_pids_limit() -> int:
    from pathlib import Path
    import re

    text = (
        Path(__file__).resolve().parents[2] / "deploy" / "compose.yaml"
    ).read_text(encoding="utf-8")
    match = re.search(r"^\s*pids_limit:\s*(\d+)\s*$", text, re.MULTILINE)
    assert match, "compose.yaml no longer declares pids_limit"
    return int(match.group(1))


def _dockerfile_worker_count() -> int:
    from pathlib import Path
    import re

    text = (
        Path(__file__).resolve().parents[2] / "deploy" / "Dockerfile"
    ).read_text(encoding="utf-8")
    match = re.search(r'"--workers",\s*"(\d+)"', text)
    assert match, "Dockerfile no longer pins a worker count"
    return int(match.group(1))


def test_e014_pids_limit_covers_the_capacity_this_service_declares():
    """The pids cgroup counts threads as tasks, and every endpoint here is a
    sync `def` served from the AnyIO thread pool.

    Measured with production flags, each worker reaches AnyIO's default
    40-thread ceiling rather than the application's own 32 + 4 boundary,
    because threads are reused and released lazily. The limit has to cover
    that, plus one main thread per worker, plus the supervisor, the
    multiprocessing resource tracker, the `init: true` process and the
    healthcheck subprocess.
    """

    import anyio
    import anyio.to_thread

    async def _pool_size() -> int:
        return int(
            anyio.to_thread.current_default_thread_limiter().total_tokens
        )

    per_worker_threads = anyio.run(_pool_size)
    workers = _dockerfile_worker_count()
    # main thread per worker, supervisor, resource tracker, init, healthcheck
    overhead = workers + 4
    required = workers * per_worker_threads + overhead

    assert _compose_pids_limit() >= required, (
        f"pids_limit {_compose_pids_limit()} is below the {required} tasks "
        f"{workers} workers can reach at {per_worker_threads} pool threads "
        "each; either raise the limit or lower the declared capacity"
    )


def test_e014_the_declared_request_capacity_still_fits_under_the_limit():
    """Adjacent control: raising the limit must not be allowed to hide a
    capacity increase that outgrows it again."""

    from app.request_limits import (
        MAX_HOSTED_COMPUTE_REQUESTS_PER_WORKER,
        MAX_HOSTED_REQUESTS_PER_WORKER,
    )

    workers = _dockerfile_worker_count()
    declared = MAX_HOSTED_REQUESTS_PER_WORKER + MAX_HOSTED_COMPUTE_REQUESTS_PER_WORKER
    assert workers * (declared + 1) + workers + 4 <= _compose_pids_limit()
