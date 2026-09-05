from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest
from app.frontend_assets import discover_source_assets
from app.main import create_app
from app.runtime_static import RuntimeStaticFiles
from app.settings import AppProfile, AppSettings, load_settings
from tests.backend import minimal_chart_payload


BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend"


def _payload() -> dict:
    return minimal_chart_payload(
        year=1997, month=8, day=17, hour=9, minute=42,
        latitude=24.1477, longitude=120.6736, altitude_m=80,
    )


def test_profile_parser_accepts_only_closed_vocabulary():
    assert load_settings({}).profile is AppProfile.PRIVATE_ALPHA
    assert (
        load_settings({"CLASSICAL_ASTROLOGY_PROFILE": "private_alpha"}).profile
        is AppProfile.PRIVATE_ALPHA
    )
    assert (
        load_settings({"CLASSICAL_ASTROLOGY_PROFILE": "public"}).profile
        is AppProfile.PUBLIC
    )
    with pytest.raises(RuntimeError, match="unsupported application profile"):
        load_settings({"CLASSICAL_ASTROLOGY_PROFILE": "production"})


def test_build_revision_is_release_controlled_and_fail_closed():
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

    default = load_settings({})
    assert default.source_revision is None
    assert default.revision_source is None

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


def test_offline_openapi_documents_parser_boundary_without_live_schema():
    application = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    schema = application.openapi()

    assert "400" in schema["paths"]["/api/chart"]["post"]["responses"]
    assert "503" in schema["paths"]["/api/chart"]["post"]["responses"]
    assert "400" in schema["paths"]["/api/places/search"]["post"]["responses"]
    assert "503" in schema["paths"]["/api/places/search"]["post"]["responses"]
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
    application = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    with TestClient(application) as client:
        health = client.request("TRACE", "/api/health")
        chart = client.request("TRACE", "/api/chart")
        search = client.request("TRACE", "/api/places/search")

    assert (health.status_code, health.headers.get("allow")) == (405, "GET")
    assert (chart.status_code, chart.headers.get("allow")) == (405, "POST")
    assert (search.status_code, search.headers.get("allow")) == (405, "POST")


def test_private_alpha_exposes_only_hosted_health_and_no_live_schema():
    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    with TestClient(hosted_app) as client:
        health = client.get("/api/health")
        runtime = client.get("/api/runtime-health")
        schema = client.get("/openapi.json")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "ready": True,
        "readiness_scope": "process_liveness_only",
    }
    assert runtime.status_code == 404
    assert schema.status_code == 404


def test_public_profile_keeps_hosted_safety_without_private_indexing_headers():
    public_app = create_app(AppSettings(profile=AppProfile.PUBLIC))
    payload = _payload()
    payload["datetime"]["month"] = 13
    with TestClient(public_app) as client:
        health = client.get("/api/health")
        runtime = client.get("/api/runtime-health")
        schema = client.get("/openapi.json")
        unsupported = client.post(
            "/api/chart",
            content=b"{}",
            headers={"Content-Type": "text/plain"},
        )
        invalid = client.post("/api/chart", json=payload)
        index = client.get("/")

    assert health.json() == {
        "status": "ok",
        "ready": True,
        "readiness_scope": "process_liveness_only",
    }
    assert runtime.status_code == 404
    assert schema.status_code == 404
    assert unsupported.status_code == 415
    assert invalid.status_code == 422
    assert all(set(issue) == {"type", "loc"} for issue in invalid.json()["detail"])
    assert "x-robots-tag" not in index.headers


def test_client_configuration_exposes_only_the_closed_profile_name():
    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    public_app = create_app(AppSettings(profile=AppProfile.PUBLIC))

    with TestClient(hosted_app) as client:
        hosted_config = client.get("/api/client-config")
    with TestClient(public_app) as client:
        public_config = client.get("/api/client-config")

    assert hosted_config.status_code == 200
    assert hosted_config.json() == {"profile": "private_alpha"}
    assert public_config.status_code == 200
    assert public_config.json() == {"profile": "public"}
    assert "/api/client-config" not in hosted_app.openapi()["paths"]


