"""Rise/set/transit events in a fixed apparent topocentric frame.

The event definition uses the refracted upper-limb horizon crossing and does
not follow display computation modes. The response and triggered Dossier
warning expose this boundary explicitly.
"""

import math
from typing import Final

import swisseph as swe

from .trace import Trace
from .time_utils import jd_ut_to_iso_utc


EVENT_FLAGS = {
    "rise": swe.CALC_RISE,
    "set": swe.CALC_SET,
    "upper_transit": swe.CALC_MTRANSIT,
    "lower_transit": swe.CALC_ITRANSIT,
}
EPHEMERIS_FLAGS = swe.FLG_SWIEPH

# 與輸出一起回報的框架宣告。消費端不必反推本模組吃不吃 computation_mode。
FRAME_DECLARATION = {
    "follows_computation_mode": False,
    "position_mode": "apparent_always",
    "center": "topocentric_always",
    "refraction": "applied_by_definition_of_rise_and_set",
    "mth_q_009_apparent_altitude_suppression_applies": False,
    "reason": (
        "A rise is the instant the refracted upper limb crosses the horizon; "
        "removing refraction would remove the event being reported, not merely "
        "change its precision."
    ),
    "ruling": "MTH-Q-018, Sebastian 2026-08-03: scope A plus a triggered explanation",
    "finding": "RT-BACKEND-9-E-009",
}


def _event_value(jd_ut: float) -> dict:
    return {
        "jd_ut": jd_ut,
        "utc_time": jd_ut_to_iso_utc(jd_ut),
    }


def _rise_trans_call(
    start_jd_ut: float,
    body_id: int,
    event_flag: int,
    geopos: tuple[float, float, float],
    pressure_hpa: float,
    temperature_c: float,
) -> tuple[int, float | None]:
    rsmi = event_flag
    # Swiss default for rise/set is the upper limb with standard refraction.
    # Do not add BIT_DISC_CENTER or BIT_NO_REFRACTION; expose that choice in metadata.
    result, times = swe.rise_trans(
        start_jd_ut,
        body_id,
        rsmi,
        geopos,
        pressure_hpa,
        temperature_c,
        EPHEMERIS_FLAGS,
    )
    return result, times[0] if result == 0 else None


# The regimes this search can end in, declared once so they can be enumerated.
# A regime that no sample reaches is a regime nobody has tested, and until these
# were names in the source there was no way to ask which ones the corpus covers.
EVENT_STATUSES: Final = ("found", "partial", "no_event")
EVENT_REASONS: Final = (
    "swiss_circumpolar",
    "circumpolar_before_reference_window",
    "no_event_in_searched_window",
)
CIRCUMPOLAR_AT_REFERENCE: Final = EVENT_REASONS[0]
CIRCUMPOLAR_BEFORE_WINDOW: Final = EVENT_REASONS[1]
NO_EVENT_IN_WINDOW: Final = EVENT_REASONS[2]


def _find_event_pair(
    reference_jd_ut: float,
    body_id: int,
    event_flag: int,
    geopos: tuple[float, float, float],
    pressure_hpa: float,
    temperature_c: float,
) -> dict:
    after_reference = math.nextafter(reference_jd_ut, math.inf)
    next_status, next_jd = _rise_trans_call(
        after_reference,
        body_id,
        event_flag,
        geopos,
        pressure_hpa,
        temperature_c,
    )
    if next_status == -2:
        return {
            "status": EVENT_STATUSES[2],
            "reason": CIRCUMPOLAR_AT_REFERENCE,
            "previous": None,
            "next": None,
        }

    # Circumpolar is a property of the interval that contains the reference
    # instant.  A backward probe two days earlier can land inside a different
    # interval — at 69.65N the midnight sun ends and an event exists on the same
    # day a probe two days back is still circumpolar.  Reading that probe as the
    # body's present state confiscated an event Swiss had already returned, and
    # the response then contradicted its own sampled altitudes.  A missing side
    # is now reported as a missing side.
    previous_jd = None
    previous_reason = None
    cursor = reference_jd_ut - 2.0
    for _ in range(6):
        status, candidate = _rise_trans_call(
            cursor,
            body_id,
            event_flag,
            geopos,
            pressure_hpa,
            temperature_c,
        )
        if status == -2:
            previous_reason = CIRCUMPOLAR_BEFORE_WINDOW
            break
        if candidate is None or candidate >= reference_jd_ut:
            previous_reason = NO_EVENT_IN_WINDOW
            break
        previous_jd = candidate
        cursor = math.nextafter(candidate, math.inf)

    if previous_jd is not None and next_jd is not None:
        return {
            "status": EVENT_STATUSES[0],
            "reason": None,
            "previous": _event_value(previous_jd),
            "next": _event_value(next_jd),
        }
    # One side only.  Raising here used to turn a legal boundary geometry into an
    # unhandled 500; the caller cannot act on that and the user cannot either.
    return {
        "status": EVENT_STATUSES[1],
        "reason": previous_reason or NO_EVENT_IN_WINDOW,
        "previous": _event_value(previous_jd) if previous_jd is not None else None,
        "next": _event_value(next_jd) if next_jd is not None else None,
    }


