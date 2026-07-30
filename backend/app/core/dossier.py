"""Backend-authored, versioned receipt for one chart calculation.

The dossier records validated inputs, effective policy, runtime provenance, and
structured warnings.  It deliberately does not duplicate the calculated chart
tables or the full trace: those remain canonical in their existing response
paths.
"""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os

import swisseph as swe

from ..config import PRODUCT_YEAR_RANGE
from ..ephemeris import EPHE_DIR


DOSSIER_VERSION = "0.3.2"

_EPHEMERIS_DATASET_LINEAGE = {
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

_EPHEMERIS_FILE_ROLES = {
    "sepl_18.se1": "major_planet_segment_1800_2399",
    "semo_18.se1": "moon_segment_1800_2399",
    "seas_18.se1": "asteroid_segment_not_used_by_current_product",
    "sefstars.txt": "fixed_star_catalog",
    "seorbel.txt": "auxiliary_orbital_elements",
}

_FLAG_NAMES = (
    ("FLG_JPLEPH", swe.FLG_JPLEPH),
    ("FLG_SWIEPH", swe.FLG_SWIEPH),
    ("FLG_MOSEPH", swe.FLG_MOSEPH),
    ("FLG_HELCTR", swe.FLG_HELCTR),
    ("FLG_TRUEPOS", swe.FLG_TRUEPOS),
    ("FLG_J2000", swe.FLG_J2000),
    ("FLG_NONUT", swe.FLG_NONUT),
    ("FLG_SPEED", swe.FLG_SPEED),
    ("FLG_EQUATORIAL", swe.FLG_EQUATORIAL),
    ("FLG_XYZ", swe.FLG_XYZ),
    ("FLG_RADIANS", swe.FLG_RADIANS),
    ("FLG_BARYCTR", swe.FLG_BARYCTR),
    ("FLG_TOPOCTR", swe.FLG_TOPOCTR),
    ("FLG_SIDEREAL", swe.FLG_SIDEREAL),
)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def _available_input_files() -> tuple[dict, ...]:
    """Hash the small bundled input set once without claiming each file was opened."""

    entries = []
    for filename, role in _EPHEMERIS_FILE_ROLES.items():
        path = os.path.join(EPHE_DIR, filename)
        exists = os.path.isfile(path)
        entries.append(
            {
                "filename": filename,
                "role": role,
                "exists": exists,
                "size_bytes": os.path.getsize(path) if exists else None,
                "sha256": _sha256(path) if exists else None,
            }
        )
    return tuple(entries)


def _retflag_receipt(value: int | None) -> dict:
    if value is None:
        return {"value": None, "names": []}
    return {
        "value": value,
        "names": [
            name
            for name, bit in _FLAG_NAMES
            if value & bit
        ],
    }


def _object_provenance(item: dict) -> dict:
    if item.get("error"):
        result_status = "failed"
    elif item.get("longitude") is None:
        result_status = "not_applicable"
    else:
        result_status = "available"

    return {
        "key": item["key"],
        "name": item["name"],
        "result_status": result_status,
        "used_full_ephemeris": item.get("used_full_ephemeris"),
        "retflags": {
            "ecliptic": _retflag_receipt(item.get("retflag_ecliptic")),
            "equatorial": _retflag_receipt(item.get("retflag_equatorial")),
            "horizontal_source": _retflag_receipt(
                item.get("retflag_horizontal_source")
            ),
        },
    }


def _module_status(
    options,
    derived_methods: dict,
    event_module_availability: dict,
) -> dict:
    sect = derived_methods["sect"]
    lots = derived_methods["lots"]
    void_of_course = derived_methods["void_of_course"]
    planet_in_house = derived_methods["planet_in_house"]

    if sect is None:
        sect_status = "not_requested"
    elif sect.get("is_day") is None:
        sect_status = "not_applicable"
    else:
        sect_status = "computed"

    if not options.include_lots:
        lots_status = "not_requested"
    elif not lots or lots.get("fortune") is None or lots.get("spirit") is None:
        lots_status = "not_applicable"
    else:
        lots_status = "computed"

    if not options.include_void_of_course:
        void_of_course_status = "not_requested"
    elif (
        not void_of_course
        or void_of_course.get("is_void_of_course") is None
    ):
        void_of_course_status = "not_applicable"
    else:
        void_of_course_status = "computed"

    return {
        "core_positions": "computed",
        "house_division": "computed",
        "planet_in_house": planet_in_house["execution_status"],
        "fixed_stars": (
            "computed" if options.include_fixed_stars else "not_requested"
        ),
        "outer_planets": (
            "computed" if options.include_outer_planets else "not_requested"
        ),
        "antiscia": (
            "computed" if options.include_antiscia else "not_requested"
        ),
        # Sect reflects actual execution rather than merely echoing the request.
        # Lots requires Sect; VOC is independent and must not request it.
        "sect": sect_status,
        "lots": lots_status,
        "void_of_course": void_of_course_status,
        "declination_aspects": (
            "computed"
            if options.include_declination_aspects
            else "not_requested"
        ),
        "lunar_phases": event_module_availability["lunar_phases"]["status"],
        "eclipses": (
            "computed" if options.include_eclipses else "not_requested"
        ),
        "rise_set_transits": (
            "computed"
            if options.include_rise_set_transits
            else "not_requested"
        ),
    }


def _methodology_receipt(derived_methods: dict, modules: dict) -> dict:
    items = {}
    for name in (
        "house_division",
        "planet_in_house",
        "sect",
        "lots",
        "void_of_course",
        "declination_aspects",
    ):
        value = derived_methods[name]
        execution_status = modules[name]
        has_method_receipt = isinstance(value, dict) and bool(value)
        items[name] = {
            "execution_status": execution_status,
            "computed": execution_status == "computed",
            "method": value.get("method") if has_method_receipt else None,
            "method_status": (
                value.get("method_status") if has_method_receipt else None
            ),
            "method_authority": (
                value.get("method_authority") if has_method_receipt else None
            ),
            "response_path": f"derived_methods.{name}",
        }
    return {
        "policy": (
            "record_existing_status_only_no_method_authority_is_added_by_dossier"
        ),
        "items": items,
    }


def _warning(
    code: str,
    severity: str,
    message: str,
    source: str,
    affected_paths: list[str],
) -> dict:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "source": source,
        "affected_paths": affected_paths,
    }


