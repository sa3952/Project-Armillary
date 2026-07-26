"""七政（與可選外行星/南北交點）之黃道、赤道、地平座標、速度、物理現象計算。

套用統一的「計算模式」(ComputationContext)：計算中心、tropical/sidereal、apparent/true、
ecliptic-of-date/J2000、章動開關。地平座標/物理現象等在 heliocentric/barycentric 模式或
非物理星體(交點)上沒有意義時，一律輸出 null 並在 trace 註明原因，不以 0 冒充。

本模組只回傳原始天文事實，不做任何占星方法判斷（例如不判斷 out-of-bounds 是否成立、
不判斷順逆是否構成「停滯」）——這類判斷屬於方法層，見 main.py 的 derived_methods。

方位角 `azimuth` 已轉換成一般慣用的「北=0°、順時針向東遞增」；Swiss 原始的「南=0°、向西遞增」
值另外保留在 `azimuth_swiss_raw` 供核對，見 formatting.swiss_azimuth_to_standard()。

地平座標計算 (swe.azalt) 的氣壓/氣溫參數固定傳入 0/0，代表交由 swisseph 依海拔自動估計標準大氣
壓力、氣溫視為 0°C；這是未曝露為使用者可調參數的簡化假設，會讓視高度(altitude_apparent)的蒙氣
折射修正在非標準大氣條件下有些微誤差，真高度(altitude_true，未計蒙氣差)不受影響。
"""

import swisseph as swe

from .formatting import to_dms, to_hms, swiss_azimuth_to_standard
from .trace import Trace

AU_TO_KM = 149597870.7


def _motion_sign(speed: float) -> str:
    if speed > 0:
        return "positive"
    if speed < 0:
        return "negative"
    return "zero"


