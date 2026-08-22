"""七政（與可選外行星/南北交點）之黃道、赤道、地平座標、速度、物理現象計算。

套用統一的「計算模式」(ComputationContext)：計算中心、tropical/sidereal、apparent/true、
ecliptic-of-date/J2000、章動開關。地平座標/物理現象等在 heliocentric/barycentric 模式或
非物理星體(交點)上沒有意義時，一律輸出 null 並在 trace 註明原因，不以 0 冒充。

本模組只回傳原始天文事實，不做任何占星方法判斷（例如不判斷 out-of-bounds 是否成立、
不判斷順逆是否構成「停滯」）——這類判斷屬於方法層，見 main.py 的 derived_methods。

方位角 `azimuth` 已轉換成一般慣用的「北=0°、順時針向東遞增」；Swiss 原始的「南=0°、向西遞增」
值另外保留在 `azimuth_swiss_raw` 供核對，見 formatting.swiss_azimuth_to_standard()。

地平座標計算 (swe.azalt) 會傳入使用者的 `pressure_hpa` 與 `temperature_c`。氣壓未指定時傳入
0 hPa，讓 Swiss Ephemeris 依海拔估算；氣溫仍使用 request 值（預設 0°C）。這些大氣參數只影響
視高度(altitude_apparent)的蒙氣折射修正，真高度(altitude_true，未計蒙氣差)不受影響。
"""

import swisseph as swe

from .formatting import to_dms, to_hms, swiss_azimuth_to_standard
from .trace import Trace

AU_TO_KM = 149597870.7

# MTH-Q-009 裁決（Sebastian 2026-08-03，A1）：`position_mode="true"` 時不輸出視高度。
# true position 已移除光行時與光行差，得到的是幾何方向；再對該方向套用大氣折射，
# 會讓同一個數值同時假設「光瞬時到達」與「光穿過大氣」兩個互斥前提。這個數字
# 無法被正確解讀，也無法被第三方複算，故不輸出優於輸出。
TRUE_POSITION_APPARENT_ALTITUDE_REASON = (
    "suppressed_true_position_refraction_semantics_incompatible"
)


def apparent_altitude_policy(ctx) -> tuple[bool, str | None]:
    """回傳 (是否輸出視高度, 不輸出時的 reason_code)。恆星模組共用同一條規則。"""

    if ctx.mode.position_mode == "true":
        return False, TRUE_POSITION_APPARENT_ALTITUDE_REASON
    return True, None


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

    emit_apparent_altitude, apparent_altitude_reason = apparent_altitude_policy(ctx)

    if degenerate:
        result = {
            "key": key, "name": zh_name,
            "longitude": None, "latitude": None, "distance_au": None, "distance_km": None,
            "speed_longitude": None, "speed_latitude": None, "speed_distance": None,
            "speed_source": None, "speed_position_derivative_status": "not_applicable",
            "motion_sign": None,
            "right_ascension": None, "declination": None, "speed_ra": None, "speed_dec": None,
            "longitude_dms": None, "latitude_dms": None, "right_ascension_hms": None, "declination_dms": None,
            "azimuth": None, "azimuth_swiss_raw": None, "altitude_true": None, "altitude_apparent": None,
            "altitude_apparent_reason_code": "not_applicable_for_display_center",
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
    speed_position_derivative_status = (
        "known_internal_disagreement_for_sun_moon"
        if ctx.mode.center == "topocentric" and key in {"sun", "moon"}
        else "not_independently_established"
        if ctx.mode.center == "topocentric"
        else "not_evaluated"
    )

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
        if not emit_apparent_altitude:
            app_alt = None

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
                "真高度Alt": true_alt,
                **({"視高度(含蒙氣差)": app_alt} if emit_apparent_altitude else {})}
               if ctx.horizon_meaningful else {}),
        },
        note=(
            ("" if used_full_ephemeris else "⚠ 此星體退回 Moshier 半分析模型，精度較低")
            + (
                ""
                if emit_apparent_altitude
                else "position_mode=true 下不輸出視高度：幾何方向與大氣折射是互斥前提，"
                     "混合後的數值無法被正確解讀或複算（MTH-Q-009 A1）。"
            )
            + (
                "站心speed_*是Swiss FLG_SPEED解析值；已知太陽／月亮部分欄位"
                "不等於同次站心position的有限差分導數，故不作導數精度宣稱。"
                if speed_position_derivative_status
                == "known_internal_disagreement_for_sun_moon"
                else ""
            )
        ) if ctx.horizon_meaningful
        else "地平座標於 heliocentric/barycentric 模式下無物理意義，故不計算",
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
        "speed_source": "swiss_ephemeris_flg_speed_analytic",
        "speed_position_derivative_status": speed_position_derivative_status,
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
        "altitude_apparent_reason_code": (
            None
            if app_alt is not None
            else (
                apparent_altitude_reason
                if not emit_apparent_altitude
                else "not_applicable_for_display_center"
            )
        ),
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


def make_longitude_sampler(ctx, *, moon_ctx=None, moon_id: int = swe.MOON):
    """回傳 (body_id, jd_ut) -> 黃經 的取樣函式，供求根器在時間軸上重複查星曆。

    刻意沿用 `ctx.base_flags`，讓求根結果與主要輸出處在同一個黃道系統與參考框架：
    若這裡改用別組旗標，sidereal 模式下算出的「成相時刻」會對應到另一套星座界線。
    速度旗標(FLG_SPEED)已包含在 base_flags 內，額外成本可忽略，故不另外拆一組旗標。
    """

    flags = ctx.base_flags
    moon_flags = moon_ctx.base_flags if moon_ctx is not None else None

    def longitude_at(body_id: int, jd_ut: float) -> float:
        effective_flags = (
            moon_flags
            if moon_flags is not None and body_id == moon_id
            else flags
        )
        values, _retflag = swe.calc_ut(jd_ut, body_id, effective_flags)
        return values[0]

    return longitude_at


