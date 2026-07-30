"""API 請求資料驗證模型。

日期/時區的「語意有效性」（例如 2 月 30 日不存在、IANA 時區名稱是否真實存在）刻意在此
schema 層驗證，而非留給下游計算時才失敗：這樣所有輸入錯誤都統一走 Pydantic 的 422 回應，
格式一致；下游 core/time_utils.py 若仍拋出同類例外，代表的是未預期的內部錯誤，應該讓它
以 500 曝光，而不是被籠統地當成「使用者輸入錯誤」吞掉。
"""

import datetime as _dt
import re
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import AYANAMSA_OPTIONS, PRODUCT_YEAR_RANGE


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
_FORBIDDEN_LABEL_CODEPOINTS = re.compile(
    "["
    "\u0000-\u001f"      # C0 controls, including NUL, CR and LF
    "\u007f-\u009f"      # DEL and C1 controls
    "\u200e\u200f"       # LRM / RLM
    "\u202a-\u202e"      # bidi embedding and override
    "\u2066-\u2069"      # bidi isolates
    "\ufeff"             # zero-width no-break space / BOM
    "]"
)


class StrictInputModel(BaseModel):
    """Reject misspelled/unknown fields and non-finite numbers instead of silently ignoring them."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class DateTimeInput(StrictInputModel):
    # The public request boundary is owned by PRODUCT_YEAR_RANGE. Bundled Swiss
    # files reach farther into the past, but expanding the product boundary is
    # deferred until schema/UI/Dossier/event validation can move together.
    year: int = Field(ge=PRODUCT_YEAR_RANGE[0], le=PRODUCT_YEAR_RANGE[1])
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    second: float = Field(ge=0, lt=60, default=0.0)

    @model_validator(mode="after")
    def check_valid_calendar_date(self):
        try:
            _dt.date(self.year, self.month, self.day)
        except ValueError as exc:
            raise ValueError(f"不是有效的日期：{self.year}-{self.month:02d}-{self.day:02d}（{exc}）")
        return self


class TimezoneInput(StrictInputModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"mode": {"const": "iana"}},
                        "required": ["mode"],
                    },
                    "then": {
                        "properties": {
                            "iana_name": {
                                "minLength": 1,
                                "type": "string",
                            }
                        },
                        "required": ["iana_name"],
                    },
                },
                {
                    "if": {
                        "properties": {
                            "mode": {"const": "fixed_offset"}
                        },
                        "required": ["mode"],
                    },
                    "then": {
                        "properties": {
                            "utc_offset_hours": {
                                "maximum": 14.0,
                                "minimum": -14.0,
                                "type": "number",
                            }
                        },
                        "required": ["utc_offset_hours"],
                    },
                },
            ]
        }
    )
    mode: Literal["iana", "fixed_offset"]
    iana_name: Optional[str] = None
    utc_offset_hours: Optional[float] = Field(default=None, ge=-14, le=14)
    # PEP 495 fold：本地時間在 DST 轉換附近可能模糊（同一時鐘時間對應兩個 UTC），
    # 0=較早/夏令的那一次(預設)，1=較晚/標準時間的那一次。只在 mode='iana' 時有意義。
    fold: int = Field(default=0, ge=0, le=1)

    @model_validator(mode="after")
    def check_fields(self):
        if self.mode == "iana":
            if not self.iana_name:
                raise ValueError("mode='iana' 時必須提供 iana_name，例如 'Asia/Taipei'")
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


class LocationInput(StrictInputModel):
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
        if self.place_label is not None and _FORBIDDEN_LABEL_CODEPOINTS.search(
            self.place_label
        ):
            raise ValueError(
                "place_label 不得包含控制字元、換行或雙向文字覆寫碼位"
            )
        return self

    @model_validator(mode="after")
    def check_location_resolution_receipt(self):
        if self.location_source == "manual":
            if self.source_record_id is not None:
                raise ValueError(
                    "location_source='manual' 不得提供 source_record_id"
                )
            if self.location_precision not in {
                "user_supplied_coordinates",
                "unknown",
            }:
                raise ValueError(
                    "手動座標不得冒充資料集代表點 precision"
                )
            return self

        if not self.place_label or not self.source_record_id:
            raise ValueError(
                "使用離線地名資料時必須同時提供 place_label 與 source_record_id"
            )
        if self.location_precision == "user_supplied_coordinates":
            raise ValueError(
                "資料集解析結果不得標為 user_supplied_coordinates；"
                "請提供代表點的 location_precision"
            )
        if (
            self.location_source == "geonames_cities500"
            and self.location_precision != "place_representative_point"
        ):
            raise ValueError(
                "GeoNames cities500 必須標為 place_representative_point"
            )
        if (
            self.location_source == "taiwan_moi_place_names"
            and self.location_precision
            not in {
                "settlement_representative_point",
                "administrative_area_representative_point",
            }
        ):
            raise ValueError(
                "台灣內政部地名必須標為聚落或行政區代表點"
            )
        return self


class AtmosphereInput(StrictInputModel):
    """Swiss refracted-altitude inputs; None pressure means Swiss estimates it from altitude."""

    pressure_hpa: Optional[float] = Field(default=None, gt=0, le=1100)
    temperature_c: float = Field(default=0.0, ge=-100, le=60)


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
    house_system: Literal["B", "R", "W", "P"] = "W"
    include_fixed_stars: bool = False
    # 產品核心是可驗證的天文數值；需要古典方法選擇的判斷一律 opt-in，
    # 不因 Swiss Ephemeris 能提供原料就假裝方法已獲採用。
    include_lots: bool = False
    include_antiscia: bool = False
    include_void_of_course: bool = False
    include_declination_aspects: bool = False
    include_outer_planets: bool = False
    include_lunar_phases: bool = False
    include_eclipses: bool = False
    include_rise_set_transits: bool = False


class ChartRequest(StrictInputModel):
    birth_time_precision: Literal["exact", "approximate_hour"] = "exact"
    datetime: DateTimeInput
    timezone: TimezoneInput
    location: LocationInput
    atmosphere: AtmosphereInput = Field(default_factory=AtmosphereInput)
    computation_mode: ComputationModeInput = Field(default_factory=ComputationModeInput)
    options: OptionsInput = Field(default_factory=OptionsInput)

    @model_validator(mode="after")
    def check_birth_time_precision(self):
        if self.birth_time_precision == "approximate_hour" and (
            self.datetime.minute != 0 or self.datetime.second != 0
        ):
            raise ValueError(
                "approximate_hour 只接受已知民用小時；minute 與 second 必須為 0，"
                "系統會自行建立一小時敏感度取樣。"
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
