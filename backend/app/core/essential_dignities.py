"""Source-named essential-dignity component lookups.

This module deliberately stops before interpretation or scoring.  It reports which
closed historical rule table applies to a tropical longitude.  Profiles are not
silently treated as interchangeable and no profile is a universal default.

Boundary convention
-------------------
All degree intervals are zero-based and half-open.  A source's cumulative ``6``
therefore means ``[0, 6)``; exactly 6 degrees belongs to the next segment.  A
normalized longitude is always in ``[0, 360)``, so 30 degrees within a sign cannot
occur.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypedDict

from .trace import Trace


SIGNS = (
    "aries", "taurus", "gemini", "cancer", "leo", "virgo",
    "libra", "scorpio", "sagittarius", "capricorn", "aquarius", "pisces",
)
ELEMENTS = ("fire", "earth", "air", "water")

DOMICILE_EXALTATION_PROFILE_ID = (
    "traditional_seven_domicile_exaltation_signs_v1"
)
DOMICILE_SIGNS = {
    "sun": ("leo",),
    "moon": ("cancer",),
    "mercury": ("gemini", "virgo"),
    "venus": ("taurus", "libra"),
    "mars": ("aries", "scorpio"),
    "jupiter": ("sagittarius", "pisces"),
    "saturn": ("capricorn", "aquarius"),
}
EXALTATION_SIGNS = {
    "sun": "aries",
    "moon": "taurus",
    "mercury": "virgo",
    "venus": "pisces",
    "mars": "capricorn",
    "jupiter": "cancer",
    "saturn": "libra",
}
METHOD_STATUS = "sebastian_authorized_profiles_through_mth_028_2026_08_04"
METHOD_AUTHORITY = "Sebastian rulings MTH-024 through MTH-028 (2026-08-04)"


class DignityParticipantScopeError(ValueError):
    """A profile received a participant outside its declared closed scope."""


class _SectTriplicityRule(TypedDict):
    day: tuple[str, ...]
    night: tuple[str, ...]


class _PtolemyTriplicityRule(_SectTriplicityRule):
    principal: str | None


def _segments(*pairs: tuple[str, int]) -> tuple[tuple[str, int], ...]:
    if sum(length for _ruler, length in pairs) != 30:
        raise ValueError("each bounds sign must total 30 degrees")
    return pairs


# Ptolemy, Tetrabiblos I.20, Robbins 1940 translation, pp. 97-99.
EGYPTIAN_BOUNDS = (
    _segments(("jupiter", 6), ("venus", 6), ("mercury", 8), ("mars", 5), ("saturn", 5)),
    _segments(("venus", 8), ("mercury", 6), ("jupiter", 8), ("saturn", 5), ("mars", 3)),
    _segments(("mercury", 6), ("jupiter", 6), ("venus", 5), ("mars", 7), ("saturn", 6)),
    _segments(("mars", 7), ("venus", 6), ("mercury", 6), ("jupiter", 7), ("saturn", 4)),
    _segments(("jupiter", 6), ("venus", 5), ("saturn", 7), ("mercury", 6), ("mars", 6)),
    _segments(("mercury", 7), ("venus", 10), ("jupiter", 4), ("mars", 7), ("saturn", 2)),
    _segments(("saturn", 6), ("mercury", 8), ("jupiter", 7), ("venus", 7), ("mars", 2)),
    _segments(("mars", 7), ("venus", 4), ("mercury", 8), ("jupiter", 5), ("saturn", 6)),
    _segments(("jupiter", 12), ("venus", 5), ("mercury", 4), ("saturn", 5), ("mars", 4)),
    _segments(("mercury", 7), ("jupiter", 7), ("venus", 8), ("saturn", 4), ("mars", 4)),
    _segments(("mercury", 7), ("venus", 6), ("jupiter", 7), ("mars", 5), ("saturn", 5)),
    _segments(("venus", 12), ("jupiter", 4), ("mercury", 3), ("mars", 9), ("saturn", 2)),
)

# Ptolemy's second table as printed by Robbins (1940), Tetrabiblos I.21.
# The final Libra ruler is Mars; some HTML symbol extraction duplicates Saturn.
PTOLEMY_ROBBINS_BOUNDS = (
    _segments(("jupiter", 6), ("venus", 8), ("mercury", 7), ("mars", 5), ("saturn", 4)),
    _segments(("venus", 8), ("mercury", 7), ("jupiter", 7), ("saturn", 2), ("mars", 6)),
    _segments(("mercury", 7), ("jupiter", 6), ("venus", 7), ("mars", 6), ("saturn", 4)),
    _segments(("mars", 6), ("jupiter", 7), ("mercury", 7), ("venus", 7), ("saturn", 3)),
    _segments(("jupiter", 6), ("mercury", 7), ("saturn", 6), ("venus", 6), ("mars", 5)),
    _segments(("mercury", 7), ("venus", 6), ("jupiter", 5), ("saturn", 6), ("mars", 6)),
    _segments(("saturn", 6), ("venus", 5), ("mercury", 5), ("jupiter", 8), ("mars", 6)),
    _segments(("mars", 6), ("venus", 7), ("jupiter", 8), ("mercury", 6), ("saturn", 3)),
    _segments(("jupiter", 8), ("venus", 6), ("mercury", 5), ("saturn", 6), ("mars", 5)),
    _segments(("venus", 6), ("mercury", 6), ("jupiter", 7), ("saturn", 6), ("mars", 5)),
    _segments(("saturn", 6), ("mercury", 6), ("venus", 8), ("jupiter", 5), ("mars", 5)),
    _segments(("venus", 8), ("jupiter", 6), ("mercury", 6), ("mars", 5), ("saturn", 5)),
)

# Lilly, Christian Astrology (1647), p.104, cumulative term endpoints transcribed
# from the first-edition scan and converted here to segment lengths.
LILLY_RECEIVED_BOUNDS = (
    _segments(("jupiter", 6), ("venus", 8), ("mercury", 7), ("mars", 5), ("saturn", 4)),
    _segments(("venus", 8), ("mercury", 7), ("jupiter", 7), ("saturn", 4), ("mars", 4)),
    _segments(("mercury", 7), ("jupiter", 7), ("venus", 7), ("saturn", 4), ("mars", 5)),
    _segments(("mars", 6), ("jupiter", 7), ("mercury", 7), ("venus", 7), ("saturn", 3)),
    _segments(("jupiter", 6), ("venus", 7), ("saturn", 6), ("mercury", 6), ("mars", 5)),
    _segments(("mercury", 7), ("venus", 6), ("jupiter", 5), ("saturn", 6), ("mars", 6)),
    _segments(("saturn", 6), ("venus", 5), ("jupiter", 8), ("mercury", 5), ("mars", 6)),
    _segments(("mars", 6), ("jupiter", 8), ("venus", 7), ("mercury", 6), ("saturn", 3)),
    _segments(("jupiter", 8), ("venus", 6), ("mercury", 5), ("saturn", 6), ("mars", 5)),
    _segments(("venus", 6), ("mercury", 6), ("jupiter", 7), ("mars", 6), ("saturn", 5)),
    _segments(("saturn", 6), ("mercury", 6), ("venus", 8), ("jupiter", 5), ("mars", 5)),
    _segments(("venus", 8), ("jupiter", 6), ("mercury", 6), ("mars", 6), ("saturn", 4)),
)

BOUNDS_TABLES = {
    "egyptian_bounds_robbins_1940_v1": EGYPTIAN_BOUNDS,
    "ptolemy_bounds_robbins_1940_v1": PTOLEMY_ROBBINS_BOUNDS,
    "lilly_received_bounds_1647_v1": LILLY_RECEIVED_BOUNDS,
}

BOUNDS_SOURCES = {
    "egyptian_bounds_robbins_1940_v1": (
        "Ptolemy, Tetrabiblos I.20, Egyptian terms table; "
        "Frank E. Robbins trans., Loeb 1940, pp.97-99"
    ),
    "chaldaean_bounds_ptolemy_i_21_v1": (
        "Ptolemy, Tetrabiblos I.21: triplicity sequence, sect-dependent "
        "Saturn/Mercury order, lengths 8/7/6/5/4"
    ),
    "ptolemy_bounds_robbins_1940_v1": (
        "Ptolemy, Tetrabiblos I.21, second terms table as printed by "
        "Frank E. Robbins, Loeb 1940, pp.103-107"
    ),
    "lilly_received_bounds_1647_v1": (
        "William Lilly, Christian Astrology (1647), p.104, Table of the "
        "Essential Dignities, first-edition scan transcription"
    ),
}

CHALDAEAN_DAY_ORDER = (
    ("jupiter", "venus", "saturn", "mercury", "mars"),
    ("venus", "saturn", "mercury", "mars", "jupiter"),
    ("saturn", "mercury", "mars", "jupiter", "venus"),
    ("mars", "jupiter", "venus", "saturn", "mercury"),
)
CHALDAEAN_LENGTHS = (8, 7, 6, 5, 4)

CHALDEAN_FACES = (
    ("mars", "sun", "venus"), ("mercury", "moon", "saturn"),
    ("jupiter", "mars", "sun"), ("venus", "mercury", "moon"),
    ("saturn", "jupiter", "mars"), ("sun", "venus", "mercury"),
    ("moon", "saturn", "jupiter"), ("mars", "sun", "venus"),
    ("mercury", "moon", "saturn"), ("jupiter", "mars", "sun"),
    ("venus", "mercury", "moon"), ("saturn", "jupiter", "mars"),
)

DOROTHEAN = {
    "fire": ("sun", "jupiter", "saturn"),
    "earth": ("venus", "moon", "mars"),
    "air": ("saturn", "mercury", "jupiter"),
    "water": ("venus", "mars", "moon"),
}
PTOLEMY_TEXTUAL: dict[str, _PtolemyTriplicityRule] = {
    "fire": {"day": ("sun",), "night": ("jupiter",), "principal": None},
    "earth": {"day": ("venus",), "night": ("moon",), "principal": None},
    "air": {"day": ("saturn",), "night": ("mercury",), "principal": None},
    "water": {"day": ("mars", "venus"), "night": ("mars", "moon"), "principal": "mars"},
}
LILLY_COMPACT: dict[str, _SectTriplicityRule] = {
    "fire": {"day": ("sun",), "night": ("jupiter",)},
    "earth": {"day": ("venus",), "night": ("moon",)},
    "air": {"day": ("saturn",), "night": ("mercury",)},
    "water": {"day": ("mars",), "night": ("mars",)},
}

TRIPLICITY_IDS = (
    "dorothean_triplicity_three_rulers_v1",
    "ptolemy_triplicity_textual_corulership_v1",
    "lilly_triplicity_compact_1647_v1",
)
TRIPLICITY_SOURCES = {
    TRIPLICITY_IDS[0]: (
        "Dorotheus of Sidon, Carmen Astrologicum I.1, David Pingree trans. "
        "(1976), ordered day/night/third rulers"
    ),
    TRIPLICITY_IDS[1]: (
        "Ptolemy, Tetrabiblos I.18, Frank E. Robbins trans. (1940), "
        "including Mars principal and Venus/Moon co-rulership for water"
    ),
    TRIPLICITY_IDS[2]: (
        "William Lilly, Christian Astrology (1647), p.104, compact "
        "day/night triplicity table"
    ),
}


def _location(longitude: float) -> tuple[int, str, float]:
    normalized = longitude % 360.0
    sign_index = int(normalized // 30.0)
    return sign_index, SIGNS[sign_index], normalized - sign_index * 30.0


def _find_segment(
    segments: Iterable[tuple[str, int]], degree: float
) -> dict:
    start = 0.0
    for ruler, length in segments:
        end = start + length
        if start <= degree < end:
            return {
                "ruler": ruler,
                "segment_start_degrees": start,
                "segment_end_degrees": end,
            }
        start = end
    raise AssertionError(f"degree outside normalized sign: {degree}")


def _base_object(body: dict) -> dict:
    sign_index, sign, degree = _location(body["longitude"])
    return {
        "key": body["key"],
        "name": body["name"],
        "longitude": body["longitude"],
        "sign_index": sign_index,
        "sign": sign,
        "degree_in_sign": degree,
    }


def _chaldaean_segments(sign_index: int, *, is_day: bool) -> tuple:
    order = list(CHALDAEAN_DAY_ORDER[sign_index % 4])
    if not is_day:
        saturn = order.index("saturn")
        mercury = order.index("mercury")
        order[saturn], order[mercury] = order[mercury], order[saturn]
    return tuple(zip(order, CHALDAEAN_LENGTHS, strict=True))


CHALDAEAN_SEGMENTS_BY_SECT = {
    True: tuple(_chaldaean_segments(index, is_day=True) for index in range(12)),
    False: tuple(_chaldaean_segments(index, is_day=False) for index in range(12)),
}


def _bounds_profile(
    profile_id: str, bodies: list[dict], sect_is_day: bool | None
) -> dict:
    objects = []
    for body in bodies:
        item = _base_object(body)
        sign_index = item["sign_index"]
        degree = item["degree_in_sign"]
        if profile_id == "chaldaean_bounds_ptolemy_i_21_v1":
            day = _find_segment(CHALDAEAN_SEGMENTS_BY_SECT[True][sign_index], degree)
            night = _find_segment(CHALDAEAN_SEGMENTS_BY_SECT[False][sign_index], degree)
            selected = day if sect_is_day is True else night if sect_is_day is False else None
            selected_by_sect = dict(selected) if selected is not None else None
            if selected is not None:
                item.update(selected)
            else:
                item.update(
                    {
                        "ruler": None,
                        "segment_start_degrees": None,
                        "segment_end_degrees": None,
                    }
                )
            item.update(
                {
                    "day_candidate": day,
                    "night_candidate": night,
                    "selected_by_sect": selected_by_sect,
                    "selected_available": selected is not None,
                }
            )
        else:
            table = BOUNDS_TABLES[profile_id]
            item.update(_find_segment(table[sign_index], degree))
        objects.append(item)
    return {
        "profile_id": profile_id,
        "technique": "bounds",
        "source": BOUNDS_SOURCES[profile_id],
        "interval_semantics": "half_open_[start,end)",
        "sect_dependency": (
            "saturn_mercury_order" if profile_id.startswith("chaldaean_") else None
        ),
        "objects": objects,
    }


def _decan_profile(profile_id: str, bodies: list[dict]) -> dict:
    ruler_type = (
        "planet"
        if profile_id == "chaldean_planetary_faces_firmicus_ii_4_v1"
        else "sign"
    )
    objects = []
    for body in bodies:
        item = _base_object(body)
        decan_index = min(int(item["degree_in_sign"] // 10.0), 2)
        if ruler_type == "planet":
            ruler = CHALDEAN_FACES[item["sign_index"]][decan_index]
        else:
            ruler = SIGNS[(item["sign_index"] * 3 + decan_index) % 12]
        item.update(
            {
                "decan_number": decan_index + 1,
                "segment_start_degrees": float(decan_index * 10),
                "segment_end_degrees": float((decan_index + 1) * 10),
                "ruler_type": ruler_type,
                "ruler": ruler,
            }
        )
        objects.append(item)
    source = (
        "Firmicus Maternus, Mathesis II.4, planetary decans/faces"
        if ruler_type == "planet"
        else "Manilius, Astronomica IV.294-386 (decan assignments from IV.312ff.)"
    )
    return {
        "profile_id": profile_id,
        "technique": "face_decan",
        "source": source,
        "ruler_type": ruler_type,
        "interval_semantics": "half_open_10_degree_bins",
        "objects": objects,
    }


def _domicile_exaltation_profile(bodies: list[dict]) -> dict:
    """Match the traditional seven against whole-sign houses/exaltations.

    This profile deliberately does not choose among transmitted exact exaltation
    degrees.  Ptolemy, Tetrabiblos I.17 and I.19 supplies a whole-sign scheme;
    Dorotheus I.1-2, Valens III.4, and Firmicus Mathesis II.2-3 also preserve
    the familiar sign assignments while supplying degree traditions whose
    variants require a separate method decision.
    """

    unsupported = sorted(
        {
            str(body.get("key", "<missing>"))
            for body in bodies
            if body.get("key") not in DOMICILE_SIGNS
        }
    )
    if unsupported:
        raise DignityParticipantScopeError(
            "unsupported domicile/exaltation participant: "
            + ", ".join(unsupported)
        )

    objects = []
    for body in bodies:
        item = _base_object(body)
        key = item["key"]
        domicile_signs = DOMICILE_SIGNS[key]
        exaltation_sign = EXALTATION_SIGNS[key]
        item.update(
            {
                "domicile_signs": list(domicile_signs),
                "exaltation_sign": exaltation_sign,
                "domicile_matched": item["sign"] in domicile_signs,
                "exaltation_matched": item["sign"] == exaltation_sign,
            }
        )
        objects.append(item)
    return {
        "profile_id": DOMICILE_EXALTATION_PROFILE_ID,
        "technique": "domicile_exaltation",
        "source": (
            "Ptolemy, Tetrabiblos I.17 and I.19, Frank E. Robbins trans. "
            "(Loeb 1940); sign assignments corroborated by Dorotheus "
            "Carmen Astrologicum I.1-2, Valens Anthologies III.4, and "
            "Firmicus Maternus Mathesis II.2-3"
        ),
        "sources": [
            {
                "work": "Ptolemy, Tetrabiblos",
                "location": "I.17 and I.19",
                "role": (
                    "whole_sign_domicile_and_exaltation_basis_without_"
                    "exact_degrees_in_the_cited_chapters"
                ),
            },
            {
                "work": "Dorotheus of Sidon, Carmen Astrologicum",
                "location": "I.1-2",
                "role": (
                    "corroborating_sign_assignments_source_also_supplies_"
                    "exact_degrees_not_evaluated_by_this_profile"
                ),
            },
            {
                "work": "Vettius Valens, Anthologies",
                "location": "III.4",
                "role": (
                    "corroborating_exaltation_signs_source_also_supplies_"
                    "exact_degrees_not_evaluated_by_this_profile"
                ),
            },
            {
                "work": "Firmicus Maternus, Mathesis",
                "location": "II.2-3",
                "role": (
                    "corroborating_domiciles_and_exaltation_signs_source_"
                    "also_supplies_exact_degrees_not_evaluated_by_this_profile"
                ),
            },
        ],
        "source_scope_note": (
            "Ptolemy I.17 and I.19 support the sign-level output used here. "
            "The corroborating Dorotheus, Valens, and Firmicus passages also "
            "transmit exact-degree traditions that this profile deliberately "
            "does not evaluate; their inclusion does not imply that ancient "
            "traditions were sign-only."
        ),
        "match_basis": "whole_tropical_sign",
        "degree_policy": (
            "specific_exaltation_degrees_not_selected_or_evaluated"
        ),
        "exact_exaltation_degrees_evaluated": False,
        "debility_evaluated": False,
        "not_evaluated": [
            "detriment",
            "fall",
            "peregrine",
            "reception",
        ],
        "participant_scope": "classical_seven_planets_only",
        "objects": objects,
        "interpretation": None,
        "score": None,
    }


def _triplicity_profile(
    profile_id: str, bodies: list[dict], sect_is_day: bool | None
) -> dict:
    objects = []
    for body in bodies:
        item = _base_object(body)
        element = ELEMENTS[item["sign_index"] % 4]
        if profile_id == TRIPLICITY_IDS[0]:
            day, night, third = DOROTHEAN[element]
            day_rulers = [day, night, third]
            night_rulers = [night, day, third]
            structure = "ordered_day_night_third_participating"
        elif profile_id == TRIPLICITY_IDS[1]:
            ptolemy_rule = PTOLEMY_TEXTUAL[element]
            day_rulers = list(ptolemy_rule["day"])
            night_rulers = list(ptolemy_rule["night"])
            structure = "textual_principal_and_sect_corulership"
            item["principal_ruler"] = ptolemy_rule["principal"]
        else:
            lilly_rule = LILLY_COMPACT[element]
            day_rulers = list(lilly_rule["day"])
            night_rulers = list(lilly_rule["night"])
            structure = "compact_day_night_table"
        selected = (
            list(day_rulers) if sect_is_day is True
            else list(night_rulers) if sect_is_day is False
            else None
        )
        item.update(
            {
                "element": element,
                "day_rulers": day_rulers,
                "night_rulers": night_rulers,
                "selected_rulers": selected,
                "selected_available": selected is not None,
                "structure": structure,
            }
        )
        objects.append(item)
    return {
        "profile_id": profile_id,
        "technique": "triplicity",
        "source": TRIPLICITY_SOURCES[profile_id],
        "objects": objects,
    }


def compute_essential_dignities(
    *,
    bodies: list[dict],
    include_domicile_exaltation: bool,
    requested_explicitly: bool,
    domicile_exaltation_defaulted: bool,
    bounds_profile: str | None,
    decan_profile: str | None,
    triplicity_profile: str | None,
    include_triplicity_comparison: bool,
    center: str,
    zodiac: str,
    ecliptic_frame: str,
    nutation: bool,
    sect_is_day: bool | None,
    sect_basis: str,
    trace: Trace,
) -> dict | None:
    """Compute selected source-named components for the classical seven only."""

    requested = bool(
        include_domicile_exaltation
        or bounds_profile
        or decan_profile
        or triplicity_profile
        or include_triplicity_comparison
    )
    if not requested:
        return None
    selected = {
        "domicile_exaltation": (
            DOMICILE_EXALTATION_PROFILE_ID
            if include_domicile_exaltation
            else None
        ),
        "bounds": bounds_profile,
        "face_decan": decan_profile,
        "triplicity": triplicity_profile,
    }
    if center not in {"geocentric", "topocentric"}:
        trace.add(
            "具名古典尊貴元件",
            inputs={"center": center, "selected_profiles": selected},
            result={"execution_status": "not_applicable"},
            note=(
                "本批裁決未授權把古典尊貴表套入 heliocentric／"
                "barycentric longitude，故 fail closed。"
            ),
        )
        return {
            "method": "named_essential_dignity_components_v1",
            "method_status": METHOD_STATUS,
            "method_authority": METHOD_AUTHORITY,
            "requested": True,
            "requested_explicitly": requested_explicitly,
            "defaulted": domicile_exaltation_defaulted,
            "executed": False,
            "applicable": False,
            "available": False,
            "source": "selected_named_primary_text_profiles",
            "reason_code": "non_earth_center_dignity_basis_not_authorized",
            "coordinate_center": center,
            "zodiac_basis": "tropical_earth_center_only",
            "selected_profiles": selected,
            "research_comparison_profiles": [],
            "profile_results": {},
            "interpretation": None,
            "score": None,
        }
    if zodiac != "tropical":
        trace.add(
            "具名古典尊貴元件",
            inputs={"zodiac": zodiac, "selected_profiles": selected},
            result={"execution_status": "not_applicable"},
            note=(
                "本批裁決未授權把古典 tropical tables 靜默套入 sidereal "
                "longitude，故 fail closed。"
            ),
        )
        return {
            "method": "named_essential_dignity_components_v1",
            "method_status": METHOD_STATUS,
            "method_authority": METHOD_AUTHORITY,
            "requested": True,
            "requested_explicitly": requested_explicitly,
            "defaulted": domicile_exaltation_defaulted,
            "executed": False,
            "applicable": False,
            "available": False,
            "source": "selected_named_primary_text_profiles",
            "reason_code": "sidereal_dignity_basis_not_authorized",
            "zodiac_basis": "tropical_only",
            "selected_profiles": selected,
            "research_comparison_profiles": [],
            "profile_results": {},
            "interpretation": None,
            "score": None,
        }
    if ecliptic_frame != "of_date":
        trace.add(
            "具名古典尊貴元件",
            inputs={
                "ecliptic_frame": ecliptic_frame,
                "selected_profiles": selected,
            },
            result={"execution_status": "not_applicable"},
            note=(
                "Sebastian 2026-08-04 裁決正式 dignity 基準使用 of-date；"
                "J2000 只保留天文／研究座標，不查古典表。"
            ),
        )
        return {
            "method": "named_essential_dignity_components_v1",
            "method_status": METHOD_STATUS,
            "method_authority": METHOD_AUTHORITY,
            "requested": True,
            "requested_explicitly": requested_explicitly,
            "defaulted": domicile_exaltation_defaulted,
            "executed": False,
            "applicable": False,
            "available": False,
            "source": "selected_named_primary_text_profiles",
            "reason_code": "j2000_dignity_basis_not_authorized",
            "coordinate_center": center,
            "ecliptic_frame": ecliptic_frame,
            "nutation": nutation,
            "zodiac_basis": "tropical_of_date_nutation_on",
            "selected_profiles": selected,
            "research_comparison_profiles": [],
            "profile_results": {},
            "interpretation": None,
            "score": None,
        }
    if not nutation:
        trace.add(
            "具名古典尊貴元件",
            inputs={"nutation": nutation, "selected_profiles": selected},
            result={"execution_status": "not_applicable"},
            note=(
                "Sebastian 2026-08-04 裁決正式 dignity 基準使用 nutation-on；"
                "mean-of-date 比較留待日後另設 research-only profile。"
            ),
        )
        return {
            "method": "named_essential_dignity_components_v1",
            "method_status": METHOD_STATUS,
            "method_authority": METHOD_AUTHORITY,
            "requested": True,
            "requested_explicitly": requested_explicitly,
            "defaulted": domicile_exaltation_defaulted,
            "executed": False,
            "applicable": False,
            "available": False,
            "source": "selected_named_primary_text_profiles",
            "reason_code": "nutation_off_dignity_basis_not_authorized",
            "coordinate_center": center,
            "ecliptic_frame": ecliptic_frame,
            "nutation": nutation,
            "zodiac_basis": "tropical_of_date_nutation_on",
            "selected_profiles": selected,
            "research_comparison_profiles": [],
            "profile_results": {},
            "interpretation": None,
            "score": None,
        }

    usable_bodies = [body for body in bodies if body.get("longitude") is not None]
    results = {}
    if include_domicile_exaltation:
        results[DOMICILE_EXALTATION_PROFILE_ID] = (
            _domicile_exaltation_profile(usable_bodies)
        )
    if bounds_profile:
        results[bounds_profile] = _bounds_profile(
            bounds_profile, usable_bodies, sect_is_day
        )
    if decan_profile:
        results[decan_profile] = _decan_profile(decan_profile, usable_bodies)
    triplicity_ids = []
    if triplicity_profile:
        triplicity_ids.append(triplicity_profile)
    if include_triplicity_comparison:
        triplicity_ids.extend(TRIPLICITY_IDS)
    triplicity_ids = list(dict.fromkeys(triplicity_ids))
    for profile_id in triplicity_ids:
        results[profile_id] = _triplicity_profile(
            profile_id, usable_bodies, sect_is_day
        )

    trace.add(
        "具名古典尊貴元件",
        formula=(
            "tropical longitude -> whole-sign domicile/exaltation match and/or "
            "source-named half-open table; no scoring or interpretation"
        ),
        inputs={
            "selected_profiles": selected,
            "triplicity_research_comparison": include_triplicity_comparison,
            "sect_is_day": sect_is_day,
            "sect_basis": sect_basis,
        },
        result={"profile_ids": list(results)},
        note=(
            "Traditional-seven whole-sign domicile/exaltation is kept separate "
            "from exact exaltation-degree traditions. Dorothean, Ptolemy textual, "
            "Lilly compact, Egyptian, Chaldaean, Robbins and Lilly tables remain "
            "separate profiles."
        ),
    )
    return {
        "method": "named_essential_dignity_components_v1",
        "method_status": METHOD_STATUS,
        "method_authority": METHOD_AUTHORITY,
        "requested": True,
        "requested_explicitly": requested_explicitly,
        "defaulted": domicile_exaltation_defaulted,
        "executed": True,
        "applicable": True,
        "available": bool(results),
        "source": "selected_named_primary_text_profiles",
        "reason_code": None,
        "zodiac_basis": "tropical",
        "coordinate_center": center,
        "ecliptic_frame": ecliptic_frame,
        "nutation": nutation,
        "participant_scope": "classical_seven_planets_only",
        "sect": {
            "is_day": sect_is_day,
            "basis": sect_basis,
            "selected_ruler_available": sect_is_day is not None,
        },
        "selected_profiles": selected,
        "research_comparison_profiles": (
            list(TRIPLICITY_IDS) if include_triplicity_comparison else []
        ),
        "profile_results": results,
        "interpretation": None,
        "score": None,
    }
