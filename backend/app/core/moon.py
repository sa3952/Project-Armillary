"""月空亡 (Void of Course)：候選入相位事件搜尋（含技法假設）與最終判定，明確分層。

搜尋本身已內含技法假設——採用哪些星體(古典七政)、哪些相位角(托勒密五大相位)、
以何種數值方法估計精確時刻——因此以具名 method 標註；「是否構成空亡」的最終布林判定
獨立成 determine_void_of_course()，不與搜尋結果混在一起，方便日後替換不同流派規則。

方法名稱的來源說明（RES-MTH-SOURCES-2026-08-03 §2）：
本模組實作的是「月亮離開現在的星座前不完成任何托勒密相位」＋要求相位精確完成。
該定義有兩處為現代創新——以星座界線阻斷月亮的相位能力，以及不採容許度——
其推廣者為 20 世紀的 Al H. Morrison，**不是古典或中世紀傳統**：

- Antiochus / Porphyry（2–3 世紀）：未來 30° 內不完成相位，與星座界線無關。
- Antiochus（另一版）：一日一夜（約 13°）內不與任何行星結合。
- Masha'allah（9 世紀）、Bonatti（13 世紀）：分離後不再 apply，與星座界線無關。
- Lilly（1647）成文定義提及星座，但實務採容許度（moieties），界線不阻斷 application。

舊名 `classical_ptolemaic_applying_linear_v1` 中的 "classical" 因此是可被證偽的陳述，
依憲法屬完全禁止之列，故更名為如實描述其定義來源者。日後若要提供古典定義，
應另立具名方法並列（例如 hellenistic_30_degree_kenodromia_v1、lilly_moiety_application_v1），
而非改寫本方法。

數值解法（MTH-Q-003 乙）：
原本的線性外插已於 2026-08-03 依裁決廢止，改由 `core/root_finding.py` 的兩段式
求根器求解，且**換座邊界本身也改為求根**而非除法外插——月亮在 2.5 天內的黃經速度
變化足以讓「(邊界 − 現在黃經) / 現在速度」產生數分鐘到數十分鐘的誤差，而該誤差
直接決定哪些候選相位算在邊界之內，也就直接決定空亡的布林結果。
"""

import math

from ..config import PTOLEMAIC_ASPECTS
from .root_finding import SOLVER_NAME, find_crossings_detailed, wrap_to_signed_180
from .trace import Trace

VOC_METHOD_NAME = "modern_sign_bounded_exact_perfection_v1"
# 數值解法與方法定義分開記錄：解法變更時只需改本常數，方法名稱不受影響。
VOC_SOLVER_NAME = SOLVER_NAME
VOC_METHOD_STATUS = "provisional_pending_method_audit"

# 現行預設的來源尚未解決，而輸出必須說出這件事。
#
# `modern_sign_bounded_exact_perfection_v1` 的核心規則是「星座界線阻斷 application」。
# `SEBASTIAN_METHOD_RULINGS_2026-08-03.md` §13.5 記載該規則的出處有兩說並存——
# 20 世紀的 Al H. Morrison，或中世紀阿拉伯傳統——**兩者皆未查證**：未取得 Morrison
# 原書，亦未查閱 Masha'allah／Sahl／Bonatti 的校勘本。
#
# 依 Sebastian 2026-08-03 E-011 甲案採用的證據標準，只有二手依據的方法**可以**出貨，
# 但必須在收據中明示未轉錄一手文本。此前這項揭露只存在於文件裡，使用者看不到；
# `method_provenance` 只說了「現代、非古典」，沒說「連這個歸屬本身都未定」。
#
# 這不是說判定數值有誤——換座時刻與相位完成時刻由求根器算出，與規則出處無關。
# 被揭露的是**規則本身的來源強度**。
VOC_SOURCE_VERIFICATION = {
    "primary_texts_independently_transcribed": False,
    "sign_boundary_rule_attribution": "unresolved",
    "competing_attributions": (
        "al_h_morrison_20th_century",
        "medieval_arabic_transmission",
    ),
    "consulted": (),
    "not_consulted": (
        "Al H. Morrison, Void-of-Course Moon material (1980s newsletters)",
        "Masha'allah, On Reception",
        "Sahl ibn Bishr, Introduction to Astrology",
        "Bonatti, Liber Astronomiae Tract V",
    ),
    "recorded_in": (
        "docs/decisions/product/SEBASTIAN_METHOD_RULINGS_2026-08-03.md §13.5"
    ),
    "consequence": (
        "method_status cannot be raised to adopted until the attribution is "
        "settled against a named edition"
    ),
}

