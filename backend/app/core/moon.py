"""月空亡 (Void of Course)：候選入相位事件搜尋（含技法假設）與最終判定，明確分層。

搜尋本身已內含技法假設——採用哪些星體(古典七政)、哪些相位角(托勒密五大相位)、
以線性外插估計精確時刻——因此以具名 method 標註；「是否構成空亡」的最終布林判定
獨立成 determine_void_of_course()，不與搜尋結果混在一起，方便日後替換不同流派規則。
"""

import math

from ..config import PTOLEMAIC_ASPECTS
from .trace import Trace

VOC_METHOD_NAME = "classical_ptolemaic_applying_linear_v1"
VOC_METHOD_STATUS = "provisional_pending_method_audit"


def _time_to_relative_target(moon_lon, moon_speed, target_lon_now, other_speed):
    """求解 moon_lon + moon_speed*t ≡ target_lon_now + other_speed*t (mod 360) 的最小正 t（線性近似，速度視為短期內不變）。"""
    relative_speed = moon_speed - other_speed
    if relative_speed == 0:
        return None

    diff = (target_lon_now - moon_lon) % 360.0
    candidates = []
    for d in (diff, diff - 360.0):
        t = d / relative_speed
        if t > 1e-6:
            candidates.append(t)
    return min(candidates) if candidates else None


def find_voc_candidates(moon: dict, other_bodies: list, trace: Trace) -> dict:
    """回傳距離換座邊界的時間，以及邊界前會精確成立的所有候選入相位事件（依時間排序）。"""
    moon_lon = moon["longitude"]
    moon_speed = moon["speed_longitude"]

    next_boundary = math.floor(moon_lon / 30.0) * 30.0 + 30.0
    time_to_boundary_days = (next_boundary - moon_lon) / moon_speed if moon_speed > 0 else float("inf")

    trace.add(
        "VOC候選搜尋：距離下一個星座邊界的時間",
        formula="t_邊界 = (下一星座起點 − 月亮黃經) / 月亮每日速度",
        inputs={"月亮黃經": moon_lon, "月亮速度(度/日)": moon_speed, "下一星座起點": next_boundary},
        result={"距邊界時間(天)": time_to_boundary_days},
    )

    candidates = []
    for body in other_bodies:
        if body["longitude"] is None or body["speed_longitude"] is None:
            continue
        for aspect_angle in PTOLEMAIC_ASPECTS:
            for target in {(body["longitude"] + aspect_angle) % 360.0, (body["longitude"] - aspect_angle) % 360.0}:
                t = _time_to_relative_target(moon_lon, moon_speed, target, body["speed_longitude"])
                if t is None or t > time_to_boundary_days:
                    continue
                candidates.append({"body": body["name"], "aspect_angle": aspect_angle, "time_days": t})

    candidates.sort(key=lambda c: c["time_days"])

    trace.add(
        f"VOC候選搜尋 (method={VOC_METHOD_NAME})",
        formula="以月亮與其餘七政之相對角速度線性外插，求各托勒密相位(0/60/90/120/180°)成正確角的時間，僅保留邊界前發生者",
        result={"候選事件數": len(candidates), "候選清單": candidates},
        note="採線性近似（短期內視速度不變），非精確逐秒交點時刻；此搜尋已內含「用哪些星體/哪些相位角」的技法假設，"
             "屬方法假設而非純天文事實",
    )

    return {
        "method": VOC_METHOD_NAME,
        "method_status": VOC_METHOD_STATUS,
        "method_authority": None,
        "time_to_sign_exit_hours": time_to_boundary_days * 24.0,
        "candidates": candidates,
    }


def determine_void_of_course(voc_data: dict, trace: Trace) -> dict:
    """方法層最終判定：候選清單為空 = 空亡。"""
    candidates = voc_data["candidates"]
    is_void = len(candidates) == 0
    soonest = candidates[0] if candidates else None

    trace.add(
        "月空亡(VOC)最終判定",
        formula="若邊界前無任何候選入相位事件 -> 空亡；否則以最快完成者為離開星座前的入相位",
        result=(
            {"最快入相位": f"{soonest['body']} {soonest['aspect_angle']}°，約 {soonest['time_days']*24:.2f} 小時後"}
            if soonest else {"結果": "離開星座前無任何入相位 -> 月空亡"}
        ),
    )

    return {
        "method": voc_data["method"],
        "method_status": voc_data["method_status"],
        "method_authority": voc_data["method_authority"],
        "is_void_of_course": is_void,
        "time_to_sign_exit_hours": voc_data["time_to_sign_exit_hours"],
        "next_completing_aspect": soonest,
        "all_candidates": candidates,
    }
