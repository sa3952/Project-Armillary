"""API 請求資料驗證模型。

日期/時區的「語意有效性」（例如 2 月 30 日不存在、IANA 時區名稱是否真實存在）刻意在此
schema 層驗證，而非留給下游計算時才失敗：這樣所有輸入錯誤都統一走 Pydantic 的 422 回應，
格式一致；下游 core/time_utils.py 若仍拋出同類例外，代表的是未預期的內部錯誤，應該讓它
以 500 曝光，而不是被籠統地當成「使用者輸入錯誤」吞掉。
"""

import datetime as _dt
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import AYANAMSA_OPTIONS


class StrictInputModel(BaseModel):
    """Reject misspelled/unknown fields and non-finite numbers instead of silently ignoring them."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class DateTimeInput(StrictInputModel):
    # 上限對齊 backend/ephe 實際內附的星曆檔涵蓋範圍(1800-2399)，超出此範圍會在計算時失敗
    year: int = Field(ge=1900, le=2399)
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
    mode: Literal["iana", "fixed_offset"]
    iana_name: Optional[str] = None
    utc_offset_hours: Optional[float] = Field(default=None, ge=-14, le=14)
    # PEP 495 fold：本地時間在 DST 轉換附近可能模糊（同一時鐘時間對應兩個 UTC），
    # 0=較早/夏令的那一次(預設)，1=較晚/標準時間的那一次。只在 mode='iana' 時有意義。
    fold: Literal[0, 1] = 0

    @model_validator(mode="after")
    def check_fields(self):
        if self.mode == "iana":
            if not self.iana_name:
                raise ValueError("mode='iana' 時必須提供 iana_name，例如 'Asia/Taipei'")
            try:
                ZoneInfo(self.iana_name)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"無效的 IANA 時區名稱 '{self.iana_name}'：{exc}")
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
    include_fixed_stars: bool = True
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
    datetime: DateTimeInput
    timezone: TimezoneInput
    location: LocationInput
    atmosphere: AtmosphereInput = Field(default_factory=AtmosphereInput)
    computation_mode: ComputationModeInput = Field(default_factory=ComputationModeInput)
    options: OptionsInput = Field(default_factory=OptionsInput)
