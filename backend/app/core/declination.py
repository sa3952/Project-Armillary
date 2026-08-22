"""赤緯相位 (平行 Parallel／反平行 Contra-parallel) ——屬方法層而非天文資料層。

赤緯數值本身（見 astronomical_data.bodies 的 declination 欄位）是天文事實；
「相差在 orb 之內即構成相位」是帶容許誤差的技法判斷，因此在此明確標註具名 method
與所用 orb，回傳結果應歸類為 derived_methods，不與原始星體位置混在同一層。

MTH-Q-004 裁決（Sebastian 2026-08-03，A1，比照三王星處理）：
標為 `research_only`，維持預設關閉、opt-in，介面須明示其為**近現代技法、非古典傳統**。
赤緯平行／反平行主要由 20 世紀作者推廣，非希臘化或中世紀主流；既有實作可保留，
但**稱之為「古典」將構成不實陳述**（憲法：可被證偽的陳述完全禁止）。
"""

from .trace import Trace

DECLINATION_ASPECT_METHOD_NAME = "parallel_contra_parallel_fixed_orb_v1"
DECLINATION_ASPECT_METHOD_STATUS = "provisional_pending_method_audit"
DECLINATION_ASPECT_CLASSIFICATION = "research_only"
DECLINATION_ASPECT_CLASSIFICATION_RULING = "MTH-Q-004 A1 (2026-08-03)"
DECLINATION_ASPECT_PROVENANCE = "modern_20th_century_not_classical"


def _declination_side(value: float) -> str:
    if value == 0.0:
        return "zero"
    return "north" if value > 0.0 else "south"


def compute_declination_aspects(
    bodies: list,
    orb: float,
    trace: Trace,
    *,
    default_orb: float = 1.0,
) -> dict:
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

            side_a = _declination_side(d1)
            side_b = _declination_side(d2)
            if "zero" in (side_a, side_b):
                continue

            if side_a == side_b and abs(d1 - d2) <= orb:
                aspects.append({
                    "type": "parallel", "body_a": a["name"], "body_b": b["name"],
                    "declination_a": d1, "declination_b": d2, "diff": abs(d1 - d2),
                })
            elif side_a != side_b and abs(d1 + d2) <= orb:
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
        "method_classification": DECLINATION_ASPECT_CLASSIFICATION,
        "classification_ruling": DECLINATION_ASPECT_CLASSIFICATION_RULING,
        "method_provenance": DECLINATION_ASPECT_PROVENANCE,
        "declination_sides": {
            body["name"]: _declination_side(body["declination"])
            for body in bodies
            if body.get("declination") is not None
        },
        "orb_degrees": orb,
        "orb_receipt": {
            "requested": orb != default_orb,
            "executed": True,
            "applicable": True,
            "available": True,
            "source": "user_override" if orb != default_orb else "product_default",
            "default_orb_degrees": default_orb,
            "effective_orb_degrees": orb,
        },
        "aspects": aspects,
    }