# 換座搜尋視窗。
#
# 初版寫的理由是「月亮速度介於 11.7–15.4 度/日，故 30/11.7 ≈ 2.57 日，取 3.0 日
# 有餘裕」。**那個速度區間只對地心成立**（RT-BACKEND-9-E-006）。實測
# topocentric 模式在高海拔測站可低到 6.21 度/日、高到 21.15 度/日——視差在一天
# 之內就能讓瞬時黃經速度變動一倍以上。以最低瞬時速度外推，跨越 30 度需要 4.83 日，
# 超過原本的視窗。
#
# 實際量測：在該測站掃描一整年、每 3 小時起算一次，最久的一次換座是 2.65 日，
# 且 topocentric 黃經在取樣中未曾倒退（monotonic），故「第一個根即換座時刻」的
# 前提成立。2.65 對 3.0 只有 12% 餘裕，而該量測只涵蓋一個測站一年，
# 因此視窗放寬到 6.0 日。搜尋是掃描式的，放寬只多花幾次星曆查詢。
_SIGN_EXIT_SEARCH_WINDOW_DAYS = 6.0
# 粗掃描步長 0.05 日（72 分鐘）。取 topocentric 上限 21.2 度/日再加上被相位行星
# 逆行的貢獻，單步最多移動約 1.1 度，遠低於 root_finding 區分真過零與環繞不連續
# 所需的 180 度。
_COARSE_STEP_DAYS = 0.05
# 收斂容差 1e-6 日約 0.086 秒。
_TOLERANCE_DAYS = 1e-6
# 恰好落在盤上時刻（t=0）的相位不計入「離開星座前會完成的入相位」。
# 見 find_voc_candidates 的說明與 MTH-Q-017。
_PRESENT_INSTANT_EPSILON_DAYS = 1e-9


def _find_sign_exit_days(moon_id, jd_ut, longitude_at, current_longitude):
    """求月亮實際跨過下一個星座界線的時刻（自盤上時刻起算的日數）。

    以「距離下一個星座起點還差幾度」為求根函式，在整個搜尋視窗上實際查星曆，
    因此不受月亮速度變化影響。月亮在視窗內不可能逆行（其黃經速度恆為正），
    故第一個根即為換座時刻。
    """

    boundary = math.floor(current_longitude / 30.0) * 30.0 + 30.0

    def distance_to_boundary(offset_days: float) -> float:
        return wrap_to_signed_180(longitude_at(moon_id, jd_ut + offset_days) - boundary)

    crossings = find_crossings_detailed(
        distance_to_boundary,
        window_days=_SIGN_EXIT_SEARCH_WINDOW_DAYS,
        coarse_step_days=_COARSE_STEP_DAYS,
        tolerance_days=_TOLERANCE_DAYS,
        max_roots=1,
    )
    return (crossings[0] if crossings else None), boundary


def _find_aspect_perfections(
    moon_id,
    body,
    jd_ut,
    longitude_at,
    window_days,
):
    """回傳月亮與某星體在視窗內所有精確成相的時刻與相位角。"""

    events: list[dict] = []
    body_id = body.get("body_id")
    if body_id is None:
        return events

    for aspect_angle in PTOLEMAIC_ASPECTS:
        # 合(0°)與沖(180°)的 +angle 與 −angle 在 (-180,180] 上重合，用集合自動去重。
        targets = {
            wrap_to_signed_180(aspect_angle),
            wrap_to_signed_180(-aspect_angle),
        }
        for target in targets:
            def separation_at(offset_days: float, _target=target) -> float:
                moment = jd_ut + offset_days
                return wrap_to_signed_180(
                    longitude_at(moon_id, moment)
                    - longitude_at(body_id, moment)
                    - _target
                )

            for record in find_crossings_detailed(
                separation_at,
                window_days=window_days,
                coarse_step_days=_COARSE_STEP_DAYS,
                tolerance_days=_TOLERANCE_DAYS,
            ):
                events.append(
                    {
                        "body": body["name"],
                        "body_key": body["key"],
                        "aspect_angle": aspect_angle,
                        "time_days": record["t"],
                        # MTH-Q-003 乙要求輸出括號端點與迭代次數供第三方複算。
                        "solver_evidence": {
                            "bracket": record["bracket"],
                            "bisection_iterations": record["iterations"],
                            "residual_degrees": record["residual_degrees"],
                        },
                    }
                )
    return events


