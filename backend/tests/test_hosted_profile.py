from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]


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


def test_private_alpha_static_openapi_is_generated_offline_not_served():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    schema = hosted_app.openapi()

    assert "/api/chart" in schema["paths"]
    assert "/api/health" in schema["paths"]
    assert "/api/runtime-health" not in schema["paths"]
    assert json.dumps(schema, sort_keys=True)


def test_private_alpha_chart_works_without_emitting_per_request_event():
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
    assert all(event["route"] != "/api/chart" for event in events)
    assert [event["route"] for event in events] == ["/api/health"]


def test_private_alpha_suppresses_chart_subtree_but_not_lookalike_paths():
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
    assert [event["route"] for event in events] == ["frontend_or_unmatched"]


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


def test_local_validation_errors_keep_existing_diagnostic_contract():
    from app.main import create_app
    from app.settings import AppSettings, AppProfile

    local_app = create_app(AppSettings(profile=AppProfile.LOCAL))
    payload = _payload()
    payload["datetime"]["month"] = 13

    with TestClient(local_app) as client:
        response = client.post("/api/chart", json=payload)

    assert response.status_code == 422
    assert any("input" in issue for issue in response.json()["detail"])


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
