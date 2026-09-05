"""Bounded sensitivity summaries for hour-level and date-only inputs."""

from __future__ import annotations

import datetime as dtmod
from typing import TypedDict

import swisseph as swe

from ..config import BODY_ID_BY_KEY, CLASSICAL_BODIES, NODE_BODIES, OUTER_BODIES
from .bodies import compute_bodies, make_longitude_sampler
from .computation_mode import ComputationContext
from .house_placements import compute_planet_house_placements
from .houses import HouseSystemUnavailableError, compute_houses
from .lots import compute_lots, determine_sect
from .moon import determine_void_of_course, find_voc_candidates
from .time_utils import (
    AmbiguousLocalTimeChoiceRequiredError,
    NonexistentLocalTimeError,
    _to_utc_datetime,
    compute_time_conversion,
)
from .trace import Trace


# Which response paths a sensitivity pass can speak about, and what each
# precision actually does with them.  The disclosure ledger is derived from
# these by subtraction rather than written out beside them: a hand-kept list
# drifts silently the moment a pass starts or stops recomputing something,
# and the reader cannot tell a path that was assessed and stable from a path
# that was never looked at.
#
# Paths that describe the request rather than a result — the atmosphere, the
# time conversion, the fixed-star policy — are not assessable: nothing about
# them varies with the birth instant within the interval.
#
# The universe is fixed rather than narrowed to the modules a given request
# asked for.  A ledger that shrinks with the request answers "was this
# assessed?" only for what happened to be switched on, and leaves the reader to
# infer the rest from an absence.
ASSESSABLE_RESPONSE_PATHS = (
    "astronomical_data.angles",
    "astronomical_data.bodies",
    "astronomical_data.extra_angles",
    "astronomical_data.fixed_stars",
    "astronomical_data.horizon_events",
    "astronomical_data.lunar_apsides",
    "astronomical_data.lunar_events",
    "astronomical_data.nodes",
    "astronomical_data.parallax_moon",
    "derived_geometry.antiscia",
    "derived_methods.aspects",
    "derived_methods.declination_aspects",
    "derived_methods.essential_dignities",
    "derived_methods.house_division",
    "derived_methods.lots",
    "derived_methods.planet_in_house",
    "derived_methods.sect",
    "derived_methods.void_of_course",
)

# "partial" is its own state because a boolean agreeing across probes does not
# make the module it belongs to stable.  Reporting it as fully assessed would
# let a reader take one field's stability for the whole module's.
COVERAGE_BY_PRECISION: dict[str, dict[str, str]] = {
    "approximate_hour": {
        "astronomical_data.angles": "recomputed",
        "astronomical_data.bodies": "recomputed",
        "derived_methods.house_division": "recomputed",
        "derived_methods.lots": "recomputed",
        "derived_methods.planet_in_house": "recomputed",
        "derived_methods.sect": "recomputed",
        "derived_methods.void_of_course": "partial",
    },
    "date_only": {
        "astronomical_data.bodies": "recomputed",
        "astronomical_data.nodes": "recomputed",
    },
}

# What the partial coverage above does and does not compare.
PARTIAL_COVERAGE_SEMANTICS = {
    "derived_methods.void_of_course": (
        "只比較了「是否月空亡」這一項；離相位的出宮時刻、下一個完成的相位、"
        "其完成時刻，以及求解器證據，都沒有在各取樣點之間重新評估。"
    ),
}


# A probe that cannot evaluate one sample degrades that sample.  The except
# clause named the one refusal that had actually been seen, and its siblings —
# a house system unavailable at this latitude, an hour the zone repeats — were
# outside it, so a refusal about one sampling instant answered for the whole
# chart.  The set is the declared domain refusals, not the ones observed so far.
UNEVALUABLE_SAMPLE_ERRORS = (
    NonexistentLocalTimeError,
    AmbiguousLocalTimeChoiceRequiredError,
    HouseSystemUnavailableError,
)


