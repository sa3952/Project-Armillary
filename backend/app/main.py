"""FastAPI chart orchestration; facts, geometry and methods stay separate."""

import asyncio
from dataclasses import dataclass
import importlib.metadata
import os
from pathlib import Path
import re
import threading
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

import swisseph as swe
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from http import HTTPStatus

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import (
    BODY_ID_BY_KEY,
    CLASSICAL_BODIES,
    NODE_BODIES,
    OUTER_BODIES,
    MODERN_MINOR_BODIES,
    LUNAR_APSIDES,
    FIXED_STARS,
    DECLINATION_ASPECT_ORB,
)
from .ephemeris import (
    FullEphemerisRequiredError,
    ensure_ephemeris_initialized_for_thread,
    release_ephemeris_for_thread,
    has_full_ephemeris_files,
    init_ephemeris,
)
from .schemas import (
    ChartRequest,
    HostedBoundaryErrorResponse,
    HostedValidationResponse,
    OptionsInput,
    PlaceSearchRequest,
)
from .core.trace import Trace
from .core.tz_database import TZ_DATABASE
from .core.computation_mode import ComputationContext
from .core.time_utils import (
    AmbiguousLocalTimeChoiceRequiredError,
    NonexistentLocalTimeError,
    compute_time_conversion,
)
from .core.bodies import compute_bodies, derive_node_antipode, make_longitude_sampler
from .core.essential_dignities import compute_essential_dignities
from .core.aspects import compute_aspects
from .core.declination import compute_declination_aspects, declination_participants
from .core.houses import (
    EXTRA_ANGLE_PROVENANCE,
    HouseSystemUnavailableError,
    compute_houses,
)
from .core.house_placements import compute_planet_house_placements
from .core.birth_time_sensitivity import (
    approximate_hour_representative_request,
    build_approximate_hour_sensitivity,
    build_date_only_sensitivity,
)
from .core.fixed_stars import compute_fixed_stars, fixed_star_receipt
from .core.lots import determine_sect, compute_lots
from .core.antiscia import compute_antiscia
from .core.moon import find_voc_candidates, determine_void_of_course, VOC_METHOD_NAME
from .core.lunar_events import compute_primary_phases, compute_previous_eclipses
from .core.horizon_events import compute_horizon_events
from .core.root_finding import wrap_to_signed_180
from .core.dossier import DOSSIER_VERSION, build_calculation_dossier
from .core.place_catalog import (
    PlaceCatalog,
    PlaceCatalogUnavailableError,
)
from .privacy_logging import (
    DOMAIN_ERROR_STATE_KEY,
    PrivacyBoundaryMiddleware,
    emit_security_event,
)
from .request_limits import (
    RETRY_AFTER_SECONDS,
    ApiMethodBoundary,
    DeclaredHostBoundary,
    ChartRequestBoundary,
    RequestCapacityBoundary,
    mark_compute_entered,
)
from .runtime_static import RuntimeStaticFiles
from .frontend_release import load_runtime_release
from .frontend_assets import discover_source_assets
from .settings import AppProfile, AppSettings, load_settings

SCHEMA_VERSION = "0.13.0"

_CLASSICAL_KEYS = tuple(body["key"] for body in CLASSICAL_BODIES)
_OUTER_KEYS = tuple(body["key"] for body in OUTER_BODIES)
_NODE_KEYS = tuple(body["key"] for body in NODE_BODIES)

init_ephemeris()

# Bundled ephemeris availability is immutable for the process lifetime.
_HAS_FULL_EPHEMERIS = has_full_ephemeris_files()

# Python distribution and embedded Swiss C-library versions are distinct.
try:
    _PYSWISSEPH_DISTRIBUTION_VERSION: str | None = (
        importlib.metadata.version("pyswisseph")
    )
except importlib.metadata.PackageNotFoundError:
    _PYSWISSEPH_DISTRIBUTION_VERSION = None

def health_check() -> dict[str, object]:
    """Report process liveness, not end-to-end calculation readiness."""
    return {
        "status": "ok",
        "ready": True,
        "readiness_scope": "process_liveness_only",
    }


def client_configuration(
    settings: AppSettings,
    release_identity: dict[str, object] | None = None,
):
    """Expose the profile and verified deployment identity, when available."""
    response: dict[str, object] = {"profile": settings.profile.value}
    if release_identity is not None:
        response["release_identity"] = release_identity
    return response


def _exception_response(
    *,
    status_code: int,
    code: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code}},
        headers=headers,
    )


async def _handle_domain_error(request: Request, exc: Exception):
    """Map closed domain failures without giving each class a parallel owner."""

    status_code = 422
    headers = None
    code = getattr(exc, "code", "swiss_ephemeris_error")
    if isinstance(exc, FullEphemerisRequiredError):
        status_code = 503
    elif isinstance(exc, PlaceCatalogUnavailableError):
        status_code = 503
    elif isinstance(exc, ComputeCapacityExhaustedError):
        status_code = 503
        headers = {"Retry-After": str(exc.retry_after_seconds)}
    elif isinstance(exc, swe.Error):
        status_code = 500
        code = "swiss_ephemeris_error"
    request.scope.setdefault("state", {})[DOMAIN_ERROR_STATE_KEY] = code
    return _exception_response(
        status_code=status_code,
        code=code,
        headers=headers,
    )


_HOSTED_VALIDATION_LOCATIONS = frozenset(
    {
        "body",
        "datetime",
        "year",
        "month",
        "day",
        "hour",
        "minute",
        "second",
        "birth_time_precision",
        "fold",
        "timezone",
        "mode",
        "iana_name",
        "utc_offset_hours",
        "location",
        "latitude",
        "longitude",
        "altitude_m",
        "place_label",
        "location_source",
        "source_record_id",
        "location_precision",
        "atmosphere",
        "pressure_hpa",
        "temperature_c",
        "options",
        "computation_mode",
        "center",
        "zodiac",
        "ayanamsa",
        "position_mode",
        "ecliptic_frame",
        "nutation",
        "query",
        "country_code",
        "limit",
    }
) | frozenset(OptionsInput.model_fields)
_HOSTED_VALIDATION_TYPE = re.compile(r"^[a-z0-9_.]{1,64}$")


