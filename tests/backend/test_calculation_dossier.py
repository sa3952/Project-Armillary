"""Acceptance contract for the backend-authored Calculation Dossier."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path

import swisseph as swe
from fastapi.testclient import TestClient

from app.main import app, create_app
from app.schemas import ChartRequest, OptionsInput
from app.settings import AppProfile, AppSettings


client = TestClient(app)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def dossier_payload() -> dict:
    return {
        "datetime": {
            "year": 2000,
            "month": 1,
            "day": 1,
            "hour": 12,
            "minute": 0,
            "second": 0,
        },
        "timezone": {
            "mode": "iana",
            "iana_name": "Asia/Taipei",
            "fold": 0,
        },
        "location": {
            "latitude": 25.033,
            "longitude": 121.5654,
            "altitude_m": 10,
        },
        "atmosphere": {
            "pressure_hpa": None,
            "temperature_c": 0,
        },
        "computation_mode": {
            "center": "geocentric",
            "zodiac": "tropical",
            "ayanamsa": "fagan_bradley",
            "position_mode": "apparent",
            "ecliptic_frame": "of_date",
            "nutation": True,
        },
        "options": {
            "house_system": "W",
            "include_fixed_stars": False,
            "include_lots": False,
            "include_antiscia": False,
            "include_void_of_course": False,
            "include_declination_aspects": False,
            "include_outer_planets": False,
            "include_lunar_phases": False,
            "include_eclipses": False,
            "include_rise_set_transits": False,
        },
    }


def _warning_codes(dossier: dict) -> set[str]:
    return {warning["code"] for warning in dossier["warnings"]}


def test_dossier_is_a_versioned_backend_receipt_with_replayable_inputs():
    response = client.post("/api/chart", json=dossier_payload())
    assert response.status_code == 200
    data = response.json()

    assert data["schema_version"] == "0.13.0"
    dossier = data["calculation_dossier"]
    assert dossier["dossier_version"] == "0.6.0"
    assert dossier["status"] == "provisional"
    assert dossier["authority"] == "backend_effective_runtime"
    assert dossier["build_identity"] == {
        "status": "unavailable",
        "source_revision": None,
        "revision_source": None,
    }
    expected_receipt = ChartRequest.model_validate(
        dossier_payload()
    ).model_dump(mode="json")
    assert dossier["input_receipt"] == expected_receipt

    replay = client.post("/api/chart", json=dossier["input_receipt"])
    assert replay.status_code == 200
    replay_data = replay.json()
    assert replay_data["astronomical_data"]["time"]["utc_time"] == (
        data["astronomical_data"]["time"]["utc_time"]
    )
    assert replay_data["astronomical_data"]["time"]["jd_ut"] == (
        data["astronomical_data"]["time"]["jd_ut"]
    )
    assert replay_data["astronomical_data"]["bodies"][0]["longitude"] == (
        data["astronomical_data"]["bodies"][0]["longitude"]
    )


def test_dossier_separates_time_policy_requested_options_and_effective_modules():
    data = client.post("/api/chart", json=dossier_payload()).json()
    dossier = data["calculation_dossier"]
    time = dossier["time_conversion"]

    assert time["calendar"] == {
        "system": "gregorian",
        "swiss_flag": "GREG_CAL",
        "supported_year_range": [1900, 2399],
    }
    assert time["conversion_function"] == "swe.utc_to_jd"
    assert time["timezone_mode"] == "iana"
    assert time["timezone_label"] == "Asia/Taipei"
    assert time["fold"] == 0
    assert time["resolved_utc_offset_hours"] == 8.0
    assert time["utc_iso_8601"].endswith("Z")
    assert time["jd_ut1"] == data["astronomical_data"]["time"]["jd_ut"]
    assert time["jd_tt"] == data["astronomical_data"]["time"]["jd_et"]

    policy = dossier["calculation_policy"]
    assert policy["computation_mode"] == dossier_payload()["computation_mode"]
    assert policy["normalized_requested_options"] == data["requested_options"]
    assert "requested_options" not in policy
    assert "effective_options" not in policy
    assert policy["modules"]["core_positions"] == "computed"
    assert policy["modules"]["fixed_stars"] == "not_requested"
    assert policy["modules"]["lunar_phases"] == "not_requested"
    assert policy["modules"]["eclipses"] == "not_requested"
    assert policy["modules"]["rise_set_transits"] == "not_requested"
    assert policy["coordinate_conventions"]["azimuth"] == (
        "north_0_degrees_clockwise_east"
    )
    assert "FLG_SWIEPH" in policy["flag_policy"]["base_position_flags"]["names"]
    assert "FLG_SPEED" in policy["flag_policy"]["base_position_flags"]["names"]
    assert "FLG_EQUATORIAL" in (
        policy["flag_policy"]["equatorial_source_flags"]["names"]
    )
    assert time["ecl_nut_retflag"]["value"] == (
        data["astronomical_data"]["time"]["ecl_nut_retflag"]
    )
    assert time["ayanamsa_degrees"] is None


def test_option_state_receipts_cover_the_exact_options_model_and_input_presence():
    """D-QA-03: every option has a closed execution receipt, not only an echo."""

    payload = dossier_payload()
    # Explicitly repeat one default and select one opt-in so explicit/defaulted
    # and requested/not-requested are both represented in one real API call.
    payload["options"]["include_aspects"] = True
    payload["options"]["include_chiron"] = True
    response = client.post("/api/chart", json=payload)
    assert response.status_code == 200
    data = response.json()
    states = data["calculation_dossier"]["calculation_policy"]["option_states"]

    assert set(states) == set(OptionsInput.model_fields)
    assert states["include_aspects"]["input_presence"] == "explicit"
    assert states["include_aspects"]["requested"] is True
    assert states["include_aspects"]["executed"] is True
    assert states["include_aspects"]["available"] is True
    assert states["include_chiron"]["available"] is True
    assert states["include_south_nodes"]["input_presence"] == "defaulted"
    assert states["include_south_nodes"]["reason_code"] == "option_not_requested"
    assert states["moon_position_profile"]["input_presence"] == "defaulted"
    assert states["moon_position_profile"]["requested"] is True
    assert states["moon_position_profile"]["executed"] is True
    assert states["moon_position_profile"]["available"] is True
    for name, state in states.items():
        assert set(state) == {
            "input_presence",
            "requested_value",
            "requested",
            "executed",
            "applicable",
            "available",
            "source",
            "reason_code",
            "response_paths",
        }, name
        assert state["input_presence"] in {"explicit", "defaulted"}
        assert state["response_paths"], name
        if state["available"]:
            assert state["executed"] is True, name
            assert state["applicable"] is True, name
            assert state["source"] is not None, name


def test_available_option_response_paths_are_real_non_null_response_paths():
    """QA-RT-03-001: an available receipt may not cite a pseudo-selector."""

    payload = dossier_payload()
    payload["options"].update(include_chiron=True)
    response = client.post("/api/chart", json=payload)
    assert response.status_code == 200
    data = response.json()
    states = data["calculation_dossier"]["calculation_policy"]["option_states"]

    def resolve(path: str):
        value = data
        for component in path.split("."):
            assert "[" not in component and "]" not in component, path
            assert isinstance(value, dict) and component in value, path
            value = value[component]
        return value

    for name, state in states.items():
        if not state["available"]:
            continue
        assert state["response_paths"], name
        assert resolve(state["source"]) is not None, (name, state["source"])
        for path in state["response_paths"]:
            assert resolve(path) is not None, (name, path)


def test_dependent_option_receipts_name_parent_not_requested_and_house_inapplicability():
    payload = dossier_payload()
    payload["options"].update(
        {
            "include_houses": False,
            "include_antiscia": False,
            "antiscia_include_nodes": True,
            "include_aspects": False,
            "include_aspect_perfection": True,
            "aspect_include_nodes": True,
            "include_extra_angles": True,
            "include_anti_vertex": True,
        }
    )
    response = client.post("/api/chart", json=payload)
    assert response.status_code == 200
    dossier = response.json()["calculation_dossier"]
    states = dossier["calculation_policy"]["option_states"]

    for name in (
        "antiscia_include_nodes",
        "include_aspect_perfection",
        "aspect_include_nodes",
    ):
        assert states[name] == {
            **states[name],
            "requested": True,
            "executed": False,
            "applicable": False,
            "available": False,
            "source": None,
            "reason_code": "parent_module_not_requested_or_not_applicable",
        }
    for name in ("include_extra_angles", "include_anti_vertex"):
        assert states[name]["requested"] is True
        assert states[name]["executed"] is False
        assert states[name]["applicable"] is False
        assert states[name]["available"] is False
        assert states[name]["reason_code"] == "house_calculation_not_executed"


def test_option_state_records_attempted_upstream_failure_without_calling_it_available():
    payload = dossier_payload()
    payload["datetime"].update(
        year=2399, month=12, day=31, hour=12, minute=0, second=0
    )
    payload["options"]["include_lunar_phases"] = True
    response = client.post("/api/chart", json=payload)
    assert response.status_code == 200
    state = response.json()["calculation_dossier"]["calculation_policy"][
        "option_states"
    ]["include_lunar_phases"]

    assert state["requested"] is True
    assert state["executed"] is True
    assert state["applicable"] is False
    assert state["available"] is False
    assert state["source"] == "astronomical_data.lunar_events"
    assert state["reason_code"] == "full_ephemeris_unavailable_for_search_window"


def test_dignity_option_states_fail_closed_with_the_method_boundary_reason():
    payload = dossier_payload()
    payload["computation_mode"]["zodiac"] = "sidereal"
    payload["options"].update(
        {
            "include_domicile_exaltation": True,
            "bounds_profile": "egyptian_bounds_robbins_1940_v1",
            "decan_profile": "chaldean_planetary_faces_firmicus_ii_4_v1",
            "triplicity_profile": "dorothean_triplicity_three_rulers_v1",
        }
    )
    response = client.post("/api/chart", json=payload)
    assert response.status_code == 200
    states = response.json()["calculation_dossier"]["calculation_policy"][
        "option_states"
    ]

    for name in (
        "include_domicile_exaltation",
        "bounds_profile",
        "decan_profile",
        "triplicity_profile",
    ):
        assert states[name]["requested"] is True
        assert states[name]["executed"] is False
        assert states[name]["applicable"] is False
        assert states[name]["available"] is False
        assert states[name]["source"] is None
        assert states[name]["reason_code"] == (
            "sidereal_dignity_basis_not_authorized"
        )


def test_dossier_records_engine_files_and_actual_retflags_without_absolute_paths():
    data = client.post("/api/chart", json=dossier_payload()).json()
    dossier = data["calculation_dossier"]
    engine = dossier["engine"]

    assert engine["pyswisseph_distribution_version"] == (
        importlib.metadata.version("pyswisseph")
    )
    assert engine["swiss_ephemeris_library_version"] == swe.version
    assert engine["requested_ephemeris_source"] == "Swiss Ephemeris files"
    assert engine["ephemeris_dataset_lineage"] == {
        "status": "declared_from_bundled_file_headers",
        "representation": "Swiss Ephemeris compressed files",
        "jpl_ephemeris_basis": "DE441",
        "applies_to": ["sepl_18.se1", "semo_18.se1"],
        "evidence": "bundled_file_header_checked_at_development_time",
        "runtime_limit": (
            "returned retflags identify the Swiss/Moshier source family, "
            "not the exact opened file or JPL kernel lineage"
        ),
    }

    files = engine["available_input_files"]
    assert {entry["filename"] for entry in files} == {
        "sepl_18.se1",
        "semo_18.se1",
        "seas_18.se1",
        "sefstars.txt",
        "seorbel.txt",
    }
    for entry in files:
        assert entry["exists"] is True
        assert entry["size_bytes"] > 0
        assert len(entry["sha256"]) == 64
        int(entry["sha256"], 16)
        assert "/" not in entry["filename"]
        assert "path" not in entry

    core_records = dossier["provenance"]["core_objects"]
    assert len(core_records) == 9
    assert (
        dossier["provenance"][
            "all_core_calculation_sources_used_full_ephemeris"
        ]
        is True
    )
    assert dossier["provenance"]["core_result_status_counts"] == {
        "available": 9,
        "not_applicable": 0,
        "failed": 0,
    }
    assert "all_core_objects_used_full_ephemeris" not in dossier["provenance"]
    for record in core_records:
        assert record["used_full_ephemeris"] is True
        assert record["result_status"] == "available"
        for coordinate in ("ecliptic", "equatorial", "horizontal_source"):
            flag = record["retflags"][coordinate]
            assert isinstance(flag["value"], int)
            assert "FLG_SWIEPH" in flag["names"]
            assert "FLG_MOSEPH" not in flag["names"]


def test_dossier_build_identity_accepts_only_release_controlled_settings():
    revision = "a" * 40
    release_app = create_app(
        AppSettings(
            profile=AppProfile.PRIVATE_ALPHA,
            source_revision=revision,
            revision_source=(
                "build_environment:"
                "CLASSICAL_ASTROLOGY_SOURCE_REVISION"
            ),
        )
    )

    with TestClient(release_app) as release_client:
        response = release_client.post("/api/chart", json=dossier_payload())

    assert response.status_code == 200
    assert response.json()["calculation_dossier"]["build_identity"] == {
        "status": "available",
        "source_revision": revision,
        "revision_source": (
            "build_environment:"
            "CLASSICAL_ASTROLOGY_SOURCE_REVISION"
        ),
    }
    local_response = client.post("/api/chart", json=dossier_payload())
    assert local_response.json()["calculation_dossier"]["build_identity"] == {
        "status": "unavailable",
        "source_revision": None,
        "revision_source": None,
    }


def test_request_cannot_supply_or_override_build_identity():
    payload = dossier_payload()
    payload["build_identity"] = {
        "status": "available",
        "source_revision": "f" * 40,
        "revision_source": "request",
    }

    rejected = client.post(
        "/api/chart",
        json=payload,
        headers={
            "X-Source-Revision": "f" * 40,
        },
    )

    assert rejected.status_code == 422


def test_dossier_limits_horizon_event_source_to_requested_flag_evidence():
    payload = dossier_payload()
    payload["options"]["include_rise_set_transits"] = True

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 200
    receipt = response.json()["calculation_dossier"]["provenance"][
        "event_modules"
    ]["rise_set_transits"]
    assert receipt == {
        "computed": True,
        "source_policy": "Swiss files requested via FLG_SWIEPH",
        "actual_source_verified": False,
        "source_evidence": (
            "requested_flag_only_rise_trans_returns_no_ephemeris_retflag"
        ),
        "detail_path": "astronomical_data.horizon_events",
    }


def test_dossier_has_structured_method_and_dst_warnings():
    default_data = client.post("/api/chart", json=dossier_payload()).json()
    default_dossier = default_data["calculation_dossier"]

    assert "provisional_method_result" in _warning_codes(default_dossier)
    houses = default_dossier["methodology"]["items"]["house_division"]
    assert houses["computed"] is True
    assert houses["method_status"] == "provisional_pending_method_audit"
    assert houses["method_authority"] is None

    payload = dossier_payload()
    payload["datetime"] = {
        "year": 2024,
        "month": 11,
        "day": 3,
        "hour": 1,
        "minute": 30,
        "second": 0,
    }
    payload["timezone"] = {
        "mode": "iana",
        "iana_name": "America/New_York",
        "fold": 1,
    }
    ambiguous = client.post("/api/chart", json=payload)
    assert ambiguous.status_code == 200
    dossier = ambiguous.json()["calculation_dossier"]
    assert "ambiguous_local_time" in _warning_codes(dossier)
    assert dossier["time_conversion"]["fold"] == 1
    assert dossier["time_conversion"]["dst_warning"]


def test_trace_receipt_is_integrity_checkable_and_privacy_scope_is_explicit():
    data = client.post("/api/chart", json=dossier_payload()).json()
    dossier = data["calculation_dossier"]
    trace = data["calculation_trace"]
    canonical = json.dumps(
        trace,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    assert dossier["trace_receipt"] == {
        "response_path": "calculation_trace",
        "step_count": len(trace),
        "python_json_serialization_sha256": hashlib.sha256(canonical).hexdigest(),
        "serialization_recipe": {
            "implementation": "Python json.dumps",
            "ensure_ascii": False,
            "sort_keys": True,
            "separators": [",", ":"],
            "encoding": "UTF-8",
            "float_serialization": "Python runtime json encoder",
            "portable_across_languages": False,
        },
    }
    privacy = dossier["privacy"]
    assert set(privacy) == {
        "privacy_attestation_version",
        "attestation_status",
        "contains_sensitive_birth_data",
        "anonymous_share_ready",
        "evidence_semantics",
        "claims",
        # FPI-2026-08-06-E-011：未涵蓋層的敘述隨部署 profile 變動，因此收據
        # 必須說出自己描述的是哪一種部署，否則它又會在錯誤的形態下沉默。
        "deployment_profile",
        "deployment_profile_status",
        "uncovered_layer_semantics",
        "uncovered_layers",
    }
    assert privacy["privacy_attestation_version"] == "1.3.0"
    assert privacy["attestation_status"] == (
        "provisional_pending_external_review"
    )
    assert privacy["contains_sensitive_birth_data"] is True
    assert privacy["anonymous_share_ready"] is False
    assert privacy["evidence_semantics"] == (
        "repository_test_references_not_execution_attestation"
    )
    assert "application_persistence" not in privacy
    assert "application_request_body_logging" not in privacy


def test_privacy_attestation_claims_are_layered_closed_and_evidence_bound():
    dossier = client.post(
        "/api/chart",
        json=dossier_payload(),
    ).json()["calculation_dossier"]
    privacy = dossier["privacy"]
    claims = privacy["claims"]

    expected_ids = {
        "application_chart_path_no_persistence",
        "application_telemetry_allowlist",
        "asgi_exception_data_minimization",
        "canonical_launcher_access_log_suppression",
        "browser_transient_sensitive_state",
    }
    expected_layers = {
        "application_request_path",
        "application_event_sink",
        "asgi_application_boundary",
        "canonical_local_launcher",
        "browser_application",
    }
    assert {claim["id"] for claim in claims} == expected_ids
    assert {claim["enforcement_layer"] for claim in claims} == expected_layers

    expected_statuses = {
        "application_chart_path_no_persistence": "implemented_in_application_layer",
        "application_telemetry_allowlist": "implemented_in_application_layer",
        "asgi_exception_data_minimization": "implemented_in_asgi_layer",
        "canonical_launcher_access_log_suppression": (
            "conditional_on_canonical_launcher"
        ),
        "browser_transient_sensitive_state": "conditional_on_bundled_frontend",
    }

    for claim in claims:
        assert set(claim) == {
            "id",
            "status",
            "statement",
            "enforcement_layer",
            "control",
            "evidence",
            "scope",
            "limitations",
        }
        assert claim["status"] == expected_statuses[claim["id"]]
        assert set(claim["control"]) == {"id", "mechanism"}
        assert claim["control"]["id"]
        assert claim["control"]["mechanism"]
        assert claim["evidence"]
        for evidence in claim["evidence"]:
            assert set(evidence) == {"type", "reference", "semantics"}
            assert evidence["type"] in {
                "python_test_reference",
                "node_test_reference",
                "static_contract_reference",
            }
            assert not evidence["reference"].startswith("/")
            assert evidence["semantics"] == (
                "repository_pointer_not_test_execution_result"
            )
            relative_path, separator, test_id = evidence["reference"].partition(
                "::"
            )
            evidence_path = PROJECT_ROOT / relative_path
            assert evidence_path.is_file(), evidence["reference"]
            if separator:
                assert test_id in evidence_path.read_text(
                    encoding="utf-8"
                ), evidence["reference"]
        assert set(claim["scope"]) == {
            "surface",
            "applies_to",
            "excludes",
        }
        assert claim["scope"]["surface"] == "current_local_product"
        assert claim["scope"]["applies_to"]
        assert isinstance(claim["scope"]["excludes"], list)
        assert claim["limitations"]
        assert any(
            evidence["type"]
            in {"python_test_reference", "node_test_reference"}
            for evidence in claim["evidence"]
        ), "a static contract reference must never be the claim's only reference"

    assert all(
        evidence["type"] not in {
            "source_contract_test",
            "python_regression_test",
            "node_regression_test",
            "static_source_contract_test",
        }
        for claim in claims
        for evidence in claim["evidence"]
    )

    launcher_claim = next(
        claim
        for claim in claims
        if claim["id"] == "canonical_launcher_access_log_suppression"
    )
    browser_claim = next(
        claim
        for claim in claims
        if claim["id"] == "browser_transient_sensitive_state"
    )
    assert launcher_claim["status"] == "conditional_on_canonical_launcher"
    assert "manual or alternate Uvicorn invocation" in launcher_claim["scope"][
        "excludes"
    ]
    assert browser_claim["status"] == "conditional_on_bundled_frontend"
    assert (
        "API clients that do not load the bundled frontend"
        in browser_claim["scope"]["excludes"]
    )

    serialized_statuses = " ".join(
        [
            privacy["attestation_status"],
            *(claim["status"] for claim in claims),
        ]
    ).lower()
    for unsupported_word in ("secure", "verified", "disabled"):
        assert unsupported_word not in serialized_statuses

    # FPI-2026-08-06-E-011：前兩層的敘述隨部署 profile 變動，因此這裡改為
    # 釘住結構與該層的關鍵語意，而不是逐字複製一段會隨 profile 改變的文字。
    # 本測試走的是預設（local）path。逐 profile 的措辭由
    # tests/backend/test_blind_review_second_batch.py 看守。
    assert [layer["layer"] for layer in privacy["uncovered_layers"]] == [
        "reverse_proxy_cdn_waf",
        "hosting_supervisor",
        "third_party_telemetry",
        "browser_os_native_memory",
    ]
    assert all(
        layer["status"] == "outside_current_control_scope"
        for layer in privacy["uncovered_layers"]
    )
    assert "不存在此層" in privacy["uncovered_layers"][0]["note"]
    assert "本機執行" in privacy["uncovered_layers"][1]["note"]
    assert privacy["uncovered_layers"][2:] == [
        {
            "layer": "third_party_telemetry",
            "status": "outside_current_control_scope",
            "note": "目前未整合；未來新增前必須重新進行隱私審查。",
        },
        {
            "layer": "browser_os_native_memory",
            "status": "outside_current_control_scope",
            "note": "不宣稱RAM、swap、crash restore或原生函式庫資料可安全抹除。",
        },
    ]


def test_privacy_attestation_is_policy_invariant_across_calculation_modes():
    base_privacy = client.post(
        "/api/chart",
        json=dossier_payload(),
    ).json()["calculation_dossier"]["privacy"]
    mode_variants = [
        {
            "center": "geocentric",
            "zodiac": "sidereal",
            "ayanamsa": "hipparchos",
            "position_mode": "true",
            "ecliptic_frame": "j2000",
            "nutation": False,
        },
        {
            "center": "topocentric",
            "zodiac": "tropical",
            "ayanamsa": "fagan_bradley",
            "position_mode": "apparent",
            "ecliptic_frame": "of_date",
            "nutation": True,
        },
        {
            "center": "heliocentric",
            "zodiac": "tropical",
            "ayanamsa": "fagan_bradley",
            "position_mode": "true",
            "ecliptic_frame": "j2000",
            "nutation": False,
        },
        {
            "center": "barycentric",
            "zodiac": "sidereal",
            "ayanamsa": "sassanian",
            "position_mode": "apparent",
            "ecliptic_frame": "of_date",
            "nutation": True,
        },
    ]

    for mode in mode_variants:
        payload = dossier_payload()
        payload["computation_mode"] = mode
        response = client.post("/api/chart", json=payload)
        assert response.status_code == 200, mode
        assert response.json()["calculation_dossier"]["privacy"] == (
            base_privacy
        ), mode


def test_non_physical_centers_distinguish_execution_from_usable_results():
    expected_not_applicable_object = {
        "heliocentric": "sun",
        "barycentric": "true_node",
    }

    for center, object_key in expected_not_applicable_object.items():
        payload = dossier_payload()
        payload["computation_mode"]["center"] = center
        payload["options"]["include_lots"] = True
        payload["options"]["include_void_of_course"] = True

        response = client.post("/api/chart", json=payload)
        assert response.status_code == 200
        dossier = response.json()["calculation_dossier"]
        modules = dossier["calculation_policy"]["modules"]

        for name in ("sect", "lots", "void_of_course"):
            assert modules[name] == "not_applicable"
            receipt = dossier["methodology"]["items"][name]
            assert receipt["execution_status"] == "not_applicable"
            assert receipt["computed"] is False
            assert receipt["method_status"] == "provisional_pending_method_audit"

        assert "module_not_applicable_in_mode" in _warning_codes(dossier)

        record = next(
            item
            for item in dossier["provenance"]["core_objects"]
            if item["key"] == object_key
        )
        assert record["result_status"] == "not_applicable"
        # Source usage and result applicability are intentionally orthogonal.
        assert record["used_full_ephemeris"] is True
        assert (
            dossier["provenance"][
                "all_core_calculation_sources_used_full_ephemeris"
            ]
            is True
        )
        assert (
            dossier["provenance"]["core_result_status_counts"]["not_applicable"]
            >= 1
        )
