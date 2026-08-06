from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend"


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


def test_profile_parser_accepts_only_closed_vocabulary():
    from app.settings import AppProfile, load_settings

    assert load_settings({}).profile is AppProfile.LOCAL
    assert (
        load_settings({"CLASSICAL_ASTROLOGY_PROFILE": "private_alpha"}).profile
        is AppProfile.PRIVATE_ALPHA
    )
    with pytest.raises(RuntimeError, match="unsupported application profile"):
        load_settings({"CLASSICAL_ASTROLOGY_PROFILE": "production"})


def test_build_revision_is_release_controlled_and_fail_closed():
    from app.settings import load_settings

    revision = "b" * 40
    settings = load_settings(
        {
            "CLASSICAL_ASTROLOGY_PROFILE": "private_alpha",
            "CLASSICAL_ASTROLOGY_SOURCE_REVISION": revision,
        }
    )
    assert settings.source_revision == revision
    assert settings.revision_source == (
        "build_environment:CLASSICAL_ASTROLOGY_SOURCE_REVISION"
    )

    local = load_settings({})
    assert local.source_revision is None
    assert local.revision_source is None

    uncommitted = load_settings(
        {"CLASSICAL_ASTROLOGY_SOURCE_REVISION": "uncommitted"}
    )
    assert uncommitted.source_revision is None
    assert uncommitted.revision_source is None

    with pytest.raises(RuntimeError, match="source revision"):
        load_settings(
            {"CLASSICAL_ASTROLOGY_SOURCE_REVISION": "caller-controlled"}
        )


def test_unknown_profile_fails_closed_during_process_import():
    environment = {**os.environ, "CLASSICAL_ASTROLOGY_PROFILE": "production"}
    completed = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=BACKEND_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode != 0
    assert "unsupported application profile" in completed.stderr


def test_local_profile_preserves_runtime_health_and_live_openapi():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    local_app = create_app(AppSettings(profile=AppProfile.LOCAL))
    with TestClient(local_app) as client:
        health = client.get("/api/health")
        runtime = client.get(
            "/api/runtime-health",
            headers={"X-Local-Runtime-Nonce": "local-profile-test-nonce"},
        )
        schema = client.get("/openapi.json")

    assert health.status_code == 200
    assert health.json()["runtime_contract"] == "local-runtime-v2"
    assert runtime.status_code == 503
    assert runtime.json()["detail"]["code"] == "runtime_auth_unavailable"
    assert schema.status_code == 200
    assert "/api/runtime-health" in schema.json()["paths"]


def test_local_openapi_documents_runtime_failure_and_parser_boundary():
    from app.main import create_app
    from app.settings import AppProfile, AppSettings

    local_app = create_app(AppSettings(profile=AppProfile.LOCAL))
    with TestClient(local_app) as client:
        schema = client.get("/openapi.json").json()

    assert "400" in schema["paths"]["/api/chart"]["post"]["responses"]
    assert "503" in schema["paths"]["/api/chart"]["post"]["responses"]
    assert "400" in schema["paths"]["/api/places/search"]["post"]["responses"]
    assert "503" in schema["paths"]["/api/places/search"]["post"]["responses"]
    runtime_failure = schema["paths"]["/api/runtime-health"]["get"][
        "responses"
    ]["503"]["content"]["application/json"]["schema"]
    assert runtime_failure == {
        "$ref": "#/components/schemas/HostedBoundaryErrorResponse"
    }
    location_conditions = schema["components"]["schemas"]["LocationInput"][
        "allOf"
    ]
    assert any(
        condition.get("if", {}).get("properties", {})
        .get("location_source", {})
        .get("const")
        == "geonames_cities500"
        for condition in location_conditions
    )
    assert any(
        condition.get("if", {}).get("not") == {"required": ["location_source"]}
        for condition in location_conditions
    )


def test_api_method_rejections_keep_allow_header_before_static_mount():
    from app.main import create_app
    from app.settings import AppProfile, AppSettings

    local_app = create_app(AppSettings(profile=AppProfile.LOCAL))
    with TestClient(local_app) as client:
        health = client.request("TRACE", "/api/health")
        chart = client.request("TRACE", "/api/chart")
        search = client.request("TRACE", "/api/places/search")
        runtime = client.request("TRACE", "/api/runtime-health")

    assert (health.status_code, health.headers.get("allow")) == (405, "GET")
    assert (chart.status_code, chart.headers.get("allow")) == (405, "POST")
    assert (search.status_code, search.headers.get("allow")) == (405, "POST")
    assert (runtime.status_code, runtime.headers.get("allow")) == (405, "GET")


