"""/api/chart 回歸測試：把這次對話中手動用 curl 驗證過的基準命盤與邊界案例，
寫成可重跑的自動化測試，取代「commit message 宣稱測過但沒有可重跑檔案」的狀態。

執行方式（於 backend/ 目錄下）：
    source .venv/bin/activate
    pip install -r requirements-dev.txt   # 只需第一次
    pytest tests/ -v
"""

import concurrent.futures
import datetime as dt
import hashlib
import hmac
import json
from pathlib import Path

import pytest
import swisseph as swe
from fastapi.testclient import TestClient

from app import ephemeris
from app.main import app, create_app
from app.settings import AppProfile, AppSettings
from app.core.formatting import to_dms, to_hms, swiss_azimuth_to_standard  # noqa: F401

client = TestClient(app)
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
EPHE_DIR = Path(__file__).resolve().parents[2] / "backend" / "ephe"


def base_payload():
    """回傳一份全新的基準請求字典；呼叫端自行以 payload[...] = {...} 覆寫需要的區塊。"""
    return {
        "datetime": {"year": 2000, "month": 1, "day": 1, "hour": 12, "minute": 0, "second": 0},
        "timezone": {"mode": "iana", "iana_name": "Asia/Taipei"},
        "location": {"latitude": 25.0330, "longitude": 121.5654, "altitude_m": 10},
        # 多數既有回歸案例刻意覆蓋完整舊功能；產品／API 的真正預設值另由
        # test_default_request_only_computes_core_astronomy() 驗證為 method opt-in。
        "options": {
            "include_fixed_stars": True,
            "include_lots": True,
            "include_antiscia": True,
            "include_void_of_course": True,
            "include_declination_aspects": True,
        },
    }


def test_chart_request_reasserts_ephemeris_path_in_worker_thread(monkeypatch):
    """Linux source builds keep the Swiss ephemeris path per worker thread."""
    import threading
    import app.main as main_module

    calling_thread = threading.get_ident()
    init_threads = []
    original_init = main_module.ensure_ephemeris_initialized_for_thread

    def recording_init():
        init_threads.append(threading.get_ident())
        return original_init()

    monkeypatch.setattr(
        main_module,
        "ensure_ephemeris_initialized_for_thread",
        recording_init,
    )
    response = client.post("/api/chart", json=base_payload())

    assert response.status_code == 200
    assert init_threads
    assert all(thread_id != calling_thread for thread_id in init_threads)


def test_ephemeris_path_is_set_once_per_thread_not_once_per_request():
    """The path is reasserted per thread, which is what the comment claims.

    The previous code called init_ephemeris() on every request, so "once per
    worker thread" and "every time" were indistinguishable in the source.  A
    thread that has already set the path must not set it again, and a thread
    that has not must still set it — including a thread created after the
    module was imported.
    """
    import threading

    from app.ephemeris import ensure_ephemeris_initialized_for_thread

    results: list[bool] = []

    def run_in_fresh_thread():
        results.append(ensure_ephemeris_initialized_for_thread())
        results.append(ensure_ephemeris_initialized_for_thread())
        results.append(ensure_ephemeris_initialized_for_thread())

    worker = threading.Thread(target=run_in_fresh_thread)
    worker.start()
    worker.join()

    # First call does the work; every later call in that same thread is a no-op.
    assert results == [True, False, False]

    # A second fresh thread does not inherit the first thread's flag.
    second: list[bool] = []
    another = threading.Thread(
        target=lambda: second.append(ensure_ephemeris_initialized_for_thread())
    )
    another.start()
    another.join()
    assert second == [True]


