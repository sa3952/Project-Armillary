"""統一「計算模式」設定：一次設定、套用到所有星體計算。

包含：計算中心 (geo/topo/helio/bary)、黃道系統 (tropical/sidereal + ayanamsa)、
視位置/真位置、當日黃道/J2000、章動開關。這裡只負責組出 swisseph flags 與判斷
「地平座標/日夜盤等仰賴地平系統的資料在目前模式下是否仍有意義」，實際計算呼叫仍在
各自的 core 模組內進行。
"""

import swisseph as swe

from ..config import AYANAMSA_OPTIONS
from .trace import Trace

CENTER_FLAGS = {
    "geocentric": 0,
    "topocentric": swe.FLG_TOPOCTR,
    "heliocentric": swe.FLG_HELCTR,
    "barycentric": swe.FLG_BARYCTR,
}


class ComputationContext:
    def __init__(self, mode, location):
        self.mode = mode
        self.location = location
        self.base_flags = swe.FLG_SWIEPH | swe.FLG_SPEED | CENTER_FLAGS[mode.center]

        if mode.zodiac == "sidereal":
            self.base_flags |= swe.FLG_SIDEREAL
        if mode.position_mode == "true":
            self.base_flags |= swe.FLG_TRUEPOS
        if mode.ecliptic_frame == "j2000":
            self.base_flags |= swe.FLG_J2000
        if not mode.nutation:
            self.base_flags |= swe.FLG_NONUT

        if mode.center == "topocentric":
            swe.set_topo(location.longitude, location.latitude, location.altitude_m)
        if mode.zodiac == "sidereal":
            swe.set_sid_mode(AYANAMSA_OPTIONS[mode.ayanamsa], 0, 0)

    @property
    def horizon_meaningful(self) -> bool:
        """地平座標(方位角/高度)、日夜盤、月空亡、阿拉伯點等仰賴「觀測者站在地表看天空」這個前提，
        在 heliocentric/barycentric 模式下沒有物理意義，故回傳 False 時應輸出 null 並註明原因。"""
        return self.mode.center in ("geocentric", "topocentric")

    @property
    def horizontal_source_flags(self) -> int:
        """Return physical-sky flags for the ecliptic input consumed by ``swe.azalt``.

        Sidereal zodiac, J2000, and mean (no-nutation) coordinates are output
        reference-frame choices. Feeding any of them to ``ECL2HOR`` would
        incorrectly rotate the observer's physical sky, because Swiss expects
        true tropical ecliptic coordinates of date at that boundary.
        Topocentric center and true/apparent position remain intentional
        physical choices.
        """
        return (
            self.base_flags
            & ~swe.FLG_SIDEREAL
            & ~swe.FLG_J2000
            & ~swe.FLG_NONUT
        )

    @property
    def equatorial_source_flags(self) -> int:
        """Return flags for RA/Dec without applying a sidereal-zodiac offset.

        Sidereal/tropical is a convention for ecliptic longitude, not a second
        definition of right ascension. J2000 and no-nutation remain available
        because they explicitly select the equatorial reference frame.
        """
        return self.base_flags & ~swe.FLG_SIDEREAL

    def ayanamsa_value(self, jd_ut: float):
        """回傳 swe.get_ayanamsa_ut() 的值，用於顯示與 houses.py/antiscia.py 的 sidereal 換算。

        已知的精度細節（實測+推導確認，非 bug）：get_ayanamsa_ut() 是相對「平均(mean)」
        春分點定義的傳統 ayanamsa 數值；而 calc_ut() 搭配 FLG_SIDEREAL 算出的恆星黃經，
        預設（章動=on）是相對「真(true/apparent)」春分點。兩者相差量精確等於當下的黃經
        章動值（例：2000-01-01 04:00 UT 實測相差 13.93 角秒，與當時 nutation_longitude
        完全吻合，見 tests/backend/test_chart_api.py 的 test_sidereal_antiscia_*）。
        換算回 tropical 座標核對時會有這個量級（通常數角秒到二十角秒內）的殘差，屬於
        章動慣例差異，不是換算公式錯誤，實務上遠小於任何占星技法會用到的容許誤差。
        """
        if self.mode.zodiac != "sidereal":
            return None
        return swe.get_ayanamsa_ut(jd_ut)

    def describe(self, trace: Trace, jd_ut: float):
        ayan = self.ayanamsa_value(jd_ut)
        trace.add(
            "計算模式設定",
            formula="套用於本次所有星體計算的統一旗標",
            inputs={
                "計算中心": self.mode.center,
                "黃道系統": self.mode.zodiac,
                "ayanamsa": self.mode.ayanamsa if self.mode.zodiac == "sidereal" else "-",
                "位置": self.mode.position_mode,
                "黃道參考框架": self.mode.ecliptic_frame,
                "章動": "on" if self.mode.nutation else "off",
            },
            result={
                "requested_flags": self.base_flags,
                "ayanamsa數值(度)": ayan,
            },
        )
