"""API 請求資料驗證模型。

日期/時區的「語意有效性」（例如 2 月 30 日不存在、IANA 時區名稱是否真實存在）刻意在此
schema 層驗證，而非留給下游計算時才失敗：這樣所有輸入錯誤都統一走 Pydantic 的 422 回應，
格式一致；下游 core/time_utils.py 若仍拋出同類例外，代表的是未預期的內部錯誤，應該讓它
以 500 曝光，而不是被籠統地當成「使用者輸入錯誤」吞掉。
"""

import datetime as _dt
import unicodedata
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


# Dates whose final UTC minute contains a positive leap second in the bundled
# Swiss 2.10.03 table.  The API accepts 60 only at 23:59 on one of these UTC
# dates; future additions require an explicit source/table update rather than
# making every arbitrary `:60` look valid.
KNOWN_UTC_LEAP_SECOND_DATES = frozenset({
    (1972, 6, 30), (1972, 12, 31), (1973, 12, 31), (1974, 12, 31),
    (1975, 12, 31), (1976, 12, 31), (1977, 12, 31), (1978, 12, 31),
    (1979, 12, 31), (1981, 6, 30), (1982, 6, 30), (1983, 6, 30),
    (1985, 6, 30), (1987, 12, 31), (1989, 12, 31), (1990, 12, 31),
    (1992, 6, 30), (1993, 6, 30), (1994, 6, 30), (1995, 12, 31),
    (1997, 6, 30), (1998, 12, 31), (2005, 12, 31), (2008, 12, 31),
    (2012, 6, 30), (2015, 6, 30), (2016, 12, 31),
})

from .config import (
    AYANAMSA_OPTIONS,
    DEFAULT_ATMOSPHERE_TEMPERATURE_C,
    PRODUCT_YEAR_RANGE,
)


_PORTABLE_IANA_TIMEZONE_KEYS = frozenset(available_timezones())


# `place_label` is the only free-text field the request schema accepts, and it
# is copied verbatim into the Calculation Dossier and therefore into every
# export artifact.  A serializer must still do its own escaping, but these
# codepoints have no legitimate use in a place name and are rejected at the
# boundary so that every downstream consumer benefits, not only the CSV
# serializer:
#   - C0/C1 controls, including NUL, which is legal JSON but breaks many
#     consumers and truncates C string handling;
#   - CR/LF, which break the line structure of the TSV-shaped section copy
#     and of plain-text exports;
#   - bidi overrides and isolates, which allow a rendered label to disagree
#     with the stored label in the UI and in every export.
#
# The rule is expressed as Unicode general categories rather than as an
# enumerated codepoint list.  A blacklist of invisible codepoints does not
# converge: every Unicode release may add another format character, and the
# enumerated version of this rule already missed ZWSP (U+200B) and the line and
# paragraph separators (U+2028/U+2029), which some JSON and JavaScript
# consumers treat as line terminators.  Categories cover those, and cover
# additions this code has not seen.
#
#   Cc  C0/C1 controls, including NUL, CR and LF
#   Cf  format characters: bidi overrides and isolates, LRM/RLM, ZWSP,
#       BOM/ZWNBSP
#   Zl  U+2028 line separator
#   Zp  U+2029 paragraph separator
_FORBIDDEN_LABEL_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

# ZWNJ (U+200C) and ZWJ (U+200D) are category Cf but are deliberately exempt.
# They are orthographically required in Persian, Arabic and Indic scripts,
# where they select joining and non-joining letter forms; banning them would
# reject legitimate place names rather than hostile ones.  Unlike the bidi
# controls they cannot reorder a rendered label, so the display-spoofing
# argument that justifies this boundary does not reach them.
_ALLOWED_FORMAT_CODEPOINTS = frozenset("\u200c\u200d")


def _first_forbidden_label_codepoint(label: str) -> str | None:
    """Return the first codepoint a place label may not contain, if any."""

    for character in label:
        if character in _ALLOWED_FORMAT_CODEPOINTS:
            continue
        if unicodedata.category(character) in _FORBIDDEN_LABEL_CATEGORIES:
            return character
    return None