def test_private_alpha_exposes_only_hosted_health_and_no_live_schema():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    with TestClient(hosted_app) as client:
        health = client.get("/api/health")
        runtime = client.get("/api/runtime-health")
        schema = client.get("/openapi.json")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "ready": True}
    assert runtime.status_code == 404
    assert schema.status_code == 404


def test_client_configuration_exposes_only_the_closed_profile_name():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    local_app = create_app(AppSettings(profile=AppProfile.LOCAL))
    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))

    with TestClient(local_app) as client:
        local_config = client.get("/api/client-config")
        local_schema = client.get("/openapi.json").json()
    with TestClient(hosted_app) as client:
        hosted_config = client.get("/api/client-config")

    assert local_config.status_code == 200
    assert local_config.json() == {"profile": "local"}
    assert hosted_config.status_code == 200
    assert hosted_config.json() == {"profile": "private_alpha"}
    assert "/api/client-config" not in local_schema["paths"]


def test_private_alpha_applies_noindex_headers_without_changing_local_profile():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    local_app = create_app(AppSettings(profile=AppProfile.LOCAL))
    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))

    with TestClient(local_app) as client:
        local_index = client.get("/")
    with TestClient(hosted_app) as client:
        hosted_index = client.get("/")
        hosted_missing = client.get("/not-a-real-route")

    assert "x-robots-tag" not in local_index.headers
    assert (
        hosted_index.headers["x-robots-tag"]
        == "noindex, nofollow, noarchive"
    )
    assert (
        hosted_missing.headers["x-robots-tag"]
        == "noindex, nofollow, noarchive"
    )


def test_frontend_uses_explicit_zh_tw_urls_and_does_not_keep_legacy_aliases():
    """The first public URL shape must not silently drift when English is added."""
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    application = create_app(AppSettings(profile=AppProfile.LOCAL))
    with TestClient(application, follow_redirects=False) as client:
        root = client.get("/")
        localized = {
            route: client.get(route)
            for route in (
                "/zh-TW/",
                "/zh-TW/calculate",
                "/zh-TW/features",
                "/zh-TW/validation",
                "/zh-TW/trust",
                "/zh-TW/security",
                "/zh-TW/roadmap",
                "/zh-TW/blog",
                "/zh-TW/about",
                "/zh-TW/contact",
                "/zh-TW/legal/privacy",
                "/zh-TW/legal/terms",
                "/zh-TW/legal/copyright",
            )
        }
        legacy = {
            route: client.get(route)
            for route in ("/calculate", "/trust", "/legal/privacy")
        }

    assert root.status_code == 308
    assert root.headers["location"] == "/zh-TW/"
    assert all(response.status_code == 200 for response in localized.values())
    assert all('lang="zh-TW"' in response.text for response in localized.values())
    assert all(response.status_code == 404 for response in legacy.values())


def test_private_alpha_static_openapi_is_generated_offline_not_served():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    schema = hosted_app.openapi()

    assert "/api/chart" in schema["paths"]
    assert "/api/health" in schema["paths"]
    assert "/api/runtime-health" not in schema["paths"]
    assert json.dumps(schema, sort_keys=True)


def test_private_alpha_chart_emits_status_only_allowlisted_event():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    events: list[dict] = []
    hosted_app = create_app(
        AppSettings(profile=AppProfile.PRIVATE_ALPHA),
        event_emitter=lambda event: events.append(event) or True,
    )
    with TestClient(hosted_app) as client:
        response = client.post("/api/chart", json=_payload())
        health = client.get("/api/health")

    assert response.status_code == 200
    assert health.status_code == 200
    assert [event["route"] for event in events] == [
        "/api/chart",
        "/api/health",
    ]
    chart_event = events[0]
    assert chart_event["status_code"] == 200
    assert chart_event["outcome"] == "success"
    encoded = json.dumps(chart_event, ensure_ascii=False)
    assert str(_payload()["location"]["latitude"]) not in encoded
    assert str(_payload()["location"]["longitude"]) not in encoded
    assert "1997" not in encoded


def test_private_alpha_chart_subtree_uses_closed_route_labels():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    events: list[dict] = []
    hosted_app = create_app(
        AppSettings(profile=AppProfile.PRIVATE_ALPHA),
        event_emitter=lambda event: events.append(event) or True,
    )
    with TestClient(hosted_app, follow_redirects=False) as client:
        trailing_slash = client.post("/api/chart/", json=_payload())
        future_child = client.post("/api/chart/future", json=_payload())
        lookalike = client.get("/api/chartography")

    assert trailing_slash.status_code == 405
    assert future_child.status_code == 405
    assert lookalike.status_code == 404
    assert [event["route"] for event in events] == [
        "frontend_or_unmatched",
        "frontend_or_unmatched",
        "frontend_or_unmatched",
    ]