def compute_bodies(body_defs: list, jd_ut: float, ctx, atmosphere, trace: Trace,
                    physical: bool = True, moon_relative: bool = False) -> list:
    results = []
    for definition in body_defs:
        result = compute_body(
            definition["id"],
            definition["key"],
            definition["zh"],
            jd_ut,
            ctx,
            atmosphere,
            trace,
            physical=physical,
            moon_relative=moon_relative,
        )
        for metadata_key in (
            "calculation_source",
            "method_classification",
            "naming_note",
        ):
            if metadata_key in definition:
                result[metadata_key] = definition[metadata_key]
        if definition.get("expose_swiss_body_id"):
            result["swiss_body_id"] = definition["id"]
        results.append(result)
    return results


def derive_node_antipode(
    north: dict,
    *,
    key: str,
    name: str,
    body_id: int,
    jd_ut: float,
    ctx,
    atmosphere,
    trace: Trace,
) -> dict:
    """Derive a descending lunar node as the directional antipode of a north node.

    The adopted product contract is intentionally directional: longitude/latitude and
    equatorial direction are antipodal, while distance is left null because adding 180°
    to a node direction does not independently determine a physical radial distance.
    """

    derivation = {
        "derived_from": north["key"],
        "calculation_source": "north_node_direction_antipode",
        "derivation_formula": (
            "ecliptic_longitude=(north+180) mod 360; ecliptic_latitude=-north; "
            "right_ascension=(north+180) mod 360; declination=-north"
        ),
    }
    if north.get("longitude") is None:
        result = dict(north)
        result.update({"key": key, "name": name, **derivation})
        result["distance_au"] = None
        result["distance_km"] = None
        result["speed_distance"] = None
        trace.add(
            f"{name} 導出",
            formula="北交點方向的黃道／赤道對蹠點",
            note="來源北交點不可用，故南交點同樣不可用。",
        )
        return result

    longitude = (north["longitude"] + 180.0) % 360.0
    latitude = -north["latitude"]
    right_ascension = (north["right_ascension"] + 180.0) % 360.0
    declination = -north["declination"]
    speed_latitude = (
        -north["speed_latitude"]
        if north.get("speed_latitude") is not None
        else None
    )
    speed_dec = (
        -north["speed_dec"] if north.get("speed_dec") is not None else None
    )

    azimuth = azimuth_raw = altitude_true = altitude_apparent = None
    horizontal_retflag = None
    emit_apparent, apparent_reason = apparent_altitude_policy(ctx)
    used_full_ephemeris = north.get("used_full_ephemeris")
    if ctx.horizon_meaningful:
        horizontal_values, horizontal_retflag = swe.calc_ut(
            jd_ut, body_id, ctx.horizontal_source_flags
        )
        south_horizontal = (
            (horizontal_values[0] + 180.0) % 360.0,
            -horizontal_values[1],
            horizontal_values[2],
        )
        pressure_hpa = (
            atmosphere.pressure_hpa
            if atmosphere.pressure_hpa is not None
            else 0.0
        )
        azimuth_raw, altitude_true, altitude_apparent = swe.azalt(
            jd_ut,
            swe.ECL2HOR,
            (ctx.location.longitude, ctx.location.latitude, ctx.location.altitude_m),
            pressure_hpa,
            atmosphere.temperature_c,
            south_horizontal,
        )
        azimuth = swiss_azimuth_to_standard(azimuth_raw)
        if not emit_apparent:
            altitude_apparent = None
        used_full_ephemeris = bool(used_full_ephemeris) and bool(
            horizontal_retflag & swe.FLG_SWIEPH
        ) and not bool(horizontal_retflag & swe.FLG_MOSEPH)

    trace.add(
        f"{name} 導出",
        formula=derivation["derivation_formula"],
        inputs={"來源": north["key"], "來源黃經": north["longitude"]},
        result={"黃經": longitude, "赤經": right_ascension, "赤緯": declination},
        note="這是北交點方向的幾何對蹠點，不是另一個獨立 Swiss body。",
    )
    return {
        "key": key,
        "name": name,
        "longitude": longitude,
        "latitude": latitude,
        "distance_au": None,
        "distance_km": None,
        "speed_longitude": north["speed_longitude"],
        "speed_latitude": speed_latitude,
        "speed_distance": None,
        "motion_sign": north["motion_sign"],
        "right_ascension": right_ascension,
        "declination": declination,
        "speed_ra": north["speed_ra"],
        "speed_dec": speed_dec,
        "longitude_dms": to_dms(longitude, wrap_360=True),
        "latitude_dms": to_dms(latitude, signed=True),
        "right_ascension_hms": to_hms(right_ascension),
        "declination_dms": to_dms(declination, signed=True),
        "azimuth": azimuth,
        "azimuth_swiss_raw": azimuth_raw,
        "altitude_true": altitude_true,
        "altitude_apparent": altitude_apparent,
        "altitude_apparent_reason_code": (
            None
            if altitude_apparent is not None
            else (
                apparent_reason
                if not emit_apparent
                else "not_applicable_for_display_center"
            )
        ),
        "phase_angle": None,
        "illuminated_fraction": None,
        "elongation": None,
        "apparent_diameter": None,
        "apparent_magnitude": None,
        "retflag_ecliptic": north.get("retflag_ecliptic"),
        "retflag_equatorial": north.get("retflag_equatorial"),
        "retflag_horizontal_source": horizontal_retflag,
        "used_full_ephemeris": used_full_ephemeris,
        **derivation,
    }
