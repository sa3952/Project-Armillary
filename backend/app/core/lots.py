"""日夜盤判定 (Sect) 與阿拉伯點 Lot of Fortune / Lot of Spirit ——皆屬「方法層」而非
天文資料層：核心天文事實只有太陽的地平高度，「高度>0=日盤」是一條規則、「日夜盤決定
用哪個阿拉伯點公式」也是規則，因此在此明確標註具名 method，不與原始天文資料混在一起。

兩者都仰賴「觀測者站在地表看天空」這個地平座標前提，heliocentric/barycentric
模式下沒有意義，一律回傳 is_day/fortune/spirit 為 None 並在 trace 註明原因。
"""

from .trace import Trace

SECT_METHOD_NAME = "sun_center_true_altitude_gt_zero_v1"
LOTS_METHOD_NAME = "traditional_day_night_formula_v1"
METHOD_STATUS = "provisional_pending_method_audit"


def determine_sect(sun_altitude_true, trace: Trace) -> dict:
    """太陽地平真高度 > 0 視為日生盤 (diurnal)，否則夜生盤 (nocturnal)。
    這是一條方法規則（採太陽中心、未計入蒙氣折射），不是天文事實本身。"""
    if sun_altitude_true is None:
        trace.add(
            "日夜盤判定 (Sect)",
            note="⚠ heliocentric/barycentric 模式下太陽沒有地平高度，日夜盤無法判定，回傳 null。",
        )
        return {
            "method": SECT_METHOD_NAME,
            "method_status": METHOD_STATUS,
            "method_authority": None,
            "is_day": None,
            "sun_altitude_used": None,
        }

    is_day = sun_altitude_true > 0
    trace.add(
        f"日夜盤判定 (Sect, method={SECT_METHOD_NAME})",
        formula="太陽地平真高度 > 0 → 日生盤；否則 → 夜生盤（採太陽中心，未計入蒙氣折射）",
        inputs={"太陽真高度": sun_altitude_true},
        result={"盤性": "日生盤 (Day)" if is_day else "夜生盤 (Night)"},
    )
    return {
        "method": SECT_METHOD_NAME,
        "method_status": METHOD_STATUS,
        "method_authority": None,
        "is_day": is_day,
        "sun_altitude_used": sun_altitude_true,
    }


def compute_lots(asc: float, sun_lon: float, moon_lon: float, sect: dict, trace: Trace) -> dict:
    is_day = sect["is_day"]
    if is_day is None or sun_lon is None or moon_lon is None:
        trace.add(
            "阿拉伯點：Fortune / Spirit",
            note="⚠ 缺少日夜盤判定或太陽/月亮黃經（heliocentric/barycentric 模式下），無法計算，回傳 null。",
        )
        return {
            "method": LOTS_METHOD_NAME,
            "method_status": METHOD_STATUS,
            "method_authority": None,
            "fortune": None,
            "spirit": None,
            "depends_on_sect": is_day,
        }

    if is_day:
        fortune = (asc + moon_lon - sun_lon) % 360.0
        spirit = (asc + sun_lon - moon_lon) % 360.0
        formula_fortune = "Fortune = ASC + Moon − Sun（日盤公式）"
        formula_spirit = "Spirit = ASC + Sun − Moon（日盤公式）"
    else:
        fortune = (asc + sun_lon - moon_lon) % 360.0
        spirit = (asc + moon_lon - sun_lon) % 360.0
        formula_fortune = "Fortune = ASC + Sun − Moon（夜盤公式）"
        formula_spirit = "Spirit = ASC + Moon − Sun（夜盤公式）"

    trace.add(
        f"阿拉伯點：Lot of Fortune (method={LOTS_METHOD_NAME})",
        formula=formula_fortune,
        inputs={"ASC": asc, "太陽黃經": sun_lon, "月亮黃經": moon_lon},
        result={"Fortune 黃經": fortune},
    )
    trace.add(
        f"阿拉伯點：Lot of Spirit (method={LOTS_METHOD_NAME})",
        formula=formula_spirit,
        inputs={"ASC": asc, "太陽黃經": sun_lon, "月亮黃經": moon_lon},
        result={"Spirit 黃經": spirit},
    )

    return {
        "method": LOTS_METHOD_NAME,
        "method_status": METHOD_STATUS,
        "method_authority": None,
        "fortune": fortune,
        "spirit": spirit,
        "depends_on_sect": is_day,
    }
