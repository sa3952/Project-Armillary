"""Bounded sensitivity summary for an hour-level birth-time input."""

from __future__ import annotations

import datetime as dtmod

from ..config import CLASSICAL_BODIES
from .bodies import compute_bodies
from .computation_mode import ComputationContext
from .house_placements import compute_planet_house_placements
from .houses import compute_houses
from .lots import compute_lots, determine_sect
from .moon import determine_void_of_course, find_voc_candidates
from .time_utils import compute_time_conversion
from .trace import Trace


PROBE_OFFSETS_SECONDS = (0, 900, 1800, 2700, 3599)
TRANSITION_RESOLUTION_SECONDS = 15


def _sign_index(longitude: float) -> int:
    return int((longitude % 360.0) // 30.0)


def _circular_envelope(values: list[float]) -> dict:
    """Return the smallest sampled arc, avoiding a false 0°/360° min/max span."""

    normalized = sorted(value % 360.0 for value in values)
    if len(normalized) == 1:
        return {
            "start_degrees": normalized[0],
            "end_degrees": normalized[0],
            "span_degrees": 0.0,
            "crosses_zero": False,
            "sampled_values": normalized,
        }
    gaps = []
    for index, left in enumerate(normalized):
        right = normalized[(index + 1) % len(normalized)]
        gap = (right - left) % 360.0
        gaps.append((gap, index))
    _largest_gap, gap_index = max(gaps)
    start = normalized[(gap_index + 1) % len(normalized)]
    end = normalized[gap_index]
    span = (end - start) % 360.0
    return {
        "start_degrees": start,
        "end_degrees": end,
        "span_degrees": span,
        "crosses_zero": end < start,
        "sampled_values": normalized,
    }


def _probe_request(request, offset_seconds: int):
    minute, second = divmod(offset_seconds, 60)
    return request.model_copy(
        update={
            "datetime": request.datetime.model_copy(
                update={"minute": minute, "second": float(second)}
            )
        }
    )


def _extract_probe(
    *,
    offset_seconds: int,
    request,
    body_defs: list[dict],
    representative: dict | None,
) -> dict:
    if representative is not None:
        return {
            "offset_seconds": offset_seconds,
            "minute": offset_seconds // 60,
            "second": offset_seconds % 60,
            **representative,
        }

    probe_request = _probe_request(request, offset_seconds)
    trace = Trace()
    time_conversion = compute_time_conversion(
        probe_request.datetime,
        probe_request.timezone,
        probe_request.location,
        trace,
        require_explicit_fold=True,
    )
    jd_ut = time_conversion["jd_ut"]
    context = ComputationContext(
        probe_request.computation_mode,
        probe_request.location,
    )
    bodies = compute_bodies(
        body_defs,
        jd_ut,
        context,
        probe_request.atmosphere,
        trace,
        physical=False,
    )
    houses = compute_houses(
        probe_request.options.house_system,
        jd_ut,
        probe_request.location,
        context,
        trace,
    )
    house_receipt = {
        "system_code": probe_request.options.house_system,
        "system_name": houses["system_name"],
        "cusps": houses["cusps"],
    }
    placements = compute_planet_house_placements(
        bodies,
        house_receipt,
        context,
        trace,
    )

    sect = lots = voc = None
    if probe_request.options.include_lots:
        body_map = {item["key"]: item for item in bodies}
        sect = determine_sect(body_map["sun"]["altitude_true"], trace)
        lots = compute_lots(
            houses["asc"],
            body_map["sun"]["longitude"],
            body_map["moon"]["longitude"],
            sect,
            trace,
        )
    if (
        probe_request.options.include_void_of_course
        and context.horizon_meaningful
    ):
        body_map = {item["key"]: item for item in bodies}
        classical_keys = {
            item["key"]
            for item in CLASSICAL_BODIES
            if item["key"] != "moon"
        }
        other_bodies = [
            item
            for item in bodies
            if item["key"] in classical_keys
        ]
        voc = determine_void_of_course(
            find_voc_candidates(body_map["moon"], other_bodies, trace),
            trace,
        )
    elif probe_request.options.include_void_of_course:
        voc = {"is_void_of_course": None}

    return {
        "offset_seconds": offset_seconds,
        "minute": offset_seconds // 60,
        "second": offset_seconds % 60,
        "input_local_time": time_conversion["input_local_time"],
        "bodies": bodies,
        "houses": houses,
        "placements": placements,
        "sect": sect,
        "lots": lots,
        "void_of_course": voc,
    }


def _status(possible_values: list) -> str:
    return (
        "sampled_stable"
        if len(possible_values) <= 1
        else "varies_within_sampled_hour"
    )


def _classification_state(probe: dict, body_defs: list[dict]) -> dict:
    body_map = {item["key"]: item for item in probe["bodies"]}
    state = {
        f"body_signs.{item['key']}": _sign_index(
            body_map[item["key"]]["longitude"]
        )
        for item in body_defs
    }
    if probe["placements"]["execution_status"] == "computed":
        state.update(
            {
                f"planet_in_house.{item['key']}": item["house"]
                for item in probe["placements"]["placements"]
            }
        )
    if probe["sect"] is not None:
        state["sect.is_day"] = probe["sect"]["is_day"]
    if (
        isinstance(probe["void_of_course"], dict)
        and "is_void_of_course" in probe["void_of_course"]
    ):
        state["void_of_course.is_void_of_course"] = probe[
            "void_of_course"
        ]["is_void_of_course"]
    return state


def _format_local_offset(request, offset_seconds: int) -> str:
    start = dtmod.datetime(
        request.datetime.year,
        request.datetime.month,
        request.datetime.day,
        request.datetime.hour,
    )
    return (start + dtmod.timedelta(seconds=offset_seconds)).isoformat(
        timespec="seconds"
    )


def _refine_transitions(
    *,
    request,
    body_defs: list[dict],
    probes_by_offset: dict[int, dict],
) -> list[dict]:
    """Refine only sampled classification changes; hidden reversals remain possible."""

    transitions = []
    ordered_offsets = sorted(probes_by_offset)
    for left_offset, right_offset in zip(
        ordered_offsets,
        ordered_offsets[1:],
    ):
        left_state = _classification_state(
            probes_by_offset[left_offset],
            body_defs,
        )
        right_state = _classification_state(
            probes_by_offset[right_offset],
            body_defs,
        )
        if left_state == right_state:
            continue

        original_left_state = left_state
        original_right_state = right_state
        while (
            right_offset - left_offset
            > TRANSITION_RESOLUTION_SECONDS
        ):
            midpoint = (left_offset + right_offset) // 2
            if midpoint not in probes_by_offset:
                probes_by_offset[midpoint] = _extract_probe(
                    offset_seconds=midpoint,
                    request=request,
                    body_defs=body_defs,
                    representative=None,
                )
            midpoint_state = _classification_state(
                probes_by_offset[midpoint],
                body_defs,
            )
            if midpoint_state == left_state:
                left_offset = midpoint
            else:
                right_offset = midpoint
                right_state = midpoint_state

        changed_paths = sorted(
            key
            for key in set(original_left_state) | set(original_right_state)
            if original_left_state.get(key)
            != original_right_state.get(key)
        )
        transitions.append(
            {
                "lower_bound_local": _format_local_offset(
                    request,
                    left_offset,
                ),
                "upper_bound_local": _format_local_offset(
                    request,
                    right_offset,
                ),
                "resolution_seconds": right_offset - left_offset,
                "changed_paths": changed_paths,
                "semantics": (
                    "bounded_bisection_of_a_sampled_classification_change"
                ),
            }
        )
    return transitions


def build_approximate_hour_sensitivity(
    *,
    request,
    body_defs: list[dict],
    representative_bodies: list[dict],
    representative_houses: dict,
    representative_placements: dict,
    representative_sect: dict | None,
    representative_lots: dict | None,
    representative_void_of_course: dict | None,
) -> dict:
    """Build five probe receipts but reuse the already-computed 30-minute chart."""

    representative = {
        "input_local_time": (
            f"{request.datetime.year:04d}-{request.datetime.month:02d}-"
            f"{request.datetime.day:02d} {request.datetime.hour:02d}:30:00.00"
        ),
        "bodies": representative_bodies,
        "houses": representative_houses,
        "placements": representative_placements,
        "sect": representative_sect,
        "lots": representative_lots,
        "void_of_course": representative_void_of_course,
    }
    probes_by_offset = {
        offset_seconds: _extract_probe(
            offset_seconds=offset_seconds,
            request=request,
            body_defs=body_defs,
            representative=(
                representative if offset_seconds == 1800 else None
            ),
        )
        for offset_seconds in PROBE_OFFSETS_SECONDS
    }
    probes = [
        probes_by_offset[offset]
        for offset in PROBE_OFFSETS_SECONDS
    ]
    transitions = _refine_transitions(
        request=request,
        body_defs=body_defs,
        probes_by_offset=probes_by_offset,
    )

    body_signs = []
    for body_def in body_defs:
        values = [
            next(
                body["longitude"]
                for body in probe["bodies"]
                if body["key"] == body_def["key"]
            )
            for probe in probes
        ]
        possible_signs = sorted({_sign_index(value) for value in values})
        body_signs.append(
            {
                "key": body_def["key"],
                "representative_sign_index": _sign_index(values[2]),
                "possible_sign_indices": possible_signs,
                "status": _status(possible_signs),
                "longitude_envelope": _circular_envelope(values),
            }
        )

    planet_in_house = []
    if representative_placements["execution_status"] == "computed":
        for body_def in body_defs:
            possible_houses = sorted(
                {
                    next(
                        placement["house"]
                        for placement in probe["placements"]["placements"]
                        if placement["key"] == body_def["key"]
                    )
                    for probe in probes
                }
            )
            representative_house = next(
                placement["house"]
                for placement in representative_placements["placements"]
                if placement["key"] == body_def["key"]
            )
            planet_in_house.append(
                {
                    "key": body_def["key"],
                    "representative_house": representative_house,
                    "possible_houses": possible_houses,
                    "status": _status(possible_houses),
                }
            )

    angles = {
        key: _circular_envelope(
            [probe["houses"][key] for probe in probes]
        )
        for key in ("asc", "mc", "desc", "ic")
    }
    house_cusps = [
        {
            "house": index + 1,
            **_circular_envelope(
                [probe["houses"]["cusps"][index] for probe in probes]
            ),
        }
        for index in range(12)
    ]

    classifications = [
        item["status"] for item in body_signs + planet_in_house
    ]
    sect_summary = None
    if representative_sect is not None:
        possible = list(
            dict.fromkeys(probe["sect"]["is_day"] for probe in probes)
        )
        sect_summary = {
            "representative_is_day": representative_sect["is_day"],
            "possible_is_day": possible,
            "status": _status(possible),
        }
        classifications.append(sect_summary["status"])

    lots_summary = None
    if representative_lots:
        lots_summary = {
            key: _circular_envelope(
                [probe["lots"][key] for probe in probes]
            )
            for key in ("fortune", "spirit")
        }

    voc_summary = None
    if representative_void_of_course:
        possible = list(
            dict.fromkeys(
                probe["void_of_course"]["is_void_of_course"]
                for probe in probes
            )
        )
        voc_summary = {
            "representative_is_void_of_course": (
                representative_void_of_course["is_void_of_course"]
            ),
            "possible_is_void_of_course": possible,
            "status": _status(possible),
        }
        classifications.append(voc_summary["status"])

    start = dtmod.datetime(
        request.datetime.year,
        request.datetime.month,
        request.datetime.day,
        request.datetime.hour,
    )
    end = start + dtmod.timedelta(hours=1)
    return {
        "precision": "approximate_hour",
        "status": (
            "varies_within_sampled_hour"
            if "varies_within_sampled_hour" in classifications
            else "sampled_stable"
        ),
        "interval_start_local": start.isoformat(timespec="minutes"),
        "interval_end_exclusive_local": end.isoformat(timespec="minutes"),
        "representative_minute": 30,
        "representative_local_time": representative["input_local_time"],
        "sampling_semantics": (
            "five_discrete_probes_not_continuous_hour_proof"
        ),
        "probes": [
            {
                "minute": probe["minute"],
                "second": probe["second"],
                "input_local_time": probe["input_local_time"],
            }
            for probe in probes
        ],
        "transitions": transitions,
        "transition_refinement_policy": (
            "bounded_bisection_only_when_adjacent_probe_classifications_differ"
        ),
        "angles": angles,
        "house_cusps": house_cusps,
        "body_signs": body_signs,
        "planet_in_house": planet_in_house,
        "sect": sect_summary,
        "lots": lots_summary,
        "void_of_course": voc_summary,
        "not_evaluated_paths": [
            "astronomical_data.fixed_stars",
            "astronomical_data.lunar_events",
            "astronomical_data.horizon_events",
            "derived_geometry.antiscia",
            "derived_methods.declination_aspects",
        ],
        "limitations": [
            (
                "Five probes can reveal sampled changes but cannot prove "
                "continuous stability for every instant in the hour."
            ),
            (
                "The midpoint chart is representative, not a claim that the "
                "birth occurred at minute 30."
            ),
        ],
    }
