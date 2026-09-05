"""Backend-authored receipt for validated inputs, policy and provenance."""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os

import swisseph as swe

from ..config import PRODUCT_YEAR_RANGE
from ..ephemeris import EPHE_DIR
from ..frontend_release import (
    CANONICAL_JSON_ENSURE_ASCII,
    CANONICAL_JSON_SEPARATORS,
    CANONICAL_JSON_SORT_KEYS,
    canonical_json_bytes,
)
from ..privacy_receipt import privacy_attestation
from . import essential_dignities


DOSSIER_VERSION = "0.6.0"

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
    "seas_18.se1": "minor_planet_segment_for_optional_chiron",
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
        "calculation_source": item.get("calculation_source"),
        "derived_from": item.get("derived_from"),
        "used_full_ephemeris": item.get("used_full_ephemeris"),
        "retflags": {
            "ecliptic": _retflag_receipt(item.get("retflag_ecliptic")),
            "equatorial": _retflag_receipt(item.get("retflag_equatorial")),
            "horizontal_source": _retflag_receipt(
                item.get("retflag_horizontal_source")
            ),
        },
    }


def _execution_status(
    requested: bool,
    *,
    available: bool,
    applicable: bool = True,
) -> str:
    """Map one observable module state to the public closed vocabulary."""

    if not requested:
        return "not_requested"
    return "computed" if applicable and available else "not_applicable"


def _module_status(
    options,
    derived_methods: dict,
    event_module_availability: dict,
    astronomical_data: dict,
) -> dict:
    sect = derived_methods["sect"]
    lots = derived_methods["lots"]
    void_of_course = derived_methods["void_of_course"]
    planet_in_house = derived_methods["planet_in_house"]
    aspects = derived_methods["aspects"]
    south_nodes = [
        node
        for node in astronomical_data["nodes"]
        if node["key"] in {"true_south_node", "mean_south_node"}
    ]
    chiron = next(
        (body for body in astronomical_data["bodies"] if body["key"] == "chiron"),
        None,
    )
    extra_angles = astronomical_data.get("extra_angles") or {}
    anti_vertex = (extra_angles.get("angles") or {}).get("anti_vertex")
    lunar_apsides = astronomical_data.get("lunar_apsides") or {}
    parallax_moon = astronomical_data.get("parallax_moon") or {}
    essential_dignities = derived_methods.get("essential_dignities") or {}

    sect_status = _execution_status(
        options.include_lots,
        applicable=options.include_houses,
        available=sect is not None and sect.get("is_day") is not None,
    )
    lots_status = _execution_status(
        options.include_lots,
        applicable=options.include_houses,
        available=bool(
            lots
            and lots.get("fortune") is not None
            and lots.get("spirit") is not None
        ),
    )
    void_of_course_status = _execution_status(
        options.include_void_of_course,
        available=bool(
            void_of_course
            and void_of_course.get("is_void_of_course") is not None
        ),
    )
    # Fewer than two participants is not_applicable, not a silent empty result.
    aspects_status = _execution_status(
        options.include_aspects,
        available=bool(aspects and aspects.get("pairs")),
    )

    return {
        "core_positions": "computed",
        "house_division": derived_methods["house_division"].get(
            "execution_status", "computed"
        ),
        "planet_in_house": planet_in_house["execution_status"],
        "aspects": aspects_status,
        "fixed_stars": _execution_status(
            options.include_fixed_stars, available=True
        ),
        "outer_planets": _execution_status(
            options.include_outer_planets, available=True
        ),
        "chiron": _execution_status(
            options.include_chiron,
            available=chiron is not None and chiron.get("longitude") is not None,
        ),
        "south_nodes": _execution_status(
            options.include_south_nodes,
            available=bool(south_nodes) and all(
                node.get("longitude") is not None for node in south_nodes
            ),
        ),
        "anti_vertex": _execution_status(
            options.include_anti_vertex,
            available=(
                anti_vertex is not None
                and anti_vertex.get("longitude") is not None
            ),
        ),
        "lunar_apsides": _execution_status(
            options.include_lilith_priapus,
            available=bool(lunar_apsides.get("available")),
        ),
        "parallax_moon": _execution_status(
            options.moon_position_profile != "global_computation_mode",
            available=bool(parallax_moon.get("available")),
        ),
        "essential_dignities": _execution_status(
            bool(essential_dignities),
            available=bool(essential_dignities.get("available")),
        ),
        "antiscia": _execution_status(options.include_antiscia, available=True),
        # Sect reflects actual execution rather than merely echoing the request.
        # Lots requires Sect; VOC is independent and must not request it.
        "sect": sect_status,
        "lots": lots_status,
        "void_of_course": void_of_course_status,
        "declination_aspects": _execution_status(
            options.include_declination_aspects, available=True
        ),
        "lunar_phases": event_module_availability["lunar_phases"]["status"],
        "eclipses": _execution_status(options.include_eclipses, available=True),
        "rise_set_transits": _execution_status(
            options.include_rise_set_transits, available=True
        ),
    }


