"""恆星位置與星等（含完整赤緯相關資料），套用統一計算模式。"""

import swisseph as swe

from .formatting import to_dms, to_hms, swiss_azimuth_to_standard
from .trace import Trace


def compute_fixed_stars(star_defs: list, jd_ut: float, ctx, atmosphere, trace: Trace) -> list:
    flags = ctx.base_flags
    results = []
    for star in star_defs:
        name = star["name"]
        try:
            ecl, matched_name, retflag_ecl = swe.fixstar2_ut(name, jd_ut, flags)
            equ, _matched_name2, retflag_eq = swe.fixstar2_ut(
                name,
                jd_ut,
                ctx.equatorial_source_flags | swe.FLG_EQUATORIAL,
            )
            mag, _ = swe.fixstar2_mag(name)
        except swe.Error as exc:
            trace.add(
                f"恆星 {star['zh']}({name}) 查詢失敗",
                note=f"⚠ {exc}（可能缺少 sefstars.txt 或名稱不在星表中，已略過）",
            )
            results.append({
                "key": star["key"], "name": star["zh"], "input_name": name,
                "catalog_name": None, "error": str(exc),
                "longitude": None, "latitude": None, "distance_au": None,
                "speed_longitude": None, "speed_latitude": None, "speed_distance": None,
                "right_ascension": None, "declination": None, "speed_ra": None, "speed_dec": None,
                "azimuth": None, "azimuth_swiss_raw": None, "altitude_true": None, "altitude_apparent": None,
                "magnitude": None, "retflag_horizontal_source": None, "used_full_ephemeris": None,
            })
            continue

        ecl_lon, ecl_lat, dist, speed_lon, speed_lat, speed_dist = ecl
        ra, dec, _dist2, speed_ra, speed_dec, _sd2 = equ
        used_full_ephemeris = bool(retflag_ecl & swe.FLG_SWIEPH) and bool(retflag_eq & swe.FLG_SWIEPH)

        az = az_raw = true_alt = app_alt = None
        horizontal_retflag = None
        if ctx.horizon_meaningful:
            horizontal_flags = ctx.horizontal_source_flags
            if horizontal_flags == flags:
                horizontal_ecl = ecl
                horizontal_retflag = retflag_ecl
            else:
                horizontal_ecl, _horizontal_name, horizontal_retflag = swe.fixstar2_ut(
                    name,
                    jd_ut,
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

        trace.add(
            f"恆星 {star['zh']}（{matched_name}）位置計算",
            formula="黃道使用計算模式旗標；赤道移除 sidereal 後加 FLG_EQUATORIAL",
            inputs={
                "JD(UT)": jd_ut,
                "氣壓(hPa；0=依海拔估算)": atmosphere.pressure_hpa or 0.0,
                "溫度(°C)": atmosphere.temperature_c,
                "地平座標來源flags(tropical of-date)": ctx.horizontal_source_flags,
            },
            result={
                "黃經": ecl_lon, "黃緯": ecl_lat,
                "赤經": ra, "赤緯": dec,
                **({"方位角Az(北=0,順時針)": az, "方位角Az(Swiss原始,南=0,向西)": az_raw, "高度Alt": true_alt} if ctx.horizon_meaningful else {}),
                "星等": mag,
            },
        )

        results.append({
            "key": star["key"],
            "name": star["zh"],
            "input_name": name,
            "catalog_name": matched_name,
            "error": None,
            "longitude": ecl_lon,
            "latitude": ecl_lat,
            "distance_au": dist,
            "speed_longitude": speed_lon,
            "speed_latitude": speed_lat,
            "speed_distance": speed_dist,
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
            "magnitude": mag,
            "retflag_ecliptic": retflag_ecl,
            "retflag_equatorial": retflag_eq,
            "retflag_horizontal_source": horizontal_retflag,
            "used_full_ephemeris": used_full_ephemeris,
        })
    return results
