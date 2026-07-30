"""Rise, set, upper-transit, and lower-transit events for major bodies."""

import datetime as dt
import math

import swisseph as swe

from .trace import Trace


EVENT_FLAGS = {
    "rise": swe.CALC_RISE,
    "set": swe.CALC_SET,
    "upper_transit": swe.CALC_MTRANSIT,
    "lower_transit": swe.CALC_ITRANSIT,
}
EPHEMERIS_FLAGS = swe.FLG_SWIEPH


def _jd_ut_to_iso_utc(jd_ut: float) -> str:
    year, month, day, hour, minute, second = swe.jdut1_to_utc(jd_ut, swe.GREG_CAL)
    value = dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc)
    value += dt.timedelta(seconds=second)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _event_value(jd_ut: float) -> dict:
    return {
        "jd_ut": jd_ut,
        "utc_time": _jd_ut_to_iso_utc(jd_ut),
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
    if event_flag in (swe.CALC_RISE, swe.CALC_SET):
        # Swiss default is the upper limb with standard refraction. Do not add
        # BIT_DISC_CENTER or BIT_NO_REFRACTION; expose that choice in metadata.
        rsmi = event_flag
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
            "status": "no_event",
            "reason": "swiss_circumpolar",
            "previous": None,
            "next": None,
        }

    previous_jd = None
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
            return {
                "status": "no_event",
                "reason": "swiss_circumpolar",
                "previous": None,
                "next": None,
            }
        if candidate is None or candidate >= reference_jd_ut:
            break
        previous_jd = candidate
        cursor = math.nextafter(candidate, math.inf)

    if previous_jd is None or next_jd is None:
        raise swe.Error("Could not bracket rise/set/transit event around reference time")
    return {
        "status": "found",
        "reason": None,
        "previous": _event_value(previous_jd),
        "next": _event_value(next_jd),
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
        if events["rise"]["status"] == "no_event" or events["set"]["status"] == "no_event":
            visibility, visibility_evidence = _sample_visibility(
                reference_jd_ut,
                body["id"],
                geopos,
                pressure_hpa,
                atmosphere.temperature_c,
            )
            if (
                events["rise"]["status"] == "found"
                or events["set"]["status"] == "found"
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
