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

    if sect is None:
        sect_status = (
            "not_applicable"
            if options.include_lots and not options.include_houses
            else "not_requested"
        )
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

    if not options.include_aspects:
        aspects_status = "not_requested"
    elif not aspects or not aspects.get("pairs"):
        # 參與者不足兩個時沒有任何組合可比較（例如 heliocentric 模式下太陽退化為
        # null）。這與「沒有要求」是不同的情況，必須分開回報。
        aspects_status = "not_applicable"
    else:
        aspects_status = "computed"

    return {
        "core_positions": "computed",
        "house_division": derived_methods["house_division"].get(
            "execution_status", "computed"
        ),
        "planet_in_house": planet_in_house["execution_status"],
        "aspects": aspects_status,
        "fixed_stars": (
            "computed" if options.include_fixed_stars else "not_requested"
        ),
        "outer_planets": (
            "computed" if options.include_outer_planets else "not_requested"
        ),
        "chiron": (
            "not_requested"
            if not options.include_chiron
            else "computed"
            if chiron is not None and chiron.get("longitude") is not None
            else "not_applicable"
        ),
        "south_nodes": (
            "not_requested"
            if not options.include_south_nodes
            else "computed"
            if south_nodes and all(
                node.get("longitude") is not None for node in south_nodes
            )
            else "not_applicable"
        ),
        "anti_vertex": (
            "not_requested"
            if not options.include_anti_vertex
            else "computed"
            if anti_vertex is not None
            else "not_applicable"
        ),
        "lunar_apsides": (
            "not_requested"
            if not options.include_lilith_priapus
            else "computed"
            if lunar_apsides.get("available")
            else "not_applicable"
        ),
        "parallax_moon": (
            "not_requested"
            if options.moon_position_profile == "global_computation_mode"
            else "computed"
            if parallax_moon.get("available")
            else "not_applicable"
        ),
        "essential_dignities": (
            "not_requested"
            if not essential_dignities
            else "computed"
            if essential_dignities.get("available")
            else "not_applicable"
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
    """Closed per-option execution receipts for the exact OptionsInput set.

    ``requested_options`` preserves values, but a value alone cannot say
    whether its consumer ran or whether a dependent option was ignored.  This
    table keeps input presence separate from execution state and is deliberately
    exhaustive: the contract test compares its keys with ``OptionsInput``.
    """

    options = request.options
    explicit = set(options.model_fields_set)
    receipts: dict[str, dict] = {}

    def add(
        name: str,
        *,
        requested: bool,
        executed: bool,
        applicable: bool,
        available: bool,
        source: str | None,
        reason_code: str | None,
        response_paths: list[str],
    ) -> None:
        receipts[name] = {
            "input_presence": "explicit" if name in explicit else "defaulted",
            "requested_value": getattr(options, name),
            "requested": requested,
            "executed": executed,
            "applicable": applicable,
            "available": available,
            "source": source,
            "reason_code": reason_code,
            "response_paths": response_paths,
        }

    module_toggles = {
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
    reason_by_module = {
        "house_division": "house_calculation_disabled",
        "lots": (derived_methods.get("lots") or {}).get("reason_code"),
        "void_of_course": (derived_methods.get("void_of_course") or {}).get("reason_code"),
        "aspects": (derived_methods.get("aspects") or {}).get("reason_code"),
        "anti_vertex": "house_calculation_not_executed",
        "lunar_phases": event_module_availability["lunar_phases"].get("reason_code"),
    }
    for option_name, (module_name, response_path) in module_toggles.items():
        requested = bool(getattr(options, option_name))
        status = modules[module_name]
        # Lunar-phase not_applicable is an attempted upstream search failure;
        # the other not_applicable states are skipped incompatible consumers.
        attempted_failure = (
            module_name == "lunar_phases"
            and requested
            and status == "not_applicable"
        )
        executed = status == "computed" or attempted_failure
        applicable = requested and status != "not_applicable"
        available = status == "computed"
        reason = None
        if not requested:
            reason = "option_not_requested"
        elif status == "not_applicable":
            reason = reason_by_module.get(module_name) or "module_not_applicable"
        add(
            option_name,
            requested=requested,
            executed=executed,
            applicable=applicable,
            available=available,
            source=response_path if executed else None,
            reason_code=reason,
            response_paths=[response_path],
        )

    # Domicile/exaltation is one independently selectable dignity component;
    # the aggregate essential_dignities module may still run for another profile.
    dignity_requested = bool(options.include_domicile_exaltation)
    dignity_status = modules["essential_dignities"]
    dignity_available = dignity_requested and dignity_status == "computed"
    dignity_reason = (derived_methods.get("essential_dignities") or {}).get(
        "reason_code"
    )
    add(
        "include_domicile_exaltation",
        requested=dignity_requested,
        executed=dignity_available,
        applicable=dignity_requested and dignity_status != "not_applicable",
        available=dignity_available,
        source=("derived_methods.essential_dignities.profile_results" if dignity_available else None),
        reason_code=(
            "option_not_requested"
            if not dignity_requested
            else dignity_reason
            if dignity_status == "not_applicable"
            else None
        ),
        response_paths=["derived_methods.essential_dignities"],
    )

    # Extra angles has a structured receipt in astronomical_data even though
    # the legacy modules summary only names Anti-Vertex separately.
    extra_requested = bool(options.include_extra_angles)
    extra_applicable = extra_requested and bool(options.include_houses)
    add(
        "include_extra_angles",
        requested=extra_requested,
        executed=extra_applicable,
        applicable=extra_applicable,
        available=extra_applicable,
        source=("astronomical_data.extra_angles" if extra_applicable else None),
        reason_code=(
            "option_not_requested"
            if not extra_requested
            else "house_calculation_not_executed"
            if not options.include_houses
            else None
        ),
        response_paths=["astronomical_data.extra_angles"],
    )

    # Profile-valued modules whose non-default value is itself the request.
    moon_requested = True
    moon_status = modules["parallax_moon"]
    moon_is_global = options.moon_position_profile == "global_computation_mode"
    moon_executed = moon_is_global or moon_status == "computed"
    moon_available = moon_is_global or moon_status == "computed"
    add(
        "moon_position_profile",
        requested=moon_requested,
        executed=moon_executed,
        applicable=moon_is_global or moon_status != "not_applicable",
        available=moon_available,
        source=(
            "astronomical_data.bodies"
            if moon_is_global
            else "astronomical_data.parallax_moon"
            if moon_status == "computed"
            else None
        ),
        reason_code=(
            "module_not_applicable"
            if not moon_is_global and moon_status == "not_applicable"
            else None
        ),
        response_paths=[
            (
                "astronomical_data.bodies"
                if moon_is_global
                else "astronomical_data.parallax_moon"
            )
        ],
    )

    # Configuration options inherit their parent module state.  ``selected``
    # distinguishes None-valued optional profiles from defaulted profiles that
    # really are executed (house system, aspect set, partile convention, etc.).
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
        available = bool(selected and status == "computed")
        add(
            name,
            requested=bool(selected),
            executed=available,
            applicable=bool(selected and status != "not_applicable"),
            available=available,
            source=path if available else None,
            reason_code=(
                "configuration_not_selected"
                if not selected
                else dignity_reason
                if status == "not_applicable"
                and name in {"bounds_profile", "decan_profile", "triplicity_profile"}
                else "module_not_applicable"
                if status == "not_applicable"
                else None
            ),
            response_paths=[path],
        )

    dependent_flags = {
        "antiscia_include_nodes": (bool(options.include_antiscia), modules["antiscia"], "derived_geometry.antiscia.scope"),
        "include_aspect_perfection": (bool(options.include_aspects), modules["aspects"], "derived_methods.aspects.perfection"),
        "aspect_include_nodes": (bool(options.include_aspects), modules["aspects"], "derived_methods.aspects.participants"),
        "aspect_include_angles": (bool(options.include_aspects and options.include_houses), modules["aspects"], "derived_methods.aspects.degree_based.angle_participation"),
        "triplicity_include_research_comparison": (True, modules["essential_dignities"], "derived_methods.essential_dignities.profile_results"),
    }
    for name, (parent_applicable, status, path) in dependent_flags.items():
        requested = bool(getattr(options, name))
        applicable = requested and parent_applicable and status != "not_applicable"
        available = applicable and status == "computed"
        add(
            name,
            requested=requested,
            executed=available,
            applicable=applicable,
            available=available,
            source=path if available else None,
            reason_code=(
                "option_not_requested"
                if not requested
                else "parent_module_not_requested_or_not_applicable"
                if not applicable
                else None
            ),
            response_paths=[path],
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


# 邊界警告的門檻。三者都是「呈現決定」而非方法裁決：它們不改變任何計算結果，
# 只標示哪些結果對出生時刻或座標的微小誤差特別敏感。門檻取值理由見各常數註解。
#
# 星座邊界 1.0°：星體以最快的月亮計約需 1.6 小時走完，以太陽計約一日。落在此範圍內
# 表示星座歸屬對出生日期／時刻的常見誤差敏感。
SIGN_BOUNDARY_WARNING_DEGREES = 1.0
# 宮頭 1.0°：ASC 每約 4 分鐘移動 1°，故 1° 對應約 4 分鐘的出生時刻誤差——正好是
# 一般人記得的出生時刻精度。MTH-Q-007 A3 已裁決一律顯示距最近宮頭的角距；
# 本警告是那項裁決在「敏感時」的主動提示，兩者不衝突。
CUSP_PROXIMITY_WARNING_DEGREES = 1.0


def _boundary_warnings(
    *,
    bodies: list[dict],
    planet_in_house: dict,
    latitude_regime: dict | None,
    sect: dict | None,
) -> list[dict]:
    """高緯度、宮頭鄰近、星座邊界鄰近三類敏感度提示。

    這些都不是錯誤：計算結果本身有效。它們標示的是「若出生時刻或座標有一點誤差，
    這個結論就會翻掉」，而使用者無從自行看出這一點。
    """

    warnings = []

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
                "docs/product/methods/METHOD_AUDIT.md",
                [
                    "calculation_dossier.calculation_policy.computation_mode",
                    "astronomical_data.atmosphere",
                ],
            )
        )
        # MTH-Q-018 裁決（Sebastian 2026-08-03，甲＋觸發式說明）：
        # MTH-Q-009 的視高度抑制只涵蓋星體／恆星欄位，升降事件維持自己的框架。
        # 這在同一份回應裡會造成一個看起來矛盾的畫面——一邊 altitude_apparent
        # 為 null，一邊升降證據裡有九十幾筆視高度取樣。裁決要求在該組合**實際
        # 發生時**主動說明原因，而不是只寫在契約文件裡等使用者自己去查。
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


# `FPI-2026-08-06-E-011`. Two layers of this attestation used to be written as
# constants describing a local, not-yet-deployed checkout. Under
# `CLASSICAL_ASTROLOGY_PROFILE=private_alpha` — the profile the twenty invitees
# use — the receipt still told each of them that no reverse-proxy layer exists
# and that the VPS is not deployed. That is not stale copy: the whole purpose of
# this attestation is to let a reader work out which enforcement layers touch
# their birth data, and `AGENTS.md` §4A requires those layers to be named. A
# reader who is told "this layer does not exist" concludes no proxy sees the
# request. After deployment that conclusion is wrong.
#
# The first attempt at this wording said, under a hosted profile, that a
# reverse proxy layer "exists". Codex's independent replay sent the finding
# back for that, and it was right: a process knows which profile it was
# started with, and nothing more. It cannot see what is in front of it, so
# "the deployment requires one" is an argument about intent, not an
# observation — the receipt would have been asserting a fact it has no way to
# check, which is the same defect as the original, pointed the other way.
#
# The wording therefore states the profile's *expectation* and says plainly
# that this is a declared intent rather than evidence about this run. Anything
# stronger is a privacy claim, which is Sebastian's to make and a deployment
# canary's to support, not this function's.
_UNCOVERED_LAYERS_BY_PROFILE: dict[str, dict[str, str]] = {
    "local": {
        "reverse_proxy_cdn_waf": (
            "本機執行路徑不存在此層。此陳述只涵蓋本機 profile；"
            "託管部署的形態另行敘述於該 profile 的收據。"
        ),
        "hosting_supervisor": (
            "本機執行，無 hosting supervisor。Infomaniak provider 已選且"
            "identity verification 已通過，但本機 profile 下不涉及該主機。"
        ),
    },
    "private_alpha": {
        "reverse_proxy_cdn_waf": (
            "本 profile **預期**應用程式前方有一層反向代理（規劃為 host NGINX）。"
            "這是本次執行所宣告的部署意圖，**不是本次執行確實具有該層的證據**——"
            "行程只知道自己被以哪個 profile 啟動，看不到自己前面有什麼。"
            "本產品亦未驗證該層的 log 關閉、retention 或轉發標頭處理，"
            "相關證據須由部署方以 deployment canary 提出。"
        ),
        "hosting_supervisor": (
            "本 profile **預期**託管於 Infomaniak VPS（identity verification 已通過）。"
            "同上：這是宣告的部署意圖，不是本次執行的實測結果。"
            "本產品未驗證 host 層的 log、backup、snapshot 或 retention，"
            "hypervisor 與機房人員的存取亦不在本產品控制範圍內。"
        ),
    },
}


def _privacy_attestation(deployment_profile: str | None = None) -> dict:
    """Describe implemented controls without claiming per-request revalidation.

    `deployment_profile` selects the wording for the layers this product does
    not control. An unrecognised or absent profile falls back to `local` and
    says so in `deployment_profile_status`, so a receipt can never silently
    describe the wrong deployment shape again.
    """

    resolved = (
        deployment_profile
        if deployment_profile in _UNCOVERED_LAYERS_BY_PROFILE
        else "local"
    )
    profile_status = (
        "declared_by_running_process"
        if deployment_profile in _UNCOVERED_LAYERS_BY_PROFILE
        else "not_declared_defaulted_to_local"
    )
    uncovered_notes = _UNCOVERED_LAYERS_BY_PROFILE[resolved]

    return {
        "deployment_profile": resolved,
        "deployment_profile_status": profile_status,
        "uncovered_layer_semantics": (
            "these layers are named so the reader can see what this product "
            "does not control; presence of a layer is not a claim about its "
            "behaviour"
        ),
        # 1.3.0：新增 deployment_profile／deployment_profile_status 與
        # uncovered_layer_semantics，且未涵蓋層的敘述改為隨 profile 變動
        # （`FPI-2026-08-06-E-011`）。純新增欄位，既有欄位語意未變。
        "privacy_attestation_version": "1.3.0",
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
                            "test_privacy_lifecycle_guards_requests_exports_"
                            "and_page_exit"
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
                "note": uncovered_notes["reverse_proxy_cdn_waf"],
            },
            {
                "layer": "hosting_supervisor",
                "status": "outside_current_control_scope",
                "note": uncovered_notes["hosting_supervisor"],
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
            "available": anti_vertex is not None,
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
        "privacy": _privacy_attestation(deployment_profile),
    }
