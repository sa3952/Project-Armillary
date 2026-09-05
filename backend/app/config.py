"""古典占星計算所需的固定設定：星體清單、恆星清單、宮位制代碼、ayanamsa選項、預設容許誤差。"""

import swisseph as swe

# Public request-contract boundary.  Consumers that expose or describe the
# accepted year range import this tuple instead of duplicating the numbers.
PRODUCT_YEAR_RANGE = (1900, 2399)

# 古典七政
CLASSICAL_BODIES = [
    {"key": "sun", "zh": "太陽", "id": swe.SUN},
    {"key": "moon", "zh": "月亮", "id": swe.MOON},
    {"key": "mercury", "zh": "水星", "id": swe.MERCURY},
    {"key": "venus", "zh": "金星", "id": swe.VENUS},
    {"key": "mars", "zh": "火星", "id": swe.MARS},
    {"key": "jupiter", "zh": "木星", "id": swe.JUPITER},
    {"key": "saturn", "zh": "土星", "id": swe.SATURN},
]

# 現代外行星（三王星，非古典占星本體，預設不計算，使用者可另外勾選）
OUTER_BODIES = [
    {"key": "uranus", "zh": "天王星", "id": swe.URANUS},
    {"key": "neptune", "zh": "海王星", "id": swe.NEPTUNE},
    {"key": "pluto", "zh": "冥王星", "id": swe.PLUTO},
]

# 現代小天體。與三王星分開設選項，避免「include_outer_planets」在不知情下
# 擴張成另一套物件集合。這些物件不自動加入古典方法或相位參與者。
MODERN_MINOR_BODIES = [
    {
        "key": "chiron",
        "zh": "凱龍星",
        "id": swe.CHIRON,
        "calculation_source": "swiss_ephemeris_minor_planet",
        "method_classification": "modern_body_not_classical_planet",
    },
]

# CMP-A6（Sebastian 2026-08-04，方案三）。這些是月球軌道遠近地點，不是
# 物理天體，也不是小行星 1181 Lilith。Natural Priapus 必須用 Swiss 的
# INTP_PERG 獨立計算；Swiss 文件明示 natural apogee/perigee 通常不精確對沖。
LUNAR_APSIDES = [
    {
        "key": "mean_lilith",
        "zh": "平均黑月莉莉絲",
        "id": swe.MEAN_APOG,
        "expose_swiss_body_id": True,
        "calculation_source": "swiss_ephemeris_mean_lunar_apogee",
        "method_classification": "modern_research_lunar_apsis_not_physical_body",
        "naming_note": (
            "Swiss Ephemeris mean lunar apogee; astrological Mean Black Moon "
            "Lilith, not asteroid 1181 Lilith"
        ),
    },
    {
        "key": "natural_lilith",
        "zh": "自然／插值月球遠地點（Lilith）",
        "id": swe.INTP_APOG,
        "expose_swiss_body_id": True,
        "calculation_source": (
            "swiss_ephemeris_interpolated_natural_lunar_apogee"
        ),
        "method_classification": "modern_research_lunar_apsis_not_physical_body",
        "naming_note": (
            "Swiss Ephemeris interpolated/natural lunar apogee; not the "
            "osculating point marketed as True Lilith"
        ),
    },
    {
        "key": "natural_priapus",
        "zh": "自然／插值月球近地點（Priapus）",
        "id": swe.INTP_PERG,
        "expose_swiss_body_id": True,
        "calculation_source": (
            "swiss_ephemeris_interpolated_natural_lunar_perigee"
        ),
        "method_classification": "modern_research_lunar_apsis_not_physical_body",
        "naming_note": (
            "Swiss Ephemeris interpolated/natural lunar perigee (Priapus); "
            "independently calculated, not Lilith plus 180 degrees"
        ),
    },
]

# 南北交點
#
# 標籤刻意不用「真交點」。Swiss Ephemeris 自己的文件說得很清楚：
#   "In the strict sense of the word, even the 'true' nodes are true only twice a
#    month, viz. at the times when the Moon crosses the ecliptic."
#   "There are no planetary nodes or apsides ... that really deserve the label
#    'true'. ... It is more appropriate to call them 'osculating'."
# 所謂真交點是密切軌道(osculating orbit)的交點：把此刻的瞬時軌道當成兩體問題來解，
# 只在月亮實際穿越黃道的那兩個時刻為真，其餘時間是數學構造。它在平均交點附近振盪
# ±約 1.29°、週期約半年，每年數次短暫順行——那個順行是模型產物，不是天象。
#
# Sebastian 2026-08-03 裁決（MTH-Q-008 甲）：**正式技法採平均交點**，
# 理由是古代與中世紀的交點一律出自平均運動表（Ptolemy《Handy Tables》、印度 siddhānta），
# 真交點在現代計算能力出現前不可得；本產品定位為古典西洋占星。
# 兩者仍並列輸出，因為差距達 1°–1.5°，讀者有權看到。
# 見 RES-MTH-SOURCES-2026-08-03 §3。
#
# API 鍵值 true_node 維持不變以保相容；顯示標籤改為如實描述。
NODE_BODIES = [
    {"key": "true_node", "zh": "北交點(密切)", "id": swe.TRUE_NODE},
    {"key": "mean_node", "zh": "北交點(平均)", "id": swe.MEAN_NODE},
]

