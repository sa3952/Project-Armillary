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


def _scope(*headers: tuple[bytes, bytes]) -> dict:
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/chart",
        "raw_path": b"/api/chart",
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1234),
        "headers": list(headers),
        "state": {},
    }


@pytest.mark.parametrize(
    "content_type",
    (
        None,
        "text/plain",
        "application/json; charset=utf-8; charset=utf-8",
        "application/json; charset=latin-1",
    ),
)
def test_hosted_chart_rejects_unsupported_media_types(content_type):
    headers = {} if content_type is None else {"Content-Type": content_type}
    with _hosted_client() as client:
        response = client.post("/api/chart", content=b"{}", headers=headers)
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "unsupported_media_type"


def test_hosted_chart_accepts_json_with_utf8_charset():
    body = json.dumps(_payload()).encode()
    with _hosted_client() as client:
        response = client.post(
            "/api/chart",
            content=body,
            headers={"Content-Type": "application/json; charset=UTF-8"},
        )

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("path", "body"),
    (
        ("/api/chart", b"{" + b" " * (16 * 1024) + b"}"),
        ("/api/places/search", b'{"query":"' + b"x" * (16 * 1024) + b'"}'),
    ),
)
def test_hosted_endpoints_reject_known_oversize_before_schema_parse(path, body):
    with _hosted_client() as client:
        response = client.post(
            path,
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "request_body_too_large"


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

    scope = _scope(
        (b"content-type", b"application/json"),
        (b"content-length", b"2"),
    )
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
    from app.main import create_app
    from app.settings import AppProfile, AppSettings

    application = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    scope = _scope((b"content-type", b"application/json"))
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


@pytest.mark.parametrize("profile", ("private_alpha", "public"))
def test_capacity_boundary_wraps_unsupported_api_method_boundary(profile):
    from app.main import create_app
    from app.request_limits import (
        ApiMethodBoundary,
        ChartRequestBoundary,
        RequestCapacityBoundary,
    )
    from app.settings import AppProfile, AppSettings

    application = create_app(AppSettings(profile=AppProfile(profile)))
    application.middleware_stack = application.build_middleware_stack()
    current = application.middleware_stack
    seen = []
    while hasattr(current, "app"):
        if isinstance(
            current,
            (ChartRequestBoundary, RequestCapacityBoundary, ApiMethodBoundary),
        ):
            seen.append(type(current))
        current = current.app
    # Outermost first.  The body buffer used to sit outside the counter, so a
    # connection trickling a request body was already inside the application
    # before anything counted it; only the edge's own buffering stopped that
    # reaching production, and that is somebody else's configuration.
    assert seen == [RequestCapacityBoundary, ChartRequestBoundary, ApiMethodBoundary]


def test_incomplete_body_does_not_reserve_compute_capacity():
    from app.request_limits import ChartRequestBoundary, RequestCapacityBoundary

    capacity = RequestCapacityBoundary(None, max_compute_concurrent=1)
    first_seen = asyncio.Event()
    finish_body = asyncio.Event()

    async def downstream(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    capacity.app = downstream
    application = ChartRequestBoundary(capacity)
    messages = iter((
        {"type": "http.request", "body": b"{", "more_body": True},
        {"type": "http.request", "body": b"}", "more_body": False},
    ))

    async def receive():
        message = next(messages)
        if message.get("more_body"):
            first_seen.set()
            await finish_body.wait()
        return message

    async def send(_message):
        pass

    async def exercise():
        task = asyncio.create_task(application(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/chart",
                "headers": [(b"content-type", b"application/json")],
            },
            receive,
            send,
        ))
        await first_seen.wait()
        assert capacity._active_compute == 0
        finish_body.set()
        await task
        assert capacity._active_compute == 0

    asyncio.run(exercise())


def test_capacity_boundary_refuses_a_second_active_compute_request():
    from app.request_limits import RequestCapacityBoundary

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def downstream(_scope, _receive, send):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    boundary = RequestCapacityBoundary(
        downstream,
        max_concurrent=2,
        max_compute_concurrent=1,
    )
    scope = {"type": "http", "method": "POST", "path": "/api/chart"}

    async def one():
        sent = []

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await boundary(scope.copy(), receive, send)
        return sent

    async def exercise():
        first = asyncio.create_task(one())
        await entered.wait()
        second = await one()
        release.set()
        await first
        return second

    second = asyncio.run(exercise())
    start = next(item for item in second if item["type"] == "http.response.start")
    body = next(item for item in second if item["type"] == "http.response.body")
    assert start["status"] == 503
    assert json.loads(body["body"])["detail"]["code"] == (
        "compute_capacity_exhausted"
    )


def test_known_oversized_content_length_never_calls_downstream():
    from app.request_limits import ChartRequestBoundary

    downstream_called = False

    async def downstream(_scope, _receive, _send):
        nonlocal downstream_called
        downstream_called = True

    scope = _scope(
        (b"content-type", b"application/json"),
        (b"content-length", b"16385"),
    )
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

    scope = _scope(
        (b"content-type", b"application/json"),
        (b"content-length", b"2"),
        (b"content-length", b"2"),
    )
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

    scope = _scope((b"content-type", b"application/json"))

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

    scope = _scope((b"content-type", b"application/json"))

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
    with _hosted_client() as client:
        response = client.post(
            "/api/chart",
            content=b"",
            headers={"Content-Type": "application/json", "Content-Length": "0"},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "empty_request_body"


def test_compute_capacity_refusal_is_bounded_rather_than_an_open_ended_stall():
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
# Container process budget
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

    from app.request_limits import (
        MAX_HOSTED_COMPUTE_REQUESTS_PER_WORKER,
        MAX_HOSTED_REQUESTS_PER_WORKER,
    )

    workers = _dockerfile_worker_count()
    declared = MAX_HOSTED_REQUESTS_PER_WORKER + MAX_HOSTED_COMPUTE_REQUESTS_PER_WORKER
    assert workers * (declared + 1) + workers + 4 <= _compose_pids_limit()


# Three declarations decide whether a POST endpoint is bounded: the routes the
# application registers, the route table `ApiMethodBoundary` is constructed
# with, and `_BOUNDED_JSON_PATHS`, which selects both the JSON body ceiling and
# the compute pool.  They are separate literals, so the invariant that they name
# the same set is asserted here rather than assumed.  A POST route present in
# one and absent from another is a defect regardless of what any other layer
# happens to do with it.
def _hosted_application():
    from app.main import create_app
    from app.settings import AppProfile, AppSettings

    return create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))