def find_voc_candidates(
    moon: dict,
    other_bodies: list,
    trace: Trace,
    *,
    jd_ut: float,
    longitude_at,
    moon_id: int,
) -> dict:
    """回傳距離換座邊界的時間，以及邊界前會精確成立的所有候選入相位事件（依時間排序）。"""

    moon_lon = moon["longitude"]
    moon_speed = moon["speed_longitude"]

    sign_exit, boundary = _find_sign_exit_days(
        moon_id, jd_ut, longitude_at, moon_lon
    )
    time_to_boundary_days = sign_exit["t"] if sign_exit else None

    if time_to_boundary_days is None:
        # 月亮黃經速度恆為正，3 日視窗內必定換座；找不到根代表星曆或輸入異常，
        # 不得靜默當成「邊界在無限遠」而讓所有相位都算數。
        trace.add(
            "VOC候選搜尋：距離下一個星座邊界的時間",
            inputs={"月亮黃經": moon_lon, "下一星座起點": boundary},
            note="⚠ 求根器在 3 日視窗內找不到換座時刻，VOC 判定無法進行。",
        )
        return {
            "method": VOC_METHOD_NAME,
            "method_status": VOC_METHOD_STATUS,
            "method_authority": None,
            "method_provenance": "modern_20th_century_not_classical",
            "source_verification": VOC_SOURCE_VERIFICATION,
            "solver": VOC_SOLVER_NAME,
            "solver_status": "sign_exit_not_found_in_search_window",
            "time_to_sign_exit_hours": None,
            "candidates": [],
            "exact_at_chart_moment": [],
            "present_instant_policy": None,
        }

    trace.add(
        "VOC候選搜尋：距離下一個星座邊界的時間",
        formula="以 root_finding 求 月亮黃經(t) − 下一星座起點 = 0 的最早時刻",
        inputs={
            "月亮黃經": moon_lon,
            "月亮瞬時速度(度/日)": moon_speed,
            "下一星座起點": boundary,
            "求根器": VOC_SOLVER_NAME,
        },
        result={
            "距邊界時間(天)": time_to_boundary_days,
            "括號區間(天)": [
                sign_exit["bracket"]["low_days"],
                sign_exit["bracket"]["high_days"],
            ],
            "括號端點殘差(度)": [
                sign_exit["bracket"]["low_offset_degrees"],
                sign_exit["bracket"]["high_offset_degrees"],
            ],
            "二分迭代次數": sign_exit["iterations"],
            "收斂殘差(度)": sign_exit["residual_degrees"],
        },
        note="改以求根取代 (邊界 − 黃經) / 瞬時速度：月亮速度在 11.7–15.4 度/日之間變化，"
             "除法外插的誤差足以改變哪些相位算在邊界之內，進而改變空亡的布林結果。"
             "括號端點與迭代次數一併輸出，第三方可據以複算（MTH-Q-003 乙）。",
    )

    found = []
    for body in other_bodies:
        if body["longitude"] is None or body["speed_longitude"] is None:
            continue
        found.extend(
            _find_aspect_perfections(
                moon_id,
                body,
                jd_ut,
                longitude_at,
                time_to_boundary_days,
            )
        )

    # **MTH-Q-017 已裁決（Sebastian 2026-08-03）：恰在盤上時刻精確成立的相位
    # 計入空亡判定。** 理由是它此刻正在完成——有事情正在發生，月亮不是空亡。
    #
    # 紅隊指出的問題（RT-BACKEND-9-E-005）不是「t=0 被計入」本身，而是它被
    # **默默**當成一個未來事件回報：改用求根器之後 t=0 混進 candidates，
    # 既無標記也無政策說明，而舊的線性外插以 `t > 1e-6` 排除它。
    #
    # 因此裁決後的實作是「計入，但可分辨」：t=0 的事件另列在
    # exact_at_chart_moment，不混進代表未來的 candidates；空亡判定則同時看兩者。
    # `next_completing_aspect` 維持「最快的**未來**相位」語意，可能為 null。
    candidates = [
        event for event in found
        if event["time_days"] > _PRESENT_INSTANT_EPSILON_DAYS
    ]
    exact_now = [
        event for event in found
        if event["time_days"] <= _PRESENT_INSTANT_EPSILON_DAYS
    ]
    candidates.sort(key=lambda c: c["time_days"])
    exact_now.sort(key=lambda c: c["time_days"])

    trace.add(
        f"VOC候選搜尋 (method={VOC_METHOD_NAME}, solver={VOC_SOLVER_NAME})",
        formula="對每個托勒密相位角(0/60/90/120/180°)的兩個帶號目標，"
                "以粗掃描分割根區間後二分求根，僅保留換座邊界前發生者",
        result={"候選事件數": len(candidates), "候選清單": candidates},
        note="求根器在每個取樣點實際查詢星曆，不假設速度為定值，"
             "因此對月亮速度變化、被相位行星站留、以及同一視窗內的多重根皆成立。"
             "此搜尋仍內含「用哪些星體/哪些相位角」的技法假設，屬方法假設而非純天文事實。"
             "定義為現代（Morrison 系）的星座界線＋精確完成，"
             "與希臘化 30° 及 Lilly 容許度兩種古典定義不同，見 RES-MTH-SOURCES-2026-08-03 §2",
    )

    return {
        "method": VOC_METHOD_NAME,
        "method_status": VOC_METHOD_STATUS,
        "method_authority": None,
        "method_provenance": "modern_20th_century_not_classical",
        "source_verification": VOC_SOURCE_VERIFICATION,
        "solver": VOC_SOLVER_NAME,
        "solver_status": "converged",
        "time_to_sign_exit_hours": time_to_boundary_days * 24.0,
        "sign_exit_solver_evidence": {
            "boundary_longitude": boundary,
            "bracket": sign_exit["bracket"],
            "bisection_iterations": sign_exit["iterations"],
            "residual_degrees": sign_exit["residual_degrees"],
            "search_window_days": _SIGN_EXIT_SEARCH_WINDOW_DAYS,
        },
        "candidates": candidates,
        "exact_at_chart_moment": exact_now,
        "present_instant_policy": {
            "counts_toward_void_verdict": True,
            "epsilon_days": _PRESENT_INSTANT_EPSILON_DAYS,
            "ruling": "MTH-Q-017, Sebastian 2026-08-03",
            "note": (
                "在盤上時刻恰好精確成立的相位既非入相位也非出相位，但它此刻"
                "正在完成，故計入空亡判定（月亮不空亡）。它與未來相位分列兩個"
                "欄位以便分辨；next_completing_aspect 仍只取最快的未來相位。"
                f"「此刻」的容差為 {_PRESENT_INSTANT_EPSILON_DAYS} 日"
                "（約 0.086 毫秒），實務上極少觸發。"
            ),
        },
    }


