"""宮位計算：單一宮位制的 12 宮宮頭與古典角點 (ASC/DSC/MC/IC/ARMC)。

Sidereal Whole Sign 必須讓 Swiss 直接以 ``FLG_SIDEREAL`` 建立恆星黃道
30°整數宮界；先算 tropical Whole Sign 再減 ayanamsa 會得到落在任意度數的錯誤宮頭。
其他 sidereal 宮位制保留既有「tropical 宮頭 − ayanamsa」契約（僅套用在
黃道經度類的值上；ARMC 是恆星時性質的角度，不做 ayanamsa 平移）。

Vertex、Equatorial Ascendant、Co-Ascendant(Koch/Munkasey)、Polar Ascendant 是
swe_houses_ex 附帶回傳的現代/技術性角點，非古典占星角點，因此不進入主要輸出，
只寫進 calculation_trace 供有興趣者核對。

宮位／ASC／MC／地平系統本質上是「觀測者站在地表看天空」定義出來的概念，恆為地心
（或站心）視角，不受 computation_mode.center 影響——即使使用者把星體位置切成
heliocentric/barycentric，這裡算出來的宮位仍然是地球上的觀測者看到的宮位，這是
刻意的設計而非未定義的語意混合，但容易讓人誤解成「這個程式的座標系統前後不一致」，
所以在 heliocentric/barycentric 模式下會額外寫一筆 trace 註明。
"""

import swisseph as swe

from ..config import HOUSE_SYSTEMS
from .trace import Trace


class HouseSystemUnavailableError(Exception):
    """The selected house system is not mathematically available at this latitude."""

    code = "house_system_unavailable"

    def __init__(
        self,
        *,
        code: str,
        name: str,
        latitude: float,
        reason: str | None = None,
    ):
        self.house_system = code
        self.latitude = latitude
        if reason:
            message = (
                f"{name} ({code}) 在緯度 {latitude}° 產生退化宮頭（{reason}）；"
                "請改用此緯度可定義的宮位制。"
            )
        else:
            message = (
                f"{name} ({code}) 在緯度 {latitude}° 無法計算；"
                "請改用此緯度可定義的宮位制。"
            )
        super().__init__(message)


def _has_duplicate_cusps(cusps: tuple[float, ...], tolerance: float = 1e-7) -> bool:
    normalized = [cusp % 360.0 for cusp in cusps]
    for index, left in enumerate(normalized):
        for right in normalized[index + 1:]:
            difference = abs((left - right + 180.0) % 360.0 - 180.0)
            if difference < tolerance:
                return True
    return False


def compute_houses(house_system_code: str, jd_ut: float, location, ctx, trace: Trace) -> dict:
    ayanamsa = ctx.ayanamsa_value(jd_ut) if ctx.mode.zodiac == "sidereal" else None
    swiss_sidereal_whole_sign = (
        ctx.mode.zodiac == "sidereal" and house_system_code == "W"
    )

    def shift(lon):
        if swiss_sidereal_whole_sign:
            return lon % 360.0
        offset = ayanamsa or 0.0
        return (lon - offset) % 360.0

    code = house_system_code
    name = HOUSE_SYSTEMS.get(code, code)
    if abs(location.latitude) >= 90.0:
        raise HouseSystemUnavailableError(
            code=code,
            name=name,
            latitude=location.latitude,
            reason="地理極點的上升點在數學上未定義",
        )
    try:
        cusps, ascmc = swe.houses_ex(
            jd_ut,
            location.latitude,
            location.longitude,
            code.encode(),
            swe.FLG_SIDEREAL if swiss_sidereal_whole_sign else 0,
        )
    except swe.Error as exc:
        raise HouseSystemUnavailableError(
            code=code,
            name=name,
            latitude=location.latitude,
        ) from exc
    if code != "W" and _has_duplicate_cusps(cusps):
        raise HouseSystemUnavailableError(
            code=code,
            name=name,
            latitude=location.latitude,
            reason="十二宮宮頭並非十二個相異方向",
        )

    cusps_shifted = [shift(c) for c in cusps]
    asc = shift(ascmc[0])
    mc = shift(ascmc[1])
    armc = ascmc[2]  # 恆星時性質，不做 ayanamsa 平移
    vertex = shift(ascmc[3])
    equatorial_ascendant = shift(ascmc[4])
    co_ascendant_koch = shift(ascmc[5])
    co_ascendant_munkasey = shift(ascmc[6])
    polar_ascendant = shift(ascmc[7])
    desc = (asc + 180.0) % 360.0
    ic = (mc + 180.0) % 360.0

    if ctx.mode.center in ("heliocentric", "barycentric"):
        trace.add(
            "宮位座標框架提醒",
            note=f"計算模式的計算中心為 {ctx.mode.center}，但宮位/ASC/MC/地平系統定義上恆為觀測者"
                 "站在地表看天空的視角，不受此設定影響——本結果仍是地心視角的宮位，"
                 "並非把日心/質心行星位置硬套進地心宮位系統造成的不一致。",
        )

    frame_notes = []
    if ctx.mode.ecliptic_frame == "j2000":
        frame_notes.append("星體黃經採 J2000 參考框架")
    if not ctx.mode.nutation:
        frame_notes.append("星體黃經採不含章動的平均框架")
    if frame_notes:
        trace.add(
            "宮位與星體參考框架提醒",
            note="；".join(frame_notes)
            + "；宮頭／ASC／MC 仍由 swe.houses_ex 以當日真回歸框架計算。"
            "本程式不據此產生星體落宮判斷，兩組數值不可直接混合作落宮推論。",
        )

    trace.add(
        f"宮位制：{name} ({code})",
        formula=(
            "swe.houses_ex(JD_UT, 地理緯度, 地理經度, 宮位制代碼"
            + (
                ", FLG_SIDEREAL)"
                if swiss_sidereal_whole_sign
                else ")"
            )
            + (
                "；其他 sidereal 宮位制：黃道經度類角點減去 ayanamsa"
                if ayanamsa is not None
                and not swiss_sidereal_whole_sign
                else ""
            )
        ),
        inputs={"JD(UT)": jd_ut, "緯度": location.latitude, "經度": location.longitude,
                **({"ayanamsa": ayanamsa} if ayanamsa is not None else {}),
                "houses_ex_flags": (
                    swe.FLG_SIDEREAL
                    if swiss_sidereal_whole_sign
                    else 0
                )},
        result={
            "ASC": asc, "MC": mc, "ARMC": armc, "DESC": desc, "IC": ic,
            "12宮宮頭": cusps_shifted,
        },
        note="以下為 swisseph 附帶回傳、非古典占星角點，僅供技術核對：Vertex=%.4f，"
             "Equatorial Ascendant=%.4f，Co-Ascendant(Koch)=%.4f，Co-Ascendant(Munkasey)=%.4f，"
             "Polar Ascendant=%.4f" % (vertex, equatorial_ascendant, co_ascendant_koch,
                                        co_ascendant_munkasey, polar_ascendant),
    )

    return {
        "system_name": name,
        "cusps": cusps_shifted,
        "asc": asc,
        "mc": mc,
        "desc": desc,
        "ic": ic,
        "armc": armc,
    }