def _hosted_validation_detail(exc: RequestValidationError) -> list[dict]:
    """Return only the closed fields the bundled client actually consumes."""

    sanitized = []
    for issue in exc.errors():
        raw_type = issue.get("type")
        issue_type = (
            raw_type
            if isinstance(raw_type, str)
            and _HOSTED_VALIDATION_TYPE.fullmatch(raw_type)
            else "invalid"
        )
        location: list[str | int] = []
        for item in issue.get("loc", ()):
            if isinstance(item, int):
                location.append(item)
            elif item in _HOSTED_VALIDATION_LOCATIONS:
                location.append(item)
            else:
                location.append("input")
        sanitized.append({"type": issue_type, "loc": location})
    return sanitized


async def _handle_http_exception(request: Request, exc: Exception):
    """Give the framework's own refusals the shape this service declares.

    `detail` carried three mutually exclusive types: an object for domain
    refusals, a list for validation, and a bare string wherever Starlette
    answered — the asset mount's 405 for a non-GET, for instance.  A consumer
    could not branch on it.  Two shapes remain and they mean different things:
    an object names one refusal, a list enumerates field errors.
    """

    status_code = getattr(exc, "status_code", 500)
    headers = dict(getattr(exc, "headers", None) or {})
    code = HTTPStatus(status_code).phrase.lower().replace(" ", "_")
    return _exception_response(
        status_code=status_code,
        code=code,
        headers=headers or None,
    )


async def _handle_request_validation(
    request: Request,
    exc: RequestValidationError,
):
    detail = _hosted_validation_detail(exc)
    # A body the parser never read is a syntax refusal.  422 states that the
    # syntax was understood and the content was semantically wrong, so a client
    # could not tell "my JSON is broken" from "my values were rejected".
    malformed = bool(detail) and all(
        issue["type"] == "json_invalid" for issue in detail
    )
    return JSONResponse(
        status_code=400 if malformed else 422,
        content={"detail": detail},
    )


# Swiss center／sidereal mode are process-global; serialize per worker.
_COMPUTE_LOCK = threading.Lock()

# Bound queue wait without interrupting an in-flight Swiss calculation.
_COMPUTE_LOCK_WAIT_TIMEOUT_SECONDS = 20.0
# The retry contract is declared once, in the boundary that also refuses.
_COMPUTE_LOCK_RETRY_AFTER_SECONDS = RETRY_AFTER_SECONDS
_BUILD_IDENTITY_CONTEXT: ContextVar[dict | None] = ContextVar(
    "classical_astrology_build_identity",
    default=None,
)
# Process identity belongs in context, not in caller-controlled chart input.
_DEPLOYMENT_PROFILE_CONTEXT: ContextVar[str | None] = ContextVar(
    "classical_astrology_deployment_profile",
    default=None,
)


def _aspect_participants(
    options,
    bodies_all: list,
    lots: dict,
    angles: dict,
    *,
    angle_aspects_applicable: bool,
) -> list[dict]:
    by_key = {body["key"]: body for body in bodies_all}
    participants = []

    def add_body(key: str, category: str):
        body = by_key.get(key)
        if body is None:
            return
        participants.append(
            {
                "key": key,
                "name": body["name"],
                "longitude": body["longitude"],
                "speed_longitude": body["speed_longitude"],
                "body_id": BODY_ID_BY_KEY[key],
                "category": category,
            }
        )

    groups = (
        (_CLASSICAL_KEYS, "classical_planet", True),
        (_NODE_KEYS, "lunar_node", options.aspect_include_nodes),
        (_OUTER_KEYS, "outer_planet", options.include_outer_planets),
    )
    for keys, category, enabled in groups:
        if enabled:
            for key in keys:
                add_body(key, category)

    point_groups = (
        (lots or {}, (("fortune", "福點"), ("spirit", "精神點")), "lot", options.include_lots),
        (angles, (("asc", "上升點"), ("mc", "天頂")), "angle", options.aspect_include_angles and angle_aspects_applicable),
    )
    for values, definitions, category, enabled in point_groups:
        if enabled:
            for key, name in definitions:
                longitude = values.get(key)
                if longitude is None:
                    continue
                participants.append({
                    "key": key, "name": name, "longitude": longitude,
                    "speed_longitude": None, "body_id": None,
                    "category": category,
                })
    return participants


_EXTRA_ANGLE_SOURCE_LABEL = {
    "swiss_ephemeris_houses_ex": "swe.houses_ex 附帶回傳",
    "vertex_longitude_antipode": "由 Vertex 黃經對蹠導出",
}


def _extra_angles_container(
    selected: dict,
    *,
    applicable: bool,
    reason_code: str | None,
) -> dict:
    """Describe the members that are actually here.

    The note used to be a hand-written sentence saying five angles came from
    `swe.houses_ex`.  Requesting only the anti-vertex produced one member that
    came from somewhere else, under a container asserting both the count and the
    source — the member's own receipt was right and the container contradicted
    it.  Both are now read off the members.
    """

    sources = sorted({
        entry["calculation_source"] for entry in selected.values()
    })
    grouped = {
        source: sorted(
            key for key, entry in selected.items()
            if entry["calculation_source"] == source
        )
        for source in sources
    }
    note = "；".join(
        f"{len(keys)} 個角點{_EXTRA_ANGLE_SOURCE_LABEL[source]}"
        for source, keys in grouped.items()
    ) or "本次沒有角點"
    return {
        "requested": True,
        "executed": applicable,
        "applicable": applicable,
        "available": applicable,
        "reason_code": reason_code,
        "source": sources[0] if len(sources) == 1 else "per_angle",
        "semantics": "non_classical_technical_angles_opt_in",
        "note": (
            f"{note}。皆非古典占星的四角。本產品輸出其數值，不對其占星用途表示任何立場。"
        ),
        "angles": selected,
    }


def _unavailable_state(reason_code: str, *, requested: bool) -> dict:
    return {
        "requested": requested,
        "executed": False,
        "applicable": False,
        "available": False,
        "reason_code": reason_code,
    }


class ComputeCapacityExhaustedError(Exception):
    """Waiting for the Swiss compute lock exceeded the bounded queue budget."""

    code = "compute_capacity_exhausted"
    retry_after_seconds = _COMPUTE_LOCK_RETRY_AFTER_SECONDS


def compute_chart(req: ChartRequest):
    # Swiss global state requires serialization; bound only the wait, never an
    # in-flight calculation, so overload becomes an explicit retryable refusal.
    if not _COMPUTE_LOCK.acquire(timeout=_COMPUTE_LOCK_WAIT_TIMEOUT_SECONDS):
        raise ComputeCapacityExhaustedError()
    return _compute_chart_with_acquired_lock(req)


def _compute_chart_with_acquired_lock(req: ChartRequest):
    try:
        return _compute_chart_locked(req)
    finally:
        try:
            release_ephemeris_for_thread()
        finally:
            _COMPUTE_LOCK.release()


