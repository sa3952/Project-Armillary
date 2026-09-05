"""日夜盤判定 (Sect) 與阿拉伯點 Lot of Fortune / Lot of Spirit ——皆屬「方法層」而非
天文資料層：核心天文事實只有太陽的地平高度，「高度>0=日盤」是一條規則、「日夜盤決定
用哪個阿拉伯點公式」也是規則，因此在此明確標註具名 method，不與原始天文資料混在一起。

兩者都仰賴「觀測者站在地表看天空」這個地平座標前提，heliocentric/barycentric
模式下沒有意義，一律回傳 is_day/fortune/spirit 為 None 並在 trace 註明原因。
"""

from .trace import Trace

SECT_METHOD_NAME = "sun_center_true_altitude_gt_zero_v1"
# 古代來源（Paulus、Firmicus、Rhetorius 一系）只說太陽「在地上」(ὑπὲρ γῆν)，
# **沒有任何來源規定太陽正好在地平附近時如何判定**。Robert Hand 的標準專論亦僅
# 承認 twilight 是模糊地帶而未給解法。故本規則是本產品為求可重現而採的慣例，
# 不是古典來源所規定者。見 RES-MTH-SOURCES-2026-08-03 §1。
SECT_METHOD_PROVENANCE = "product_convention_no_classical_source_for_boundary"

# 臨界容差（Sebastian 2026-08-03 裁決，MTH-Q-001 C2）：±50 角分。
# 取此值的理由是文獻上唯一有物理意義的模糊寬度——大氣折射約 34′ 加太陽半徑 16′，
# 故上緣初現（視日出）時太陽中心約在幾何地平下 50′；此區間換算成時間約 3–5 分鐘。
SECT_NEAR_CRITICAL_DEGREES = 50.0 / 60.0
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
        "method_provenance": SECT_METHOD_PROVENANCE,
            "is_day": None,
            "sun_altitude_used": None,
            "near_critical": None,
            "near_critical_tolerance_degrees": SECT_NEAR_CRITICAL_DEGREES,
        }

    is_day = sun_altitude_true > 0
    near_critical = abs(sun_altitude_true) <= SECT_NEAR_CRITICAL_DEGREES
    trace.add(
        f"日夜盤判定 (Sect, method={SECT_METHOD_NAME})",
        formula="太陽地平真高度 > 0 → 日生盤；否則 → 夜生盤（採太陽中心，未計入蒙氣折射）",
        inputs={"太陽真高度": sun_altitude_true},
        result={
            "盤性": "日生盤 (Day)" if is_day else "夜生盤 (Night)",
            "臨近臨界": near_critical,
        },
        note=(
            "⚠ 太陽高度在地平上下 50 角分內，屬日夜交界的模糊區間（約 3–5 分鐘）。"
            "古典來源只規定太陽「在地上」，未規定此區間如何判定；本產品採幾何中心、"
            "不計折射，是為求可重現而採的慣例。此盤的日夜判定對出生時刻高度敏感。"
            if near_critical else ""
        ),
    )
    return {
        "method": SECT_METHOD_NAME,
        "method_status": METHOD_STATUS,
        "method_authority": None,
        "method_provenance": SECT_METHOD_PROVENANCE,
        "is_day": is_day,
        "sun_altitude_used": sun_altitude_true,
        "near_critical": near_critical,
        "near_critical_tolerance_degrees": SECT_NEAR_CRITICAL_DEGREES,
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
            "sect_near_critical": sect.get("near_critical"),
            "sect_near_critical_tolerance_degrees": sect.get(
                "near_critical_tolerance_degrees"
            ),
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
        # The formula was chosen by the sect.  Passing only the boolean dropped
        # the uncertainty the sect had already computed, and these longitudes
        # move by up to 2*(Moon-Sun) across that window.
        "sect_near_critical": sect.get("near_critical"),
        "sect_near_critical_tolerance_degrees": sect.get(
            "near_critical_tolerance_degrees"
        ),
    }