BODY_ID_BY_KEY = {
    body["key"]: body["id"]
    for body in (
        CLASSICAL_BODIES + OUTER_BODIES + MODERN_MINOR_BODIES + NODE_BODIES
    )
}

# 需要單一交點的技法一律採此者（MTH-Q-008 甲 裁決）。
# 目前沒有任何技法消費交點；本常數是為日後的技法預先固定選擇。
FORMAL_TECHNIQUE_NODE_KEY = "mean_node"

# 古典占星常引用的恆星（名稱需與 sefstars.txt 內拼寫完全一致，大小寫不拘）
FIXED_STARS = [
    {"key": "algol", "zh": "大陵五", "name": "Algol"},
    {"key": "alcyone", "zh": "昴宿六(昴星團)", "name": "Alcyone"},
    {"key": "aldebaran", "zh": "畢宿五", "name": "Aldebaran"},
    {"key": "capella", "zh": "五車二", "name": "Capella"},
    {"key": "sirius", "zh": "天狼星", "name": "Sirius"},
    {"key": "procyon", "zh": "南河三", "name": "Procyon"},
    {"key": "regulus", "zh": "軒轅十四", "name": "Regulus"},
    {"key": "alkaid", "zh": "搖光(大熊座η)", "name": "Alkaid"},
    {"key": "algorab", "zh": "軫宿一", "name": "Algorab"},
    {"key": "spica", "zh": "角宿一", "name": "Spica"},
    {"key": "arcturus", "zh": "大角", "name": "Arcturus"},
    {"key": "alphecca", "zh": "貫索四", "name": "Alphecca"},
    {"key": "antares", "zh": "心宿二", "name": "Antares"},
    {"key": "vega", "zh": "織女一", "name": "Vega"},
    {"key": "deneb_algedi", "zh": "壘壁陣四", "name": "Deneb Algedi"},
    {"key": "rigel", "zh": "參宿七", "name": "Rigel"},
    {"key": "altair", "zh": "河鼓二(牛郎星)", "name": "Altair"},
    {"key": "castor", "zh": "北河二", "name": "Castor"},
    {"key": "pollux", "zh": "北河三", "name": "Pollux"},
    {"key": "betelgeuse", "zh": "參宿四", "name": "Betelgeuse"},
    {"key": "alpheratz", "zh": "壁宿二", "name": "Alpheratz"},
    {"key": "mira", "zh": "芻蒿增二", "name": "Mira"},
    {"key": "bellatrix", "zh": "參宿五", "name": "Bellatrix"},
    {"key": "canopus", "zh": "老人星", "name": "Canopus"},
    {"key": "vindemiatrix", "zh": "東次將", "name": "Vindemiatrix"},
    {"key": "fomalhaut", "zh": "北落師門", "name": "Fomalhaut"},
    {"key": "achernar", "zh": "水委一", "name": "Achernar"},
    {"key": "polaris", "zh": "勾陳一(北極星)", "name": "Polaris"},
    {"key": "deneb", "zh": "天津四", "name": "Deneb"},
    {"key": "hadar", "zh": "馬腹一", "name": "Hadar"},
    # sefstars.txt 裡 "Menkar" 這個名字被兩顆不同的星共用（alCet 鯨魚座α本尊，
    # 與較暗的 laCet 鯨魚座λ），純文字查詢 "Menkar" 實測會比對到後者（星等4.7，
    # 並非一般認知、星等2.53 的鯨魚座α）。用 "名稱,星座縮寫" 格式明確指定星座編號解除歧義。
    {"key": "menkar", "zh": "天囷一", "name": "Menkar,alCet"},
    {"key": "schedar", "zh": "王良四", "name": "Schedar"},
    {"key": "hamal", "zh": "婁宿三", "name": "Hamal"},
    {"key": "zubenelgenubi", "zh": "氐宿一", "name": "Zubenelgenubi"},
]

# 宮位制代碼 (swe_houses hsys 單一字元)
HOUSE_SYSTEMS = {
    "B": "Alcabitius",
    "R": "Regiomontanus",
    "W": "Whole Sign",
    "P": "Placidus",
}

# 古典占星脈絡下較站得住腳的西方歷史性 ayanamsa（非印度占星常用的 Lahiri/KP）
AYANAMSA_OPTIONS = {
    "fagan_bradley": swe.SIDM_FAGAN_BRADLEY,
    "hipparchos": swe.SIDM_HIPPARCHOS,
    "sassanian": swe.SIDM_SASSANIAN,
    "aldebaran_15_tau": swe.SIDM_ALDEBARAN_15TAU,
}

# 預設容許誤差 (度)
DECLINATION_ASPECT_ORB = 1.0

# 月空亡(VOC)判斷用的托勒密五大相位角度
PTOLEMAIC_ASPECTS = [0.0, 60.0, 90.0, 120.0, 180.0]

# One declared default for the refraction atmosphere.  The bundled client sent
# 15 and the schema defaulted to 0.0, so anyone reproducing a published chart
# through the API without an atmosphere block computed against a different sky.
DEFAULT_ATMOSPHERE_TEMPERATURE_C = 15.0