def test_private_alpha_does_not_grant_cross_origin_access():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    with TestClient(hosted_app) as client:
        response = client.options(
            "/api/chart",
            headers={
                "Origin": "https://untrusted.invalid",
                "Access-Control-Request-Method": "POST",
            },
        )

    assert "access-control-allow-origin" not in response.headers


def test_private_alpha_validation_errors_do_not_echo_sensitive_input():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    payload = _payload()
    payload["datetime"]["month"] = 13
    payload["location"]["unexpected_CANARY_1997_24_1477"] = (
        "PRIVATE_ALPHA_SENSITIVE_CANARY"
    )

    with TestClient(hosted_app) as client:
        response = client.post("/api/chart", json=payload)

    assert response.status_code == 422
    body = response.json()
    rendered = json.dumps(body, ensure_ascii=False)
    assert "PRIVATE_ALPHA_SENSITIVE_CANARY" not in rendered
    assert "unexpected_CANARY_1997_24_1477" not in rendered
    assert all(
        set(issue) <= {"type", "loc"}
        for issue in body["detail"]
    )
    assert all(
        {"input", "msg", "ctx"}.isdisjoint(issue)
        for issue in body["detail"]
    )


def test_numeric_fields_reject_boolean_and_string_coercion():
    from app.main import create_app
    from app.settings import AppProfile, AppSettings

    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    boolean_payload = _payload()
    boolean_payload["timezone"] = {
        "mode": "fixed_offset",
        "utc_offset_hours": False,
    }
    fold_payload = _payload()
    fold_payload["timezone"]["fold"] = False
    string_payload = _payload()
    string_payload["location"]["longitude"] = "120.0"

    with TestClient(hosted_app) as client:
        boolean_response = client.post("/api/chart", json=boolean_payload)
        fold_response = client.post("/api/chart", json=fold_payload)
        string_response = client.post("/api/chart", json=string_payload)

    assert boolean_response.status_code == 422
    assert fold_response.status_code == 422
    assert string_response.status_code == 422


def test_local_validation_errors_use_the_same_non_echoing_contract():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    local_app = create_app(AppSettings(profile=AppProfile.LOCAL))
    payload = _payload()
    payload["datetime"]["month"] = 13

    with TestClient(local_app) as client:
        response = client.post("/api/chart", json=payload)

    assert response.status_code == 422
    assert all("input" not in issue for issue in response.json()["detail"])
    assert all(
        set(issue) == {"type", "loc"}
        for issue in response.json()["detail"]
    )


def test_private_alpha_known_errors_return_only_closed_code(monkeypatch):
    from app import main
    from app.ephemeris import FullEphemerisRequiredError
    from app.settings import AppSettings, AppProfile

    def fail(_request):
        raise FullEphemerisRequiredError(
            operation="CANARY_OPERATION",
            jd_ut=2597667.2296284684,
            retflag=4,
        )

    monkeypatch.setattr(main, "_compute_chart_locked", fail)
    hosted_app = main.create_app(
        AppSettings(profile=AppProfile.PRIVATE_ALPHA)
    )
    with TestClient(hosted_app) as client:
        response = client.post("/api/chart", json=_payload())

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "full_ephemeris_required"}
    }
    assert "2597667" not in response.text
    assert "CANARY_OPERATION" not in response.text


def test_hosted_boundary_rejects_conflicting_content_length_and_transfer_encoding():
    """RFC 9112 forbids framing a body with both Content-Length and
    Transfer-Encoding.  h11 accepts the combination and prefers Content-Length,
    so before this bound such a request reached Pydantic and returned 422; the
    only defence was reverse-proxy normalization, a layer this boundary does not
    own."""
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    with TestClient(hosted_app) as client:
        response = client.post(
            "/api/chart",
            content=json.dumps(_payload()).encode(),
            headers={
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["code"] == "conflicting_request_framing"

    # Positive control: the same body without the conflicting header succeeds,
    # so the rejection is caused by the framing and not by the payload.
    with TestClient(hosted_app) as client:
        ok = client.post("/api/chart", json=_payload())
    assert ok.status_code == 200, ok.text


def test_hosted_boundary_rejects_oversized_headers_before_chart_parsing():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    with TestClient(hosted_app) as client:
        response = client.post(
            "/api/chart",
            json=_payload(),
            headers={"X-Oversized-Probe": "a" * (20 * 1024)},
        )

    assert response.status_code == 431, response.text
    assert response.json()["detail"]["code"] == "request_headers_too_large"