async def compute_chart_for_request(req: ChartRequest, request: Request):
    """Cancel only queued work; once Swiss starts, retain lock and admission."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _COMPUTE_LOCK_WAIT_TIMEOUT_SECONDS
    while True:
        if _COMPUTE_LOCK.acquire(blocking=False):
            if isinstance(getattr(request, "scope", None), dict):
                mark_compute_entered(request.scope)
            break
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise ComputeCapacityExhaustedError()
        await asyncio.sleep(min(0.05, remaining))

    worker = asyncio.create_task(
        run_in_threadpool(_compute_chart_with_acquired_lock, req)
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Native Swiss work cannot be cancelled safely.  Keep this request's
        # capacity and the process-global lock until its worker actually exits.
        await asyncio.shield(worker)
        raise


@dataclass(frozen=True)
class AstronomicalFacts:
    angles: Any
    asc: Any
    bodies: Any
    bodies_all: Any
    bodies_by_key: Any
    body_defs: Any
    build_identity: Any
    ctx: Any
    deployment_profile: Any
    event_module_availability: Any
    extra_angles: Any
    fixed_stars: Any
    horizon_events: Any
    house_division: Any
    houses_for_placement: Any
    input_request: Any
    jd_ut: Any
    latitude_regime: Any
    longitude_at: Any
    lunar_apsides: Any
    lunar_events: Any
    node_defs: Any
    nodes: Any
    parallax_moon: Any
    planet_in_house: Any
    req: Any
    representative_offset_seconds: int
    time_conv: Any
    trace: Any


@dataclass(frozen=True)
class DerivedChart:
    antiscia: Any
    aspects: Any
    declination_aspects: Any
    essential_dignities: Any
    lots: Any
    sect: Any
    void_of_course: Any


def _astronomical_facts(req: ChartRequest) -> AstronomicalFacts:
    input_request = req
    representative_offset_seconds = 1800
    if input_request.birth_time_precision == "approximate_hour":
        req, representative_offset_seconds = (
            approximate_hour_representative_request(input_request)
        )
    elif input_request.birth_time_precision == "date_only":
        # 12:00 只是重現性良好的 representative anchor；input receipt 仍保留
        # 00:00:00 日期容器，且 Dossier 明示這不是出生時刻。
        req = input_request.model_copy(
            update={
                "datetime": input_request.datetime.model_copy(
                    update={"hour": 12, "minute": 0, "second": 0.0}
                )
            }
        )

    build_identity = _BUILD_IDENTITY_CONTEXT.get() or {
        "status": "unavailable",
        "source_revision": None,
        "revision_source": None,
    }
    deployment_profile = _DEPLOYMENT_PROFILE_CONTEXT.get()
    # Each worker thread must bind the verified ephemeris path while holding
    # the Swiss-state lock; later calls on that thread are no-ops.
    ensure_ephemeris_initialized_for_thread()
    trace = Trace()

    if not _HAS_FULL_EPHEMERIS:
        trace.add(
            "⚠ 星曆檔檢查",
            note="backend/ephe 目錄未偵測到完整星曆檔，計算將退回 Moshier 半分析模型（精度較低）。請參閱 README 下載星曆檔。",
        )

    time_conv = compute_time_conversion(
        req.datetime,
        req.timezone,
        req.location,
        trace,
        require_explicit_fold=(
            input_request.birth_time_precision == "approximate_hour"
        ),
        input_semantics=(
            "representative_computational_anchor_not_birth_time"
            if input_request.birth_time_precision == "date_only"
            else "representative_midpoint_not_exact_birth_time"
            if input_request.birth_time_precision == "approximate_hour"
            else "exact_birth_time"
        ),
    )
    jd_ut = time_conv["jd_ut"]

    ctx = ComputationContext(req.computation_mode, req.location)
    ctx.describe(trace, jd_ut)
    time_conv["ayanamsa"] = ctx.ayanamsa_value(jd_ut)

    # --- astronomical_data：原始天文事實 ---
    body_defs = (
        [{"id": b["id"], "key": b["key"], "zh": b["zh"]} for b in CLASSICAL_BODIES]
        + ([{"id": b["id"], "key": b["key"], "zh": b["zh"]} for b in OUTER_BODIES] if req.options.include_outer_planets else [])
        + ([dict(b) for b in MODERN_MINOR_BODIES] if req.options.include_chiron else [])
    )
    bodies = compute_bodies(body_defs, jd_ut, ctx, req.atmosphere, trace, physical=True)

    # Optional Moon-only topocentric mode retains the geocentric reference and
    # makes the selected downstream origin explicit; it is not a precision claim.
    parallax_moon = None
    moon_ctx = None
    if req.options.moon_position_profile == "moon_only_topocentric_v1":
        geocentric_moon = next(body for body in bodies if body["key"] == "moon")
        moon_mode = req.computation_mode.model_copy(update={"center": "topocentric"})
        moon_ctx = ComputationContext(moon_mode, req.location)
        topocentric_moon = compute_bodies(
            [next(body for body in body_defs if body["key"] == "moon")],
            jd_ut,
            moon_ctx,
            req.atmosphere,
            trace,
            physical=True,
        )[0]
        geocentric_reference = {
            **geocentric_moon,
            "coordinate_origin": "earth_center",
            "role": "retained_reference_not_used_by_effective_downstream",
        }
        effective_topocentric = {
            **topocentric_moon,
            "coordinate_origin": "topocentric_observer",
            "effective_coordinate_origin": "topocentric_observer",
            "role": "effective_moon_used_by_downstream",
        }
        bodies = [
            effective_topocentric if body["key"] == "moon" else body
            for body in bodies
        ]
        moon_longitude_delta = wrap_to_signed_180(
            topocentric_moon["longitude"] - geocentric_moon["longitude"]
        )
        parallax_moon = {
            "method": "moon_only_topocentric_comparison_v1",
            "method_status": "sebastian_authorized_2026_08_04",
            "method_authority": "Sebastian ruling 2026-08-04 CMP-A10",
            "requested": True,
            "executed": True,
            "applicable": True,
            "available": True,
            "source": "Swiss Ephemeris SEFLG_TOPOCTR with observer geoposition",
            "coordinate_frame": (
                "mixed_origin_geocentric_chart_with_topocentric_effective_moon"
            ),
            "global_center": "geocentric",
            "effective_moon_center": "topocentric",
            "geocentric_reference": geocentric_reference,
            "topocentric_effective": effective_topocentric,
            "longitude_delta_degrees": moon_longitude_delta,
            "effective_downstream_paths": [
                "astronomical_data.bodies[moon]",
                "derived_methods.planet_in_house",
                "derived_geometry.antiscia",
                "derived_methods.lots",
                "derived_methods.void_of_course",
                "derived_methods.declination_aspects",
                "derived_methods.aspects",
                "derived_methods.essential_dignities",
                "birth_time_sensitivity",
            ],
            "claim": "different_observer_origin_not_more_accurate",
            "difference": {
                "longitude_signed_degrees": moon_longitude_delta,
                "latitude_degrees": (
                    topocentric_moon["latitude"] - geocentric_moon["latitude"]
                ),
                "right_ascension_signed_degrees": wrap_to_signed_180(
                    topocentric_moon["right_ascension"]
                    - geocentric_moon["right_ascension"]
                ),
                "declination_degrees": (
                    topocentric_moon["declination"] - geocentric_moon["declination"]
                ),
            },
            "downstream_policy": {
                "effective_moon": "topocentric",
                "other_bodies": "geocentric",
                "frame_semantics": "intentional_mixed_origin_profile",
                "interpretive_claim": None,
            },
        }

    lunar_apsides = None
    if req.options.include_lilith_priapus:
        apsis_points = compute_bodies(
            [dict(body) for body in LUNAR_APSIDES],
            jd_ut,
            ctx,
            req.atmosphere,
            trace,
            physical=False,
            moon_relative=True,
        )
        lunar_apsides = {
            "method": "swiss_lunar_apsides_named_points_v1",
            "method_status": "sebastian_authorized_2026_08_04",
            "method_authority": "Sebastian ruling 2026-08-04 CMP-A6 option 3",
            "requested": True,
            "executed": True,
            "applicable": ctx.mode.center in {"geocentric", "topocentric"},
            "available": all(
                point.get("longitude") is not None for point in apsis_points
            ),
            "source": "Swiss Ephemeris MEAN_APOG, INTP_APOG and INTP_PERG",
            "coordinate_policy": {
                "orbital_reference": "geocentric_lunar_orbit_model",
                "topocentric_parallax_applied": False,
                "reason": (
                    "Swiss Ephemeris does not distinguish topocentric from "
                    "geocentric lunar nodes/apogees for these orbital points"
                ),
            },
            "aspect_participation": "not_included_by_this_option",
            "points": apsis_points,
            "scope": {
                "classification": "modern_research_additional_points",
                "automatic_aspects": False,
                "automatic_classical_methods": False,
                "natural_priapus_is_independently_calculated": True,
                "natural_priapus_is_not_forced_opposite_natural_lilith": True,
            },
        }

    node_defs = [{"id": b["id"], "key": b["key"], "zh": b["zh"]} for b in NODE_BODIES]
    nodes = compute_bodies(
        node_defs,
        jd_ut,
        ctx,
        req.atmosphere,
        trace,
        physical=False,
        moon_relative=True,
    )
    if req.options.include_south_nodes:
        nodes_by_key = {node["key"]: node for node in nodes}
        nodes.extend(
            [
                derive_node_antipode(
                    nodes_by_key["true_node"],
                    key="true_south_node",
                    name="南交點(密切)",
                    body_id=BODY_ID_BY_KEY["true_node"],
                    jd_ut=jd_ut,
                    ctx=ctx,
                    atmosphere=req.atmosphere,
                    trace=trace,
                ),
                derive_node_antipode(
                    nodes_by_key["mean_node"],
                    key="mean_south_node",
                    name="南交點(平均)",
                    body_id=BODY_ID_BY_KEY["mean_node"],
                    jd_ut=jd_ut,
                    ctx=ctx,
                    atmosphere=req.atmosphere,
                    trace=trace,
                ),
            ]
        )
    bodies_all = bodies + nodes
    bodies_by_key = {b["key"]: b for b in bodies_all}
    longitude_at = make_longitude_sampler(ctx, moon_ctx=moon_ctx)

    houses = None
    houses_for_placement = None
    latitude_regime = None
    # `ctx.horizon_meaningful` is the one premise that decides whether an
    # Earth-relative frame exists at all.  Only the planet placement read it, so
    # a heliocentric chart returned an applicable ascendant beside a refusal that
    # named the same frame.  Every consumer of that frame now reads it here.
    earth_frame = ctx.horizon_meaningful
    if req.options.include_houses and earth_frame:
        houses = compute_houses(
            req.options.house_system, jd_ut, req.location, ctx, trace
        )
        latitude_regime = houses["latitude_regime"]
        asc = houses["asc"]
        angles = {
            "requested": True,
            "executed": True,
            "applicable": True,
            "available": True,
            "reason_code": None,
            "source": "Swiss Ephemeris houses_ex",
            "asc": houses["asc"],
            "mc": houses["mc"],
            "desc": houses["desc"],
            "ic": houses["ic"],
            "armc": houses["armc"],
        }
        # Either None, or one of two differently shaped receipts.
        extra_angles: dict | None = None
        if req.options.include_extra_angles or req.options.include_anti_vertex:
            selected_extra_angles = {}
            if req.options.include_extra_angles:
                selected_extra_angles.update(
                    {
                        key: {
                            "longitude": value,
                            **EXTRA_ANGLE_PROVENANCE[key],
                        }
                        for key, value in houses["extra_angles"].items()
                    }
                )
            if req.options.include_anti_vertex:
                vertex = houses["extra_angles"]["vertex"]
                selected_extra_angles["anti_vertex"] = {
                    "longitude": (vertex + 180.0) % 360.0,
                    "source_vertex_longitude_degrees": vertex,
                    **EXTRA_ANGLE_PROVENANCE["anti_vertex"],
                }
            extra_angles = _extra_angles_container(
                selected_extra_angles, applicable=True, reason_code=None
            )

        house_division = {
            "method": "swiss_ephemeris_house_division_v1",
            "method_status": "provisional_pending_method_audit",
            "method_authority": None,
            "execution_status": "computed",
            "system_code": req.options.house_system,
            "system_name": houses["system_name"],
            "cusps": houses["cusps"],
            "angles_source": "astronomical_data.angles",
        }
        houses_for_placement = houses
        planet_in_house = compute_planet_house_placements(
            bodies,
            houses_for_placement,
            ctx,
            trace,
        )
    else:
        asc = None
        unavailable_reason = (
            "non_earth_observer_center"
            if req.options.include_houses
            else "house_calculation_not_executed"
        )
        angles = {
            **_unavailable_state(
                unavailable_reason, requested=req.options.include_houses
            ),
            "status": (
                "not_applicable"
                if req.options.include_houses
                else "not_requested"
            ),
            "source": None,
            "asc": None,
            "mc": None,
            "desc": None,
            "ic": None,
            "armc": None,
        }
        # Fail closed by presence, the way the nodes already do: the requested
        # angles stay in the response with a null longitude and the reason, so a
        # consumer sees that the module answered rather than that it vanished.
        if req.options.include_extra_angles or req.options.include_anti_vertex:
            # Populated only when the dependency was requested: an angle that was
            # asked for and could not be produced says so with a null longitude,
            # while houses nobody asked for leave nothing behind.
            unavailable_angles = {}
            if req.options.include_extra_angles:
                unavailable_angles.update({
                    key: {
                        "longitude": None,
                        "reason_code": unavailable_reason,
                        **EXTRA_ANGLE_PROVENANCE[key],
                    }
                    for key in EXTRA_ANGLE_PROVENANCE
                    if key != "anti_vertex"
                })
            if req.options.include_anti_vertex:
                unavailable_angles["anti_vertex"] = {
                    "longitude": None,
                    "source_vertex_longitude_degrees": None,
                    "reason_code": unavailable_reason,
                    **EXTRA_ANGLE_PROVENANCE["anti_vertex"],
                }
            extra_angles = {
                **_extra_angles_container(
                    unavailable_angles,
                    applicable=False,
                    reason_code=unavailable_reason,
                ),
                "status": "not_applicable",
            }
        else:
            extra_angles = None
        house_division = {
            **_unavailable_state(
                "house_calculation_disabled", requested=False
            ),
            "method": None,
            "method_status": None,
            "method_authority": None,
            "execution_status": "not_requested",
            "requested_system_code": req.options.house_system,
            "system_code": None,
            "system_name": None,
            "cusps": [],
            "angles_source": None,
        }
        # The placement answers for itself rather than being told it was not
        # requested: when houses were asked for and the observer has no horizon,
        # the honest receipt is the module's own not_applicable with its own
        # reason code, which is the same premise the angles above now read.
        planet_in_house = (
            compute_planet_house_placements(bodies, None, ctx, trace)
            if req.options.include_houses
            else {
                "method": None,
                "method_status": None,
                "method_authority": None,
                "execution_status": "not_requested",
                "reason_codes": ["house_calculation_not_executed"],
                "house_system_code": None,
                "house_system_name": None,
                "interval_semantics": None,
                "placements": [],
            }
        )
        trace.add(
            "宮位與角點",
            inputs={"include_houses": False},
            result={"execution_status": "not_requested"},
            note=(
                "依 request 真正略過 swe.houses_ex；未產生宮頭、ASC、MC、"
                "額外角點或行星落宮。"
            ),
        )

    fixed_stars = []
    if req.options.include_fixed_stars:
        fixed_stars = compute_fixed_stars(FIXED_STARS, jd_ut, ctx, req.atmosphere, trace)

    lunar_events = {}
    event_module_availability = {
        "lunar_phases": {
            "requested": req.options.include_lunar_phases,
            "status": (
                "not_requested"
                if not req.options.include_lunar_phases
                else "computed"
            ),
            "reason_code": None,
        }
    }
    if req.options.include_lunar_phases:
        try:
            lunar_events.update(compute_primary_phases(jd_ut, trace))
        except FullEphemerisRequiredError:
            event_module_availability["lunar_phases"] = {
                "requested": True,
                "status": "not_applicable",
                "reason_code": (
                    "full_ephemeris_unavailable_for_search_window"
                ),
            }
            trace.add(
                "主要月相事件",
                note=(
                    "請求日期的核心命盤可計算，但月相搜尋會離開完整 "
                    "Swiss Ephemeris files 覆蓋範圍；此模組標為不可用。"
                ),
            )
    if req.options.include_eclipses:
        lunar_events["eclipses"] = compute_previous_eclipses(jd_ut, trace)

    horizon_events = {}
    if req.options.include_rise_set_transits:
        horizon_events = compute_horizon_events(
            body_defs,
            jd_ut,
            req.location,
            req.atmosphere,
            trace,
        )

    return AstronomicalFacts(
        angles=angles,
        asc=asc,
        bodies=bodies,
        bodies_all=bodies_all,
        bodies_by_key=bodies_by_key,
        body_defs=body_defs,
        build_identity=build_identity,
        ctx=ctx,
        deployment_profile=deployment_profile,
        event_module_availability=event_module_availability,
        extra_angles=extra_angles,
        fixed_stars=fixed_stars,
        horizon_events=horizon_events,
        house_division=house_division,
        houses_for_placement=houses_for_placement,
        input_request=input_request,
        jd_ut=jd_ut,
        latitude_regime=latitude_regime,
        longitude_at=longitude_at,
        lunar_apsides=lunar_apsides,
        lunar_events=lunar_events,
        node_defs=node_defs,
        nodes=nodes,
        parallax_moon=parallax_moon,
        planet_in_house=planet_in_house,
        req=req,
        representative_offset_seconds=representative_offset_seconds,
        time_conv=time_conv,
        trace=trace,
    )


def _derived_chart(facts: AstronomicalFacts) -> DerivedChart:
    (angles, asc, bodies, bodies_all, bodies_by_key, ctx, input_request, jd_ut, longitude_at, lunar_apsides, req, time_conv, trace,) = (
        facts.angles,
        facts.asc,
        facts.bodies,
        facts.bodies_all,
        facts.bodies_by_key,
        facts.ctx,
        facts.input_request,
        facts.jd_ut,
        facts.longitude_at,
        facts.lunar_apsides,
        facts.req,
        facts.time_conv,
        facts.trace,
    )

    # --- derived_geometry：純幾何轉換，不做「是否成立」的判斷 ---
    antiscia = {}
    if req.options.include_antiscia:
        # Adopted scope: classical seven, plus nodes only when explicitly requested.
        antiscia_keys = set(_CLASSICAL_KEYS)
        if req.options.antiscia_include_nodes:
            antiscia_keys |= set(_NODE_KEYS)
        antiscia_input = [
            body for body in bodies_all if body["key"] in antiscia_keys
        ]
        antiscia = compute_antiscia(
            antiscia_input,
            trace,
            ayanamsa=time_conv["ayanamsa"] or 0.0,
        )
        antiscia["scope"] = {
            "ruling": "MTH-Q-008 乙 (2026-08-03)",
            "included": (
                ["classical_planets", "lunar_nodes"]
                if req.options.antiscia_include_nodes
                else ["classical_planets"]
            ),
            "excluded": (
                ["fixed_stars", "angles", "outer_planets", "lots"]
                + ([] if req.options.antiscia_include_nodes else ["lunar_nodes"])
            ),
            "included_keys": [body["key"] for body in antiscia_input],
            "semantics": (
                "geometric_mirror_coordinates_only_no_relationship_verdict_no_orb"
            ),
        }

    # --- derived_methods：帶技法假設的判斷結果，每項皆標註具名 method ---
    sect = None
    if req.options.include_lots and req.options.include_houses:
        sect = determine_sect(bodies_by_key["sun"]["altitude_true"], trace)

    lots = {}
    if req.options.include_lots and req.options.include_houses:
        if sect is None:
            raise RuntimeError("sect must exist when lots are requested")
        lots = compute_lots(asc, bodies_by_key["sun"]["longitude"], bodies_by_key["moon"]["longitude"], sect, trace)
    elif req.options.include_lots:
        sect = {
            **_unavailable_state(
                "house_calculation_not_executed", requested=True
            ),
            "method": None,
            "method_status": None,
            "method_authority": None,
            "execution_status": "not_applicable",
            "source": None,
            "is_day": None,
        }
        lots = {
            **_unavailable_state(
                "house_calculation_not_executed", requested=True
            ),
            "method": None,
            "method_status": None,
            "method_authority": None,
            "execution_status": "not_applicable",
            "source": None,
            "fortune": None,
            "spirit": None,
            "depends_on_sect": None,
        }
        trace.add(
            "Sect 與 Lots",
            inputs={"include_lots": True, "include_houses": False},
            result={"execution_status": "not_applicable"},
            note="Lots 依賴 ASC；無宮位模式不以代表時刻產生 ASC 或 Sect 下游判定。",
        )

    void_of_course = {}
    if req.options.include_void_of_course:
        if ctx.horizon_meaningful:
            classical_keys_no_moon = {b["key"] for b in CLASSICAL_BODIES if b["key"] != "moon"}
            other_bodies_for_voc = [
                {**body, "body_id": BODY_ID_BY_KEY[body["key"]]}
                for body in bodies_all
                if body["key"] in classical_keys_no_moon
            ]
            voc_candidates = find_voc_candidates(
                bodies_by_key["moon"],
                other_bodies_for_voc,
                trace,
                jd_ut=jd_ut,
                longitude_at=longitude_at,
                moon_id=BODY_ID_BY_KEY["moon"],
            )
            void_of_course = determine_void_of_course(voc_candidates, trace)
        else:
            trace.add("月空亡(VOC)", note="⚠ heliocentric/barycentric 模式下無地平座標基礎，VOC 無法定義，回傳 null。")
            void_of_course = {
                "method": VOC_METHOD_NAME,
                "method_status": "provisional_pending_method_audit",
                "method_authority": None,
                "is_void_of_course": None,
                "time_to_sign_exit_hours": None, "next_completing_aspect": None, "all_candidates": [],
            }

    declination_aspects = {}
    if req.options.include_declination_aspects:
        declination_aspects = compute_declination_aspects(
            declination_participants(
                bodies_all,
                include_nodes=req.options.aspect_include_nodes,
            ),
            req.options.declination_aspect_orb_degrees,
            trace,
            default_orb=DECLINATION_ASPECT_ORB,
        )

    aspects = {}
    if req.options.include_aspects:
        angle_aspects_applicable = (
            req.options.include_houses
            and req.computation_mode.center in {"geocentric", "topocentric"}
            and req.computation_mode.ecliptic_frame == "of_date"
            and req.computation_mode.nutation
        )
        aspects = compute_aspects(
            _aspect_participants(
                req.options,
                bodies_all,
                lots,
                angles,
                angle_aspects_applicable=angle_aspects_applicable,
            ),
            trace,
            orb_profile_key=req.options.aspect_orb_profile,
            aspect_set_profile_key=req.options.aspect_set_profile,
            orb_scale_percent=req.options.aspect_orb_scale_percent,
            fixed_orb_degrees=req.options.aspect_fixed_orb_degrees,
            angle_orb_degrees=req.options.aspect_angle_orb_degrees,
            angles_requested=req.options.aspect_include_angles,
            angles_applicable=angle_aspects_applicable,
            angle_inapplicable_reason_code=(
                "house_calculation_not_executed"
                if not req.options.include_houses
                else "angle_frame_incompatible_with_body_longitudes"
            ),
            partile_profile_key=req.options.partile_profile,
            include_perfection=req.options.include_aspect_perfection,
            jd_ut=jd_ut,
            longitude_at=longitude_at,
        )

    dignity_sect_is_day = None
    dignity_sect_basis = "unavailable_date_only"
    if input_request.birth_time_precision != "date_only" and ctx.horizon_meaningful:
        dignity_sect_is_day = bodies_by_key["sun"]["altitude_true"] > 0.0
        dignity_sect_basis = (
            "sun_center_true_altitude_gt_zero_v1_exact_birth_time"
            if input_request.birth_time_precision == "exact"
            else (
                "sun_center_true_altitude_gt_zero_v1_"
                "representative_midpoint_not_exact_birth_time"
            )
        )
    elif not ctx.horizon_meaningful:
        dignity_sect_basis = "unavailable_for_display_center"
    essential_dignities = compute_essential_dignities(
        bodies=[body for body in bodies if body["key"] in _CLASSICAL_KEYS],
        include_domicile_exaltation=(
            req.options.include_domicile_exaltation
        ),
        requested_explicitly=bool(
            (
                "include_domicile_exaltation"
                in req.options.model_fields_set
                and req.options.include_domicile_exaltation
            )
            or (
                "bounds_profile" in req.options.model_fields_set
                and req.options.bounds_profile is not None
            )
            or (
                "decan_profile" in req.options.model_fields_set
                and req.options.decan_profile is not None
            )
            or (
                "triplicity_profile" in req.options.model_fields_set
                and req.options.triplicity_profile is not None
            )
            or (
                "triplicity_include_research_comparison"
                in req.options.model_fields_set
                and req.options.triplicity_include_research_comparison
            )
        ),
        domicile_exaltation_defaulted=(
            req.options.include_domicile_exaltation
            and "include_domicile_exaltation"
            not in req.options.model_fields_set
        ),
        bounds_profile=req.options.bounds_profile,
        decan_profile=req.options.decan_profile,
        triplicity_profile=req.options.triplicity_profile,
        include_triplicity_comparison=(
            req.options.triplicity_include_research_comparison
        ),
        center=req.computation_mode.center,
        zodiac=req.computation_mode.zodiac,
        ecliptic_frame=req.computation_mode.ecliptic_frame,
        nutation=req.computation_mode.nutation,
        sect_is_day=dignity_sect_is_day,
        sect_basis=dignity_sect_basis,
        trace=trace,
    )

    if input_request.birth_time_precision == "date_only":
        for requested, result in (
            (req.options.include_void_of_course, void_of_course),
            (req.options.include_aspects, aspects),
            (req.options.include_declination_aspects, declination_aspects),
            (essential_dignities is not None, essential_dignities),
        ):
            if requested and result:
                result["time_basis"] = (
                    "representative_computational_anchor_not_birth_time"
                )
                result["date_only_sensitivity_status"] = (
                    "not_evaluated_across_civil_day"
                )
        if lunar_apsides is not None:
            lunar_apsides["time_basis"] = (
                "representative_computational_anchor_not_birth_time"
            )
            lunar_apsides["date_only_sensitivity_status"] = (
                "not_evaluated_across_civil_day"
            )

    return DerivedChart(
        antiscia=antiscia,
        aspects=aspects,
        declination_aspects=declination_aspects,
        essential_dignities=essential_dignities,
        lots=lots,
        sect=sect,
        void_of_course=void_of_course,
    )


def _assemble_chart_response(
    facts: AstronomicalFacts,
    methods: DerivedChart,
) -> dict[str, Any]:
    (angles, bodies, body_defs, build_identity, ctx, deployment_profile, event_module_availability, extra_angles, fixed_stars, horizon_events, house_division, houses_for_placement, input_request, latitude_regime, lunar_apsides, lunar_events, node_defs, nodes, parallax_moon, planet_in_house, req, representative_offset_seconds, time_conv, trace,) = (
        facts.angles,
        facts.bodies,
        facts.body_defs,
        facts.build_identity,
        facts.ctx,
        facts.deployment_profile,
        facts.event_module_availability,
        facts.extra_angles,
        facts.fixed_stars,
        facts.horizon_events,
        facts.house_division,
        facts.houses_for_placement,
        facts.input_request,
        facts.latitude_regime,
        facts.lunar_apsides,
        facts.lunar_events,
        facts.node_defs,
        facts.nodes,
        facts.parallax_moon,
        facts.planet_in_house,
        facts.req,
        facts.representative_offset_seconds,
        facts.time_conv,
        facts.trace,
    )

    (antiscia, aspects, declination_aspects, essential_dignities, lots, sect, void_of_course,) = (
        methods.antiscia,
        methods.aspects,
        methods.declination_aspects,
        methods.essential_dignities,
        methods.lots,
        methods.sect,
        methods.void_of_course,
    )

    library_info = {
        "pyswisseph_distribution_version": _PYSWISSEPH_DISTRIBUTION_VERSION,
        "swiss_ephemeris_library_version": swe.version,
        # IANA database version is a calculation input and must be receipted.
        "tz_database": dict(TZ_DATABASE),
        "note": "兩者是不同的版本號：前者是 pip 安裝的 pyswisseph 套件版本，後者是其內建 "
                "Swiss Ephemeris C 函式庫自報的版本，數字格式相近但並非同一個值。"
                "tz_database 是本地時間換算所用的 IANA 時區資料庫版本，"
                "它不由本產品固定：macOS 由作業系統提供、容器由 image 提供，"
                "更新後同一組輸入可能換算出不同的 UTC。",
    }
    astronomical_data = {
        "time": time_conv,
        "atmosphere": {
            "pressure_hpa": req.atmosphere.pressure_hpa,
            "pressure_mode": (
                "user_supplied" if req.atmosphere.pressure_hpa is not None
                else "swiss_estimate_from_altitude"
            ),
            "temperature_c": req.atmosphere.temperature_c,
            "refraction": "swiss_ephemeris_standard_model",
            "applies_to": "altitude_apparent",
        },
        "bodies": bodies,
        "nodes": nodes,
        "fixed_stars": fixed_stars,
        "fixed_star_policy": fixed_star_receipt(
            FIXED_STARS,
            req.options.include_fixed_stars,
        ),
        "lunar_apsides": lunar_apsides,
        "parallax_moon": parallax_moon,
        "angles": angles,
        "extra_angles": extra_angles,
        "lunar_events": lunar_events,
        "horizon_events": horizon_events,
    }
    derived_methods = {
        "house_division": house_division,
        "planet_in_house": planet_in_house,
        "sect": sect,
        "lots": lots,
        "void_of_course": void_of_course,
        "declination_aspects": declination_aspects,
        "aspects": aspects,
        "essential_dignities": essential_dignities,
    }
    if input_request.birth_time_precision == "approximate_hour":
        birth_time_sensitivity = build_approximate_hour_sensitivity(
            request=input_request,
            body_defs=body_defs,
            representative_bodies=bodies,
            representative_houses=houses_for_placement,
            representative_placements=planet_in_house,
            representative_sect=sect,
            representative_lots=lots,
            representative_void_of_course=void_of_course,
            representative_offset_seconds=representative_offset_seconds,
        )
    elif input_request.birth_time_precision == "date_only":
        birth_time_sensitivity = build_date_only_sensitivity(
            request=input_request,
            body_defs=body_defs,
            node_defs=node_defs,
            representative_bodies=bodies,
            representative_nodes=nodes,
        )
    else:
        birth_time_sensitivity = {
            "precision": "exact",
            "status": "not_applicable",
        }
    trace_steps = trace.as_list()
    calculation_dossier = build_calculation_dossier(
        request=input_request,
        effective_request=req,
        time_conversion=time_conv,
        context=ctx,
        library_info=library_info,
        astronomical_data=astronomical_data,
        derived_methods=derived_methods,
        trace_steps=trace_steps,
        full_ephemeris_files_available=_HAS_FULL_EPHEMERIS,
        build_identity=build_identity,
        deployment_profile=deployment_profile,
        birth_time_sensitivity=birth_time_sensitivity,
        event_module_availability=event_module_availability,
        latitude_regime=latitude_regime,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "output_contract": {
            "status": "provisional",
            "compatibility": "additive_during_0.x_unless_a_documented_correctness_fix_requires_change",
            "layers": {
                "astronomical_data": "直接天文計算與座標／時間轉換",
                "derived_geometry": "只需明示公式的幾何轉換",
                "derived_methods": "需要占星方法或門檻選擇的結果；預設不計算",
            },
        },
        "requested_options": req.options.model_dump(),
        "library_info": library_info,
        "computation_mode": req.computation_mode.model_dump(),
        "calculation_dossier": calculation_dossier,
        "astronomical_data": astronomical_data,
        "derived_geometry": {
            "antiscia": antiscia,
        },
        "derived_methods": derived_methods,
        "birth_time_sensitivity": birth_time_sensitivity,
        "calculation_trace": trace_steps,
    }


def _compute_chart_locked(req: ChartRequest):
    facts = _astronomical_facts(req)
    methods = _derived_chart(facts)
    return _assemble_chart_response(facts, methods)


def create_app(
    settings: AppSettings | None = None,
    *,
    event_emitter: Callable[[dict], bool] | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings()
    authority_root = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    )
    release_frontend_root, release_identity, release_assets = load_runtime_release(
        environment=os.environ,
        authority_root=Path(authority_root),
        backend_public_source_revision=resolved_settings.source_revision,
        api_schema_version=SCHEMA_VERSION,
        dossier_version=DOSSIER_VERSION,
    )
    runtime_build_identity = dict(resolved_settings.build_identity)
    if release_identity is not None:
        runtime_build_identity["release_identity"] = release_identity
    place_catalog = PlaceCatalog(resolved_settings.place_catalog_path)
    application = FastAPI(
        title="古典西洋占星天文計算 API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.settings = resolved_settings
    application.state.release_identity = release_identity

    async def profile_compute_chart(req: ChartRequest, request: Request):
        token = _BUILD_IDENTITY_CONTEXT.set(runtime_build_identity)
        profile_token = _DEPLOYMENT_PROFILE_CONTEXT.set(
            resolved_settings.profile.value
        )
        try:
            return await compute_chart_for_request(req, request)
        finally:
            _DEPLOYMENT_PROFILE_CONTEXT.reset(profile_token)
            _BUILD_IDENTITY_CONTEXT.reset(token)

    def search_places(request: PlaceSearchRequest):
        return place_catalog.search(
            query=request.query,
            country_code=request.country_code,
            limit=request.limit,
        )

    # FastAPI response specs are heterogeneous by design.
    json_responses: dict[int | str, dict[str, Any]] = {
        400: {"model": HostedBoundaryErrorResponse},
        503: {"model": HostedBoundaryErrorResponse},
        431: {"model": HostedBoundaryErrorResponse},
        413: {"model": HostedBoundaryErrorResponse},
        415: {"model": HostedBoundaryErrorResponse},
        422: {
            "model": HostedValidationResponse | HostedBoundaryErrorResponse
        },
    }

    api_method_routes = {
        "/api/health": frozenset({"GET"}),
        "/api/client-config": frozenset({"GET"}),
        "/api/chart": frozenset({"POST"}),
        "/api/places/search": frozenset({"POST"}),
    }
    application.add_api_route(
        "/api/health",
        health_check,
        methods=["GET"],
        name="health_check",
        response_model=dict[str, object],
        response_description=(
            "Process-liveness response; this is not end-to-end calculation readiness."
        ),
    )
    application.add_api_route(
        "/api/client-config",
        lambda: client_configuration(resolved_settings, release_identity),
        methods=["GET"],
        name="client_configuration",
        include_in_schema=False,
    )
    application.add_api_route(
        "/api/chart",
        profile_compute_chart,
        methods=["POST"],
        name="compute_chart",
        response_model=dict[str, Any],
        response_description=(
            "Versioned chart response; see output_contract and deploy/frontend-contract.json."
        ),
        responses=json_responses,
    )
    application.add_api_route(
        "/api/places/search",
        search_places,
        methods=["POST"],
        name="search_places",
        response_model=dict[str, Any],
        response_description=(
            "Bundled-catalog search response with query and execution receipts."
        ),
        responses=json_responses,
    )

    # Validation output never echoes rejected birth values in any profile.
    for error_type, handler in (
        (NonexistentLocalTimeError, _handle_domain_error),
        (AmbiguousLocalTimeChoiceRequiredError, _handle_domain_error),
        (FullEphemerisRequiredError, _handle_domain_error),
        (HouseSystemUnavailableError, _handle_domain_error),
        (PlaceCatalogUnavailableError, _handle_domain_error),
        (RequestValidationError, _handle_request_validation),
        (ComputeCapacityExhaustedError, _handle_domain_error),
        (swe.Error, _handle_domain_error),
        (StarletteHTTPException, _handle_http_exception),
    ):
        application.add_exception_handler(error_type, handler)  # type: ignore[arg-type]

    frontend_dir = str(
        release_frontend_root
        if release_frontend_root is not None
        else Path(authority_root) / "frontend"
    )
    if os.path.isdir(frontend_dir):
        allowed_assets = (
            release_assets
            if release_assets is not None
            else discover_source_assets(Path(frontend_dir))
        )
        @application.get("/", include_in_schema=False)
        async def redirect_default_locale() -> RedirectResponse:
            """Keep the public entry deterministic; never infer browser language."""
            return RedirectResponse(url="/zh-TW/", status_code=308)

        application.mount(
            "/",
            RuntimeStaticFiles(
                directory=frontend_dir,
                html=True,
                allowed_assets=allowed_assets,
            ),
            name="frontend",
        )

    application.add_middleware(
        ApiMethodBoundary,
        route_methods=api_method_routes,
    )

    # Buffer and validate bounded JSON before route execution. Middleware order
    # is inside-out: the last one added runs first.
    application.add_middleware(ChartRequestBoundary)
    application.add_middleware(RequestCapacityBoundary)
    # Outermost after privacy: a request for a host this deployment does not
    # answer for is refused before any budget is spent on it.
    application.add_middleware(
        DeclaredHostBoundary,
        expected_host=resolved_settings.expected_host,
    )

    # Privacy remains the outer user middleware so it can contain downstream
    # response-lifecycle failures and apply headers to request-boundary errors.
    # The lambda preserves existing tests that replace the module-level sink.
    emitter = event_emitter or (lambda event: emit_security_event(event))
    application.add_middleware(
        PrivacyBoundaryMiddleware,
        event_emitter=emitter,
        additional_response_headers=(
            {"X-Robots-Tag": "noindex, nofollow, noarchive"}
            if resolved_settings.capabilities.emit_noindex
            else None
        ),
        cache_policy=resolved_settings.capabilities.cache_policy,
    )
    return application


app = create_app()