def _coverage_ledger(precision: str) -> dict:
    coverage = COVERAGE_BY_PRECISION[precision]
    unknown = set(coverage) - set(ASSESSABLE_RESPONSE_PATHS)
    if unknown:
        raise ValueError(f"coverage names unassessable paths: {sorted(unknown)}")
    return {
        "not_evaluated_paths": [
            path for path in ASSESSABLE_RESPONSE_PATHS if path not in coverage
        ],
        "partially_evaluated_paths": [
            {"path": path, "semantics": PARTIAL_COVERAGE_SEMANTICS[path]}
            for path in ASSESSABLE_RESPONSE_PATHS
            if coverage.get(path) == "partial"
        ],
    }


PROBE_OFFSETS_SECONDS = (0, 900, 1800, 2700, 3599)
TRANSITION_RESOLUTION_SECONDS = 15


def approximate_hour_representative_request(request):
    """Choose the midpoint when real, otherwise the first real governed probe."""

    preferred = (1800, *(value for value in PROBE_OFFSETS_SECONDS if value != 1800))
    for offset_seconds in preferred:
        candidate = request.model_copy(
            update={
                "datetime": request.datetime.model_copy(
                    update={
                        "minute": offset_seconds // 60,
                        "second": float(offset_seconds % 60),
                    }
                )
            }
        )
        try:
            _to_utc_datetime(
                candidate.datetime,
                candidate.timezone,
                require_explicit_fold=True,
            )
        except NonexistentLocalTimeError:
            continue
        return candidate, offset_seconds
    raise NonexistentLocalTimeError(
        "no instant in the stated civil hour exists in this time zone"
    )


class _ResolvedLocalInstant(TypedDict):
    local_time: str
    fold: int
    utc_datetime: dtmod.datetime
    utc_time: str

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