def test_private_alpha_applies_noindex_while_public_remains_indexable():
    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    public_app = create_app(AppSettings(profile=AppProfile.PUBLIC))

    with TestClient(hosted_app) as client:
        hosted_index = client.get("/")
        hosted_missing = client.get("/not-a-real-route")
    with TestClient(public_app) as client:
        public_index = client.get("/")

    assert (
        hosted_index.headers["x-robots-tag"]
        == "noindex, nofollow, noarchive"
    )
    assert (
        hosted_missing.headers["x-robots-tag"]
        == "noindex, nofollow, noarchive"
    )
    assert "x-robots-tag" not in public_index.headers


def test_static_aliases_redirect_to_one_canonical_url_shape():
    application = create_app(AppSettings(profile=AppProfile.PUBLIC))
    with TestClient(application, follow_redirects=False) as client:
        observations = {
            path: client.get(path)
            for path in (
                "/zh-TW/",
                "/zh-TW/index.html",
                "/zh-TW/features",
                "/zh-TW/features.html",
                "/zh-TW/features/",
            )
        }

    assert observations["/zh-TW/"].status_code == 200
    assert observations["/zh-TW/features"].status_code == 200
    assert observations["/zh-TW/index.html"].headers["location"] == "/zh-TW/"
    assert observations["/zh-TW/features.html"].headers["location"] == (
        "/zh-TW/features"
    )
    assert observations["/zh-TW/features/"].headers["location"] == (
        "/zh-TW/features"
    )
    assert all(
        response.status_code == 308
        for path, response in observations.items()
        if path not in {"/zh-TW/", "/zh-TW/features"}
    )

    runtime = RuntimeStaticFiles(
        directory=BACKEND_ROOT.parent / "frontend",
        html=True,
        allowed_assets=discover_source_assets(BACKEND_ROOT.parent / "frontend"),
    )
    assert runtime._canonical_redirect({
        "method": "GET",
        "path": "//zh-TW//features",
        "query_string": b"",
    }) == "/zh-TW/features"


def test_public_discovery_assets_are_exact_runtime_files_not_source_metadata():
    application = create_app(AppSettings(profile=AppProfile.PUBLIC))
    with TestClient(application) as client:
        robots = client.get("/robots.txt")
        sitemap = client.get("/sitemap.xml")
        indexnow = client.get("/bbb6ac30c1952dbb592262ea2656d708.txt")
        source_manifest = client.get("/surfaces.json")

    assert robots.status_code == 200
    assert robots.headers["content-type"].startswith("text/plain")
    assert robots.text.endswith(
        "Sitemap: https://projectarmillary.com/sitemap.xml\n"
    )
    assert sitemap.status_code == 200
    assert sitemap.headers["content-type"].split(";", 1)[0] in {
        "application/xml",
        "text/xml",
    }
    assert "https://projectarmillary.com/zh-TW/features" in sitemap.text
    assert "features.html" not in sitemap.text
    assert indexnow.text == "bbb6ac30c1952dbb592262ea2656d708\n"
    assert source_manifest.status_code == 404


def test_frontend_uses_explicit_zh_tw_urls_and_does_not_keep_legacy_aliases():
    """The first public URL shape must not silently drift when English is added."""
    application = create_app(AppSettings(profile=AppProfile.PUBLIC))
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
    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    schema = hosted_app.openapi()

    assert "/api/chart" in schema["paths"]
    assert "/api/health" in schema["paths"]
    assert "/api/runtime-health" not in schema["paths"]
    assert json.dumps(schema, sort_keys=True)


def test_private_alpha_chart_emits_status_only_allowlisted_event():
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
    # `request_id` is a `uuid4().hex`, independent of the request, so scanning
    # it for a birth year makes this canary's verdict depend on a coincidence:
    # the export rehearsal drew an id containing "1997" and the canary went red
    # on a run that leaked nothing.  The id is asserted against its declared
    # domain instead, and the scan covers every field that carries meaning.
    assert re.fullmatch(r"[0-9a-f]{32}", chart_event["request_id"])
    meaningful = {
        name: value
        for name, value in chart_event.items()
        if name != "request_id"
    }
    encoded = json.dumps(meaningful, ensure_ascii=False)
    assert str(_payload()["location"]["latitude"]) not in encoded
    assert str(_payload()["location"]["longitude"]) not in encoded
    assert "1997" not in encoded