class StrictInputModel(BaseModel):
    """Reject misspelled/unknown fields and non-finite numbers instead of silently ignoring them."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def canonicalize_signed_zero(self):
        # JSON distinguishes the spelling -0.0 even though IEEE comparison
        # does not.  Keeping that sign leaked impossible civil times such as
        # ``12:00:-0.00`` and made semantically equal coordinates produce
        # different receipts.  Normalize only direct float fields at the
        # request boundary; bools and nonzero values remain untouched.
        for name in type(self).model_fields:
            value = getattr(self, name)
            if type(value) is float and value == 0.0:
                setattr(self, name, 0.0)
        return self


class DateTimeInput(StrictInputModel):
    # The public request boundary is owned by PRODUCT_YEAR_RANGE. Bundled Swiss
    # files reach farther into the past, but expanding the product boundary is
    # deferred until schema/UI/Dossier/event validation can move together.
    year: int = Field(ge=PRODUCT_YEAR_RANGE[0], le=PRODUCT_YEAR_RANGE[1])
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    second: float = Field(ge=0, le=60, default=0.0)

    @model_validator(mode="after")
    def check_valid_calendar_date(self):
        try:
            _dt.date(self.year, self.month, self.day)
        except ValueError as exc:
            raise ValueError(f"不是有效的日期：{self.year}-{self.month:02d}-{self.day:02d}（{exc}）")
        return self


def _conditional_rules(
    field: str, rules: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    return {
        "allOf": [
            {
                "if": {
                    "properties": {field: {"const": value}},
                    "required": [field],
                },
                "then": rule,
            }
            for value, rule in rules.items()
        ]
    }


_TIMEZONE_SCHEMA_RULES: dict[str, dict[str, Any]] = {
    "iana": {
        "properties": {"iana_name": {"minLength": 1, "type": "string"}},
        "required": ["iana_name"],
    },
    "fixed_offset": {
        "properties": {
            "utc_offset_hours": {"maximum": 14.0, "minimum": -14.0, "type": "number"},
            "fold": {"const": 0},
        },
        "required": ["utc_offset_hours"],
    },
}


class TimezoneInput(StrictInputModel):
    model_config = ConfigDict(
        json_schema_extra=_conditional_rules("mode", _TIMEZONE_SCHEMA_RULES)
    )
    mode: Literal["iana", "fixed_offset"]
    iana_name: Optional[str] = None
    utc_offset_hours: Optional[float] = Field(default=None, ge=-14, le=14)
    # PEP 495 fold：本地時間在 DST 轉換附近可能模糊（同一時鐘時間對應兩個 UTC），
    # 0=較早/夏令的那一次(預設)，1=較晚/標準時間的那一次。只在 mode='iana' 時有意義。
    fold: int = Field(default=0, ge=0, le=1)

    # Derive inert-field rejection from the same declaration that owns each
    # mode's meaningful fields.
    _MODE_FIELDS = {"iana": "iana_name", "fixed_offset": "utc_offset_hours"}

    @model_validator(mode="after")
    def check_fields(self):
        inert = [
            field
            for mode, field in self._MODE_FIELDS.items()
            if mode != self.mode and getattr(self, field) is not None
        ]
        if inert:
            raise ValueError(
                f"mode='{self.mode}' 時這些欄位沒有作用，不得提供："
                + "、".join(sorted(inert))
            )
        if self.mode == "iana":
            if not self.iana_name:
                raise ValueError("mode='iana' 時必須提供 iana_name，例如 'Asia/Taipei'")
            if self.iana_name not in _PORTABLE_IANA_TIMEZONE_KEYS:
                raise ValueError(
                    "無效或非標準大小寫的 IANA 時區名稱；"
                    "請使用如 'Asia/Taipei' 的標準名稱。"
                )
            try:
                ZoneInfo(self.iana_name)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                # ZoneInfo raises ValueError, not ZoneInfoNotFoundError, for an
                # absolute or `..`-containing key.  Catching only the latter
                # relied on Pydantic converting the escaped ValueError, which
                # makes the rejection accidental rather than local.  The message
                # deliberately omits the submitted value: input echo must not
                # depend on the hosted profile's response sanitizer.
                raise ValueError(
                    f"無效的 IANA 時區名稱（{type(exc).__name__}）；"
                    "請使用如 'Asia/Taipei' 的標準名稱。"
                )
        if self.mode == "fixed_offset":
            if self.utc_offset_hours is None:
                raise ValueError("mode='fixed_offset' 時必須提供 utc_offset_hours")
            # 固定偏移沒有 DST 轉換，fold 不會有任何作用。與其靜默忽略讓使用者誤以為
            # 自己已經選到了另一種 DST 解讀，不如明確拒絕。
            if self.fold != 0:
                raise ValueError(
                    "fold 只在 mode='iana' 時有意義（固定 UTC 偏移沒有日光節約時間轉換，"
                    "不會有模糊時刻）；請改用 mode='iana' 並指定 iana_name，或移除 fold。"
                )
        return self


_LOCATION_PRECISIONS = {
    "manual": ("user_supplied_coordinates", "unknown"),
    "geonames_cities500": ("place_representative_point",),
    "taiwan_moi_place_names": (
        "settlement_representative_point",
        "administrative_area_representative_point",
    ),
}


def _precision_schema(source: str) -> dict[str, Any]:
    values = _LOCATION_PRECISIONS[source]
    return {"const": values[0]} if len(values) == 1 else {"enum": list(values)}


_MANUAL_LOCATION_SCHEMA = {
    "properties": {
        "source_record_id": {"type": "null"},
        "location_precision": _precision_schema("manual"),
    }
}
_LOCATION_SCHEMA_RULES: dict[str, dict[str, Any]] = {
    "manual": _MANUAL_LOCATION_SCHEMA,
    "geonames_cities500": {
        "properties": {
            "place_label": {"type": "string", "minLength": 1},
            "source_record_id": {"type": "string", "minLength": 1},
            "location_precision": _precision_schema("geonames_cities500"),
        },
        "required": ["place_label", "source_record_id", "location_precision"],
    },
    "taiwan_moi_place_names": {
        "properties": {
            "place_label": {"type": "string", "minLength": 1},
            "source_record_id": {"type": "string", "minLength": 1},
            "location_precision": _precision_schema("taiwan_moi_place_names"),
        },
        "required": ["place_label", "source_record_id", "location_precision"],
    },
}
_LOCATION_SCHEMA = _conditional_rules("location_source", _LOCATION_SCHEMA_RULES)
_LOCATION_SCHEMA["allOf"].insert(0, {
    "if": {"not": {"required": ["location_source"]}},
    "then": _MANUAL_LOCATION_SCHEMA,
})


class LocationInput(StrictInputModel):
    model_config = ConfigDict(json_schema_extra=_LOCATION_SCHEMA)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    altitude_m: float = Field(default=0.0, ge=-500, le=10000)
    place_label: Optional[str] = Field(default=None, min_length=1, max_length=200)
    location_source: Literal[
        "manual",
        "geonames_cities500",
        "taiwan_moi_place_names",
    ] = "manual"
    source_record_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    location_precision: Literal[
        "user_supplied_coordinates",
        "place_representative_point",
        "settlement_representative_point",
        "administrative_area_representative_point",
        "unknown",
    ] = "user_supplied_coordinates"

    @model_validator(mode="after")
    def check_place_label_has_no_control_or_bidi_codepoints(self):
        if self.place_label is None:
            return self
        forbidden = _first_forbidden_label_codepoint(self.place_label)
        if forbidden is not None:
            # The offending codepoint is reported as U+XXXX rather than echoed,
            # so the message stays diagnosable without putting an invisible or
            # direction-flipping character into an error string that other
            # surfaces will render.
            raise ValueError(
                "place_label 不得包含控制字元、換行、行段分隔或雙向文字覆寫碼位"
                f"（U+{ord(forbidden):04X}）"
            )
        return self

    @model_validator(mode="after")
    def check_location_resolution_receipt(self):
        if self.location_precision not in _LOCATION_PRECISIONS[
            self.location_source
        ]:
            raise ValueError(
                f"location_source='{self.location_source}' 不接受此 location_precision"
            )
        if self.location_source == "manual":
            if self.source_record_id is not None:
                raise ValueError(
                    "location_source='manual' 不得提供 source_record_id"
                )
            return self

        if not self.place_label or not self.source_record_id:
            raise ValueError(
                "使用離線地名資料時必須同時提供 place_label 與 source_record_id"
            )
        return self


class AtmosphereInput(StrictInputModel):
    """Swiss refracted-altitude inputs; None pressure means Swiss estimates it from altitude."""

    pressure_hpa: Optional[float] = Field(default=None, gt=0, le=1100)
    temperature_c: float = Field(
        default=DEFAULT_ATMOSPHERE_TEMPERATURE_C, ge=-100, le=60
    )


class ComputationModeInput(StrictInputModel):
    """統一計算模式：一次設定，套用到本次請求的所有星體計算。"""

    center: Literal["geocentric", "topocentric", "heliocentric", "barycentric"] = "geocentric"
    zodiac: Literal["tropical", "sidereal"] = "tropical"
    ayanamsa: Literal["fagan_bradley", "hipparchos", "sassanian", "aldebaran_15_tau"] = "fagan_bradley"
    position_mode: Literal["apparent", "true"] = "apparent"
    ecliptic_frame: Literal["of_date", "j2000"] = "of_date"
    nutation: bool = True

    @model_validator(mode="after")
    def check_ayanamsa(self):
        if self.zodiac == "sidereal" and self.ayanamsa not in AYANAMSA_OPTIONS:
            raise ValueError(f"不支援的 ayanamsa: {self.ayanamsa}，可用: {list(AYANAMSA_OPTIONS.keys())}")
        return self


class OptionsInput(StrictInputModel):
    # 一次只計算一種宮位制。選擇何種宮位制屬方法決策，不宣稱四種系統具有相同權威地位。
    # `False` 是 CMP-C2 的真正無宮位模式：不得先算完再從 response 隱藏。
    # house_system 在此時只保留為未執行的 request 欄位，不得出現在 effective receipt。
    include_houses: bool = True
    house_system: Literal["B", "R", "W", "P"] = "W"
    include_fixed_stars: bool = False
    # 產品核心是可驗證的天文數值；需要古典方法選擇的判斷一律 opt-in，
    # 不因 Swiss Ephemeris 能提供原料就假裝方法已獲採用。
    include_lots: bool = False
    include_antiscia: bool = False
    # MTH-Q-008 乙：反照點的對象範圍為「七政（必要）＋交點（可選）」，
    # 恆星與軸點不計算。此開關即該裁決的「可選」。
    antiscia_include_nodes: bool = False
    include_void_of_course: bool = False
    include_declination_aspects: bool = False
    # MTH-Q-004 的研究性分類不變；1°只是既有產品預設。使用者可在 0–3°
    # 間明示覆寫，response 必須保存 default 與 effective 值。
    declination_aspect_orb_degrees: float = Field(gt=0.0, le=3.0, default=1.0)
    include_outer_planets: bool = False
    include_chiron: bool = False
    # Sebastian 2026-08-04 CMP-A6 方案三：平均遠地點、Swiss 的
    # interpolated/natural 遠地點，以及獨立計算的 natural 近地點。
    # 三者是現代／研究附加點，不因開啟而自動參與相位或古典方法。
    include_lilith_priapus: bool = False
    # CMP-A10：整體計算模式維持 geocentric，但月亮另以出生地站心位置取代
    # bodies 中的 effective Moon；response 同時保存地心 reference。此選項不等於
    # 「比較準」，而是明示的 mixed-origin coordinate policy。
    moon_position_profile: Literal[
        "global_computation_mode", "moon_only_topocentric_v1"
    ] = "global_computation_mode"
    # 南交點不是另一個 Swiss body：它是所選北交點方向的精確對蹠點。
    # 預設保留既有兩個北交點；明示要求時才把兩個導出南交點加入 nodes。
    include_south_nodes: bool = False
    include_lunar_phases: bool = False
    include_eclipses: bool = False
    include_rise_set_transits: bool = False

    # --- 黃道相位 ---
    #
    # **MTH-Q-016 已裁決（Sebastian 2026-08-03）：預設開啟，但可 opt-out。**
    #
    # 預設值是裁決事項，不是實作事項：待審閱的方法項目不得默默成為預設的
    # 使用者可見計算。此項由 Sebastian 正式裁決為預設開啟。
    #
    # 這是本檔唯一預設開啟的判斷類選項。它與其餘項目的差別在於：預設輸出的
    # 整宮配置與角距離都是算術，未選 orb_profile 時 `in_orb` 一律為 null，
    # 故預設狀態不含任何容許度承諾。方法收據仍為 provisional，Dossier 也仍會
    # 發出 `provisional_method_result` 通知——預設開啟不等於方法已獲採用。
    include_aspects: bool = True
    # 不設預設容許度表。歷史來源彼此不一致（見 core/aspects.py docstring），
    # 由本程式代選一張等於代替 Sebastian 做出方法裁決。
    aspect_orb_profile: Optional[
        Literal["abu_mashar_lineage_v1", "lilly_1647_experience_v1"]
    ] = None
    # 逐度相位集合與整宮 doctrine 分開：不論選哪個逐度集合，整宮層仍只使用
    # 托勒密五相。現代小相位 profile 是使用者選項，不宣稱古典權威。
    aspect_set_profile: Literal[
        "ptolemaic_major_v1",
        "modern_common_minor_v1",
        "modern_quintile_family_v1",
        "modern_minor_combined_v1",
    ] = "ptolemaic_major_v1"
    # 兩種明示覆寫互斥：具名七政 orb 表可按百分比縮放；或直接指定所有逐度
    # 配對的固定門檻。後者是 user override，不繼承歷史來源宣稱。
    aspect_orb_scale_percent: Optional[float] = Field(
        gt=0.0, le=300.0, default=None
    )
    aspect_fixed_orb_degrees: Optional[float] = Field(
        gt=0.0, le=30.0, default=None
    )
    # Partile 慣例（MTH-Q-012 / E-012 裁決：做成 profile）。三種慣例並存，
    # 其中兩種都出自 Lilly 本人且互相矛盾。與 orb 表不同，這一項有預設值：
    # 「同一整數度」是最通行者。
    partile_profile: Literal[
        "same_degree_number_v1",
        "within_one_degree_v1",
        "lilly_1677_three_degrees_v1",
    ] = "same_degree_number_v1"
    # 成相時刻需要在時間軸上反覆查星曆，比其餘欄位昂貴一個量級，故預設關閉。
    include_aspect_perfection: bool = False
    # 三王星與阿拉伯點是否參與相位，直接跟隨其本身的計算開關，避免出現
    # 「要求三王星入相位但沒算三王星」這種自相矛盾的請求。交點永遠已算出，
    # 故單獨給一個開關。
    aspect_include_nodes: bool = False
    # ASC／MC 只加入逐度層；不加入整宮 doctrine、不互相配對，也不推測日周運動
    # 的 applying。若未給角點門檻，仍輸出幾何但 in_orb 為 null。
    aspect_include_angles: bool = False
    aspect_angle_orb_degrees: Optional[float] = Field(
        gt=0.0, le=30.0, default=None
    )

    # --- 非古典角點 ---
    # Vertex 等五個角點由 swe.houses_ex 附帶回傳，過去被丟棄。開啟後另闢
    # astronomical_data.extra_angles 輸出，`angles` 的鍵集合不變。
    include_extra_angles: bool = False
    # Anti-Vertex 是 Vertex 的黃道對蹠點，可單獨要求，不必同時輸出其餘四個
    # 現代技術角點。
    include_anti_vertex: bool = False

    # 純後端 body preset。它只約束「星體目錄」的現代附加物件；交點、Lots 與
    # 額外角點是 points/method outputs，刻意不被此 preset 靜默關閉。
    body_selection_preset: Literal["custom", "classical_seven_v1"] = "custom"

    # --- 具名古典尊貴元件；只回傳規則／表格結果，不打分、不解讀 ---
    # 傳統七政的 domicile/exaltation 星座層級是基礎 profile，預設計算但可明示
    # 關閉。精確擢升度數、失勢／陷落、互容與總分均不在此開關範圍。
    include_domicile_exaltation: bool = False
    bounds_profile: Optional[
        Literal[
            "egyptian_bounds_robbins_1940_v1",
            "chaldaean_bounds_ptolemy_i_21_v1",
            "ptolemy_bounds_robbins_1940_v1",
            "lilly_received_bounds_1647_v1",
        ]
    ] = None
    decan_profile: Optional[
        Literal[
            "chaldean_planetary_faces_firmicus_ii_4_v1",
            "manilius_sign_decans_astronomica_iv_v1",
        ]
    ] = None
    triplicity_profile: Optional[
        Literal[
            "dorothean_triplicity_three_rulers_v1",
            "ptolemy_triplicity_textual_corulership_v1",
            "lilly_triplicity_compact_1647_v1",
        ]
    ] = None
    triplicity_include_research_comparison: bool = False

    @model_validator(mode="after")
    def check_body_selection_preset(self):
        if self.body_selection_preset != "classical_seven_v1":
            return self
        incompatible = [
            name
            for name in (
                "include_outer_planets",
                "include_fixed_stars",
                "include_chiron",
                "include_lilith_priapus",
            )
            if getattr(self, name)
        ]
        if incompatible:
            raise PydanticCustomError(
                "body_selection_conflict",
                "classical_seven_v1 conflicts with selected non-classical bodies",
            )
        return self

    @model_validator(mode="after")
    def check_aspect_orb_configuration(self):
        if (
            self.aspect_orb_scale_percent is not None
            and self.aspect_orb_profile is None
        ):
            raise PydanticCustomError(
                "aspect_orb_scale_requires_profile",
                "aspect_orb_scale_percent requires aspect_orb_profile",
            )
        if (
            self.aspect_fixed_orb_degrees is not None
            and self.aspect_orb_profile is not None
        ):
            raise PydanticCustomError(
                "aspect_orb_sources_conflict",
                "aspect_fixed_orb_degrees and aspect_orb_profile are mutually exclusive",
            )
        if (
            self.aspect_angle_orb_degrees is not None
            and not self.aspect_include_angles
        ):
            raise PydanticCustomError(
                "aspect_angle_orb_requires_angles",
                "aspect_angle_orb_degrees requires aspect_include_angles=true",
            )
        return self


class ChartRequest(StrictInputModel):
    birth_time_precision: Literal[
        "exact", "approximate_hour", "date_only"
    ] = "exact"
    datetime: DateTimeInput
    timezone: TimezoneInput
    location: LocationInput
    atmosphere: AtmosphereInput = Field(default_factory=AtmosphereInput)
    computation_mode: ComputationModeInput = Field(default_factory=ComputationModeInput)
    options: OptionsInput = Field(default_factory=OptionsInput)

    @model_validator(mode="after")
    def check_birth_time_precision(self):
        if self.datetime.second == 60:
            utc_zone = (
                self.timezone.mode == "iana"
                and self.timezone.iana_name == "UTC"
            ) or (
                self.timezone.mode == "fixed_offset"
                and self.timezone.utc_offset_hours == 0
            )
            if (
                self.birth_time_precision != "exact"
                or not utc_zone
                or self.datetime.hour != 23
                or self.datetime.minute != 59
            ):
                raise ValueError(
                    "second=60只接受已知UTC閏秒：exact、UTC、23:59:60"
                )
            if (
                self.datetime.year,
                self.datetime.month,
                self.datetime.day,
            ) not in KNOWN_UTC_LEAP_SECOND_DATES:
                raise ValueError("指定日期不是bundled Swiss已知的UTC閏秒")
        if (
            self.options.moon_position_profile
            == "moon_only_topocentric_v1"
            and self.computation_mode.center != "geocentric"
        ):
            raise PydanticCustomError(
                "moon_profile_center_conflict",
                "moon_only_topocentric_v1 requires geocentric global center",
            )
        if self.birth_time_precision == "approximate_hour" and (
            self.datetime.minute != 0 or self.datetime.second != 0
        ):
            raise PydanticCustomError(
                "approximate_hour_requires_zero_subhour",
                "approximate_hour requires zero minute and second",
            )
        if self.birth_time_precision == "date_only":
            if (
                self.datetime.hour != 0
                or self.datetime.minute != 0
                or self.datetime.second != 0
            ):
                raise PydanticCustomError(
                    "date_only_requires_zero_time",
                    "date_only requires zero hour, minute and second",
                )
            # Unknown time 本身即是不請求 houses。正規化在 request boundary 完成，
            # 讓 requested_options、Dossier input receipt 與實際執行一致；不保留一個
            # `include_houses=true` 再在下游偷偷忽略。
            if self.options.include_houses:
                self.options = self.options.model_copy(
                    update={"include_houses": False}
                )
        return self


class PlaceSearchRequest(StrictInputModel):
    query: str = Field(min_length=1, max_length=100)
    country_code: Optional[str] = Field(
        default=None,
        pattern=r"^[A-Z]{2}$",
    )
    limit: int = Field(default=10, ge=1, le=20)


class HostedValidationIssue(StrictInputModel):
    """Privacy-minimized validation item returned by the hosted profile."""

    type: str
    loc: list[str | int]


class HostedValidationResponse(StrictInputModel):
    """Hosted 422 body; deliberately omits Pydantic's input and message."""

    detail: list[HostedValidationIssue]


class HostedBoundaryErrorDetail(StrictInputModel):
    """Closed error code emitted by the hosted ASGI request boundary."""

    code: str
    message: str | None = None


class HostedBoundaryErrorResponse(StrictInputModel):
    """Malformed-parser text or a closed request-boundary error."""

    detail: str | HostedBoundaryErrorDetail