def _compute_effective_bodies(
    *, request, body_defs: list[dict], jd_ut: float, context, trace: Trace
) -> tuple[list[dict], ComputationContext | None]:
    """Apply the explicitly selected Moon origin to sensitivity probes too."""

    bodies = compute_bodies(
        body_defs,
        jd_ut,
        context,
        request.atmosphere,
        trace,
        physical=False,
    )
    moon_context = None
    if request.options.moon_position_profile == "moon_only_topocentric_v1":
        moon_mode = request.computation_mode.model_copy(update={"center": "topocentric"})
        moon_context = ComputationContext(moon_mode, request.location)
        moon = compute_bodies(
            [next(body for body in body_defs if body["key"] == "moon")],
            jd_ut,
            moon_context,
            request.atmosphere,
            trace,
            physical=False,
        )[0]
        bodies = [moon if body["key"] == "moon" else body for body in bodies]
    return bodies, moon_context


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
    bodies, moon_context = _compute_effective_bodies(
        request=probe_request,
        body_defs=body_defs,
        jd_ut=jd_ut,
        context=context,
        trace=trace,
    )
    houses = None
    if probe_request.options.include_houses:
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
    else:
        placements = {
            "execution_status": "not_requested",
            "placements": [],
        }

    sect = lots = voc = None
    if probe_request.options.include_lots and houses is not None:
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
            {**item, "body_id": BODY_ID_BY_KEY[item["key"]]}
            for item in bodies
            if item["key"] in classical_keys
        ]
        voc = determine_void_of_course(
            find_voc_candidates(
                body_map["moon"],
                other_bodies,
                trace,
                jd_ut=jd_ut,
                longitude_at=make_longitude_sampler(
                    context, moon_ctx=moon_context
                ),
                moon_id=BODY_ID_BY_KEY["moon"],
            ),
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


# A body may legitimately have no longitude: a heliocentric chart returns the
# Sun that way, and the primary response says so rather than inventing a value.
# Anything that re-reads those bodies must carry the same allowance, or it
# re-imposes a premise the chart already relaxed.
UNAVAILABLE_COORDINATE_STATUS = "not_applicable_no_longitude_in_this_mode"


def _classification_state(probe: dict, body_defs: list[dict]) -> dict:
    body_map = {item["key"]: item for item in probe["bodies"]}
    state = {
        f"body_signs.{item['key']}": _sign_index(
            body_map[item["key"]]["longitude"]
        )
        for item in body_defs
        if body_map[item["key"]]["longitude"] is not None
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

    missing = object()
    transitions: list[tuple[int, str, dict]] = []

    def state_at(offset: int) -> dict:
        if offset not in probes_by_offset:
            probes_by_offset[offset] = _extract_probe(
                offset_seconds=offset,
                request=request,
                body_defs=body_defs,
                representative=None,
            )
        return _classification_state(probes_by_offset[offset], body_defs)

    ordered_offsets = sorted(probes_by_offset)
    for segment_left, segment_right in zip(
        ordered_offsets,
        ordered_offsets[1:],
    ):
        left_state = state_at(segment_left)
        right_state = state_at(segment_right)
        if left_state == right_state:
            continue

        changed_paths = sorted(
            key
            for key in set(left_state) | set(right_state)
            if left_state.get(key, missing) != right_state.get(key, missing)
        )
        for path in changed_paths:
            left_offset = segment_left
            right_offset = segment_right
            left_value = left_state.get(path, missing)
            while right_offset - left_offset > TRANSITION_RESOLUTION_SECONDS:
                midpoint = (left_offset + right_offset) // 2
                try:
                    midpoint_state = state_at(midpoint)
                except UNEVALUABLE_SAMPLE_ERRORS:
                    break
                if midpoint_state.get(path, missing) == left_value:
                    left_offset = midpoint
                else:
                    right_offset = midpoint

            transitions.append(
                (
                    left_offset,
                    path,
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
                        "changed_paths": [path],
                        "semantics": (
                            "bounded_bisection_of_a_sampled_classification_change"
                        ),
                    },
                )
            )
    return [record for _offset, _path, record in sorted(transitions)]


def build_approximate_hour_sensitivity(
    *,
    request,
    body_defs: list[dict],
    representative_bodies: list[dict],
    representative_houses: dict | None,
    representative_placements: dict,
    representative_sect: dict | None,
    representative_lots: dict | None,
    representative_void_of_course: dict | None,
    representative_offset_seconds: int,
) -> dict:
    """Build governed probes and reuse the selected real representative chart."""

    representative = {
        "input_local_time": (
            f"{request.datetime.year:04d}-{request.datetime.month:02d}-"
            f"{request.datetime.day:02d} {request.datetime.hour:02d}:"
            f"{representative_offset_seconds // 60:02d}:"
            f"{representative_offset_seconds % 60:02d}.00"
        ),
        "bodies": representative_bodies,
        "houses": representative_houses,
        "placements": representative_placements,
        "sect": representative_sect,
        "lots": representative_lots,
        "void_of_course": representative_void_of_course,
    }
    # 半小時位移的時區（例如 Australia/Lord_Howe）會讓一個
    # 民用小時**部分**不存在：2024-10-06 的 02:00–02:29 不存在，02:30–02:59 存在。
    # 任何一個探針擲出 NonexistentLocalTimeError 就讓整個請求變成 422 是錯的：那會說
    # 「該時鐘從未指向 02:00，請改正出生時間」——但使用者說的是「生於 02 那個小時」，
    # 那句陳述與該時區規則相容，代表時刻 02:30 的整張盤也算得出來。錯誤歸因不成立。
    #
    # 這裡只跳過取不到的取樣點並具名回報，不改變「整個小時都不存在」時的拒絕：
    # 那一種仍然是使用者陳述本身不可能，應該擋下（見下方 usable_offsets 為空的分支）。
    probes_by_offset: dict[int, dict] = {}
    skipped_offsets: list[int] = []
    for offset_seconds in PROBE_OFFSETS_SECONDS:
        try:
            probes_by_offset[offset_seconds] = _extract_probe(
                offset_seconds=offset_seconds,
                request=request,
                body_defs=body_defs,
                representative=(
                    representative
                    if offset_seconds == representative_offset_seconds
                    else None
                ),
            )
        except UNEVALUABLE_SAMPLE_ERRORS:
            skipped_offsets.append(offset_seconds)

    usable_offsets = [
        offset for offset in PROBE_OFFSETS_SECONDS if offset in probes_by_offset
    ]
    if not usable_offsets:
        # 整個小時都不存在。維持既有行為：由呼叫端轉成 422。
        raise NonexistentLocalTimeError(
            "no instant in the stated civil hour exists in this time zone"
        )
    probes = [probes_by_offset[offset] for offset in usable_offsets]
    representative_index = (
        usable_offsets.index(representative_offset_seconds)
    )
    probe_coverage = {
        "requested_offsets_seconds": list(PROBE_OFFSETS_SECONDS),
        "sampled_offsets_seconds": usable_offsets,
        "skipped_nonexistent_offsets_seconds": skipped_offsets,
        "reason_code": (
            "some_probe_offsets_do_not_exist_locally" if skipped_offsets else None
        ),
        "semantics": (
            "a partially nonexistent civil hour is sampled over the part that "
            "exists; the envelope below covers only the sampled instants"
        ),
    }
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
        if any(value is None for value in values):
            body_signs.append(
                {
                    "key": body_def["key"],
                    "name": body_def["zh"],
                    "representative_sign_index": None,
                    "possible_sign_indices": [],
                    "status": UNAVAILABLE_COORDINATE_STATUS,
                    "longitude_envelope": None,
                }
            )
            continue
        possible_signs = sorted({_sign_index(value) for value in values})
        body_signs.append(
            {
                "key": body_def["key"],
                "name": body_def["zh"],
                "representative_sign_index": _sign_index(
                    values[representative_index]
                ),
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
                    "name": body_def["zh"],
                    "representative_house": representative_house,
                    "possible_houses": possible_houses,
                    "status": _status(possible_houses),
                }
            )

    # One branch maps each angle to an envelope, the other returns a receipt.
    # Stating the union beats letting whichever branch mypy reads first decide.
    angles: dict
    if representative_houses is not None:
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
    else:
        # The other branch builds a per-angle mapping; this one a receipt.
        # Annotated so the union is stated rather than inferred from whichever
        # branch mypy reads first.
        angles = {
            "status": "not_requested",
            "reason_code": "house_calculation_not_executed",
        }
        house_cusps = []

    classifications = [
        item["status"] for item in body_signs + planet_in_house
    ]
    sect_summary = None
    if (
        isinstance(representative_sect, dict)
        and representative_sect.get("is_day") is not None
    ):
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
    if (
        representative_lots
        and representative_lots.get("fortune") is not None
        and representative_lots.get("spirit") is not None
    ):
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
        "representative_minute": representative_offset_seconds // 60,
        "representative_local_time": representative["input_local_time"],
        "representative_semantics": (
            "representative_midpoint_not_exact_birth_time"
            if representative_offset_seconds == 1800
            else "representative_first_available_probe_not_exact_birth_time"
        ),
        "sampling_semantics": "five_discrete_probes_not_continuous_hour_proof",
        "probe_coverage": probe_coverage,
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
        **_coverage_ledger("approximate_hour"),
        "limitations": [
            "five_discrete_probes_not_continuous_hour_proof",
            "representative_midpoint_not_exact_birth_time",
        ],
    }


