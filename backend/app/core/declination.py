"""赤緯相位 (平行 Parallel／反平行 Contra-parallel) ——屬方法層而非天文資料層。

赤緯數值本身（見 astronomical_data.bodies 的 declination 欄位）是天文事實；
「相差在 orb 之內即構成相位」是帶容許誤差的技法判斷，因此在此明確標註具名 method
與所用 orb，回傳結果應歸類為 derived_methods，不與原始星體位置混在同一層。
"""

from .trace import Trace

DECLINATION_ASPECT_METHOD_NAME = "parallel_contra_parallel_fixed_orb_v1"
DECLINATION_ASPECT_METHOD_STATUS = "provisional_pending_method_audit"


def compute_declination_aspects(bodies: list, orb: float, trace: Trace) -> dict:
    trace.add(
        f"赤緯相位判斷方法 (method={DECLINATION_ASPECT_METHOD_NAME})",
        formula="平行(Parallel): |δ1 - δ2| ≤ 容許誤差 且 同號；反平行(Contra-parallel): |δ1 + δ2| ≤ 容許誤差 且 異號",
        inputs={"容許誤差(度)": orb},
        result={"參與星體數": len(bodies)},
    )

    aspects = []
    for i in range(len(bodies)):
        for j in range(i + 1, len(bodies)):
            a, b = bodies[i], bodies[j]
            d1, d2 = a["declination"], b["declination"]
            if d1 is None or d2 is None:
                continue

            if d1 * d2 >= 0 and abs(d1 - d2) <= orb:
                aspects.append({
                    "type": "parallel", "body_a": a["name"], "body_b": b["name"],
                    "declination_a": d1, "declination_b": d2, "diff": abs(d1 - d2),
                })
            elif d1 * d2 < 0 and abs(d1 + d2) <= orb:
                aspects.append({
                    "type": "contra_parallel", "body_a": a["name"], "body_b": b["name"],
                    "declination_a": d1, "declination_b": d2, "diff": abs(d1 + d2),
                })

    for asp in aspects:
        label = "平行" if asp["type"] == "parallel" else "反平行"
        trace.add(
            f"{asp['body_a']} 與 {asp['body_b']} 成赤緯{label}",
            inputs={"δ({})".format(asp["body_a"]): asp["declination_a"], "δ({})".format(asp["body_b"]): asp["declination_b"]},
            result={"誤差(度)": asp["diff"]},
        )

    return {
        "method": DECLINATION_ASPECT_METHOD_NAME,
        "method_status": DECLINATION_ASPECT_METHOD_STATUS,
        "method_authority": None,
        "orb_degrees": orb,
        "aspects": aspects,
    }