def _methodology_receipt(derived_methods: dict, modules: dict) -> dict:
    items = {}
    for name in (
        "house_division",
        "planet_in_house",
        "aspects",
        "sect",
        "lots",
        "void_of_course",
        "declination_aspects",
        "essential_dignities",
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


def _option_state_receipts(
    request,
    modules: dict,
    event_module_availability: dict,
    derived_methods: dict,
) -> dict:
    """Closed execution receipts for the exact OptionsInput set."""

    options = request.options
    explicit = set(options.model_fields_set)
    receipts: dict[str, dict] = {}

    def add(
        name: str,
        *,
        requested: bool,
        status: str,
        path: str,
        reason: str | None = None,
        executed: bool | None = None,
        source_path: str | None = None,
    ) -> None:
        available = requested and status == "computed"
        applicable = requested and status != "not_applicable"
        receipts[name] = {
            "input_presence": "explicit" if name in explicit else "defaulted",
            "requested_value": getattr(options, name),
            "requested": requested,
            "executed": available if executed is None else executed,
            "applicable": applicable,
            "available": available,
            "source": (
                source_path or path
                if (available if executed is None else executed)
                else None
            ),
            "reason_code": (
                "option_not_requested"
                if not requested
                else reason or "module_not_applicable"
                if not applicable
                else None
            ),
            "response_paths": [path],
        }

    toggles = {
        "include_houses": ("house_division", "derived_methods.house_division"),
        "include_fixed_stars": ("fixed_stars", "astronomical_data.fixed_stars"),
        "include_lots": ("lots", "derived_methods.lots"),
        "include_antiscia": ("antiscia", "derived_geometry.antiscia"),
        "include_void_of_course": ("void_of_course", "derived_methods.void_of_course"),
        "include_declination_aspects": ("declination_aspects", "derived_methods.declination_aspects"),
        "include_outer_planets": ("outer_planets", "astronomical_data.bodies"),
        "include_chiron": ("chiron", "astronomical_data.bodies"),
        "include_lilith_priapus": ("lunar_apsides", "astronomical_data.lunar_apsides"),
        "include_south_nodes": ("south_nodes", "astronomical_data.nodes"),
        "include_lunar_phases": ("lunar_phases", "astronomical_data.lunar_events"),
        "include_eclipses": ("eclipses", "astronomical_data.lunar_events.eclipses"),
        "include_rise_set_transits": ("rise_set_transits", "astronomical_data.horizon_events"),
        "include_aspects": ("aspects", "derived_methods.aspects"),
        "include_anti_vertex": ("anti_vertex", "astronomical_data.extra_angles.angles.anti_vertex"),
    }
    reasons = {
        "house_division": "house_calculation_disabled",
        "lots": (derived_methods.get("lots") or {}).get("reason_code"),
        "void_of_course": (derived_methods.get("void_of_course") or {}).get("reason_code"),
        "aspects": (derived_methods.get("aspects") or {}).get("reason_code"),
        "anti_vertex": "house_calculation_not_executed",
        "lunar_phases": event_module_availability["lunar_phases"].get("reason_code"),
    }
    for option_name, (module_name, path) in toggles.items():
        requested = bool(getattr(options, option_name))
        status = modules[module_name]
        attempted_failure = (
            module_name == "lunar_phases"
            and requested
            and status == "not_applicable"
        )
        add(
            option_name,
            requested=requested,
            status=status,
            path=path,
            reason=reasons.get(module_name),
            executed=status == "computed" or attempted_failure,
        )

    dignity_status = modules["essential_dignities"]
    dignity_reason = (derived_methods.get("essential_dignities") or {}).get(
        "reason_code"
    )
    add(
        "include_domicile_exaltation",
        requested=bool(options.include_domicile_exaltation),
        status=dignity_status,
        path="derived_methods.essential_dignities",
        reason=dignity_reason,
        source_path="derived_methods.essential_dignities.profile_results",
    )

    extra_requested = bool(options.include_extra_angles)
    add(
        "include_extra_angles",
        requested=extra_requested,
        status=("computed" if options.include_houses else "not_applicable"),
        path="astronomical_data.extra_angles",
        reason="house_calculation_not_executed",
    )

    moon_status = modules["parallax_moon"]
    moon_is_global = options.moon_position_profile == "global_computation_mode"
    add(
        "moon_position_profile",
        requested=True,
        status="computed" if moon_is_global else moon_status,
        path=(
            "astronomical_data.bodies"
            if moon_is_global
            else "astronomical_data.parallax_moon"
        ),
    )

    configurations = {
        "house_system": (bool(options.include_houses), modules["house_division"], "derived_methods.house_division"),
        "declination_aspect_orb_degrees": (bool(options.include_declination_aspects), modules["declination_aspects"], "derived_methods.declination_aspects"),
        "aspect_orb_profile": (options.aspect_orb_profile is not None and options.include_aspects, modules["aspects"], "derived_methods.aspects.degree_based.orb_receipt"),
        "aspect_set_profile": (bool(options.include_aspects), modules["aspects"], "derived_methods.aspects.degree_based"),
        "aspect_orb_scale_percent": (options.aspect_orb_scale_percent is not None and options.include_aspects, modules["aspects"], "derived_methods.aspects.degree_based.orb_receipt"),
        "aspect_fixed_orb_degrees": (options.aspect_fixed_orb_degrees is not None and options.include_aspects, modules["aspects"], "derived_methods.aspects.degree_based.orb_receipt"),
        "partile_profile": (bool(options.include_aspects), modules["aspects"], "derived_methods.aspects.degree_based"),
        "aspect_angle_orb_degrees": (options.aspect_angle_orb_degrees is not None and options.include_aspects and options.aspect_include_angles, modules["aspects"], "derived_methods.aspects.degree_based.angle_participation"),
        "body_selection_preset": (True, "computed", "calculation_dossier.calculation_policy.body_selection"),
        "bounds_profile": (options.bounds_profile is not None, modules["essential_dignities"], "derived_methods.essential_dignities.profile_results"),
        "decan_profile": (options.decan_profile is not None, modules["essential_dignities"], "derived_methods.essential_dignities.profile_results"),
        "triplicity_profile": (options.triplicity_profile is not None, modules["essential_dignities"], "derived_methods.essential_dignities.profile_results"),
    }
    for name, (selected, status, path) in configurations.items():
        add(
            name,
            requested=bool(selected),
            status=status,
            path=path,
            reason=(
                dignity_reason
                if name in {"bounds_profile", "decan_profile", "triplicity_profile"}
                else "module_not_applicable"
            ),
        )
        if not selected:
            receipts[name]["reason_code"] = "configuration_not_selected"

    dependent_flags = {
        "antiscia_include_nodes": (bool(options.include_antiscia), modules["antiscia"], "derived_geometry.antiscia.scope"),
        "include_aspect_perfection": (bool(options.include_aspects), modules["aspects"], "derived_methods.aspects.perfection"),
        "aspect_include_nodes": (bool(options.include_aspects), modules["aspects"], "derived_methods.aspects.participants"),
        "aspect_include_angles": (bool(options.include_aspects and options.include_houses), modules["aspects"], "derived_methods.aspects.degree_based.angle_participation"),
        "triplicity_include_research_comparison": (True, modules["essential_dignities"], "derived_methods.essential_dignities.profile_results"),
    }
    for name, (parent_applicable, status, path) in dependent_flags.items():
        requested = bool(getattr(options, name))
        reason = "parent_module_not_requested_or_not_applicable"
        executed = None
        if name == "include_aspect_perfection" and parent_applicable and requested:
            perfection = (derived_methods.get("aspects") or {}).get("perfection", {})
            status = str(perfection.get("status") or "not_applicable")
            reason = perfection.get("reason_code") or "module_not_applicable"
            executed = bool(perfection.get("executed"))
        add(
            name,
            requested=requested,
            status=(status if parent_applicable else "not_applicable"),
            path=path,
            reason=reason,
            executed=executed,
        )

    return receipts


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


# Presentation thresholds flag sensitivity without changing calculations.
# Sebastian ruled a method-layer signal, not a refusal, with this threshold on
# 2026-09-03.  Calibration: the most extreme legal pairing in the world is
# western China, where Kashgar at 76E runs on Beijing time — 2.93 hours.  3.5
# lets that through and still catches a zone picked from the wrong continent.
GEOGRAPHIC_ZONE_INCOHERENCE_HOURS = 3.5
SIGN_BOUNDARY_WARNING_DEGREES = 1.0
CUSP_PROXIMITY_WARNING_DEGREES = 1.0


def _boundary_warnings(
    *,
    bodies: list[dict],
    planet_in_house: dict,
    latitude_regime: dict | None,
    sect: dict | None,
    longitude: float | None = None,
    utc_offset_hours: float | None = None,
) -> list[dict]:
    """高緯度、宮頭鄰近、星座邊界鄰近三類敏感度提示。

    這些都不是錯誤：計算結果本身有效。它們標示的是「若出生時刻或座標有一點誤差，
    這個結論就會翻掉」，而使用者無從自行看出這一點。
    """

    warnings = []

    # Coordinates and a zone each pass their own validation; their relationship
    # belongs to neither field, so a Taipei latitude paired with a New York zone
    # produced a chart that is wrong in a way nothing said out loud.
    if longitude is not None and utc_offset_hours is not None:
        solar_offset_hours = longitude / 15.0
        divergence = abs(solar_offset_hours - utc_offset_hours)
        if divergence > GEOGRAPHIC_ZONE_INCOHERENCE_HOURS:
            warnings.append(
                _warning(
                    "geographic_timezone_incoherence",
                    "warning",
                    (
                        f"這個經度的地方平太陽時約為 UTC{solar_offset_hours:+.2f}，"
                        f"而所選時區的偏移是 UTC{utc_offset_hours:+.2f}，"
                        f"相差 {divergence:.2f} 小時，超過 "
                        f"{GEOGRAPHIC_ZONE_INCOHERENCE_HOURS} 小時的合理範圍。"
                        "計算仍依你提供的值完成；請確認座標與時區是否同屬一地。"
                    ),
                    "astronomical_data.time",
                    ["astronomical_data.time", "astronomical_data.angles"],
                )
            )

    if latitude_regime and latitude_regime["band"] != "ordinary":
        beyond_polar = latitude_regime["band"] == "beyond_polar_circle"
        distorted = latitude_regime["quadrant_distortion_expected"]
        warnings.append(
            _warning(
                (
                    "latitude_beyond_polar_circle"
                    if beyond_polar
                    else "high_latitude_house_distortion"
                ),
                "warning" if distorted else "notice",
                (
                    f"地理緯度 {latitude_regime['latitude']}° "
                    + (
                        "超過極圈（±66.5°）。"
                        if beyond_polar
                        else "屬高緯度（±60° 以上）。"
                    )
                    + (
                        "所選為四分宮制，宮位大小在此緯度會嚴重不均，"
                        "部分黃道度數可能永不升起或永不落下；宮頭數值仍為 Swiss "
                        "Ephemeris 的計算結果，但落宮結論對出生時刻高度敏感。"
                        if distorted
                        else "所選為整宮制，宮界不受緯度影響；"
                        "但地平座標與升降事件在此緯度仍可能退化。"
                    )
                ),
                "houses.latitude_regime",
                [
                    "derived_methods.house_division",
                    "derived_methods.planet_in_house",
                    "astronomical_data.angles",
                ],
            )
        )

    near_cusp = [
        placement
        for placement in planet_in_house.get("placements", [])
        if placement["distance_to_nearest_cusp_degrees"]
        <= CUSP_PROXIMITY_WARNING_DEGREES
    ]
    if near_cusp:
        warnings.append(
            _warning(
                "body_near_house_cusp",
                "notice",
                (
                    "下列星體距最近宮頭在 "
                    f"{CUSP_PROXIMITY_WARNING_DEGREES}° 以內，"
                    "落宮對出生時刻誤差敏感（上升點每約 4 分鐘移動 1°）："
                    + "、".join(
                        f"{item['name']}（第 {item['house']} 宮，"
                        f"距宮頭 {item['distance_to_nearest_cusp_degrees']:.4f}°）"
                        for item in near_cusp
                    )
                ),
                "planet_in_house.placements",
                ["derived_methods.planet_in_house.placements"],
            )
        )

    near_sign_boundary = [
        body
        for body in bodies
        if body.get("longitude") is not None
        and min(
            body["longitude"] % 30.0,
            30.0 - (body["longitude"] % 30.0),
        )
        <= SIGN_BOUNDARY_WARNING_DEGREES
    ]
    if near_sign_boundary:
        warnings.append(
            _warning(
                "body_near_sign_boundary",
                "notice",
                (
                    "下列星體距星座邊界在 "
                    f"{SIGN_BOUNDARY_WARNING_DEGREES}° 以內，"
                    "星座歸屬對出生日期／時刻誤差敏感："
                    + "、".join(
                        f"{body['name']}（"
                        f"{min(body['longitude'] % 30.0, 30.0 - (body['longitude'] % 30.0)):.4f}°）"
                        for body in near_sign_boundary
                    )
                ),
                "astronomical_data.bodies",
                [
                    "astronomical_data.bodies",
                    "derived_methods.aspects.participants",
                ],
            )
        )

    if sect and sect.get("near_critical"):
        warnings.append(
            _warning(
                "sect_near_horizon_critical",
                "warning",
                (
                    "太陽高度在地平上下 "
                    f"{sect['near_critical_tolerance_degrees'] * 60:.0f} 角分內，"
                    "屬日夜交界的模糊區間（約 3–5 分鐘）。日夜盤判定會連帶改變"
                    "阿拉伯點的公式，此盤的相關結論對出生時刻高度敏感。"
                ),
                "derived_methods.sect",
                ["derived_methods.sect", "derived_methods.lots"],
            )
        )

    return warnings


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
    bodies: list[dict],
    planet_in_house: dict,
    latitude_regime: dict | None,
    sect: dict | None,
) -> list[dict]:
    warnings = []
    warnings.extend(
        _boundary_warnings(
            bodies=bodies,
            planet_in_house=planet_in_house,
            latitude_regime=latitude_regime,
            sect=sect,
            longitude=request.location.longitude,
            utc_offset_hours=time_conversion.get("utc_offset_hours"),
        )
    )
    limited_topocentric_speeds = [
        body.get("key")
        for body in bodies
        if body.get("speed_position_derivative_status")
        == "known_internal_disagreement_for_sun_moon"
    ]
    if limited_topocentric_speeds:
        warnings.append(
            _warning(
                "topocentric_analytic_speed_limitation",
                "warning",
                "站心speed_*是Swiss FLG_SPEED解析值；太陽／月亮部分欄位已知"
                "不等於同次站心position的有限差分導數。位置仍可使用，但"
                "這些speed欄位不得解讀為已驗證的導數精度。",
                "RT-2026-08-21-VALIDATOR-V2-PRODUCT-E-005",
                ["astronomical_data.bodies"],
            )
        )

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

    if time_conversion["swiss_time_input_semantics"] == "ut1_before_1972_swiss_rule":
        warnings.append(
            _warning(
                "pre_1972_ut1_input_semantics",
                "warning",
                "Swiss Ephemeris在1972年前把utc_to_jd輸入視為UT1；"
                "civil-time標籤不可解讀為現代UTC leap-second尺度。",
                "Swiss Ephemeris handling_of_leap_seconds",
                ["calculation_dossier.time_conversion"],
            )
        )
    if time_conversion["delta_t_model"]["status"] == "far_epoch_extrapolation":
        warnings.append(
            _warning(
                "delta_t_far_epoch_extrapolation",
                "warning",
                "此年代的ΔT是Swiss內部遠期模型值；精度未由本產品獨立確立，"
                "不可把顯示的小數位讀成已知真值。",
                "Swiss Ephemeris delta_t future estimate",
                ["calculation_dossier.time_conversion.delta_t_model"],
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

    if request.options.moon_position_profile == "moon_only_topocentric_v1":
        warnings.append(
            _warning(
                "mixed_origin_moon_position",
                "warning",
                (
                    "本次全盤採地心座標，但有效月亮改採觀測地站心座標。"
                    "兩個月亮數值均保留，所有下游判定使用站心月亮；這是"
                    "刻意的混合原點研究 profile，不代表站心值普遍較準。"
                ),
                "Sebastian CMP-A10 ruling 2026-08-04",
                [
                    "astronomical_data.parallax_moon",
                    "astronomical_data.bodies",
                    "derived_methods",
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
                "product_method_audit_pending",
                [
                    "calculation_dossier.calculation_policy.computation_mode",
                    "astronomical_data.atmosphere",
                ],
            )
        )
        # Horizon events retain their own apparent topocentric frame (MTH-Q-018).
        if request.options.include_rise_set_transits:
            warnings.append(
                _warning(
                    "horizon_events_keep_apparent_frame_under_true_position",
                    "notice",
                    (
                        "您選了 position_mode=true，因此星體與恆星的視高度"
                        "（altitude_apparent）已依 MTH-Q-009 抑制為 null——"
                        "幾何方向與大氣折射是互斥前提，混用的數值無法解讀。"
                        "但升降事件模組仍會輸出視高度取樣，這不是疏漏："
                        "「升起」的定義就是星體**經過折射的上緣**穿過地平的時刻，"
                        "把折射拿掉，該事件本身就不存在。因此該模組一律使用"
                        "視位置＋站心框架，不跟隨 computation_mode，"
                        "其輸出的 contract.frame 欄位有完整宣告。"
                    ),
                    "MTH-Q-018 ruling; core/horizon_events.py",
                    [
                        "astronomical_data.horizon_events.contract.frame",
                        "calculation_dossier.calculation_policy.fixed_module_frames",
                        "astronomical_data.bodies",
                    ],
                )
            )

    date_only_unscanned_paths = [
        path
        for requested, path in (
            (
                request.options.include_void_of_course,
                "derived_methods.void_of_course",
            ),
            (request.options.include_aspects, "derived_methods.aspects"),
            (
                request.options.include_declination_aspects,
                "derived_methods.declination_aspects",
            ),
            (
                bool(
                    request.options.include_domicile_exaltation
                    or request.options.bounds_profile
                    or request.options.decan_profile
                    or request.options.triplicity_profile
                    or request.options.triplicity_include_research_comparison
                ),
                "derived_methods.essential_dignities",
            ),
        )
        if requested
    ]
    if (
        request.birth_time_precision == "date_only"
        and date_only_unscanned_paths
    ):
        warnings.append(
            _warning(
                "date_only_method_sensitivity_not_evaluated",
                "warning",
                (
                    "這些方法結果只屬當地正午的可重現計算錨點，"
                    "不是出生時刻判定；目前未評估它們在整個 civil day 內"
                    "是否改變。"
                ),
                "birth_time_precision_date_only_policy",
                date_only_unscanned_paths,
            )
        )

    if (
        request.birth_time_precision == "date_only"
        and request.options.include_lilith_priapus
    ):
        warnings.append(
            _warning(
                "date_only_lunar_apsides_sensitivity_not_evaluated",
                "notice",
                (
                    "Lilith／Priapus 數值只屬當地正午計算錨點；目前未評估"
                    "它們在整個 civil day 內的變化。"
                ),
                "birth_time_precision_date_only_policy",
                ["astronomical_data.lunar_apsides"],
            )
        )

    return warnings


def _trace_receipt(trace_steps: list[dict]) -> dict:
    serialized = canonical_json_bytes(trace_steps)
    return {
        "response_path": "calculation_trace",
        "step_count": len(trace_steps),
        "python_json_serialization_sha256": hashlib.sha256(
            serialized
        ).hexdigest(),
        "serialization_recipe": {
            "implementation": "Python json.dumps",
            "ensure_ascii": CANONICAL_JSON_ENSURE_ASCII,
            "sort_keys": CANONICAL_JSON_SORT_KEYS,
            "separators": list(CANONICAL_JSON_SEPARATORS),
            "encoding": "UTF-8",
            "float_serialization": "Python runtime json encoder",
            "portable_across_languages": False,
        },
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
    deployment_profile: str | None = None,
    birth_time_sensitivity: dict,
    event_module_availability: dict,
    latitude_regime: dict | None = None,
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
        astronomical_data,
    )
    option_states = _option_state_receipts(
        request,
        modules,
        event_module_availability,
        derived_methods,
    )
    methodology = _methodology_receipt(derived_methods, modules)
    input_receipt = request.model_dump(mode="json")

    available_files = []
    for immutable_entry in _available_input_files():
        entry = dict(immutable_entry)
        entry["required_for_this_request"] = (
            entry["filename"] in {"sepl_18.se1", "semo_18.se1"}
            or (
                entry["filename"] == "seas_18.se1"
                and request.options.include_chiron
            )
            or (
                entry["filename"] == "sefstars.txt"
                and request.options.include_fixed_stars
            )
        )
        available_files.append(entry)

    atmosphere = astronomical_data["atmosphere"]
    extra_angles = astronomical_data.get("extra_angles") or {}
    anti_vertex = (extra_angles.get("angles") or {}).get("anti_vertex")
    lunar_apsides = astronomical_data.get("lunar_apsides") or {}
    south_nodes = [
        node
        for node in astronomical_data["nodes"]
        if node["key"] in {"true_south_node", "mean_south_node"}
    ]
    chiron = next(
        (
            body
            for body in astronomical_data["bodies"]
            if body["key"] == "chiron"
        ),
        None,
    )
    body_selection = {
        "requested": request.options.body_selection_preset,
        "executed": request.options.body_selection_preset,
        "applicable": True,
        "available": True,
        "source": "sebastian_product_preset_2026_08_03",
        "classical_body_keys": [
            "sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn"
        ],
        "excluded_optional_body_groups": (
            ["outer_planets", "chiron", "fixed_stars"]
            if request.options.body_selection_preset == "classical_seven_v1"
            else []
        ),
        "unaffected_point_groups": [
            "lunar_nodes", "lots", "extra_angles"
        ],
        "south_nodes": {
            "requested": request.options.include_south_nodes,
            "executed": request.options.include_south_nodes,
            "applicable": request.computation_mode.center in {
                "geocentric", "topocentric"
            },
            "available": bool(south_nodes) and all(
                node.get("longitude") is not None for node in south_nodes
            ),
            "source": "derived_from_requested_north_node_antipodes",
        },
        "anti_vertex": {
            "requested": request.options.include_anti_vertex,
            "executed": (
                request.options.include_anti_vertex
                and request.options.include_houses
            ),
            "applicable": request.options.include_houses,
            "available": (
                anti_vertex is not None
                and anti_vertex.get("longitude") is not None
            ),
            "source": "vertex_longitude_antipode",
            "source_vertex_longitude_degrees": (
                anti_vertex.get("source_vertex_longitude_degrees")
                if anti_vertex is not None
                else None
            ),
        },
        "chiron": {
            "requested": request.options.include_chiron,
            "executed": request.options.include_chiron,
            "applicable": True,
            "available": (
                chiron is not None and chiron.get("longitude") is not None
            ),
            "source": (
                chiron.get("calculation_source")
                if chiron is not None
                else "swiss_ephemeris_minor_planet"
            ),
        },
        "lunar_apsides": {
            "requested": request.options.include_lilith_priapus,
            "executed": bool(lunar_apsides.get("executed")),
            "applicable": (
                bool(lunar_apsides.get("applicable"))
                if request.options.include_lilith_priapus
                else request.computation_mode.center in {"geocentric", "topocentric"}
            ),
            "available": bool(lunar_apsides.get("available")),
            "source": (
                "swiss_ephemeris_2_10_lunar_apsides"
                if request.options.include_lilith_priapus
                else None
            ),
            "response_path": "astronomical_data.lunar_apsides",
            "classification": "modern_research_additional_points",
        },
    }
    aspects_value = derived_methods.get("aspects") or {}
    degree_aspects = aspects_value.get("degree_based") or {}
    orb_receipt = degree_aspects.get("orb_receipt") or {}
    angle_participation = degree_aspects.get("angle_participation") or {}
    aspect_configuration = {
        "requested": request.options.include_aspects,
        "executed": modules["aspects"] == "computed",
        "applicable": modules["aspects"] != "not_applicable",
        "available": bool(aspects_value.get("pairs")),
        "source": "normalized_request_and_effective_aspect_receipt",
        "aspect_set_profile": request.options.aspect_set_profile,
        "orb_configuration_mode": orb_receipt.get("configuration_mode", "none"),
        "fixed_pair_threshold_degrees": request.options.aspect_fixed_orb_degrees,
        "profile_scale_percent": request.options.aspect_orb_scale_percent,
        "angles_requested": request.options.aspect_include_angles,
        "angles_executed": angle_participation.get("executed", False),
        "angles_applicable": angle_participation.get(
            "applicable", request.options.include_houses
        ),
        "angles_available": angle_participation.get("available", False),
        "angle_reason_code": angle_participation.get("reason_code"),
        "angle_pair_orb_degrees": request.options.aspect_angle_orb_degrees,
        "declination_orb_degrees": request.options.declination_aspect_orb_degrees,
    }
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
        bodies=astronomical_data["bodies"],
        planet_in_house=derived_methods["planet_in_house"],
        latitude_regime=latitude_regime,
        sect=derived_methods["sect"],
    )

    return {
        "dossier_version": DOSSIER_VERSION,
        "status": "provisional",
        "authority": "backend_effective_runtime",
        "build_identity": dict(build_identity),
        "input_receipt": input_receipt,
        "birth_time": {
            "precision": request.birth_time_precision,
            "input_birth_date": (
                f"{request.datetime.year:04d}-{request.datetime.month:02d}-"
                f"{request.datetime.day:02d}"
            ),
            "input_civil_hour": (
                None
                if request.birth_time_precision == "date_only"
                else (
                    f"{request.datetime.year:04d}-{request.datetime.month:02d}-"
                    f"{request.datetime.day:02d} {request.datetime.hour:02d}"
                )
            ),
            "birth_time_known": request.birth_time_precision != "date_only",
            "representative_local_time": time_conversion["input_local_time"],
            "representative_policy": (
                "local_noon_computational_anchor_not_birth_time"
                if request.birth_time_precision == "date_only"
                else "midpoint_minute_30"
                if request.birth_time_precision == "approximate_hour"
                else "exact_input"
            ),
            "representative_is_birth_time": (
                request.birth_time_precision == "exact"
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
            "input_semantics": time_conversion["input_semantics"],
            "timezone_mode": request.timezone.mode,
            "timezone_label": time_conversion["timezone_label"],
            "fold": request.timezone.fold,
            "resolved_utc_offset_hours": time_conversion["utc_offset_hours"],
            "utc_iso_8601": time_conversion["utc_time"].replace(" ", "T") + "Z",
            "jd_ut1": time_conversion["jd_ut"],
            "jd_tt": time_conversion["jd_et"],
            "delta_t_seconds": time_conversion["delta_t_seconds"],
            "delta_t_model": time_conversion["delta_t_model"],
            "swiss_time_input_semantics": time_conversion[
                "swiss_time_input_semantics"
            ],
            "leap_second_input": time_conversion["leap_second_input"],
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
            "option_states": option_states,
            "body_selection": body_selection,
            "moon_position": {
                "requested_profile": request.options.moon_position_profile,
                "global_center": request.computation_mode.center,
                "effective_moon_source": (
                    "topocentric_observer"
                    if request.options.moon_position_profile
                    == "moon_only_topocentric_v1"
                    else request.computation_mode.center
                ),
                "geocentric_reference_retained": (
                    request.options.moon_position_profile
                    == "moon_only_topocentric_v1"
                ),
                "source": "astronomical_data.parallax_moon",
            },
            "essential_dignity_profiles": {
                "domicile_exaltation": (
                    essential_dignities.DOMICILE_EXALTATION_PROFILE_ID
                    if request.options.include_domicile_exaltation
                    else None
                ),
                "bounds": request.options.bounds_profile,
                "face_decan": request.options.decan_profile,
                "triplicity": request.options.triplicity_profile,
                "triplicity_research_comparison": (
                    request.options.triplicity_include_research_comparison
                ),
                "source": (
                    "derived_methods.essential_dignities.profile_results"
                ),
            },
            "aspect_configuration": aspect_configuration,
            "house_system": {
                "requested": request.options.include_houses,
                "executed": modules["house_division"] == "computed",
                "applicable": request.options.include_houses,
                "available": modules["house_division"] == "computed",
                "source": (
                    "derived_methods.house_division"
                    if modules["house_division"] == "computed"
                    else None
                ),
                "code": (
                    request.options.house_system
                    if modules["house_division"] == "computed"
                    else None
                ),
                "requested_code": request.options.house_system,
                "calculated": modules["house_division"] == "computed",
                "method_layer": True,
                "latitude_regime": latitude_regime,
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
                "houses_and_angles": (
                    "earth_observer_frame"
                    if request.options.include_houses
                    else "not_requested"
                ),
                "lunar_events": (
                    "geocentric_apparent_tropical_of_date"
                    if request.options.include_lunar_phases
                    or request.options.include_eclipses
                    else "not_requested"
                ),
                # 升降事件模組使用自己的固定框架，不跟隨 computation_mode，
                # 且 MTH-Q-009 的視高度抑制不及於它——折射是升降事件的定義，
                # 不是附加修正。詳見 core/horizon_events.py 與 MTH-Q-018。
                "rise_set_transits": (
                    "topocentric_observer_apparent_always_ignores_computation_mode"
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
            # 時區資料庫版本屬於可重現性證據：不記錄它，卷宗就無法解釋
            # 「同樣的輸入為什麼換算出不同的 UTC」。
            "tz_database": library_info["tz_database"],
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
        "privacy": privacy_attestation(deployment_profile),
    }