def determine_void_of_course(voc_data: dict, trace: Trace) -> dict:
    """方法層最終判定：邊界前無任何相位完成 = 空亡。

    「無任何相位完成」同時看未來相位與盤上時刻恰好精確成立者——後者依
    MTH-Q-017 裁決計入。兩者仍分列，只有判定把它們合起來看。
    """

    candidates = voc_data["candidates"]
    exact_now = voc_data.get("exact_at_chart_moment", [])
    solver_failed = voc_data.get("solver_status") != "converged"
    is_void = (
        None if solver_failed else not (candidates or exact_now)
    )
    soonest = candidates[0] if candidates else None

    trace.add(
        "月空亡(VOC)最終判定",
        formula="若邊界前無任何相位完成（含盤上時刻恰好精確者）-> 空亡；"
                "否則以最快完成者為離開星座前的入相位",
        result=(
            {"結果": "⚠ 求根器未收斂，無法判定"}
            if solver_failed
            else {
                **(
                    {
                        "盤上時刻恰好精確成立": [
                            f"{item['body']} {item['aspect_angle']}°"
                            for item in exact_now
                        ]
                    }
                    if exact_now else {}
                ),
                **(
                    {"最快入相位": f"{soonest['body']} {soonest['aspect_angle']}°，約 {soonest['time_days']*24:.2f} 小時後"}
                    if soonest else {}
                ),
            }
            or {"結果": "離開星座前無任何相位完成 -> 月空亡"}
        ),
        note=(
            "盤上時刻恰好精確成立的相位依 MTH-Q-017 裁決計入判定。"
            if exact_now else ""
        ),
    )

    return {
        "method": voc_data["method"],
        "method_status": voc_data["method_status"],
        "method_authority": voc_data["method_authority"],
        "method_provenance": voc_data.get("method_provenance"),
        # This dict is rebuilt rather than spread, so anything added upstream has
        # to be carried across explicitly or it never reaches the response.
        "source_verification": voc_data.get("source_verification"),
        "solver": voc_data["solver"],
        "solver_status": voc_data.get("solver_status"),
        "is_void_of_course": is_void,
        "time_to_sign_exit_hours": voc_data["time_to_sign_exit_hours"],
        "next_completing_aspect": soonest,
        "all_candidates": candidates,
        "exact_at_chart_moment": voc_data.get("exact_at_chart_moment", []),
        "present_instant_policy": voc_data.get("present_instant_policy"),
    }