def test_private_alpha_chart_subtree_uses_closed_route_labels():
    events: list[dict] = []
    hosted_app = create_app(
        AppSettings(profile=AppProfile.PRIVATE_ALPHA),
        event_emitter=lambda event: events.append(event) or True,
    )
    with TestClient(hosted_app, follow_redirects=False) as client:
        trailing_slash = client.post("/api/chart/", json=_payload())
        future_child = client.post("/api/chart/future", json=_payload())
        lookalike = client.get("/api/chartography")

    # The trailing-slash variant is not a registered route, and the API
    # boundary now answers for its own prefix instead of letting the asset
    # mount reply 405 with no Allow header and a third body shape.  404 is
    # also the more accurate refusal: the resource does not exist.
    assert trailing_slash.status_code == 404
    assert trailing_slash.json()["detail"]["code"] == "unknown_api_path"
    assert future_child.status_code == 404
    assert lookalike.status_code == 404
    assert [event["route"] for event in events] == [
        "frontend_or_unmatched",
        "frontend_or_unmatched",
        "frontend_or_unmatched",
    ]


def test_private_alpha_does_not_grant_cross_origin_access():
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


@pytest.mark.parametrize(
    ("mutate", "reason_code", "location"),
    [
        (
            lambda payload: payload.update({
                "birth_time_precision": "approximate_hour"
            }),
            "approximate_hour_requires_zero_subhour",
            ["body"],
        ),
        (
            lambda payload: payload.update({
                "birth_time_precision": "date_only"
            }),
            "date_only_requires_zero_time",
            ["body"],
        ),
        (
            lambda payload: payload.update({
                "computation_mode": {"center": "heliocentric"},
                "options": {"moon_position_profile": "moon_only_topocentric_v1"},
            }),
            "moon_profile_center_conflict",
            ["body"],
        ),
        (
            lambda payload: payload.update({
                "options": {"aspect_orb_scale_percent": 80.0},
            }),
            "aspect_orb_scale_requires_profile",
            ["body", "options"],
        ),
    ],
)
def test_model_conflicts_expose_closed_actionable_reason_without_values(
    mutate, reason_code, location
):
    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    payload = _payload()
    mutate(payload)
    with TestClient(hosted_app) as client:
        response = client.post("/api/chart", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"] == [{
        "type": reason_code,
        "loc": location,
    }]


def test_numeric_fields_reject_boolean_and_string_coercion():
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


@pytest.mark.parametrize("profile_name", ["private_alpha", "public"])
def test_supported_profiles_return_only_closed_error_code(monkeypatch, profile_name):
    from app import main
    from app.ephemeris import FullEphemerisRequiredError
    def fail(_request):
        raise FullEphemerisRequiredError(
            operation="CANARY_OPERATION",
            jd_ut=2597667.2296284684,
            retflag=4,
        )

    monkeypatch.setattr(main, "_compute_chart_locked", fail)
    events = []
    hosted_app = main.create_app(
        AppSettings(profile=AppProfile(profile_name)),
        event_emitter=lambda event: events.append(event) or True,
    )
    with TestClient(hosted_app) as client:
        response = client.post("/api/chart", json=_payload())

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "full_ephemeris_required"}
    }
    assert "2597667" not in response.text
    assert "CANARY_OPERATION" not in response.text
    assert events[-1]["error_code"] == "full_ephemeris_required"
    assert events[-1]["outcome"] == "error"


def test_hosted_boundary_rejects_oversized_headers_before_chart_parsing():
    hosted_app = create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    with TestClient(hosted_app) as client:
        response = client.post(
            "/api/chart",
            json=_payload(),
            headers={"X-Oversized-Probe": "a" * (20 * 1024)},
        )

    assert response.status_code == 431, response.text
    assert response.json()["detail"]["code"] == "request_headers_too_large"