def _sample_visibility(
    reference_jd_ut: float,
    body_id: int,
    geopos: tuple[float, float, float],
    pressure_hpa: float,
    temperature_c: float,
) -> tuple[str, dict]:
    swe.set_topo(*geopos)
    topocentric_flags = EPHEMERIS_FLAGS | swe.FLG_TOPOCTR
    apparent_upper_limb_altitudes = []
    for index in range(97):
        jd_ut = reference_jd_ut - 0.5 + index / 48.0
        ecliptic = swe.calc_ut(jd_ut, body_id, topocentric_flags)[0]
        _azimuth, _true_altitude, apparent_altitude = swe.azalt(
            jd_ut,
            swe.ECL2HOR,
            geopos,
            pressure_hpa,
            temperature_c,
            (ecliptic[0], ecliptic[1], ecliptic[2]),
        )
        apparent_diameter = swe.pheno_ut(jd_ut, body_id, topocentric_flags)[3]
        apparent_upper_limb_altitudes.append(
            apparent_altitude + apparent_diameter / 2.0
        )

    minimum = min(apparent_upper_limb_altitudes)
    maximum = max(apparent_upper_limb_altitudes)
    evidence = {
        "method": "97 topocentric apparent upper-limb altitude samples across 48 hours",
        "coordinate_origin": "topocentric",
        "disc_position": "upper_limb",
        "minimum_apparent_upper_limb_altitude_degrees": minimum,
        "maximum_apparent_upper_limb_altitude_degrees": maximum,
    }
    if minimum > 0:
        return "always_above_horizon", evidence
    if maximum < 0:
        return "never_rises", evidence
    return "indeterminate_near_horizon", evidence


def compute_horizon_events(
    body_defs: list[dict],
    reference_jd_ut: float,
    location,
    atmosphere,
    trace: Trace,
) -> dict:
    geopos = (location.longitude, location.latitude, location.altitude_m)
    pressure_hpa = atmosphere.pressure_hpa if atmosphere.pressure_hpa is not None else 0.0
    results = []

    for body in body_defs:
        events = {
            name: _find_event_pair(
                reference_jd_ut,
                body["id"],
                flag,
                geopos,
                pressure_hpa,
                atmosphere.temperature_c,
            )
            for name, flag in EVENT_FLAGS.items()
        }
        # Only rise and set answer "does this body cross the horizon".  A
        # transit exists for a circumpolar body too, so including it here would
        # force every polar sun into an indeterminate label.
        horizon_crossings = (events["rise"], events["set"])
        if any(event["status"] != EVENT_STATUSES[0] for event in horizon_crossings):
            visibility, visibility_evidence = _sample_visibility(
                reference_jd_ut,
                body["id"],
                geopos,
                pressure_hpa,
                atmosphere.temperature_c,
            )
            if any(
                event["status"] in {EVENT_STATUSES[0], EVENT_STATUSES[1]}
                and (event["previous"] or event["next"])
                for event in horizon_crossings
            ):
                visibility = "indeterminate_near_horizon"
                visibility_evidence["event_consistency"] = (
                    "a rise or set event was found, so an always/never status "
                    "would be contradictory"
                )
        else:
            visibility = "rises_and_sets"
            visibility_evidence = None
        results.append({
            "key": body["key"],
            "name": body["zh"],
            "visibility": visibility,
            "visibility_evidence": visibility_evidence,
            "events": events,
        })

    trace.add(
        "星體升降與上下中天",
        formula="swe.rise_trans；由出生前兩日起逐事件搜尋 previous，出生時刻後搜尋 next",
        inputs={
            "reference_JD_UT": reference_jd_ut,
            "geopos": geopos,
            "pressure_hPa": pressure_hpa,
            "temperature_C": atmosphere.temperature_c,
            "rise_set_disc": "upper_limb",
            "refraction": "enabled",
            "flags": EPHEMERIS_FLAGS,
        },
        result={"body_count": len(results)},
        note="上下中天不使用 disc/refraction；升降使用 Swiss upper-limb + standard-refraction default。",
    )
    return {
        "contract": {
            "reference_time": "birth_instant",
            "directions": ["previous", "next"],
            "disc_position": "upper_limb",
            "refraction": "enabled",
            "frame": dict(FRAME_DECLARATION),
            "pressure_hpa": atmosphere.pressure_hpa,
            "pressure_mode": (
                "user_supplied" if atmosphere.pressure_hpa is not None
                else "swiss_estimate_from_altitude"
            ),
            "temperature_c": atmosphere.temperature_c,
            "ephemeris_flags": EPHEMERIS_FLAGS,
            "ephemeris_source": {
                "requested": "Swiss Ephemeris files",
                "requested_flag": EPHEMERIS_FLAGS,
                "actual_source_verified": False,
                "evidence": (
                    "requested_flag_only_rise_trans_returns_no_"
                    "ephemeris_retflag"
                ),
            },
            "transit_definition": "upper_and_lower_meridian_transit",
            "coordinate_origin": "topocentric_observer",
            "display_center_independent": True,
        },
        "bodies": results,
    }
