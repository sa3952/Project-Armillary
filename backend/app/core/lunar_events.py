"""Geocentric lunar phases, prenatal syzygy, and preceding eclipse events.

These are astronomical events only. They deliberately use a fixed, documented
geocentric apparent tropical-of-date contract rather than inheriting research
display modes such as heliocentric, sidereal, J2000, or true position.
"""

import math

import swisseph as swe

from ..ephemeris import require_full_ephemeris
from .root_finding import find_crossings_detailed
from .trace import Trace
from .time_utils import jd_ut_to_iso_utc


PHASES = {
    "new_moon": 0.0,
    "first_quarter": 90.0,
    "full_moon": 180.0,
    "last_quarter": 270.0,
}
PHASE_FLAGS = swe.FLG_SWIEPH | swe.FLG_SPEED


def _signed_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def _phase_state(jd_ut: float) -> tuple[float, float, int, int]:
    moon, moon_retflag = swe.calc_ut(jd_ut, swe.MOON, PHASE_FLAGS)
    sun, sun_retflag = swe.calc_ut(jd_ut, swe.SUN, PHASE_FLAGS)
    require_full_ephemeris(
        moon_retflag,
        operation="月相搜尋（月球位置）",
        jd_ut=jd_ut,
    )
    require_full_ephemeris(
        sun_retflag,
        operation="月相搜尋（太陽位置）",
        jd_ut=jd_ut,
    )
    elongation = (moon[0] - sun[0]) % 360.0
    relative_speed = moon[3] - sun[3]
    return elongation, relative_speed, moon_retflag, sun_retflag


# `root_finding` states plainly that Newton and secant are deliberately not used:
# they need a derivative and are not stable.  This module then wrote its own
# unguarded Newton iteration anyway, whose two failure paths both raised
# `swe.Error` — which the API maps to 500, so an oscillation in one lunar phase
# would have destroyed the whole chart request.  It brackets and bisects now,
# through the same solver every other root in this product goes through.
_PHASE_BRACKET_DAYS = 1.5
_PHASE_COARSE_STEP_DAYS = 0.05
# The elongation moves about 12.2 degrees a day, so a tolerance in days has to
# be an order of magnitude tighter than the residual the caller checks in
# degrees.  1e-9 days is under a millisecond and about 1.2e-8 degrees.
_PHASE_TOLERANCE_DAYS = 1e-9


def _refine_phase(guess_jd_ut: float, target_degrees: float) -> tuple[float, float, int, int]:
    state: dict[str, tuple[int, int]] = {}

    def separation_at(offset_days: float) -> float:
        elongation, _speed, moon_retflag, sun_retflag = _phase_state(
            guess_jd_ut - _PHASE_BRACKET_DAYS + offset_days
        )
        state["retflags"] = (moon_retflag, sun_retflag)
        return _signed_degrees(elongation - target_degrees)

    roots = find_crossings_detailed(
        separation_at,
        window_days=2 * _PHASE_BRACKET_DAYS,
        coarse_step_days=_PHASE_COARSE_STEP_DAYS,
        tolerance_days=_PHASE_TOLERANCE_DAYS,
    )
    if roots:
        nearest = min(roots, key=lambda root: abs(root["t"] - _PHASE_BRACKET_DAYS))
        jd_ut = guess_jd_ut - _PHASE_BRACKET_DAYS + nearest["t"]
        elongation, _speed, moon_retflag, sun_retflag = _phase_state(jd_ut)
        residual = _signed_degrees(elongation - target_degrees)
        return jd_ut, residual, moon_retflag, sun_retflag

    jd_ut = guess_jd_ut

    elongation, _relative_speed, moon_retflag, sun_retflag = _phase_state(jd_ut)
    residual = _signed_degrees(elongation - target_degrees)
    if abs(residual) > 1e-7:
        raise swe.Error(f"Lunar phase root did not converge; residual={residual} degrees")
    return jd_ut, residual, moon_retflag, sun_retflag


def _phase_event(reference_jd_ut: float, phase: str, direction: str) -> dict:
    target = PHASES[phase]
    elongation, _speed, _moon_retflag, _sun_retflag = _phase_state(reference_jd_ut)
    if direction == "next":
        distance = (target - elongation) % 360.0
        if distance < 1e-7:
            distance = 360.0
        guess = reference_jd_ut + distance / 12.2
    elif direction == "previous":
        distance = (elongation - target) % 360.0
        if distance < 1e-7:
            distance = 360.0
        guess = reference_jd_ut - distance / 12.2
    else:
        raise ValueError(f"Unsupported phase search direction: {direction}")

    jd_ut, residual, moon_retflag, sun_retflag = _refine_phase(guess, target)
    return {
        "phase": phase,
        "target_elongation_degrees": target,
        "direction": direction,
        "jd_ut": jd_ut,
        "utc_time": jd_ut_to_iso_utc(jd_ut),
        "angular_residual_degrees": residual,
        "calculation": "Moon-Sun apparent geocentric tropical longitude root",
        "ephemeris_source": "Swiss Ephemeris files",
        "retflag_moon": moon_retflag,
        "retflag_sun": sun_retflag,
    }


