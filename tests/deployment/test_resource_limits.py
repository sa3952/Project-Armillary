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

    assert downstream_called
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["detail"]["code"] == "request_body_too_large"


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

    with pytest.raises(Exception) as raised:
        asyncio.run(
            ChartRequestBoundary(downstream, max_body_bytes=16)(
                scope,
                receive,
                send,
            )
        )
    assert raised.type.__name__ == "_RequestBodyTooLarge"

    assert [
        message for message in sent if message["type"] == "http.response.start"
    ] == [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [],
        }
    ]
