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
        """回傳**本次計算實際套用**的 ayanamsa，供收據、houses.py 與 antiscia.py 使用。

        這裡回傳的必須是實際套用值，不是 `get_ayanamsa_ut()`：後者是相對**平**
        春分點定義的傳統數值；`calc_ut(FLG_SIDEREAL)` 在章動開啟時輸出的恆星黃經卻是
        相對**真**春分點。兩者相差恰為當下的黃經章動（實測 ±17.4 角秒內）。

        舊 docstring 把這個差稱為章動慣例殘差、「遠小於任何占星技法的容許誤差」。
        就「換算回 tropical 核對」而言那句話成立，但它沒有涵蓋這個函式的兩個實際用途：

        1. **收據。** `astronomical_data.time.ayanamsa` 宣稱的是本次從星體黃經扣掉的
           量。第三方拿它複算會差最多 17.4 角秒——這是可追溯性宣稱本身出錯，
           不是精度問題，容許誤差的說法不適用。
        2. **非整宮 sidereal 宮頭。** `houses.py` 以「tropical 宮頭 − 本值」求 sidereal
           宮頭，而星體走 Swiss 原生 `FLG_SIDEREAL`。兩者相差同一個章動量，宮頭與星體
           因此落在不同框架。落宮是硬邊界分類，±13 角秒就是一個約一秒出生時刻的翻宮窗；
           「遠小於容許誤差」對分類邊界不成立。整宮制不受影響，它本來就走原生路徑。

        `get_ayanamsa_ex_ut(jd, base_flags)` 回傳的正是同一組旗標下實際套用的值，實測與
        「同天體同時刻的 tropical 減 sidereal 黃經」差 0.000000 角秒。改用它之後，
        「tropical 宮頭 − ayanamsa」與原生 sidereal 宮頭在 P/R/B/A/C 各制皆完全相等，
        三個下游後果（收據、宮頭、antiscia 的 2×ayanamsa）一次消除。

        **這改變既有 sidereal 使用者的數值輸出**（≤17.4 角秒）並使 parity baseline 失效。
        另一條路是保留平春分點值、改讓 houses 走原生 `FLG_SIDEREAL`，但那只修宮頭，
        收據與 antiscia 仍不實。兩條路都不創設任何占星慣例；此處選擇「回報並套用同一個
        實際值」，Sebastian 可另行裁決改採另一條。
        """
        if self.mode.zodiac != "sidereal":
            return None
        # 第二個回傳值才是 ayanamsa；第一個是回傳碼。
        return swe.get_ayanamsa_ex_ut(jd_ut, self.base_flags)[1]

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