def compute_primary_phases(reference_jd_ut: float, trace: Trace) -> dict:
    results = {}
    for phase in PHASES:
        results[phase] = {
            "previous": _phase_event(reference_jd_ut, phase, "previous"),
            "next": _phase_event(reference_jd_ut, phase, "next"),
        }

    previous_new = results["new_moon"]["previous"]
    previous_full = results["full_moon"]["previous"]
    nearest = max((previous_new, previous_full), key=lambda event: event["jd_ut"])
    prenatal_syzygy = {
        **nearest,
        "definition": "nearest_previous_geocentric_new_or_full_moon",
        "interpretation": None,
    }

    trace.add(
        "前後朔望弦與出生前朔望",
        formula=(
            "以 Moon-Sun 視地心回歸黃經差對 0°/90°/180°/270° "
            "作粗掃描、分割根區間與二分求根"
        ),
        inputs={
            "reference_JD_UT": reference_jd_ut,
            "flags": PHASE_FLAGS,
            "contract": "geocentric apparent tropical of-date",
        },
        result={
            phase: {
                direction: event["utc_time"]
                for direction, event in pair.items()
            }
            for phase, pair in results.items()
        },
        note="prenatal_syzygy 僅指出生前最近一次朔或望，不含古典技法解讀。",
    )
    return {
        "contract": "geocentric_apparent_tropical_of_date",
        "primary_phases": results,
        "prenatal_syzygy": prenatal_syzygy,
    }


def _eclipse_type(retflag: int, lunar: bool) -> str:
    if retflag & swe.ECL_TOTAL:
        return "total"
    if not lunar and retflag & swe.ECL_ANNULAR_TOTAL:
        return "hybrid"
    if not lunar and retflag & swe.ECL_ANNULAR:
        return "annular"
    if retflag & swe.ECL_PARTIAL:
        return "partial"
    if lunar and retflag & swe.ECL_PENUMBRAL:
        return "penumbral"
    return "unknown"


def _contacts(tret: tuple[float, ...]) -> dict:
    labels = {
        2: "partial_begin",
        3: "partial_end",
        4: "totality_begin",
        5: "totality_end",
        6: "penumbral_or_centerline_begin",
        7: "penumbral_or_centerline_end",
    }
    return {
        label: {
            "jd_ut": tret[index],
            "utc_time": jd_ut_to_iso_utc(tret[index]),
        }
        for index, label in labels.items()
        if tret[index] > 0
    }


def _eclipse_result(kind: str, retflag: int, tret: tuple[float, ...]) -> dict:
    lunar = kind == "lunar"
    _sun, sun_retflag = swe.calc_ut(tret[0], swe.SUN, swe.FLG_SWIEPH)
    _moon, moon_retflag = swe.calc_ut(tret[0], swe.MOON, swe.FLG_SWIEPH)
    require_full_ephemeris(
        sun_retflag,
        operation=f"{'月食' if lunar else '日食'}事件（太陽位置）",
        jd_ut=tret[0],
    )
    require_full_ephemeris(
        moon_retflag,
        operation=f"{'月食' if lunar else '日食'}事件（月球位置）",
        jd_ut=tret[0],
    )
    centrality = None
    if not lunar:
        if retflag & swe.ECL_CENTRAL:
            centrality = "central"
        elif retflag & swe.ECL_NONCENTRAL:
            centrality = "noncentral"
    return {
        "kind": kind,
        "type": _eclipse_type(retflag, lunar),
        "centrality": centrality,
        "retflag": retflag,
        "retflag_sun": sun_retflag,
        "retflag_moon": moon_retflag,
        "ephemeris_source": "Swiss Ephemeris files",
        "jd_ut_maximum": tret[0],
        "utc_time_maximum": jd_ut_to_iso_utc(tret[0]),
        "contacts": _contacts(tret),
        "calculation": (
            "swe.lun_eclipse_when(backwards=True)"
            if lunar
            else "swe.sol_eclipse_when_glob(backwards=True)"
        ),
    }


def compute_previous_eclipses(reference_jd_ut: float, trace: Trace) -> dict:
    start = math.nextafter(reference_jd_ut, -math.inf)
    solar_flag, solar_times = swe.sol_eclipse_when_glob(start, swe.FLG_SWIEPH, 0, True)
    lunar_flag, lunar_times = swe.lun_eclipse_when(start, swe.FLG_SWIEPH, 0, True)
    solar = _eclipse_result("solar", solar_flag, solar_times)
    lunar = _eclipse_result("lunar", lunar_flag, lunar_times)
    trace.add(
        "出生前最近日食／月食",
        formula="Swiss Ephemeris global eclipse search, backwards=True",
        inputs={"reference_JD_UT": reference_jd_ut, "flags": swe.FLG_SWIEPH},
        result={
            "solar": solar["utc_time_maximum"],
            "lunar": lunar["utc_time_maximum"],
        },
        note="只回報天文事件與 Swiss 類型，不提供古典占星解讀。",
    )
    return {
        "previous_solar": solar,
        "previous_lunar": lunar,
        "interpretation": None,
    }