def _date_only_local_instants(
    request,
    local_value: dtmod.datetime,
) -> list[_ResolvedLocalInstant]:
    """Resolve all real instants for one wall-clock value, including both folds."""

    resolved: dict[str, _ResolvedLocalInstant] = {}
    for fold in (0, 1):
        datetime_value = request.datetime.model_copy(
            update={
                "year": local_value.year,
                "month": local_value.month,
                "day": local_value.day,
                "hour": local_value.hour,
                "minute": local_value.minute,
                "second": float(
                    local_value.second + local_value.microsecond / 1_000_000
                ),
            }
        )
        timezone_value = request.timezone.model_copy(update={"fold": fold})
        try:
            utc_value, _offset, _label, _warning = _to_utc_datetime(
                datetime_value,
                timezone_value,
            )
        except NonexistentLocalTimeError:
            continue
        key = utc_value.isoformat()
        resolved.setdefault(
            key,
            {
                "local_time": local_value.isoformat(timespec="seconds"),
                "fold": fold,
                "utc_datetime": utc_value,
                "utc_time": utc_value.isoformat().replace("+00:00", "Z"),
            },
        )
    return sorted(resolved.values(), key=lambda item: item["utc_datetime"])


def _date_only_sample_instants(
    request,
) -> tuple[
    list[_ResolvedLocalInstant],
    _ResolvedLocalInstant,
    _ResolvedLocalInstant,
]:
    start = dtmod.datetime(
        request.datetime.year,
        request.datetime.month,
        request.datetime.day,
    )
    next_day = start + dtmod.timedelta(days=1)
    samples = []
    for hour in range(24):
        samples.extend(
            _date_only_local_instants(
                request,
                start + dtmod.timedelta(hours=hour),
            )
        )
    samples.extend(
        _date_only_local_instants(
            request,
            start + dtmod.timedelta(hours=23, minutes=59, seconds=59),
        )
    )
    samples = sorted(
        {item["utc_time"]: item for item in samples}.values(),
        key=lambda item: item["utc_datetime"],
    )

    def first_real_instant(
        boundary: dtmod.datetime,
        *,
        days: int = 1,
    ) -> _ResolvedLocalInstant | None:
        # Some IANA zones advanced the clock at local midnight, so the civil-day
        # boundary is the first real wall-clock minute on or after that label,
        # not a fabricated fold-normalized midnight.  Others skipped a whole
        # civil date when they crossed the date line, so the instant that ends
        # the selected day can carry the label of a later date entirely.  The
        # search is over instants, and the label is whatever the zone gives the
        # instant it finds.
        for minute in range(days * 24 * 60):
            candidates = _date_only_local_instants(
                request,
                boundary + dtmod.timedelta(minutes=minute),
            )
            if candidates:
                return candidates[0]
        return None

    day_start = first_real_instant(start)
    # A skipped civil date is bounded in practice by a single day, and this
    # window is generous rather than open-ended: a search that never terminates
    # is not a repair.
    next_day_start = first_real_instant(next_day, days=8)
    if not samples or day_start is None or next_day_start is None:
        raise ValueError(
            "date_only civil-day boundary is unavailable in the selected timezone"
        )
    return samples, day_start, next_day_start


