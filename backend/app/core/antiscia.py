"""對蹠點 (Antiscia) 與反對蹠點 (Contra-antiscia)。

鏡射軸（夏至/冬至軸、牡羊/天秤軸）是回歸黃道(tropical)上固定的天文軸，與 sidereal 的
0°無關。若輸入黃經是 sidereal 座標，需先加回 ayanamsa 換算成 tropical 做鏡射，再減回
ayanamsa 換算回 sidereal，淨效果等同在公式中多減一次 2×ayanamsa（tropical 模式 ayanamsa=0，
公式退化回原本的版本）。

Antiscia：以巨蟹/摩羯 0 度軸 (夏至/冬至軸) 為鏡射軸 -> (180 - 黃經 - 2×ayanamsa) mod 360
Contra-antiscia：以牡羊/天秤 0 度軸為鏡射軸 -> (360 - 黃經 - 2×ayanamsa) mod 360
"""

from .trace import Trace


def compute_antiscia(bodies: list, trace: Trace, ayanamsa: float = 0.0) -> dict:
    trace.add(
        "對蹠點計算方法",
        formula="Antiscia = (180° − 黃經 − 2×ayanamsa) mod 360°；Contra-antiscia = (360° − 黃經 − 2×ayanamsa) mod 360°",
        note="鏡射軸固定於回歸黃道，sidereal 模式下需扣除 2×ayanamsa 才能對齊真實至點/分點軸" if ayanamsa else "",
    )

    antiscia = []
    contra_antiscia = []
    for body in bodies:
        lon = body["longitude"]
        if lon is None:
            trace.add(f"{body['name']} 對蹠點", note="⚠ 黃經為 null（degenerate），略過。")
            antiscia.append({"key": body["key"], "name": body["name"], "longitude": None})
            contra_antiscia.append({"key": body["key"], "name": body["name"], "longitude": None})
            continue
        anti = (180.0 - lon - 2 * ayanamsa) % 360.0
        contra = (360.0 - lon - 2 * ayanamsa) % 360.0

        trace.add(
            f"{body['name']} 對蹠點",
            inputs={"黃經": lon},
            result={"Antiscia": anti, "Contra-antiscia": contra},
        )

        antiscia.append({"key": body["key"], "name": body["name"], "longitude": anti})
        contra_antiscia.append({"key": body["key"], "name": body["name"], "longitude": contra})

    return {"antiscia": antiscia, "contra_antiscia": contra_antiscia}