def compute_body(body_id: int, key: str, zh_name: str, jd_ut: float, ctx, atmosphere,
                  trace: Trace, physical: bool = True, moon_relative: bool = False) -> dict:
    flags = ctx.base_flags
    ecl, retflag_ecl = swe.calc_ut(jd_ut, body_id, flags)
    equatorial_flags = ctx.equatorial_source_flags | swe.FLG_EQUATORIAL
    equ, retflag_eq = swe.calc_ut(jd_ut, body_id, equatorial_flags)

    ecl_lon, ecl_lat, dist_au, speed_lon, speed_lat, speed_dist = ecl
    ra, dec, _dist2, speed_ra, speed_dec, _speed_dist2 = equ

    used_full_ephemeris = bool(retflag_ecl & swe.FLG_SWIEPH) and bool(retflag_eq & swe.FLG_SWIEPH)

    # 太陽在 heliocentric 模式下沒有物理意義（不能繞自己公轉）；月球相關的合成點(交點/遠近點)
    # 是以「繞地球運行」為前提定義的，heliocentric/barycentric 模式下同樣沒有意義。
    # 兩種情況 swisseph 都只會靜默回傳退化的 0，而非報錯，故需在此明確攔截、輸出 null。
    degenerate = (
        (key == "sun" and ctx.mode.center == "heliocentric")
        or (moon_relative and ctx.mode.center in ("heliocentric", "barycentric"))
    )

    if degenerate:
        result = {
            "key": key, "name": zh_name,
            "longitude": None, "latitude": None, "distance_au": None, "distance_km": None,
            "speed_longitude": None, "speed_latitude": None, "speed_distance": None,
            "motion_sign": None,
            "right_ascension": None, "declination": None, "speed_ra": None, "speed_dec": None,
            "longitude_dms": None, "latitude_dms": None, "right_ascension_hms": None, "declination_dms": None,
            "azimuth": None, "azimuth_swiss_raw": None, "altitude_true": None, "altitude_apparent": None,
            "phase_angle": None, "illuminated_fraction": None, "elongation": None,
            "apparent_diameter": None, "apparent_magnitude": None,
            "retflag_ecliptic": retflag_ecl, "retflag_equatorial": retflag_eq,
            "retflag_horizontal_source": None,
            "used_full_ephemeris": used_full_ephemeris,
        }
        trace.add(
            f"{zh_name} 位置計算",
            note="⚠ 此點在 heliocentric/barycentric 模式下沒有物理意義"
                 + ("（太陽無法繞自身公轉）" if key == "sun" else "（月球相關合成點以繞地運行為前提定義）")
                 + "，本項全部輸出為 null，而非用 0 假冒結果。",
        )
        return result

    motion_sign = _motion_sign(speed_lon)

    az = az_raw = true_alt = app_alt = None
    horizontal_retflag = None
    if ctx.horizon_meaningful:
        horizontal_flags = ctx.horizontal_source_flags
        if horizontal_flags == flags:
            horizontal_ecl = ecl
            horizontal_retflag = retflag_ecl
        else:
            horizontal_ecl, horizontal_retflag = swe.calc_ut(
                jd_ut,
                body_id,
                horizontal_flags,
            )
        used_full_ephemeris = (
            used_full_ephemeris
            and bool(horizontal_retflag & swe.FLG_SWIEPH)
            and not bool(horizontal_retflag & swe.FLG_MOSEPH)
        )
        geopos = (ctx.location.longitude, ctx.location.latitude, ctx.location.altitude_m)
        pressure_hpa = atmosphere.pressure_hpa if atmosphere.pressure_hpa is not None else 0.0
        az_raw, true_alt, app_alt = swe.azalt(
            jd_ut,
            swe.ECL2HOR,
            geopos,
            pressure_hpa,
            atmosphere.temperature_c,
            (horizontal_ecl[0], horizontal_ecl[1], horizontal_ecl[2]),
        )
        az = swiss_azimuth_to_standard(az_raw)

    phase_angle = illum_fraction = elongation = app_diameter = app_mag = None
    if physical:
        try:
            attr = swe.pheno_ut(jd_ut, body_id, flags)
            phase_angle, illum_fraction, elongation, app_diameter, app_mag = attr[0], attr[1], attr[2], attr[3], attr[4]
            # 太陽對自己求「相位角/照明比例/距角」在幾何上是退化的(Sun-Body-Earth三角形兩頂點重合)，
            # 無論計算中心為何都沒有意義，故一律轉為 null；但視直徑與視星等是獨立公式，太陽本身仍是有效值，保留。
            if key == "sun":
                phase_angle = illum_fraction = elongation = None
        except swe.Error:
            pass

    trace.add(
        f"{zh_name} 位置計算",
        formula="黃道: swe.calc_ut(計算模式旗標)；赤道: 移除 sidereal 後加 FLG_EQUATORIAL"
                + ("；地平: swe.azalt(ECL2HOR)" if ctx.horizon_meaningful else ""),
        inputs={
            "JD(UT)": jd_ut,
            "flags": flags,
            **({
                "氣壓(hPa；0=依海拔估算)": atmosphere.pressure_hpa or 0.0,
                "溫度(°C)": atmosphere.temperature_c,
                "地平座標來源flags(tropical of-date)": ctx.horizontal_source_flags,
            } if ctx.horizon_meaningful else {}),
        },
        result={
            "黃經": ecl_lon, "黃緯": ecl_lat, "距離(AU)": dist_au,
            "黃經速度(度/日)": speed_lon, "motion_sign": motion_sign,
            "赤經": ra, "赤緯": dec,
            **({"方位角Az(北=0,順時針)": az, "方位角Az(Swiss原始,南=0,向西)": az_raw,
                "真高度Alt": true_alt, "視高度(含蒙氣差)": app_alt} if ctx.horizon_meaningful else {}),
        },
        note=(None if used_full_ephemeris else "⚠ 此星體退回 Moshier 半分析模型，精度較低")
             if ctx.horizon_meaningful else "地平座標於 heliocentric/barycentric 模式下無物理意義，故不計算",
    )

    return {
        "key": key,
        "name": zh_name,
        "longitude": ecl_lon,
        "latitude": ecl_lat,
        "distance_au": dist_au,
        "distance_km": dist_au * AU_TO_KM,
        "speed_longitude": speed_lon,
        "speed_latitude": speed_lat,
        "speed_distance": speed_dist,
        "motion_sign": motion_sign,
        "right_ascension": ra,
        "declination": dec,
        "speed_ra": speed_ra,
        "speed_dec": speed_dec,
        "longitude_dms": to_dms(ecl_lon, wrap_360=True),
        "latitude_dms": to_dms(ecl_lat, signed=True),
        "right_ascension_hms": to_hms(ra),
        "declination_dms": to_dms(dec, signed=True),
        "azimuth": az,
        "azimuth_swiss_raw": az_raw,
        "altitude_true": true_alt,
        "altitude_apparent": app_alt,
        "phase_angle": phase_angle,
        "illuminated_fraction": illum_fraction,
        "elongation": elongation,
        "apparent_diameter": app_diameter,
        "apparent_magnitude": app_mag,
        "retflag_ecliptic": retflag_ecl,
        "retflag_equatorial": retflag_eq,
        "retflag_horizontal_source": horizontal_retflag,
        "used_full_ephemeris": used_full_ephemeris,
    }


def compute_bodies(body_defs: list, jd_ut: float, ctx, atmosphere, trace: Trace,
                    physical: bool = True, moon_relative: bool = False) -> list:
    return [
        compute_body(b["id"], b["key"], b["zh"], jd_ut, ctx, atmosphere, trace,
                     physical=physical, moon_relative=moon_relative)
        for b in body_defs
    ]