def _declared_route_methods(application) -> dict[str, frozenset[str]]:
    for middleware in application.user_middleware:
        if middleware.cls.__name__ == "ApiMethodBoundary":
            return dict(middleware.kwargs["route_methods"])
    raise AssertionError("ApiMethodBoundary is not installed")


def _registered_api_methods(application) -> dict[str, frozenset[str]]:
    registered: dict[str, frozenset[str]] = {}
    for route in application.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not isinstance(path, str) or not path.startswith("/api/") or not methods:
            continue
        registered[path] = frozenset(methods) - {"HEAD"}
    return registered


def test_every_post_api_route_is_declared_to_both_request_boundaries():
    from app.request_limits import _BOUNDED_JSON_PATHS
    from scripts.tools.closed_set import require_closed_set

    application = _hosted_application()
    registered = _registered_api_methods(application)
    assert registered, "no /api/ routes were discovered"

    # The method boundary must know every API route, or it silently declines to
    # answer for one.
    require_closed_set(
        registered.keys(),
        _declared_route_methods(application).keys(),
        role="ApiMethodBoundary route universe",
    )
    assert _declared_route_methods(application) == registered, (
        "ApiMethodBoundary methods differ from the registered routes: "
        f"{_declared_route_methods(application)} != {registered}"
    )

    # Every POST endpoint must be body-bounded and compute-classified.
    posting = {path for path, methods in registered.items() if "POST" in methods}
    require_closed_set(
        posting,
        _BOUNDED_JSON_PATHS,
        role="bounded JSON POST universe",
    )


def test_both_refusals_that_mean_too_busy_say_when_to_come_back():
    """Both 503 capacity paths expose the same retry interval."""

    from app.request_limits import RETRY_AFTER_SECONDS, _error_response
    from app.main import ComputeCapacityExhaustedError

    admission = _error_response(
        503, "request_capacity_exhausted", "busy",
        retry_after_seconds=RETRY_AFTER_SECONDS,
    )
    assert admission.headers["retry-after"] == str(RETRY_AFTER_SECONDS)
    assert ComputeCapacityExhaustedError.retry_after_seconds == RETRY_AFTER_SECONDS


def test_a_request_for_an_undeclared_host_is_refused_before_any_budget():
    """Expected-host checking catches exposure mistakes; it is not authorization."""

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.settings import AppProfile, AppSettings
    from tests.backend import chart_payload

    client = TestClient(create_app(settings=AppSettings(
        profile=AppProfile.PRIVATE_ALPHA, expected_host="alpha.example.invalid",
    )))
    payload = chart_payload(options={"house_system": "W"})
    for host, expected in (
        ("alpha.example.invalid", 200),
        ("alpha.example.invalid:443", 200),
        ("172.31.240.2", 404),
        ("scanner.example", 404),
    ):
        observed = client.post("/api/chart", json=payload, headers={"host": host})
        assert observed.status_code == expected, host

    # Undeclared means unchecked: the same module runs in development.
    open_client = TestClient(create_app(settings=AppSettings(
        profile=AppProfile.PRIVATE_ALPHA,
    )))
    assert open_client.post(
        "/api/chart", json=payload, headers={"host": "anything.example"}
    ).status_code == 200


def test_interactive_lookup_cannot_exhaust_the_calculation_budget():
    """Interactive place lookup and chart computation use different budgets."""

    import asyncio
    import json as json_module

    from app.main import app
    from app.request_limits import _BOUNDED_JSON_PATHS, _COMPUTE_PATHS
    from tests.backend import chart_payload

    # The two questions have two answers.
    assert "/api/places/search" in _BOUNDED_JSON_PATHS
    assert "/api/places/search" not in _COMPUTE_PATHS
    assert _COMPUTE_PATHS <= _BOUNDED_JSON_PATHS

    async def call(path, payload):
        body = json_module.dumps(payload).encode()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "POST", "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [
                (b"host", b"t"), (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 1), "server": ("t", 80),
        }
        sent = []
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        async def send(message):
            sent.append(message)
        await app(scope, receive, send)
        return next(
            message["status"] for message in sent
            if message["type"] == "http.response.start"
        )

    async def interleaved():
        chart = chart_payload(options={"house_system": "W"})
        tasks = []
        for _ in range(4):
            tasks.append(call("/api/chart", chart))
            tasks.append(call("/api/places/search", {"query": "la"}))
        return await asyncio.gather(*tasks)

    statuses = asyncio.run(interleaved())
    charts = statuses[0::2]
    assert charts == [200, 200, 200, 200], (
        f"a concurrent lookup refused a chart: {statuses}"
    )