def _structured_warnings(
    *,
    request,
    time_conversion: dict,
    full_ephemeris_files_available: bool,
    core_records: list[dict],
    fixed_star_records: list[dict],
    fixed_stars: list[dict],
    modules: dict,
    methodology: dict,
    event_module_availability: dict,
) -> list[dict]:
    warnings = []

    if time_conversion["dst_warning"]:
        warnings.append(
            _warning(
                "ambiguous_local_time",
                "warning",
                time_conversion["dst_warning"],
                "time_conversion",
                [
                    "calculation_dossier.time_conversion",
                    "astronomical_data.time",
                ],
            )
        )

    if request.datetime.year in PRODUCT_YEAR_RANGE:
        warnings.append(
            _warning(
                "ephemeris_boundary_year",
                "warning",
                (
                    "此日期位於目前產品 "
                    f"{PRODUCT_YEAR_RANGE[0]}–{PRODUCT_YEAR_RANGE[1]} "
                    "支援範圍的邊界年份。"
                    "核心結果在實際星曆來源可用時仍會顯示；需要向支援範圍外"
                    "搜尋的事件模組可能明確標為不可用。"
                ),
                "supported_year_range_policy",
                [
                    "calculation_dossier.time_conversion.calendar",
                    "calculation_dossier.provenance.event_modules",
                ],
            )
        )

    if not full_ephemeris_files_available:
        warnings.append(
            _warning(
                "full_ephemeris_input_files_incomplete",
                "warning",
                "啟動時未偵測到完整的主要行星與月球 Swiss Ephemeris 檔案。",
                "engine_startup_check",
                [
                    "calculation_dossier.engine.available_input_files",
                    "calculation_dossier.provenance",
                ],
            )
        )

    fallback_core = [
        record["key"]
        for record in core_records
        if record["used_full_ephemeris"] is False
    ]
    fallback_stars = [
        record["key"]
        for record in fixed_star_records
        if record["used_full_ephemeris"] is False
    ]
    if fallback_core or fallback_stars:
        warnings.append(
            _warning(
                "ephemeris_fallback_detected",
                "warning",
                "部分物件未使用完整 Swiss Ephemeris files；請依 retflag 明細核對。",
                "actual_retflags",
                [
                    "calculation_dossier.provenance.core_objects",
                    "calculation_dossier.provenance.fixed_stars",
                ],
            )
        )

    failed_stars = [
        star["key"]
        for star in fixed_stars
        if star.get("error")
    ]
    if failed_stars:
        warnings.append(
            _warning(
                "fixed_star_query_failed",
                "warning",
                f"固定星查詢失敗：{', '.join(failed_stars)}",
                "fixed_star_results",
                ["astronomical_data.fixed_stars"],
            )
        )

    event_not_applicable_modules = [
        name
        for name, availability in event_module_availability.items()
        if availability["status"] == "not_applicable"
    ]
    if event_not_applicable_modules:
        warnings.append(
            _warning(
                "module_not_applicable",
                "notice",
                (
                    "核心命盤仍可計算，但下列事件模組超出其完整資料"
                    "搜尋範圍，因此未產生結果："
                    f"{', '.join(event_not_applicable_modules)}"
                ),
                "provenance.event_modules",
                [
                    "calculation_dossier.calculation_policy.modules",
                    "calculation_dossier.provenance.event_modules",
                ],
            )
        )

    not_applicable_modules = [
        name
        for name, status in modules.items()
        if (
            status == "not_applicable"
            and name not in event_not_applicable_modules
        )
    ]
    if not_applicable_modules:
        warnings.append(
            _warning(
                "module_not_applicable_in_mode",
                "notice",
                (
                    "目前計算中心或座標框架不符合這些模組的適用條件，"
                    f"因此未產生可用結果：{', '.join(not_applicable_modules)}"
                ),
                "calculation_policy.modules",
                [
                    "calculation_dossier.calculation_policy.computation_mode.center",
                    "calculation_dossier.calculation_policy.modules",
                    *[
                        f"derived_methods.{name}"
                        for name in not_applicable_modules
                        if name in methodology["items"]
                    ],
                ],
            )
        )

    for name, item in methodology["items"].items():
        if (
            item["computed"]
            and item["method_status"] == "provisional_pending_method_audit"
        ):
            warnings.append(
                _warning(
                    "provisional_method_result",
                    "notice",
                    f"{name} 已計算，但方法仍待審閱，不能視為正式採用。",
                    item["response_path"],
                    [item["response_path"]],
                )
            )

    if request.computation_mode.position_mode == "true":
        warnings.append(
            _warning(
                "true_position_refraction_semantics_provisional",
                "notice",
                "true position 與大氣折射同時使用時的產品語意仍待確認。",
                "docs/product/methods/METHOD_AUDIT.md",
                [
                    "calculation_dossier.calculation_policy.computation_mode",
                    "astronomical_data.atmosphere",
                ],
            )
        )

    return warnings