def build_date_only_sensitivity(
    *,
    request,
    body_defs: list[dict],
    node_defs: list[dict],
    representative_bodies: list[dict],
    representative_nodes: list[dict],
) -> dict:
    """Return sampled daily longitude envelopes without inventing a birth time.

    The samples are evidence about the selected civil date, not a continuous
    extrema proof.  IANA spring gaps are skipped and both folds of a repeated
    wall-clock hour are included.
    """

    sample_instants, day_start, next_day_start = _date_only_sample_instants(
        request
    )
    context = ComputationContext(request.computation_mode, request.location)
    sampled_objects: dict[str, dict] = {}

    for sample in sample_instants:
        utc_value = sample["utc_datetime"]
        utc_second = utc_value.second + utc_value.microsecond / 1_000_000
        _jd_et, jd_ut = swe.utc_to_jd(
            utc_value.year,
            utc_value.month,
            utc_value.day,
            utc_value.hour,
            utc_value.minute,
            utc_second,
            swe.GREG_CAL,
        )
        trace = Trace()
        effective_bodies, _moon_context = _compute_effective_bodies(
            request=request,
            body_defs=body_defs,
            jd_ut=jd_ut,
            context=context,
            trace=trace,
        )
        objects = effective_bodies + compute_bodies(
            node_defs,
            jd_ut,
            context,
            request.atmosphere,
            trace,
            physical=False,
            moon_relative=True,
        )
        if request.options.include_south_nodes:
            object_map = {item["key"]: item for item in objects}
            for north_key, south_key, south_name in (
                ("true_node", "true_south_node", "南交點(密切)"),
                ("mean_node", "mean_south_node", "南交點(平均)"),
            ):
                north = object_map[north_key]
                objects.append(
                    {
                        "key": south_key,
                        "name": south_name,
                        "longitude": (
                            (north["longitude"] + 180.0) % 360.0
                            if north.get("longitude") is not None
                            else None
                        ),
                    }
                )
        for item in objects:
            longitude = item.get("longitude")
            if longitude is None:
                continue
            record = sampled_objects.setdefault(
                item["key"],
                {"key": item["key"], "name": item["name"], "values": []},
            )
            record["values"].append(
                {
                    "longitude": longitude,
                    "local_time": sample["local_time"],
                    "fold": sample["fold"],
                    "utc_time": sample["utc_time"],
                }
            )

    representative_map = {
        item["key"]: item
        for item in representative_bodies + representative_nodes
    }
    position_ranges = []
    for key, record in sampled_objects.items():
        values = [item["longitude"] for item in record["values"]]
        possible_signs = sorted({_sign_index(value) for value in values})
        representative = representative_map.get(key)
        position_ranges.append(
            {
                "key": key,
                "name": record["name"],
                "representative_longitude": (
                    representative.get("longitude")
                    if representative is not None
                    else None
                ),
                "possible_sign_indices": possible_signs,
                "status": (
                    "sampled_stable"
                    if len(possible_signs) == 1
                    else "varies_within_sampled_day"
                ),
                "sampled_longitude_envelope": _circular_envelope(values),
            }
        )
    position_ranges.sort(key=lambda item: item["key"])

    moon_range = next(
        item for item in position_ranges if item["key"] == "moon"
    )
    moon_envelope = moon_range["sampled_longitude_envelope"]
    moon_values = sampled_objects["moon"]["values"]
    moon_transitions = []
    for left, right in zip(moon_values, moon_values[1:]):
        left_sign = _sign_index(left["longitude"])
        right_sign = _sign_index(right["longitude"])
        if left_sign != right_sign:
            moon_transitions.append(
                {
                    "from_sign_index": left_sign,
                    "to_sign_index": right_sign,
                    "lower_sample_local": left["local_time"],
                    "upper_sample_local": right["local_time"],
                    "semantics": "crossing_exists_between_adjacent_samples_exact_time_not_solved",
                }
            )
    minimum_boundary_distance = min(
        min(value["longitude"] % 30.0, 30.0 - value["longitude"] % 30.0)
        for value in moon_values
    )
    duration_hours = (
        next_day_start["utc_datetime"] - day_start["utc_datetime"]
    ).total_seconds() / 3600.0
    any_variation = any(
        item["status"] == "varies_within_sampled_day"
        for item in position_ranges
    )
    # The local bounds are the local rendering of the same two instants the UTC
    # bounds and the duration are taken from.  Writing them independently from
    # the nominal date is how a receipt comes to state a local midnight that
    # the zone never had.
    return {
        "precision": "date_only",
        "status": (
            "varies_within_sampled_day" if any_variation else "sampled_stable"
        ),
        "interval_start_local": day_start["local_time"],
        "interval_end_exclusive_local": next_day_start["local_time"],
        "interval_start_utc": day_start["utc_time"],
        "interval_end_exclusive_utc": next_day_start["utc_time"],
        "civil_day_duration_hours": duration_hours,
        "representative_local_time": (
            f"{request.datetime.year:04d}-{request.datetime.month:02d}-"
            f"{request.datetime.day:02d} 12:00:00.00"
        ),
        "representative_semantics": (
            "local_noon_computational_anchor_not_birth_time"
        ),
        "sampling_semantics": (
            "hourly_local_clock_samples_plus_day_end_not_continuous_extrema_proof"
        ),
        "samples": [
            {
                "local_time": item["local_time"],
                "fold": item["fold"],
                "utc_time": item["utc_time"],
            }
            for item in sample_instants
        ],
        "position_ranges": position_ranges,
        "moon_boundary_assessment": {
            "possible_sign_indices": moon_range["possible_sign_indices"],
            "could_cross_sign_boundary": (
                len(moon_range["possible_sign_indices"]) > 1
            ),
            "could_cross_zero_degrees": moon_envelope["crosses_zero"],
            "minimum_sampled_distance_to_sign_boundary_degrees": (
                minimum_boundary_distance
            ),
            "sampled_sign_boundary_transitions": moon_transitions,
            "sampled_longitude_envelope": moon_envelope,
        },
        **_coverage_ledger("date_only"),
        "limitations": [
            "hourly_local_clock_samples_plus_day_end_not_continuous_extrema_proof",
            "local_noon_computational_anchor_not_birth_time",
        ],
    }
