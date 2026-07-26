"""古典占星計算所需的固定設定：星體清單、恆星清單、宮位制代碼、ayanamsa選項、預設容許誤差。"""

import swisseph as swe

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

# 南北交點
NODE_BODIES = [
    {"key": "true_node", "zh": "北交點(真)", "id": swe.TRUE_NODE},
    {"key": "mean_node", "zh": "北交點(平)", "id": swe.MEAN_NODE},
]

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