def _trace_receipt(trace_steps: list[dict]) -> dict:
    serialized = json.dumps(
        trace_steps,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "response_path": "calculation_trace",
        "step_count": len(trace_steps),
        "python_json_serialization_sha256": hashlib.sha256(
            serialized
        ).hexdigest(),
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


def _privacy_evidence(
    evidence_type: str,
    reference: str,
) -> dict:
    return {
        "type": evidence_type,
        "reference": reference,
        "semantics": "repository_pointer_not_test_execution_result",
    }


def _privacy_claim(
    *,
    claim_id: str,
    status: str,
    statement: str,
    enforcement_layer: str,
    control_id: str,
    mechanism: str,
    evidence: list[dict],
    applies_to: list[str],
    excludes: list[str],
    limitations: list[str],
) -> dict:
    return {
        "id": claim_id,
        "status": status,
        "statement": statement,
        "enforcement_layer": enforcement_layer,
        "control": {
            "id": control_id,
            "mechanism": mechanism,
        },
        "evidence": evidence,
        "scope": {
            "surface": "current_local_product",
            "applies_to": applies_to,
            "excludes": excludes,
        },
        "limitations": limitations,
    }


def _privacy_attestation() -> dict:
    """Describe implemented controls without claiming per-request revalidation."""

    return {
        "privacy_attestation_version": "1.2.0",
        "attestation_status": "provisional_pending_external_review",
        "contains_sensitive_birth_data": True,
        "anonymous_share_ready": False,
        "evidence_semantics": (
            "repository_test_references_not_execution_attestation"
        ),
        "claims": [
            _privacy_claim(
                claim_id="application_chart_path_no_persistence",
                status="implemented_in_application_layer",
                statement=(
                    "目前 /api/chart application path 不使用出生資料資料庫、"
                    "session store、request cache或background queue，並以"
                    "Python write guard監看目前同步處理路徑。"
                ),
                enforcement_layer="application_request_path",
                control_id="application-no-persistence-current-chart-path-v1",
                mechanism=(
                    "No persistence dependency on the current chart path plus "
                    "Python file-write API regression guards."
                ),
                evidence=[
                    _privacy_evidence(
                        "python_test_reference",
                        (
                            "tests/backend/test_privacy_logging.py::"
                            "test_chart_request_does_not_use_python_file_write_apis"
                        ),
                    ),
                    _privacy_evidence(
                        "static_contract_reference",
                        (
                            "tests/backend/test_privacy_logging.py::"
                            "test_backend_has_no_third_party_telemetry_or_"
                            "persistence_dependency"
                        ),
                    ),
                ],
                applies_to=[
                    "current synchronous FastAPI POST /api/chart path",
                ],
                excludes=[
                    "pyswisseph native or OS side effects",
                    "RAM, swap, crash dump and backup retention",
                    "user-initiated browser clipboard and downloads",
                ],
                limitations=[
                    "No secure memory erasure claim.",
                    (
                        "Python write guards do not intercept every native "
                        "library or operating-system side effect."
                    ),
                ],
            ),
            _privacy_claim(
                claim_id="application_telemetry_allowlist",
                status="implemented_in_application_layer",
                statement=(
                    "Application營運事件由封閉欄位與封閉 vocabulary重建，"
                    "不直接序列化request、response、header或exception。"
                ),
                enforcement_layer="application_event_sink",
                control_id="privacy-request-event-v1",
                mechanism=(
                    "Closed event builder plus sink-side schema validation and "
                    "non-propagating dedicated logger."
                ),
                evidence=[
                    _privacy_evidence(
                        "python_test_reference",
                        (
                            "tests/backend/test_privacy_logging.py::"
                            "test_structured_event_schema_is_closed_and_"
                            "discards_attacker_controlled_text"
                        ),
                    ),
                    _privacy_evidence(
                        "python_test_reference",
                        (
                            "tests/backend/test_privacy_logging.py::"
                            "test_emitter_rejects_direct_unsanitized_event"
                        ),
                    ),
                ],
                applies_to=[
                    "classical_astrology.privacy application event sink",
                ],
                excludes=[
                    "ASGI server access log",
                    "reverse proxy, hosting supervisor and third-party telemetry",
                ],
                limitations=[
                    (
                        "Future logger or telemetry changes require a new "
                        "privacy review and canary test."
                    ),
                ],
            ),
            _privacy_claim(
                claim_id="asgi_exception_data_minimization",
                status="implemented_in_asgi_layer",
                statement=(
                    "目前ASGI application boundary以固定錯誤回應與完整"
                    "response-lifecycle containment避免原始exception進入"
                    "response或Uvicorn traceback。"
                ),
                enforcement_layer="asgi_application_boundary",
                control_id="privacy-asgi-boundary-v1",
                mechanism=(
                    "Low-level ASGI middleware contains pre-start and post-start "
                    "Exception paths and isolates event-sink failures."
                ),
                evidence=[
                    _privacy_evidence(
                        "python_test_reference",
                        (
                            "tests/backend/test_privacy_logging.py::"
                            "test_real_uvicorn_unexpected_error_does_not_emit_"
                            "traceback_or_canary"
                        ),
                    ),
                    _privacy_evidence(
                        "python_test_reference",
                        (
                            "tests/backend/test_privacy_logging.py::"
                            "test_real_uvicorn_post_start_errors_are_contained_"
                            "and_reported"
                        ),
                    ),
                    _privacy_evidence(
                        "python_test_reference",
                        (
                            "tests/backend/test_privacy_logging.py::"
                            "test_real_uvicorn_event_emitter_failure_is_isolated"
                        ),
                    ),
                ],
                applies_to=[
                    "current FastAPI application wrapped by PrivacyBoundaryMiddleware",
                ],
                excludes=[
                    "process-control BaseException and cancellation semantics",
                    "transport failures outside the application boundary",
                    "future process supervisor error capture",
                ],
                limitations=[
                    (
                        "Post-response-start failures cannot rewrite an HTTP "
                        "status already sent on the wire."
                    ),
                ],
            ),
            _privacy_claim(
                claim_id="canonical_launcher_access_log_suppression",
                status="conditional_on_canonical_launcher",
                statement=(
                    "Canonical本機launcher以--no-access-log啟動Uvicorn，"
                    "避免request line與query進入Uvicorn access output。"
                ),
                enforcement_layer="canonical_local_launcher",
                control_id="canonical-uvicorn-no-access-log-v1",
                mechanism=(
                    "scripts/local/run-local.sh passes an explicit --no-access-log "
                    "argument and real-launcher canaries inspect process output."
                ),
                evidence=[
                    _privacy_evidence(
                        "static_contract_reference",
                        (
                            "tests/backend/test_privacy_logging.py::"
                            "test_supported_launcher_disables_uvicorn_access_"
                            "log_and_loads_privacy_boundary"
                        ),
                    ),
                    _privacy_evidence(
                        "python_test_reference",
                        (
                            "tests/backend/test_privacy_logging.py::"
                            "test_real_launcher_logs_are_body_free_for_success_"
                            "rejection_and_malformed_json"
                        ),
                    ),
                ],
                applies_to=[
                    "start.command and scripts/local/run-local.sh canonical local runtime",
                ],
                excludes=[
                    "manual or alternate Uvicorn invocation",
                    "future reverse proxy, CDN, WAF and hosting logs",
                ],
                limitations=[
                    (
                        "Startup and shutdown messages remain visible in the "
                        "local terminal."
                    ),
                ],
            ),
            _privacy_claim(
                claim_id="browser_transient_sensitive_state",
                status="conditional_on_bundled_frontend",
                statement=(
                    "目前browser application不使用persistent storage API，"
                    "並集中失效canonical document、section reference、"
                    "Blob URL與in-flight request。"
                ),
                enforcement_layer="browser_application",
                control_id="browser-sensitive-lifecycle-v1",
                mechanism=(
                    "Central lifecycle controller, request generation checks, "
                    "AbortController, result clear, panic clear and pagehide clear."
                ),
                evidence=[
                    _privacy_evidence(
                        "node_test_reference",
                        "frontend/tests/privacy_lifecycle.test.cjs",
                    ),
                    _privacy_evidence(
                        "static_contract_reference",
                        (
                            "tests/integration/test_frontend_contract.py::"
                            "test_privacy_lifecycle_precedes_handlers_and_"
                            "blocks_browser_persistence"
                        ),
                    ),
                ],
                applies_to=[
                    "current same-origin frontend calculation and export UI",
                ],
                excludes=[
                    "API clients that do not load the bundled frontend",
                    "browser extensions and compromised browser",
                    "browser or OS crash/session restoration",
                    "completed clipboard writes and downloaded files",
                ],
                limitations=[
                    "autocomplete=off is a browser hint, not an enforcement API.",
                    (
                        "JavaScript cannot securely erase runtime or "
                        "operating-system memory."
                    ),
                ],
            ),
        ],
        "uncovered_layers": [
            {
                "layer": "reverse_proxy_cdn_waf",
                "status": "outside_current_control_scope",
                "note": "目前本機產品路徑不存在此層；不提供未來部署保證。",
            },
            {
                "layer": "hosting_supervisor",
                "status": "outside_current_control_scope",
                "note": (
                    "Infomaniak provider已選且identity verification已通過；"
                    "真實VPS尚未部署或驗證，host log與retention仍待deployment canary。"
                ),
            },
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
        ],
    }


def build_calculation_dossier(
    *,
    request,
    effective_request,
    time_conversion: dict,
    context,
    library_info: dict,
    astronomical_data: dict,
    derived_methods: dict,
    trace_steps: list[dict],
    full_ephemeris_files_available: bool,
    build_identity: dict,
    birth_time_sensitivity: dict,
    event_module_availability: dict,
) -> dict:
    """Build the authoritative receipt after all requested calculations finish."""

    core_records = [
        _object_provenance(item)
        for item in (
            astronomical_data["bodies"] + astronomical_data["nodes"]
        )
    ]
    fixed_star_records = [
        _object_provenance(item)
        for item in astronomical_data["fixed_stars"]
    ]
    modules = _module_status(
        request.options,
        derived_methods,
        event_module_availability,
    )
    methodology = _methodology_receipt(derived_methods, modules)
    input_receipt = request.model_dump(mode="json")

    available_files = []
    for immutable_entry in _available_input_files():
        entry = dict(immutable_entry)
        entry["required_for_this_request"] = (
            entry["filename"] in {"sepl_18.se1", "semo_18.se1"}
            or (
                entry["filename"] == "sefstars.txt"
                and request.options.include_fixed_stars
            )
        )
        available_files.append(entry)

    atmosphere = astronomical_data["atmosphere"]
    warnings = _structured_warnings(
        request=request,
        time_conversion=time_conversion,
        full_ephemeris_files_available=full_ephemeris_files_available,
        core_records=core_records,
        fixed_star_records=fixed_star_records,
        fixed_stars=astronomical_data["fixed_stars"],
        modules=modules,
        methodology=methodology,
        event_module_availability=event_module_availability,
    )

    return {
        "dossier_version": DOSSIER_VERSION,
        "status": "provisional",
        "authority": "backend_effective_runtime",
        "build_identity": dict(build_identity),
        "input_receipt": input_receipt,
        "birth_time": {
            "precision": request.birth_time_precision,
            "input_civil_hour": (
                f"{request.datetime.year:04d}-{request.datetime.month:02d}-"
                f"{request.datetime.day:02d} {request.datetime.hour:02d}"
            ),
            "representative_local_time": time_conversion["input_local_time"],
            "representative_policy": (
                "midpoint_minute_30"
                if request.birth_time_precision == "approximate_hour"
                else "exact_input"
            ),
            "sensitivity_status": birth_time_sensitivity["status"],
            "sensitivity_response_path": "birth_time_sensitivity",
        },
        "location_resolution": {
            "place_label": request.location.place_label,
            "location_source": request.location.location_source,
            "source_record_id": request.location.source_record_id,
            "location_precision": request.location.location_precision,
            "resolved_coordinates": {
                "latitude": request.location.latitude,
                "longitude": request.location.longitude,
                "altitude_m": request.location.altitude_m,
            },
            "verification_status": (
                "client_asserted_not_server_verified"
                if request.location.location_source != "manual"
                else "user_supplied"
            ),
        },
        "time_conversion": {
            "calendar": {
                "system": "gregorian",
                "swiss_flag": "GREG_CAL",
                "supported_year_range": list(PRODUCT_YEAR_RANGE),
            },
            "conversion_function": "swe.utc_to_jd",
            "input_local_time": time_conversion["input_local_time"],
            "timezone_mode": request.timezone.mode,
            "timezone_label": time_conversion["timezone_label"],
            "fold": request.timezone.fold,
            "resolved_utc_offset_hours": time_conversion["utc_offset_hours"],
            "utc_iso_8601": time_conversion["utc_time"].replace(" ", "T") + "Z",
            "jd_ut1": time_conversion["jd_ut"],
            "jd_tt": time_conversion["jd_et"],
            "delta_t_seconds": time_conversion["delta_t_seconds"],
            "ecl_nut_retflag": _retflag_receipt(
                time_conversion["ecl_nut_retflag"]
            ),
            "ayanamsa_degrees": time_conversion["ayanamsa"],
            "dst_warning": time_conversion["dst_warning"],
        },
        "calculation_policy": {
            "computation_mode": request.computation_mode.model_dump(mode="json"),
            "normalized_requested_options": request.options.model_dump(
                mode="json"
            ),
            "effective_datetime": effective_request.datetime.model_dump(
                mode="json"
            ),
            "flag_policy": {
                "base_position_flags": _retflag_receipt(context.base_flags),
                "equatorial_source_flags": _retflag_receipt(
                    context.equatorial_source_flags | swe.FLG_EQUATORIAL
                ),
                "horizontal_source_flags": (
                    _retflag_receipt(context.horizontal_source_flags)
                    if context.horizon_meaningful
                    else None
                ),
                "semantics": (
                    "policy flags requested by the App; actual calculation "
                    "sources are proven by returned retflags in provenance"
                ),
            },
            "modules": modules,
            "house_system": {
                "code": request.options.house_system,
                "calculated": True,
                "method_layer": True,
            },
            "atmosphere": atmosphere,
            "coordinate_conventions": {
                "angles": "degrees",
                "right_ascension_numeric": "degrees",
                "sidereal_time": "hours",
                "geographic_latitude": "north_positive",
                "geographic_longitude": "east_positive",
                "azimuth": "north_0_degrees_clockwise_east",
                "azimuth_swiss_raw": "south_0_degrees_clockwise_west",
                "altitudes": {
                    "true": "without_refraction",
                    "apparent": "with_swiss_standard_refraction",
                },
                "not_applicable_value": None,
            },
            "fixed_module_frames": {
                "horizontal_coordinates": (
                    "physical_tropical_of_date"
                    if context.horizon_meaningful
                    else "not_applicable_for_display_center"
                ),
                "houses_and_angles": "earth_observer_frame",
                "lunar_events": (
                    "geocentric_apparent_tropical_of_date"
                    if request.options.include_lunar_phases
                    or request.options.include_eclipses
                    else "not_requested"
                ),
                "rise_set_transits": (
                    "topocentric_observer"
                    if request.options.include_rise_set_transits
                    else "not_requested"
                ),
            },
        },
        "engine": {
            "pyswisseph_distribution_version": library_info[
                "pyswisseph_distribution_version"
            ],
            "swiss_ephemeris_library_version": library_info[
                "swiss_ephemeris_library_version"
            ],
            "requested_ephemeris_source": "Swiss Ephemeris files",
            "ephemeris_dataset_lineage": dict(
                _EPHEMERIS_DATASET_LINEAGE
            ),
            "available_input_files": available_files,
            "manifest_semantics": (
                "availability_only_actual_source_is_proven_by_each_returned_retflag"
            ),
        },
        "provenance": {
            "core_objects": core_records,
            "fixed_stars": fixed_star_records,
            "source_usage_semantics": (
                "ephemeris source usage and result applicability are "
                "independent; inspect result_status and used_full_ephemeris "
                "separately"
            ),
            "all_core_calculation_sources_used_full_ephemeris": all(
                record["used_full_ephemeris"] is True
                for record in core_records
            ),
            "core_result_status_counts": {
                status: sum(
                    record["result_status"] == status
                    for record in core_records
                )
                for status in ("available", "not_applicable", "failed")
            },
            "fallback_object_keys": [
                record["key"]
                for record in core_records + fixed_star_records
                if record["used_full_ephemeris"] is False
            ],
            "event_modules": {
                "lunar_phases": {
                    **(
                        event_module_availability["lunar_phases"]
                        if modules["lunar_phases"] == "not_applicable"
                        else {}
                    ),
                    "computed": modules["lunar_phases"] == "computed",
                    "source_policy": (
                        "Swiss files required; fail closed on Moshier fallback"
                    ),
                    "detail_path": "astronomical_data.lunar_events",
                },
                "eclipses": {
                    "computed": modules["eclipses"] == "computed",
                    "source_policy": (
                        "Swiss global search plus Sun/Moon retflag verification"
                    ),
                    "detail_path": (
                        "astronomical_data.lunar_events.eclipses"
                    ),
                },
                "rise_set_transits": {
                    "computed": modules["rise_set_transits"] == "computed",
                    "source_policy": (
                        "Swiss files requested via FLG_SWIEPH"
                    ),
                    "actual_source_verified": False,
                    "source_evidence": (
                        "requested_flag_only_rise_trans_returns_no_"
                        "ephemeris_retflag"
                    ),
                    "detail_path": "astronomical_data.horizon_events",
                },
            },
        },
        "methodology": methodology,
        "warnings": warnings,
        "trace_receipt": _trace_receipt(trace_steps),
        "privacy": _privacy_attestation(),
    }
