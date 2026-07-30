"""Planet-in-house placements using explicit zodiacal half-open cusp intervals."""

from __future__ import annotations

import math

from .trace import Trace


METHOD_NAME = "zodiacal_cusp_half_open_intervals_v1"
_BOUNDARY_TOLERANCE_DEGREES = 1e-9


def _forward_distance(start: float, end: float) -> float:
    return (end - start) % 360.0


def _house_for_longitude(longitude: float, cusps: list[float]) -> int:
    """Return the 1-based house for [cusp_n, cusp_n+1) in zodiacal order."""

    normalized = longitude % 360.0
    for index, start in enumerate(cusps):
        end = cusps[(index + 1) % 12]
        span = _forward_distance(start, end)
        if span <= _BOUNDARY_TOLERANCE_DEGREES:
            raise ValueError("house cusps contain a zero-width interval")
        offset = _forward_distance(start, normalized)
        if offset < span:
            return index + 1
    raise RuntimeError("longitude did not match any house interval")


def _inapplicable_reason_codes(context) -> list[str]:
    reasons = []
    if context.mode.center not in ("geocentric", "topocentric"):
        reasons.append("non_earth_observer_center")
    if context.mode.ecliptic_frame != "of_date":
        reasons.append("planet_house_frame_mismatch")
    if not context.mode.nutation:
        reasons.append("planet_house_nutation_mismatch")
    return reasons


def compute_planet_house_placements(
    bodies: list[dict],
    houses: dict,
    context,
    trace: Trace,
) -> dict:
    """Place physical planets only; lunar nodes remain separate non-planet points."""

    reasons = _inapplicable_reason_codes(context)
    receipt = {
        "method": METHOD_NAME,
        "method_status": "provisional_pending_method_audit",
        "method_authority": "not_established",
        "execution_status": (
            "not_applicable" if reasons else "computed"
        ),
        "reason_codes": reasons,
        "house_system_code": houses["system_code"],
        "house_system_name": houses["system_name"],
        "interval_semantics": "[cusp_n,cusp_n_plus_1)",
        "placements": [],
    }
    if reasons:
        trace.add(
            "行星落宮",
            note=(
                "目前星體黃經與地表宮頭不在可直接比較的同一框架，"
                f"因此不產生落宮：{', '.join(reasons)}。"
            ),
        )
        return receipt

    cusps = houses["cusps"]
    for body in bodies:
        longitude = body.get("longitude")
        if longitude is None:
            continue
        house = _house_for_longitude(longitude, cusps)
        start = cusps[house - 1] % 360.0
        end = cusps[house % 12] % 360.0
        distance_from_start = _forward_distance(start, longitude)
        distance_to_end = _forward_distance(longitude, end)
        receipt["placements"].append(
            {
                "key": body["key"],
                "name": body["name"],
                "longitude": longitude,
                "house": house,
                "cusp_start_longitude": start,
                "cusp_end_longitude": end,
                "distance_to_nearest_cusp_degrees": min(
                    distance_from_start,
                    distance_to_end,
                ),
                "on_cusp": math.isclose(
                    distance_from_start,
                    0.0,
                    abs_tol=_BOUNDARY_TOLERANCE_DEGREES,
                ),
                "interval_semantics": "[cusp_n,cusp_n_plus_1)",
            }
        )

    trace.add(
        "行星落宮",
        formula="星體黃經 ∈ [第 n 宮宮頭, 第 n+1 宮宮頭)",
        inputs={
            "house_system": houses["system_code"],
            "cusps": cusps,
            "planet_count": len(receipt["placements"]),
        },
        result={
            item["key"]: item["house"]
            for item in receipt["placements"]
        },
        note=(
            "宮頭使用半開區間；精確落在宮頭時歸入從該宮頭開始的新宮。"
            "本輸出只做落宮定位，不加入占星解讀。"
        ),
    )
    return receipt