def test_private_runtime_health_proves_knowledge_of_launcher_secret(monkeypatch):
    nonce = "runtime-health-test-nonce"

    monkeypatch.delenv("CLASSICAL_ASTROLOGY_RUNTIME_TOKEN", raising=False)
    unavailable = client.get(
        "/api/runtime-health",
        headers={"X-Local-Runtime-Nonce": nonce},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["code"] == "runtime_auth_unavailable"

    # A real launcher token is secrets.token_urlsafe(32) — 43 characters.  The
    # endpoint refuses to attest with anything shorter, so the fixture uses a
    # realistic length rather than a short literal.
    token = "test-only-runtime-secret-with-realistic-length"
    monkeypatch.setenv("CLASSICAL_ASTROLOGY_RUNTIME_TOKEN", token)
    response = client.get(
        "/api/runtime-health",
        headers={"X-Local-Runtime-Nonce": nonce},
    )

    assert response.status_code == 200
    assert response.json() == {
        "service": "classical-astrology-app",
        "ready": True,
        "runtime_contract": "local-runtime-v2",
        "nonce_hmac": hmac.new(
            token.encode(),
            nonce.encode(),
            hashlib.sha256,
        ).hexdigest(),
    }


def test_runtime_health_refuses_to_attest_with_a_weak_token(monkeypatch):
    """The attestation is worth exactly the token's entropy, so bound it.

    The endpoint answers HMAC(token, caller-chosen nonce), which is the correct
    shape for challenge-response and does not leak the key.  What it does give
    an attacker who can reach it is unlimited known-plaintext pairs for offline
    brute force, so the scheme's strength is the token's and nothing else.
    Rather than attest with a secret that cannot carry that weight, refuse.
    """
    nonce = "runtime-health-test-nonce"
    monkeypatch.setenv("CLASSICAL_ASTROLOGY_RUNTIME_TOKEN", "short-secret")

    response = client.get(
        "/api/runtime-health",
        headers={"X-Local-Runtime-Nonce": nonce},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "runtime_auth_token_too_weak"
    # The refusal must not itself become an oracle: no digest is returned.
    assert "nonce_hmac" not in response.text


def test_health_check_identifies_running_service():
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "classical-astrology-app"
    assert data["runtime_contract"] == "local-runtime-v2"
    assert data["swiss_ephemeris_library_version"]


# ---------------------------------------------------------------------------
# 基準命盤：2000-01-01 12:00 Asia/Taipei, 25.0330N 121.5654E
# 數值來自本次對話中已用 swisseph 直接呼叫交叉核對過的結果，若這些值改變，
# 極可能代表計算邏輯（而非精度誤差）出了問題。
# ---------------------------------------------------------------------------

def test_benchmark_chart_sun_and_ascendant():
    resp = client.post("/api/chart", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    sun = data["astronomical_data"]["bodies"][0]
    assert sun["key"] == "sun"
    assert sun["longitude"] == pytest.approx(280.0291171489897, abs=1e-6)

    angles = data["astronomical_data"]["angles"]
    house_division = data["derived_methods"]["house_division"]
    assert house_division["system_name"] == "Whole Sign"
    assert angles["asc"] == pytest.approx(15.877185639521919, abs=1e-6)


def test_benchmark_chart_full_response_shape():
    resp = client.post("/api/chart", json=base_payload())
    data = resp.json()
    assert set(data.keys()) == {
        "schema_version", "output_contract", "requested_options",
        "library_info", "computation_mode", "calculation_dossier", "astronomical_data",
        "derived_geometry", "derived_methods", "birth_time_sensitivity",
        "calculation_trace",
    }
    assert data["schema_version"].startswith("0.")
    assert data["output_contract"]["status"] == "provisional"
    assert set(data["astronomical_data"].keys()) == {
        "time", "atmosphere", "bodies", "nodes", "fixed_stars",
        "fixed_star_policy", "angles", "extra_angles",
        "lunar_apsides", "parallax_moon", "lunar_events", "horizon_events",
    }
    assert len(data["astronomical_data"]["bodies"]) == 7  # 古典七政
    assert len(data["astronomical_data"]["nodes"]) == 2   # 真/平交點
    assert "house_division" in data["derived_methods"]
    assert set(data["astronomical_data"]["angles"]) == {"asc", "mc", "desc", "ic", "armc"}


def test_default_request_only_computes_core_astronomy():
    payload = base_payload()
    payload.pop("options")
    data = client.post("/api/chart", json=payload).json()

    assert data["requested_options"]["include_fixed_stars"] is False
    assert data["astronomical_data"]["fixed_stars"] == []
    assert data["derived_geometry"]["antiscia"] == {}
    assert data["derived_methods"]["sect"] is None
    assert data["derived_methods"]["lots"] == {}
    assert data["derived_methods"]["void_of_course"] == {}
    assert data["derived_methods"]["declination_aspects"] == {}
    assert data["astronomical_data"]["lunar_events"] == {}
    assert data["astronomical_data"]["horizon_events"] == {}


def test_exact_birth_time_reports_sensitivity_not_applicable():
    data = client.post("/api/chart", json=base_payload()).json()

    assert data["birth_time_sensitivity"] == {
        "precision": "exact",
        "status": "not_applicable",
    }
    assert data["calculation_dossier"]["birth_time"]["precision"] == "exact"


def test_approximate_hour_uses_one_midpoint_chart_and_five_lightweight_probes():
    payload = base_payload()
    payload["birth_time_precision"] = "approximate_hour"
    payload["datetime"]["minute"] = 0
    payload["datetime"]["second"] = 0

    response = client.post("/api/chart", json=payload)
    assert response.status_code == 200
    data = response.json()
    sensitivity = data["birth_time_sensitivity"]

    assert data["astronomical_data"]["time"]["input_local_time"].startswith(
        "2000-01-01 12:30:"
    )
    assert sensitivity["precision"] == "approximate_hour"
    assert sensitivity["representative_minute"] == 30
    assert sensitivity["representative_semantics"] == (
        "representative_midpoint_not_exact_birth_time"
    )
    assert [probe["minute"] for probe in sensitivity["probes"]] == [
        0,
        15,
        30,
        45,
        59,
    ]
    assert all("chart" not in probe for probe in sensitivity["probes"])
    assert sensitivity["status"] in {
        "sampled_stable",
        "varies_within_sampled_hour",
    }
    assert sensitivity["sampling_semantics"] == (
        "five_discrete_probes_not_continuous_hour_proof"
    )
    assert sensitivity["planet_in_house"]
    assert sensitivity["transitions"]
    assert all(
        transition["resolution_seconds"] <= 15
        for transition in sensitivity["transitions"]
    )
    assert all(
        transition["changed_paths"]
        for transition in sensitivity["transitions"]
    )
    assert data["calculation_dossier"]["birth_time"][
        "representative_local_time"
    ].startswith("2000-01-01 12:30:")
    assert data["calculation_dossier"]["input_receipt"]["datetime"]["minute"] == 0


@pytest.mark.parametrize(
    ("minute", "second"),
    [
        (1, 0),
        (0, 0.5),
    ],
)
def test_approximate_hour_rejects_claimed_minute_or_second(minute, second):
    payload = base_payload()
    payload["birth_time_precision"] = "approximate_hour"
    payload["datetime"]["minute"] = minute
    payload["datetime"]["second"] = second

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 422


def test_approximate_ambiguous_hour_requires_explicit_fold():
    payload = base_payload()
    payload["birth_time_precision"] = "approximate_hour"
    payload["datetime"] = {
        "year": 2021,
        "month": 11,
        "day": 7,
        "hour": 1,
        "minute": 0,
        "second": 0,
    }
    payload["timezone"] = {
        "mode": "iana",
        "iana_name": "America/New_York",
    }

    missing_fold = client.post("/api/chart", json=payload)
    assert missing_fold.status_code == 422
    assert missing_fold.json()["detail"]["code"] == (
        "ambiguous_local_time_choice_required"
    )

    payload["timezone"]["fold"] = 1
    selected_fold = client.post("/api/chart", json=payload)
    assert selected_fold.status_code == 200
    assert any(
        warning["code"] == "ambiguous_local_time"
        for warning in selected_fold.json()["calculation_dossier"]["warnings"]
    )


def test_planet_in_house_uses_selected_whole_sign_cusps():
    data = client.post("/api/chart", json=base_payload()).json()
    receipt = data["derived_methods"]["planet_in_house"]

    assert receipt["execution_status"] == "computed"
    assert receipt["method"] == "zodiacal_cusp_half_open_intervals_v1"
    assert receipt["method_authority"] == "not_established"
    assert len(receipt["placements"]) == 7
    sun = next(item for item in receipt["placements"] if item["key"] == "sun")
    assert sun["house"] == 10
    assert sun["interval_semantics"] == "[cusp_n,cusp_n_plus_1)"


@pytest.mark.parametrize(
    "mode_override",
    [
        {"center": "heliocentric"},
        {"ecliptic_frame": "j2000"},
        {"nutation": False},
    ],
)
def test_planet_in_house_fails_closed_for_incompatible_frame(mode_override):
    payload = base_payload()
    payload["computation_mode"] = mode_override

    data = client.post("/api/chart", json=payload).json()
    receipt = data["derived_methods"]["planet_in_house"]

    assert receipt["execution_status"] == "not_applicable"
    assert receipt["placements"] == []
    assert receipt["reason_codes"]
    assert data["calculation_dossier"]["calculation_policy"]["modules"][
        "planet_in_house"
    ] == "not_applicable"


@pytest.mark.parametrize("year", [1900, 2399])
def test_supported_boundary_year_warns_without_suppressing_core_result(year):
    payload = base_payload()
    payload["datetime"]["year"] = year
    payload["datetime"]["month"] = 6
    payload["datetime"]["day"] = 15
    payload["options"] = {}

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["astronomical_data"]["bodies"]
    warning = next(
        item
        for item in data["calculation_dossier"]["warnings"]
        if item["code"] == "ephemeris_boundary_year"
    )
    assert warning["severity"] == "warning"
    assert "1900–2399" in warning["message"]


def test_non_boundary_supported_year_has_no_boundary_warning():
    payload = base_payload()
    payload["datetime"]["year"] = 1901

    data = client.post("/api/chart", json=payload).json()
    assert all(
        item["code"] != "ephemeris_boundary_year"
        for item in data["calculation_dossier"]["warnings"]
    )


def test_location_resolution_metadata_is_preserved_as_client_assertion():
    payload = base_payload()
    payload["location"].update(
        {
            "place_label": "臺中市西區",
            "location_source": "taiwan_moi_place_names",
            "source_record_id": "tw-moi-settlement:test-record",
            "location_precision": "settlement_representative_point",
        }
    )

    data = client.post("/api/chart", json=payload).json()
    receipt = data["calculation_dossier"]["location_resolution"]

    assert receipt["place_label"] == "臺中市西區"
    assert receipt["location_source"] == "taiwan_moi_place_names"
    assert receipt["source_record_id"] == "tw-moi-settlement:test-record"
    assert receipt["location_precision"] == "settlement_representative_point"
    assert receipt["verification_status"] == "client_asserted_not_server_verified"


def test_dataset_location_requires_traceable_source_record_id():
    payload = base_payload()
    payload["location"].update(
        {
            "place_label": "臺中市",
            "location_source": "geonames_cities500",
            "location_precision": "place_representative_point",
        }
    )

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 422


def test_manual_location_cannot_claim_dataset_precision():
    payload = base_payload()
    payload["location"]["location_precision"] = (
        "settlement_representative_point"
    )

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 422


def test_geonames_location_requires_global_place_precision():
    payload = base_payload()
    payload["location"].update(
        {
            "place_label": "Taichung",
            "location_source": "geonames_cities500",
            "source_record_id": "1668399",
            "location_precision": "settlement_representative_point",
        }
    )

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 422


def test_place_search_is_same_origin_post_and_returns_traceable_sources():
    response = client.post(
        "/api/places/search",
        json={"query": "臺中", "country_code": "TW", "limit": 5},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["execution"]["runtime_outbound"] is False
    assert data["execution"]["catalog_mode"] == "bundled_read_only_sqlite"
    assert data["results"]
    assert all(item["country_code"] == "TW" for item in data["results"])
    assert all(item["source_record_id"] for item in data["results"])
    assert all(item["location_precision"] for item in data["results"])
    assert any(
        item["source"] == "taiwan_moi_place_names"
        for item in data["results"]
    )

    assert client.get("/api/places/search").status_code in {404, 405}


def _hms_to_hours(value):
    hour, minute, second = value.split(":")
    return int(hour) + int(minute) / 60.0 + float(second) / 3600.0


def _circular_time_difference_seconds(actual_hours, expected_hours):
    hour_difference = (actual_hours - expected_hours + 12.0) % 24.0 - 12.0
    return abs(hour_difference * 3600.0)


def test_usno_external_time_baseline_distinguishes_mean_and_apparent_sidereal_time():
    fixture = json.loads((FIXTURE_DIR / "usno_2000_01_01_taipei.json").read_text())
    data = client.post("/api/chart", json=base_payload()).json()["astronomical_data"]["time"]
    expected = fixture["usno_output"]
    tolerance = fixture["tolerance"]

    # USNO fixture uses exactly 04:00:00 UT1. The App accepts 04:00:00 UTC and derives
    # UT1 through swe.utc_to_jd, so the comparison must allow DUT1 rather than pretend
    # the two time scales are identical.
    jd_error_seconds = abs(data["jd_ut"] - expected["julian_date_ut1"]) * 86400.0
    assert jd_error_seconds < tolerance["julian_date_seconds"]

    for actual_key, expected_key in [
        ("gast_hours", "gast"),
        ("gmst_hours", "gmst"),
        ("last_hours", "last"),
        ("lmst_hours", "lmst"),
    ]:
        error_seconds = _circular_time_difference_seconds(
            data[actual_key],
            _hms_to_hours(expected[expected_key]),
        )
        assert error_seconds < tolerance["time_seconds"], (
            f"{actual_key} differs from USNO by {error_seconds:.6f} seconds"
        )

    # Backward-compatible aliases must not silently switch meaning.
    assert data["gst_hours"] == data["gast_hours"]
    assert data["lst_hours"] == data["last_hours"]


@pytest.mark.parametrize(
    ("filename", "expected_sha256"),
    [
        ("sepl_18.se1", "ca1393ceab3a44fbc895887cf789c68819ae6a1cbc9b22225872dbe4ccd99a66"),
        ("semo_18.se1", "1ca07bd67c24374d77226180c20a4f9996cba013697894810518e7eb582ca4f7"),
        ("seas_18.se1", "a2cd8fc33807c78ca9a700c91c2e042258b12fc4796519e00781440b5ad8b2e2"),
        ("sefstars.txt", "18b0dcafbe5b7240773daba2c038a325f5b3fc4163f61e0a7f4e92abd4f517c6"),
    ],
)
def test_astronomy_baseline_pins_ephemeris_inputs(filename, expected_sha256):
    digest = hashlib.sha256((EPHE_DIR / filename).read_bytes()).hexdigest()
    assert digest == expected_sha256


def test_default_body_retflags_match_declared_apparent_geocentric_of_date_mode():
    data = client.post("/api/chart", json=base_payload()).json()
    requested_ecliptic = swe.FLG_SWIEPH | swe.FLG_SPEED
    requested_equatorial = requested_ecliptic | swe.FLG_EQUATORIAL

    for body in data["astronomical_data"]["bodies"] + data["astronomical_data"]["nodes"]:
        assert body["retflag_ecliptic"] & requested_ecliptic == requested_ecliptic
        assert body["retflag_equatorial"] & requested_equatorial == requested_equatorial
        assert body["retflag_ecliptic"] & swe.FLG_MOSEPH == 0
        assert body["retflag_equatorial"] & swe.FLG_MOSEPH == 0
        assert body["used_full_ephemeris"] is True


def test_j2000_epoch_utc_to_ut1_is_within_documented_dut1_tolerance():
    payload = base_payload()
    payload["datetime"] = {"year": 2000, "month": 1, "day": 1, "hour": 8, "minute": 0, "second": 0}
    time_data = client.post("/api/chart", json=payload).json()["astronomical_data"]["time"]

    # 2000-01-01 12:00 TT is JD 2451545.0 by definition. The input here is
    # 00:00 UTC and is compared to the corresponding UT1 noon boundary only
    # within one second because UTC, UT1 and TT are distinct time scales.
    assert abs(time_data["jd_ut"] - 2451544.5) * 86400.0 < 1.0
    assert time_data["jd_et"] > time_data["jd_ut"]
    assert time_data["delta_t_seconds"] == pytest.approx(
        (time_data["jd_et"] - time_data["jd_ut"]) * 86400.0,
        abs=1e-9,
    )


def test_all_fixed_stars_resolve_and_have_unique_keys():
    from app.config import FIXED_STARS

    keys = [s["key"] for s in FIXED_STARS]
    assert len(keys) == len(set(keys)), "FIXED_STARS 有重複的 key"

    payload = base_payload()
    resp = client.post("/api/chart", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    stars = data["astronomical_data"]["fixed_stars"]
    assert len(stars) == len(FIXED_STARS)
    returned_keys = {s["key"] for s in stars}
    assert returned_keys == set(keys)
    # Menkar 在 sefstars.txt 中與另一顆較暗的同名星共用名稱，
    # 需確認實際解析到的是 alCet（星等 ~2.53）而非 laCet（星等 ~4.7）。
    menkar = next(s for s in stars if s["key"] == "menkar")
    assert menkar["magnitude"] < 3.0


# ---------------------------------------------------------------------------
# DST 邊界：America/New_York 的模糊時刻與不存在時刻
# ---------------------------------------------------------------------------

def test_dst_ambiguous_time_is_flagged():
    payload = base_payload()
    payload["datetime"] = {"year": 2024, "month": 11, "day": 3, "hour": 1, "minute": 30, "second": 0}
    payload["timezone"] = {"mode": "iana", "iana_name": "America/New_York"}

    resp = client.post("/api/chart", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    warning = data["astronomical_data"]["time"]["dst_warning"]
    assert warning is not None
    assert "模糊" in warning

    # fold=0（預設）與 fold=1 應解出相差一小時的不同 UTC
    utc_fold0 = data["astronomical_data"]["time"]["utc_time"]
    payload["timezone"]["fold"] = 1
    data_fold1 = client.post("/api/chart", json=payload).json()
    utc_fold1 = data_fold1["astronomical_data"]["time"]["utc_time"]
    assert utc_fold0 != utc_fold1
    assert "模糊" in data_fold1["astronomical_data"]["time"]["dst_warning"]


def test_dst_nonexistent_time_is_rejected_without_calculating_a_chart():
    payload = base_payload()
    payload["datetime"] = {"year": 2024, "month": 3, "day": 10, "hour": 2, "minute": 30, "second": 0}
    payload["timezone"] = {"mode": "iana", "iana_name": "America/New_York"}

    resp = client.post("/api/chart", json=payload)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "nonexistent_local_time"
    assert "不存在" in detail["message"]
    assert "astronomical_data" not in resp.json()


def test_normal_time_has_no_dst_warning():
    resp = client.post("/api/chart", json=base_payload())
    assert resp.json()["astronomical_data"]["time"]["dst_warning"] is None


def test_cross_day_transition_is_classified_as_nonexistent_not_ambiguous():
    # Pacific/Apia 2011-12-30 整天因跨國際換日線變更而不存在。只比對 (hour,minute) 會誤判成
    # 「模糊」（時分恰好相同），必須比對完整 datetime 才能正確歸類為「不存在」。
    payload = base_payload()
    payload["datetime"] = {"year": 2011, "month": 12, "day": 30, "hour": 10, "minute": 0, "second": 0}
    payload["timezone"] = {"mode": "iana", "iana_name": "Pacific/Apia"}
    resp = client.post("/api/chart", json=payload)
    assert resp.status_code == 422
    message = resp.json()["detail"]["message"]
    assert "不存在" in message
    assert "模糊" not in message


@pytest.mark.parametrize("fold", [0, 1])
def test_nonexistent_time_is_rejected_for_both_fold_values(fold):
    payload = base_payload()
    payload["datetime"] = {"year": 2024, "month": 3, "day": 10, "hour": 2, "minute": 30, "second": 0}
    payload["timezone"] = {
        "mode": "iana",
        "iana_name": "America/New_York",
        "fold": fold,
    }
    resp = client.post("/api/chart", json=payload)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "nonexistent_local_time"


def test_fold_rejected_with_fixed_offset():
    # 固定偏移沒有 DST，非零 fold 沒有意義，應以 422 明確拒絕而非靜默忽略。
    payload = base_payload()
    payload["timezone"] = {"mode": "fixed_offset", "utc_offset_hours": 8, "fold": 1}
    resp = client.post("/api/chart", json=payload)
    assert resp.status_code == 422


def test_fold_zero_with_fixed_offset_is_accepted():
    payload = base_payload()
    payload["timezone"] = {"mode": "fixed_offset", "utc_offset_hours": 8, "fold": 0}
    resp = client.post("/api/chart", json=payload)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 方位角慣例：北=0°、順時針，而非 Swiss 原始的南=0°、向西
# ---------------------------------------------------------------------------

def test_azimuth_is_north_based_not_south_based():
    # 台北冬至真太陽正午，太陽應接近正南方 -> 北基準應為 ~180°
    payload = base_payload()
    payload["datetime"] = {"year": 2000, "month": 12, "day": 21, "hour": 12, "minute": 1, "second": 0}
    resp = client.post("/api/chart", json=payload)
    sun = resp.json()["astronomical_data"]["bodies"][0]
    assert sun["azimuth"] == pytest.approx(180.0, abs=5.0)
    # raw Swiss 值應接近 0（南基準），兩者應相差 180 度
    assert sun["azimuth_swiss_raw"] == pytest.approx(0.0, abs=5.0)
    assert (sun["azimuth"] - sun["azimuth_swiss_raw"]) % 360 == pytest.approx(180.0, abs=1e-6)


@pytest.mark.parametrize(
    "display_mode",
    [
        {"zodiac": "sidereal", "ayanamsa": "fagan_bradley"},
        {"ecliptic_frame": "j2000"},
        {"nutation": False},
        {
            "zodiac": "sidereal",
            "ayanamsa": "aldebaran_15_tau",
            "ecliptic_frame": "j2000",
            "nutation": False,
        },
    ],
)
def test_horizontal_coordinates_do_not_change_with_display_reference_frame(display_mode):
    """Display reference-frame choices must not rotate the physical local sky."""
    tropical_payload = base_payload()
    tropical = client.post("/api/chart", json=tropical_payload)
    assert tropical.status_code == 200

    display_payload = base_payload()
    display_payload["computation_mode"] = display_mode
    displayed = client.post("/api/chart", json=display_payload)
    assert displayed.status_code == 200

    tropical_data = tropical.json()["astronomical_data"]
    displayed_data = displayed.json()["astronomical_data"]
    for collection in ("bodies", "nodes", "fixed_stars"):
        tropical_by_key = {item["key"]: item for item in tropical_data[collection]}
        displayed_by_key = {item["key"]: item for item in displayed_data[collection]}
        assert tropical_by_key.keys() == displayed_by_key.keys()
        for key, expected in tropical_by_key.items():
            actual = displayed_by_key[key]
            assert actual["azimuth"] == pytest.approx(expected["azimuth"], abs=1e-9)
            assert actual["azimuth_swiss_raw"] == pytest.approx(
                expected["azimuth_swiss_raw"],
                abs=1e-9,
            )
            assert actual["altitude_true"] == pytest.approx(expected["altitude_true"], abs=1e-9)
            assert actual["altitude_apparent"] == pytest.approx(
                expected["altitude_apparent"],
                abs=1e-9,
            )

    # Ensure the test actually changed the requested display coordinate contract.
    assert displayed_data["bodies"][0]["longitude"] != pytest.approx(
        tropical_data["bodies"][0]["longitude"],
        abs=1e-6,
    )


def test_sidereal_zodiac_does_not_change_equatorial_coordinates():
    tropical = client.post("/api/chart", json=base_payload()).json()["astronomical_data"]
    sidereal_payload = base_payload()
    sidereal_payload["computation_mode"] = {
        "zodiac": "sidereal",
        "ayanamsa": "fagan_bradley",
    }
    sidereal = client.post("/api/chart", json=sidereal_payload).json()["astronomical_data"]

    for collection in ("bodies", "nodes", "fixed_stars"):
        expected_by_key = {item["key"]: item for item in tropical[collection]}
        actual_by_key = {item["key"]: item for item in sidereal[collection]}
        for key, expected in expected_by_key.items():
            actual = actual_by_key[key]
            assert actual["right_ascension"] == pytest.approx(
                expected["right_ascension"],
                abs=1e-9,
            )
            assert actual["declination"] == pytest.approx(
                expected["declination"],
                abs=1e-9,
            )


@pytest.mark.parametrize(
    "ayanamsa",
    [
        "fagan_bradley",
        "hipparchos",
        "sassanian",
        "aldebaran_15_tau",
    ],
)
def test_sidereal_whole_sign_cusps_use_sidereal_sign_boundaries(ayanamsa):
    payload = base_payload()
    payload["options"]["house_system"] = "W"
    payload["computation_mode"] = {
        "zodiac": "sidereal",
        "ayanamsa": ayanamsa,
    }

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 200
    data = response.json()
    asc = data["astronomical_data"]["angles"]["asc"]
    cusps = data["derived_methods"]["house_division"]["cusps"]
    assert len(cusps) == 12
    assert cusps[0] == pytest.approx((asc // 30.0) * 30.0, abs=1e-10)
    for cusp in cusps:
        assert cusp % 30.0 == pytest.approx(0.0, abs=1e-10)


@pytest.mark.parametrize("house_system", ["B", "R", "P"])
def test_sidereal_non_whole_sign_houses_keep_distinct_non_integer_cusps(
    house_system,
):
    payload = base_payload()
    payload["options"]["house_system"] = house_system
    payload["computation_mode"] = {
        "zodiac": "sidereal",
        "ayanamsa": "fagan_bradley",
    }

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 200
    cusps = response.json()["derived_methods"]["house_division"]["cusps"]
    assert len(cusps) == 12
    assert any(abs(cusp % 30.0) > 1e-6 for cusp in cusps)


def test_tropical_whole_sign_baseline_remains_unchanged():
    response = client.post("/api/chart", json=base_payload())

    assert response.status_code == 200
    data = response.json()
    asc = data["astronomical_data"]["angles"]["asc"]
    cusps = data["derived_methods"]["house_division"]["cusps"]
    assert asc == pytest.approx(15.877185639521919, abs=1e-9)
    assert cusps == pytest.approx(
        [float(degree) for degree in range(0, 360, 30)],
        abs=1e-10,
    )


def test_horizontal_source_retflag_is_included_in_ephemeris_provenance():
    payload = base_payload()
    payload["computation_mode"] = {
        "zodiac": "sidereal",
        "ecliptic_frame": "j2000",
        "nutation": False,
    }
    data = client.post("/api/chart", json=payload).json()["astronomical_data"]

    for collection in ("bodies", "nodes", "fixed_stars"):
        for item in data[collection]:
            assert item["retflag_horizontal_source"] & swe.FLG_SWIEPH
            assert not item["retflag_horizontal_source"] & swe.FLG_MOSEPH
            assert item["used_full_ephemeris"] is True


def test_atmosphere_parameters_only_change_refracted_apparent_altitude():
    default_payload = base_payload()
    default = client.post("/api/chart", json=default_payload).json()

    explicit_payload = base_payload()
    explicit_payload["atmosphere"] = {"pressure_hpa": 1013.25, "temperature_c": 25.0}
    explicit = client.post("/api/chart", json=explicit_payload).json()

    metadata = explicit["astronomical_data"]["atmosphere"]
    assert metadata == {
        "pressure_hpa": 1013.25,
        "pressure_mode": "user_supplied",
        "temperature_c": 25.0,
        "refraction": "swiss_ephemeris_standard_model",
        "applies_to": "altitude_apparent",
    }

    default_sun = default["astronomical_data"]["bodies"][0]
    explicit_sun = explicit["astronomical_data"]["bodies"][0]
    assert explicit_sun["altitude_true"] == pytest.approx(default_sun["altitude_true"], abs=1e-10)
    assert explicit_sun["altitude_apparent"] != pytest.approx(
        default_sun["altitude_apparent"],
        abs=1e-6,
    )


def test_zero_pressure_is_rejected_instead_of_mislabelled_as_user_supplied():
    payload = base_payload()
    payload["atmosphere"] = {"pressure_hpa": 0}

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 422


def test_lunar_phase_events_match_usno_and_prenatal_syzygy_is_nearest_previous_lunation():
    payload = base_payload()
    payload["options"]["include_lunar_phases"] = True
    response = client.post("/api/chart", json=payload)
    assert response.status_code == 200
    lunar = response.json()["astronomical_data"]["lunar_events"]

    fixture = json.loads((FIXTURE_DIR / "usno_lunar_phases_1999_12.json").read_text())
    expected_events = fixture["events_around_2000_01_01T04_00_00Z"]
    for phase_key, expected_pair in expected_events.items():
        for direction in ("previous", "next"):
            actual = dt.datetime.fromisoformat(
                lunar["primary_phases"][phase_key][direction]["utc_time"].replace("Z", "+00:00")
            )
            expected = dt.datetime.fromisoformat(expected_pair[direction].replace("Z", "+00:00"))
            assert abs((actual - expected).total_seconds()) < 120
            assert abs(
                lunar["primary_phases"][phase_key][direction]["angular_residual_degrees"]
            ) < 1e-7

    syzygy = lunar["prenatal_syzygy"]
    assert syzygy["phase"] == "full_moon"
    assert syzygy["utc_time"].startswith("1999-12-22T17:3")
    assert syzygy["definition"] == "nearest_previous_geocentric_new_or_full_moon"


def test_previous_solar_and_lunar_eclipses_are_swiss_events_not_method_judgments():
    payload = base_payload()
    payload["options"]["include_eclipses"] = True
    response = client.post("/api/chart", json=payload)
    assert response.status_code == 200
    eclipses = response.json()["astronomical_data"]["lunar_events"]["eclipses"]

    assert eclipses["previous_solar"]["utc_time_maximum"].startswith("1999-08-11T")
    assert eclipses["previous_solar"]["type"] == "total"
    assert eclipses["previous_lunar"]["utc_time_maximum"].startswith("1999-07-28T")
    assert eclipses["previous_lunar"]["type"] == "partial"
    assert eclipses["interpretation"] is None
    for event_key in ("previous_solar", "previous_lunar"):
        event = eclipses[event_key]
        assert event["ephemeris_source"] == "Swiss Ephemeris files"
        assert event["retflag_sun"] & swe.FLG_SWIEPH
        assert event["retflag_moon"] & swe.FLG_SWIEPH
        assert not event["retflag_sun"] & swe.FLG_MOSEPH
        assert not event["retflag_moon"] & swe.FLG_MOSEPH


def test_lunar_phase_coverage_limit_keeps_core_chart_and_names_unavailable_module():
    payload = base_payload()
    payload["datetime"] = {
        "year": 2399,
        "month": 12,
        "day": 31,
        "hour": 12,
        "minute": 0,
        "second": 0,
    }
    payload["options"]["include_lunar_phases"] = True

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["astronomical_data"]["bodies"]
    assert data["astronomical_data"]["lunar_events"] == {}
    dossier = data["calculation_dossier"]
    assert (
        dossier["calculation_policy"]["modules"]["lunar_phases"]
        == "not_applicable"
    )
    availability = dossier["provenance"]["event_modules"]["lunar_phases"]
    assert availability == {
        "requested": True,
        "status": "not_applicable",
        "reason_code": "full_ephemeris_unavailable_for_search_window",
        "computed": False,
        "source_policy": (
            "Swiss files required; fail closed on Moshier fallback"
        ),
        "detail_path": "astronomical_data.lunar_events",
    }
    assert "module_not_applicable" in {
        item["code"] for item in dossier["warnings"]
    }


def test_lunar_phase_coverage_failure_does_not_hide_other_event_modules():
    payload = base_payload()
    payload["datetime"] = {
        "year": 2399,
        "month": 12,
        "day": 31,
        "hour": 12,
        "minute": 0,
        "second": 0,
    }
    payload["options"]["include_lunar_phases"] = True
    payload["options"]["include_eclipses"] = True

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["astronomical_data"]["lunar_events"]["eclipses"]
    modules = data["calculation_dossier"]["calculation_policy"]["modules"]
    assert modules["lunar_phases"] == "not_applicable"
    assert modules["eclipses"] == "computed"


def test_lunar_phase_search_near_upper_boundary_still_uses_swiss_files():
    payload = base_payload()
    payload["datetime"] = {
        "year": 2399,
        "month": 11,
        "day": 1,
        "hour": 12,
        "minute": 0,
        "second": 0,
    }
    payload["options"]["include_lunar_phases"] = True

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 200
    events = response.json()["astronomical_data"]["lunar_events"]["primary_phases"]
    for pair in events.values():
        for event in pair.values():
            assert event["ephemeris_source"] == "Swiss Ephemeris files"
            assert event["retflag_moon"] & swe.FLG_SWIEPH
            assert event["retflag_sun"] & swe.FLG_SWIEPH
            assert event["retflag_moon"] & swe.FLG_MOSEPH == 0
            assert event["retflag_sun"] & swe.FLG_MOSEPH == 0


def test_rise_set_and_both_transits_bracket_birth_time_for_classical_bodies():
    payload = base_payload()
    payload["options"]["include_fixed_stars"] = False
    payload["options"]["include_rise_set_transits"] = True
    response = client.post("/api/chart", json=payload)
    assert response.status_code == 200
    horizon = response.json()["astronomical_data"]["horizon_events"]

    assert horizon["contract"]["disc_position"] == "upper_limb"
    assert horizon["contract"]["refraction"] == "enabled"
    assert horizon["contract"]["pressure_mode"] == "swiss_estimate_from_altitude"
    assert horizon["contract"]["ephemeris_source"] == {
        "requested": "Swiss Ephemeris files",
        "requested_flag": swe.FLG_SWIEPH,
        "actual_source_verified": False,
        "evidence": "requested_flag_only_rise_trans_returns_no_ephemeris_retflag",
    }
    assert len(horizon["bodies"]) == 7

    reference_jd = response.json()["astronomical_data"]["time"]["jd_ut"]
    for body in horizon["bodies"]:
        for event_name in ("rise", "set", "upper_transit", "lower_transit"):
            event = body["events"][event_name]
            assert event["status"] == "found"
            assert event["previous"]["jd_ut"] < reference_jd
            assert event["next"]["jd_ut"] > reference_jd
            assert event["previous"]["utc_time"].endswith("Z")
            assert event["next"]["utc_time"].endswith("Z")


def test_sun_and_moon_rise_set_transit_match_usno_one_day_table():
    payload = base_payload()
    payload["options"]["include_fixed_stars"] = False
    payload["options"]["include_rise_set_transits"] = True
    horizon = client.post("/api/chart", json=payload).json()["astronomical_data"]["horizon_events"]
    by_key = {body["key"]: body for body in horizon["bodies"]}
    fixture = json.loads((FIXTURE_DIR / "usno_rise_set_taipei_2000_01_01.json").read_text())

    expected_paths = {
        ("sun", "rise"): "previous",
        ("sun", "upper_transit"): "previous",
        ("sun", "set"): "next",
        ("moon", "rise"): "previous",
        ("moon", "upper_transit"): "previous",
        ("moon", "set"): "next",
    }
    for (body_key, event_key), direction in expected_paths.items():
        actual_utc = dt.datetime.fromisoformat(
            by_key[body_key]["events"][event_key][direction]["utc_time"].replace("Z", "+00:00")
        )
        actual_local = actual_utc.astimezone(dt.timezone(dt.timedelta(hours=8)))
        expected_local = dt.datetime.fromisoformat(
            fixture["local_events"][body_key][event_key]
        )
        assert abs((actual_local - expected_local).total_seconds()) < 120


@pytest.mark.parametrize(
    ("month", "day", "expected_visibility"),
    [
        (6, 21, "always_above_horizon"),
        (12, 21, "never_rises"),
    ],
)
def test_polar_sun_has_explicit_visibility_status_instead_of_fake_time(
    month,
    day,
    expected_visibility,
):
    payload = base_payload()
    payload["datetime"] = {"year": 2000, "month": month, "day": day, "hour": 12, "minute": 0, "second": 0}
    payload["timezone"] = {"mode": "fixed_offset", "utc_offset_hours": 0}
    payload["location"] = {"latitude": 80.0, "longitude": 0.0, "altitude_m": 0}
    payload["options"]["include_fixed_stars"] = False
    payload["options"]["include_rise_set_transits"] = True
    horizon = client.post("/api/chart", json=payload).json()["astronomical_data"]["horizon_events"]
    sun = next(body for body in horizon["bodies"] if body["key"] == "sun")

    assert sun["events"]["rise"]["status"] == "no_event"
    assert sun["events"]["set"]["status"] == "no_event"
    assert sun["visibility"] == expected_visibility
    assert sun["events"]["rise"]["previous"] is None
    assert sun["events"]["rise"]["next"] is None


def test_polar_moon_visibility_never_contradicts_a_found_rise_event():
    payload = base_payload()
    payload["datetime"] = {
        "year": 2000,
        "month": 7,
        "day": 1,
        "hour": 12,
        "minute": 0,
        "second": 0,
    }
    payload["timezone"] = {"mode": "fixed_offset", "utc_offset_hours": 0}
    payload["location"] = {"latitude": 68.0, "longitude": 0.0, "altitude_m": 0}
    payload["options"]["include_fixed_stars"] = False
    payload["options"]["include_rise_set_transits"] = True

    response = client.post("/api/chart", json=payload)
    assert response.status_code == 200
    horizon = response.json()["astronomical_data"]["horizon_events"]
    moon = next(body for body in horizon["bodies"] if body["key"] == "moon")

    assert moon["events"]["rise"]["status"] == "found"
    assert moon["visibility"] == "indeterminate_near_horizon"
    assert moon["visibility_evidence"]["coordinate_origin"] == "topocentric"
    assert moon["visibility_evidence"]["disc_position"] == "upper_limb"


def test_horizon_events_declare_observer_frame_independent_of_display_center():
    payload = base_payload()
    payload["computation_mode"] = {"center": "heliocentric"}
    payload["options"]["include_fixed_stars"] = False
    payload["options"]["include_rise_set_transits"] = True

    response = client.post("/api/chart", json=payload)
    assert response.status_code == 200
    contract = response.json()["astronomical_data"]["horizon_events"]["contract"]
    assert contract["coordinate_origin"] == "topocentric_observer"
    assert contract["display_center_independent"] is True


# ---------------------------------------------------------------------------
# DMS/HMS 進位：不應出現 "60" 這種非法值
# ---------------------------------------------------------------------------

def test_dms_carries_correctly_at_boundary():
    assert to_dms(29.999999999999996) == "30°00'00.00\""
    assert "60" not in to_dms(29.999999999999996).split("'")[1]


def test_dms_wraps_at_360_for_longitudes():
    # 黃經接近 360° 時，wrap_360=True 應進位回 0°00'00" 而非不存在的 360°00'00"
    assert to_dms(359.9999999, wrap_360=True) == "0°00'00.00\""
    # 未開 wrap_360（緯度/赤緯這類有界量）不受影響，仍照常進位
    assert to_dms(359.9999999) == "360°00'00.00\""


def test_dms_does_not_emit_negative_zero():
    # 極小負值進位後歸零，不應冠上負號變成沒有方向意義的 "-0°00'00.00""
    assert to_dms(-0.0000001, signed=True) == "+0°00'00.00\""
    assert to_dms(-0.0000001) == "0°00'00.00\""


def test_body_longitude_dms_wraps_near_360():
    # 端對端：找一顆黃經極接近 360° 的星體，確認 API 回傳的 longitude_dms 不是 360°
    # （直接用格式化函式驗證行為即可，這裡確認欄位串接正確）
    data = client.post("/api/chart", json=base_payload()).json()
    for body in data["astronomical_data"]["bodies"]:
        if body["longitude_dms"]:
            assert not body["longitude_dms"].startswith("360°")


def test_hms_carries_correctly_at_boundary():
    assert to_hms(23.9999999999999) == "01h36m00.00s"


# ---------------------------------------------------------------------------
# heliocentric/barycentric 下的 null 處理（不可用 0 冒充）
# ---------------------------------------------------------------------------

def test_heliocentric_sun_and_horizon_dependent_fields_are_null():
    payload = base_payload()
    payload["computation_mode"] = {"center": "heliocentric"}
    resp = client.post("/api/chart", json=payload)
    data = resp.json()

    sun = data["astronomical_data"]["bodies"][0]
    assert sun["longitude"] is None
    assert sun["azimuth"] is None

    assert data["derived_methods"]["sect"]["is_day"] is None
    assert data["derived_methods"]["lots"]["fortune"] is None
    assert data["derived_methods"]["void_of_course"]["is_void_of_course"] is None
    assert data["derived_methods"]["void_of_course"]["method"] is not None  # 不可為 None，見先前 appendChild bug


def test_barycentric_sun_is_real_not_degenerate():
    # 太陽的 barycentric 位置是真實的（相對太陽系質心的小幅擺動），跟 heliocentric 的
    # "無法繞自己公轉" 退化情況不同，不應被誤判為 null。
    payload = base_payload()
    payload["computation_mode"] = {"center": "barycentric"}
    resp = client.post("/api/chart", json=payload)
    sun = resp.json()["astronomical_data"]["bodies"][0]
    assert sun["longitude"] is not None
    assert sun["azimuth"] is None  # 地平座標仍無意義


# ---------------------------------------------------------------------------
# VOC 不應被外行星污染
# ---------------------------------------------------------------------------

def test_voc_not_contaminated_by_outer_planets():
    payload = base_payload()
    payload["datetime"] = {"year": 2000, "month": 1, "day": 13, "hour": 6, "minute": 0, "second": 0}

    payload["options"] = {"include_outer_planets": False, "include_void_of_course": True}
    without_outer = client.post("/api/chart", json=payload).json()["derived_methods"]["void_of_course"]

    payload["options"] = {"include_outer_planets": True, "include_void_of_course": True}
    with_outer = client.post("/api/chart", json=payload).json()["derived_methods"]["void_of_course"]

    assert without_outer["next_completing_aspect"] == with_outer["next_completing_aspect"]


# ---------------------------------------------------------------------------
# Sidereal antiscia：鏡射軸需扣掉 2x ayanamsa 才能對齊真實至點軸
# ---------------------------------------------------------------------------

def test_sidereal_antiscia_matches_tropical_after_converting_back():
    # 容許誤差刻意設為 0.02°(=72 角秒)，遠大於單純 float 誤差：get_ayanamsa_ut() 是相對
    # 平均春分點的傳統定義，calc_ut(FLG_SIDEREAL) 預設是相對真春分點（章動=on），兩者換算
    # 回來後會有一個章動量級(通常數角秒到二十角秒內)的殘差，見 ComputationContext.ayanamsa_value()
    # 的說明，這不是本測試要抓的目標（那是 2x ayanamsa 鏡射軸公式本身是否正確），
    # 容許誤差需蓋過此已知、可解釋的量級，否則會誤判成迴歸失敗。
    tropical = client.post("/api/chart", json=base_payload()).json()
    tropical_antiscia = tropical["derived_geometry"]["antiscia"]["antiscia"][0]["longitude"]

    payload = base_payload()
    payload["computation_mode"] = {"zodiac": "sidereal", "ayanamsa": "fagan_bradley"}
    sidereal = client.post("/api/chart", json=payload).json()
    sidereal_antiscia = sidereal["derived_geometry"]["antiscia"]["antiscia"][0]["longitude"]
    ayanamsa = sidereal["astronomical_data"]["time"]["ayanamsa"]

    converted_back = (sidereal_antiscia + ayanamsa) % 360
    assert converted_back == pytest.approx(tropical_antiscia, abs=0.02)


# ---------------------------------------------------------------------------
# 版本回報：兩個版本欄位必須是獨立的值
# ---------------------------------------------------------------------------

def test_library_info_reports_two_independently_sourced_versions():
    # 這個測試守的是「兩個欄位曾被設成同一個 swe.version」這個已修 bug。若只斷言非 None，
    # 那個 bug 會照樣綠燈——因為 swe.version 也非 None。所以直接比對各自的真實來源：
    # distribution 版本必須等於 importlib.metadata 查到的值，library 版本必須等於 swe.version，
    # 且兩者在目前環境下實際不同（2.10.3.2 vs 2.10.03）。
    import importlib.metadata
    import swisseph as swe

    data = client.post("/api/chart", json=base_payload()).json()
    info = data["library_info"]
    assert info["pyswisseph_distribution_version"] == importlib.metadata.version("pyswisseph")
    assert info["swiss_ephemeris_library_version"] == swe.version
    assert info["pyswisseph_distribution_version"] != info["swiss_ephemeris_library_version"]


def test_provisional_methods_are_explicitly_marked_unadopted():
    data = client.post("/api/chart", json=base_payload()).json()
    methods = data["derived_methods"]
    for key in ("house_division", "sect", "lots", "void_of_course", "declination_aspects"):
        assert methods[key]["method"]
        assert methods[key]["method_status"] == "provisional_pending_method_audit"
        assert methods[key]["method_authority"] is None


def test_security_and_privacy_headers_are_present():
    resp = client.post("/api/chart", json=base_payload())
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in resp.headers["content-security-policy"]


def test_frontend_assets_are_not_cached_across_local_schema_updates():
    for path in ("/zh-TW/", "/zh-TW/calculate.js", "/zh-TW/calculate.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


def test_static_mount_serves_only_runtime_frontend_allowlist():
    for path in (
        "/zh-TW/calculate.js",
        "/zh-TW/client-context.js",
        "/zh-TW/exporters.js",
        "/zh-TW/favicon.svg",
        "/zh-TW/privacy-lifecycle.js",
        "/zh-TW/calculate.css",
    ):
        assert client.get(path).status_code == 200

    for path in (
        "/README.md",
        "/.DS_Store",
        "/tests/exporters.test.cjs",
        "/tests/fixtures/response-compatibility.json",
    ):
        assert client.get(path).status_code == 404


def test_unused_interactive_api_docs_are_disabled_but_openapi_schema_remains_available():
    assert client.get("/docs").status_code == 404
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/chart" in schema.json()["paths"]


# ---------------------------------------------------------------------------
# 輸入驗證：非法值應乾淨地回 422，不應讓底層例外裸奔到 500
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_datetime", [
    {"year": 2000, "month": 2, "day": 30, "hour": 12, "minute": 0, "second": 0},  # 不存在的日期
    {"year": 9999, "month": 1, "day": 1, "hour": 12, "minute": 0, "second": 0},   # 超出星曆檔範圍
])
def test_invalid_datetime_rejected_with_422(bad_datetime):
    payload = base_payload()
    payload["datetime"] = bad_datetime
    resp = client.post("/api/chart", json=payload)
    assert resp.status_code == 422


def test_invalid_timezone_rejected_with_422():
    payload = base_payload()
    payload["timezone"] = {"mode": "iana", "iana_name": "Not/AZone"}
    resp = client.post("/api/chart", json=payload)
    assert resp.status_code == 422


def test_invalid_house_system_rejected_with_422():
    payload = base_payload()
    payload["options"] = {"house_system": "X"}
    resp = client.post("/api/chart", json=payload)
    assert resp.status_code == 422


def test_placidus_at_polar_latitude_is_an_explicit_unavailable_calculation():
    payload = base_payload()
    payload["location"]["latitude"] = 70
    payload["options"] = {"house_system": "P"}

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "house_system_unavailable",
        "message": "Placidus (P) 在緯度 70.0° 無法計算；請改用此緯度可定義的宮位制。",
        "house_system": "P",
        "latitude": 70.0,
    }


def test_degenerate_regiomontanus_cusps_at_geographic_pole_are_rejected():
    payload = base_payload()
    payload["location"]["latitude"] = 90
    payload["options"] = {"house_system": "R"}

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "house_system_unavailable"


@pytest.mark.parametrize("latitude", [-90, 90])
@pytest.mark.parametrize("house_system", ["B", "P", "R", "W"])
def test_undefined_geographic_pole_ascendant_is_rejected_for_every_house_system(
    latitude,
    house_system,
):
    payload = base_payload()
    payload["location"]["latitude"] = latitude
    payload["options"] = {"house_system": house_system}

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "house_system_unavailable"


@pytest.mark.parametrize(
    ("mode_override", "expected_fragment"),
    [
        ({"ecliptic_frame": "j2000"}, "J2000"),
        ({"nutation": False}, "章動"),
    ],
)
def test_house_trace_discloses_body_and_house_reference_frame_mismatch(
    mode_override,
    expected_fragment,
):
    payload = base_payload()
    payload["options"] = {"house_system": "W"}
    payload["computation_mode"] = mode_override

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 200
    reminders = [
        step
        for step in response.json()["calculation_trace"]
        if step["title"] == "宮位與星體參考框架提醒"
    ]
    assert len(reminders) == 1
    assert expected_fragment in reminders[0]["note"]


@pytest.mark.parametrize("house_system", ["B", "W"])
def test_near_pole_defined_houses_remain_available_and_angles_are_normalized(
    house_system,
):
    payload = base_payload()
    payload["location"]["latitude"] = 89.99999
    payload["options"] = {"house_system": house_system}

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 200
    data = response.json()
    houses = data["derived_methods"]["house_division"]
    angles = data["astronomical_data"]["angles"]
    assert 0.0 <= angles["asc"] < 360.0
    assert 0.0 <= angles["mc"] < 360.0
    assert all(0.0 <= cusp < 360.0 for cusp in houses["cusps"])


def test_default_house_frame_does_not_emit_mismatch_warning():
    response = client.post("/api/chart", json=base_payload())

    assert response.status_code == 200
    assert all(
        step["title"] != "宮位與星體參考框架提醒"
        for step in response.json()["calculation_trace"]
    )


def test_whole_sign_remains_available_at_polar_latitude():
    payload = base_payload()
    payload["location"]["latitude"] = 70
    payload["options"] = {"house_system": "W"}

    response = client.post("/api/chart", json=payload)

    assert response.status_code == 200
    assert response.json()["derived_methods"]["house_division"]["system_code"] == "W"


def test_unknown_input_fields_are_rejected_instead_of_silently_ignored():
    payload = base_payload()
    payload["datetime"]["minut"] = 30
    resp = client.post("/api/chart", json=payload)
    assert resp.status_code == 422
    assert any(error["type"] == "extra_forbidden" for error in resp.json()["detail"])


@pytest.mark.parametrize("altitude", [-501, 10001])
def test_implausible_birth_altitudes_are_rejected(altitude):
    payload = base_payload()
    payload["location"]["altitude_m"] = altitude
    assert client.post("/api/chart", json=payload).status_code == 422


@pytest.mark.parametrize("code", ["B", "R", "W", "P"])
def test_every_house_system_computes_successfully(code):
    payload = base_payload()
    payload["options"] = {"house_system": code}
    resp = client.post("/api/chart", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    house_division = data["derived_methods"]["house_division"]
    assert len(house_division["cusps"]) == 12
    assert data["astronomical_data"]["angles"]["asc"] is not None


def test_full_ephemeris_file_check_requires_the_supported_1800_2399_segment(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(ephemeris, "EPHE_DIR", str(tmp_path))
    (tmp_path / "sepl_00.se1").touch()
    (tmp_path / "semo_00.se1").touch()
    assert ephemeris.has_full_ephemeris_files() is False

    (tmp_path / "sepl_18.se1").touch()
    (tmp_path / "semo_18.se1").touch()
    assert ephemeris.has_full_ephemeris_files() is True


def _circular_difference(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


def test_whole_sign_cusps_are_sign_boundaries_containing_the_ascendant():
    data = client.post("/api/chart", json=base_payload()).json()
    asc = data["astronomical_data"]["angles"]["asc"]
    cusps = data["derived_methods"]["house_division"]["cusps"]

    assert all(cusp % 30.0 == pytest.approx(0.0, abs=1e-10) for cusp in cusps)
    assert int(asc // 30.0) == int(cusps[0] // 30.0)
    for left, right in zip(cusps, cusps[1:]):
        assert (right - left) % 360.0 == pytest.approx(30.0, abs=1e-10)


@pytest.mark.parametrize("code", ["B", "R", "P"])
def test_quadrant_house_systems_anchor_first_and_tenth_cusps_to_angles(code):
    payload = base_payload()
    payload["options"] = {"house_system": code}
    data = client.post("/api/chart", json=payload).json()
    angles = data["astronomical_data"]["angles"]
    cusps = data["derived_methods"]["house_division"]["cusps"]

    assert _circular_difference(cusps[0], angles["asc"]) < 1e-8
    assert _circular_difference(cusps[9], angles["mc"]) < 1e-8
    for index in range(6):
        assert _circular_difference(cusps[index], (cusps[index + 6] + 180.0) % 360.0) < 1e-8


# ---------------------------------------------------------------------------
# 並發：topocentric 模式下，兩個相距遙遠的地點同時請求不應互相污染
# （swisseph 的 set_topo 是行程全域狀態，靠 _COMPUTE_LOCK 序列化保護）
# ---------------------------------------------------------------------------

def test_concurrent_requests_do_not_cross_contaminate():
    def make_payload(lon, lat):
        payload = base_payload()
        payload["timezone"] = {"mode": "fixed_offset", "utc_offset_hours": 0}
        payload["location"] = {"latitude": lat, "longitude": lon, "altitude_m": 0}
        payload["computation_mode"] = {"center": "topocentric"}
        return payload

    loc_a = (121.5654, 25.0330)   # 台北
    loc_b = (-74.0060, 40.7128)   # 紐約

    truth_a = client.post("/api/chart", json=make_payload(*loc_a)).json()["astronomical_data"]["bodies"][1]["longitude"]
    truth_b = client.post("/api/chart", json=make_payload(*loc_b)).json()["astronomical_data"]["bodies"][1]["longitude"]
    assert truth_a != pytest.approx(truth_b, abs=1e-4)  # 视差確實造成可觀測差異，測試才有意義

    payloads = [make_payload(*(loc_a if i % 2 == 0 else loc_b)) for i in range(30)]
    expected = [truth_a if i % 2 == 0 else truth_b for i in range(30)]

    def post(p):
        return client.post("/api/chart", json=p).json()["astronomical_data"]["bodies"][1]["longitude"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
        results = list(ex.map(post, payloads))

    for i, (got, exp) in enumerate(zip(results, expected)):
        assert got == pytest.approx(exp, abs=1e-9), f"request #{i} contaminated: got {got}, expected {exp}"


_CHART_BODY = {
    "datetime": {
        "year": 1990, "month": 6, "day": 15,
        "hour": 12, "minute": 0, "second": 0.0,
    },
    "timezone": {"mode": "fixed_offset", "utc_offset_hours": 0.0},
    "location": {"latitude": 25.0, "longitude": 121.5, "altitude_m": 0.0},
}


def _chart_with_label(label):
    body = json.loads(json.dumps(_CHART_BODY))
    body["location"]["place_label"] = label
    return body


@pytest.mark.parametrize(
    "label",
    [
        "a\x00b",
        "Taipei\r\nX: 1",
        "Taipei\nX",
        "\u202eevil",
        "\u200eTaipei",
        "\ufeffTaipei",
        "Taipei\x7f",
        "Tai\u0085pei",
    ],
)
def test_place_label_rejects_control_and_bidi_codepoints(label):
    """`place_label` is the one free-text field the request schema accepts, and it
    is copied verbatim into the Calculation Dossier and therefore into every
    export artifact.  Before this bound every payload here round-tripped
    byte-identically into `location_resolution.place_label`.  A serializer still
    owns its own escaping; these codepoints have no legitimate use in a place
    name, so rejecting them at the boundary benefits every consumer rather than
    only the CSV serializer."""
    client = TestClient(app)
    response = client.post("/api/chart", json=_chart_with_label(label))
    assert response.status_code == 422, response.text
    # The validator's own message must not embed the submitted label.  Pydantic
    # additionally echoes "input" in the local profile; that echo is suppressed
    # by the hosted response sanitizer, which is asserted separately below.
    assert all(
        label not in issue.get("msg", "")
        for issue in response.json()["detail"]
    )

    hosted = TestClient(
        create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
    )
    hosted_response = hosted.post("/api/chart", json=_chart_with_label(label))
    assert hosted_response.status_code == 422
    assert "\u202e" not in hosted_response.text
    assert "\x00" not in hosted_response.text


@pytest.mark.parametrize(
    "label",
    [
        "Taipei",
        "臺北市",
        "O'Brien",
        "São Paulo",
        "Stoke-on-Trent",
        "Xi'an, Shaanxi",
        "A" * 200,
    ],
)
def test_place_label_still_accepts_ordinary_place_names(label):
    """Positive control for the rejection above: the bound must not reject a
    realistic label, including apostrophes, accents, hyphens, CJK and the exact
    maximum length."""
    client = TestClient(app)
    response = client.post("/api/chart", json=_chart_with_label(label))
    assert response.status_code == 200, response.text
    receipt = response.json()["calculation_dossier"]["location_resolution"]
    assert receipt["place_label"] == label


def test_invalid_timezone_key_is_rejected_without_echoing_the_submitted_value():
    """`ZoneInfo` raises ValueError, not ZoneInfoNotFoundError, for an absolute
    or `..`-containing key, so the previous handler failed closed only because
    Pydantic converted the escaped ValueError.  The message must also not embed
    the submitted value: input echo must not depend on the hosted profile's
    response sanitizer."""
    client = TestClient(app)
    for key in (
        "Not/AZone_MARKERONE",
        "../../etc/passwd_MARKERTWO",
        "/absolute_MARKERTHREE",
    ):
        body = json.loads(json.dumps(_CHART_BODY))
        body["timezone"] = {"mode": "iana", "iana_name": key}
        response = client.post("/api/chart", json=body)
        assert response.status_code == 422, response.text
        assert all(
            "MARKER" not in issue.get("msg", "")
            for issue in response.json()["detail"]
        ), "validator message echoes the submitted timezone key: " + key

        hosted = TestClient(
            create_app(AppSettings(profile=AppProfile.PRIVATE_ALPHA))
        )
        hosted_response = hosted.post("/api/chart", json=body)
        assert hosted_response.status_code == 422
        assert "MARKER" not in hosted_response.text
