"""兩段式角度求根：粗掃描分割根區間，再以二分法收斂。

本模組取代原本散落在 moon.py 的線性外插近似（MTH-Q-003 乙裁決廢止）。線性外插把
兩顆星體的黃經速度視為視窗期間的常數，對以下三種情況都會給出錯誤答案：

1. **速度非定值。** 月亮的黃經速度在 11.7–15.4 度/日之間變化，2.5 天的搜尋視窗內
   足以累積數度誤差；外行星在站留(station)附近速度趨近 0 並反號。
2. **多重根。** 兩顆速度相近的星體可能在同一個視窗內三次形成同一個相位
   （順行成相 → 對方逆行退回 → 再度順行成相）。線性外插只解得出一個根。
3. **相對速度反號。** 線性外插在 `relative_speed == 0` 時直接放棄，但真實情況是
   相位可能在該時刻附近相切（tangency）——差一點成相但沒有成相，或恰好成相一次。

改採的方法對這三種情況都成立，因為它**在每一個取樣點都實際查星曆**，不對星體運動
做任何解析假設：

- 第一段：以固定步長 `coarse_step_days` 掃描整個視窗，記錄 f(t) 的值。
  凡相鄰兩點變號、且跨度小於 180 度者，即為一個包住根的區間。
  「跨度小於 180 度」這個條件用來區分真正的過零與環繞不連續（f 定義在
  (-180, 180]，星體通過「距離精確相位 180 度」處時 f 會從 +180 跳到 -180）。
- 第一段補強：偵測 |f| 的局部極小值。若某個取樣點的 |f| 小於
  `tangency_probe_degrees` 而其兩側皆較大，代表該處可能有一對隱藏在單一步長內的
  根（相切或近相切）。此時對該鄰域以更細的步長遞迴重掃，深度上限
  `_MAX_REFINEMENT_DEPTH`。
- 第二段：對每個已包住根的區間跑二分法。刻意不用 Newton 或割線法——它們需要導數
  或良好初始值，且在站留附近不穩定；二分法在區間已被包住時無條件收斂，
  且每次迭代只要一次星曆查詢。

`separation_at` 由呼叫端提供，回傳「目前角距離精確相位還差幾度」的帶號值，
定義域 (-180, 180]。求根器本身不知道任何占星語意，只解 f(t) = 0。

## 已知界限（不得對外描述為「已處理」）

1. **恰好相切的偶次根。** f 觸零後不變號（相位在某一瞬間精確成立，隨即拉開），
   只有在某個取樣點恰好落在該瞬間時才會被找到。浮點數下這實質上不會發生。
   本求根器對這種情形的行為是「回報沒有根」。這在物理上是可接受的：
   相切點的存在與否對出生時刻的無窮小擾動不穩定，因此本來就不是可宣稱的結果。
   `test_a_near_miss_at_a_station_is_not_reported_as_a_perfection` 釘住此行為。
2. **細掃深度有限。** 若一對根靠得比 `coarse_step / (2·8³)` 還近，仍會被漏掉。
   呼叫端應以「該對星體的相對角速度上限 × 步長」估算自身的解析度並選定步長。
3. **視窗外的根不予回報。** 呼叫端必須自行決定視窗，並在回報結果時說明
   「未找到」指的是「視窗內未找到」，而非「不存在」。
"""

from __future__ import annotations

from collections.abc import Callable


# 二分法迭代上限。每次迭代把區間對半，故 60 次可把 0.25 日的初始區間收斂到
# 約 2e-19 日，遠低於任何實際容差；此值只是防止病態輸入造成無窮迴圈的上限。
_MAX_BISECTION_ITERATIONS = 60
# 局部極小值細掃的遞迴深度上限。每層把步長縮為 1/8，三層即 1/512，
# 對 2.5 日視窗而言是約 7 分鐘的解析度。
_MAX_REFINEMENT_DEPTH = 3
_REFINEMENT_FACTOR = 8
# 環繞／過零分辨的對半細分深度上限。每層把跨度減半，故 8 層可把單步的
# |Δf| 縮成 1/256。真實運動會隨之縮到 180 以下而被解析；真正的環繞不連續
# 則恆保持約 360 的跳躍，遞迴到底後正確地回報「無根」。
_MAX_WRAP_DISAMBIGUATION_DEPTH = 8

SOLVER_NAME = "two_stage_scan_then_bisection_v1"


def wrap_to_signed_180(degrees: float) -> float:
    """把任意角度化到 (-180, 180]。

    刻意不用 `((x + 180) % 360) - 180`：該式在 x 恰為 180 的倍數時會回傳 -180，
    使得「精確相位對面」這個點的正負號取決於浮點誤差。這裡明確把 -180 映到 +180，
    讓值域成為左開右閉的 (-180, 180]，環繞不連續點只有一個而非兩個。
    """
    wrapped = (degrees + 180.0) % 360.0 - 180.0
    if wrapped == -180.0:
        return 180.0
    return wrapped


def _bisect(
    separation_at: Callable[[float], float],
    low: float,
    high: float,
    f_low: float,
    f_high: float,
    tolerance_days: float,
) -> dict:
    """在已知變號的 [low, high] 上二分求根。

    回傳的 dict 帶著括號端點與迭代次數，因為 MTH-Q-003 乙的裁決要求
    「trace 須輸出括號端點與收斂迭代次數，使第三方能複算」。殘差
    `residual_degrees` 讓複算者能直接看出收斂品質，不必自行重跑。
    """

    bracket = {
        "low_days": low,
        "high_days": high,
        "low_offset_degrees": f_low,
        "high_offset_degrees": f_high,
    }
    iterations = 0
    for _ in range(_MAX_BISECTION_ITERATIONS):
        if high - low <= tolerance_days:
            break
        iterations += 1
        middle = 0.5 * (low + high)
        f_middle = separation_at(middle)
        if f_middle == 0.0:
            low = high = middle
            break
        if (f_low > 0.0) == (f_middle > 0.0):
            low, f_low = middle, f_middle
        else:
            high = middle
    root = 0.5 * (low + high)
    return {
        "t": root,
        "bracket": bracket,
        "iterations": iterations,
        "residual_degrees": separation_at(root),
    }


def _sign_change(f_left: float, f_right: float) -> bool:
    """兩個皆非零的端點是否異號。"""

    return (f_left > 0.0) != (f_right > 0.0)


def _is_ambiguous_span(f_left: float, f_right: float) -> bool:
    """跨度達 180 度時，無法只憑兩個端點判斷是過零還是環繞。

    f 定義在 (-180, 180] 上。從 −90 走到 +90 有兩種可能：經過 0（真的成相），
    或經過 ±180（只是環繞不連續）。兩者的 |Δf| 都恰好是 180，端點資訊不足以區分。

    舊版用嚴格的 `< 180` 判定，等於把這種情形一律當成環繞——於是
    `f(t) = −90 + 180t` 這個在 t=0.5 確實過零的函式回傳空清單
    （RT-BACKEND-9-E-003）。正確作法不是把門檻放寬成 `<= 180`（那會把真正的
    環繞誤判成根），而是**取得更多資訊**：把區間對半再取樣，直到跨度縮到
    180 以下、足以分辨為止。真實運動的 |Δf| 會隨步長等比縮小，環繞則不會。
    """

    return abs(f_left - f_right) >= 180.0


def find_crossings_detailed(
    separation_at: Callable[[float], float],
    *,
    window_days: float,
    coarse_step_days: float,
    tolerance_days: float,
    tangency_probe_degrees: float = 1.0,
    max_roots: int = 16,
    start_days: float = 0.0,
) -> list[dict]:
    """回傳 [start_days, start_days + window_days] 內所有 separation_at(t) = 0 的根紀錄。

    每筆含根的時間座標 `t`、括號端點 `bracket`、二分迭代次數 `iterations` 與收斂
    殘差 `residual_degrees`，依時間遞增排序。`window_days` 為零或負、或步長非正時
    回傳空清單，不視為錯誤——呼叫端的搜尋視窗可能因為其他條件而退化。
    """

    if window_days <= 0.0 or coarse_step_days <= 0.0:
        return []

    samples = _sample(separation_at, start_days, window_days, coarse_step_days)
    roots: list[dict] = []
    # 收集階段的上限刻意高於 `max_roots`：同一個根可能被多條路徑找到
    # （取樣點恰為根、環繞細分、相切細掃各一次），若在**去重之前**就用
    # `max_roots` 截斷，重複項會擠掉後面真正不同的根
    # （RT-BACKEND-9-E-004）。因此先寬鬆地收，去重之後才套用預算。
    # 這個上限只用來防止病態輸入把記憶體吃光，不是結果數量的政策。
    _collect(
        separation_at,
        samples,
        roots,
        tolerance_days=tolerance_days,
        tangency_probe_degrees=tangency_probe_degrees,
        collection_cap=max_roots * 4 + 64,
        depth=0,
    )
    roots.sort(key=lambda record: record["t"])

    # 相鄰根若落在同一個容差內，代表是同一個根被兩條路徑找到（例如取樣點恰為根，
    # 同時被左右兩個區間收進來），去重後才回報。
    deduped: list[dict] = []
    for record in roots:
        if not deduped or record["t"] - deduped[-1]["t"] > tolerance_days:
            deduped.append(record)
    return deduped[:max_roots]


def find_crossings(separation_at: Callable[[float], float], **kwargs) -> list[float]:
    """`find_crossings_detailed` 的便利包裝，只取根的時間座標。"""

    return [record["t"] for record in find_crossings_detailed(separation_at, **kwargs)]


def _sample(
    separation_at: Callable[[float], float],
    start: float,
    window: float,
    step: float,
) -> list[tuple[float, float]]:
    samples = []
    steps = int(window / step)
    for index in range(steps + 1):
        t = start + index * step
        if t > start + window:
            break
        samples.append((t, separation_at(t)))
    end = start + window
    if not samples or samples[-1][0] < end:
        samples.append((end, separation_at(end)))
    return samples


def _exact_sample_root(t: float) -> dict:
    """取樣點本身即為根時，仍回報同樣形狀的紀錄，讓複算格式一致。

    括號退化成一個點，這是誠實的：根就是取樣點本身，沒有做過任何收斂迭代。
    """

    return {
        "t": t,
        "bracket": {
            "low_days": t,
            "high_days": t,
            "low_offset_degrees": 0.0,
            "high_offset_degrees": 0.0,
        },
        "iterations": 0,
        "residual_degrees": 0.0,
    }


def _collect(
    separation_at: Callable[[float], float],
    samples: list[tuple[float, float]],
    roots: list[dict],
    *,
    tolerance_days: float,
    tangency_probe_degrees: float,
    collection_cap: int,
    depth: int,
) -> None:
    # --- 第一輪：取樣點本身恰為根 ---
    #
    # 這一輪必須與「相鄰區間變號」分開處理。舊版把兩者混在同一個迴圈裡，
    # 於是 f(t) = (t−1)(t−2) 以步長 1 掃描時：區間 [0,1] 因右端為零而收下
    # t=1，接著區間 [1,2] 因**左端**為零又收下一次 t=1，而 t=2 這個真正的根
    # 從頭到尾沒有被檢查過（RT-BACKEND-9-E-004）。分開兩輪之後，
    # 每個為零的取樣點只會被收一次，且不會擋住任何區間的檢查。
    for t, f_value in samples:
        if f_value == 0.0:
            roots.append(_exact_sample_root(t))

    # --- 第二輪：相鄰區間變號 ---
    for index in range(len(samples) - 1):
        if len(roots) >= collection_cap:
            return
        left_t, left_f = samples[index]
        right_t, right_f = samples[index + 1]
        if left_f == 0.0 or right_f == 0.0:
            # 已由第一輪收下；此處不得再以端點為零當成區間內另有根。
            continue
        if not _sign_change(left_f, right_f):
            continue
        if _is_ambiguous_span(left_f, right_f):
            # 端點資訊不足以分辨過零與環繞：對半細取樣再判，不用猜的。
            if depth < _MAX_WRAP_DISAMBIGUATION_DEPTH and (
                right_t - left_t
            ) > tolerance_days:
                half = (right_t - left_t) / 2.0
                _collect(
                    separation_at,
                    _sample(separation_at, left_t, right_t - left_t, half),
                    roots,
                    tolerance_days=tolerance_days,
                    tangency_probe_degrees=tangency_probe_degrees,
                    collection_cap=collection_cap,
                    depth=depth + 1,
                )
            continue
        roots.append(
            _bisect(
                separation_at,
                left_t,
                right_t,
                left_f,
                right_f,
                tolerance_days,
            )
        )

    if depth >= _MAX_REFINEMENT_DEPTH:
        return

    # 相切偵測：|f| 在內部取樣點取得局部極小且已經很小，代表該鄰域可能藏著
    # 一對步長掃不到的根。只重掃該鄰域，不重掃整個視窗。
    for index in range(1, len(samples) - 1):
        if len(roots) >= collection_cap:
            return
        previous_f = abs(samples[index - 1][1])
        current_f = abs(samples[index][1])
        next_f = abs(samples[index + 1][1])
        if current_f >= tangency_probe_degrees:
            continue
        if not (current_f < previous_f and current_f < next_f):
            continue
        left_t = samples[index - 1][0]
        right_t = samples[index + 1][0]
        # 這裡刻意**不**因為鄰域內已有根就跳過細掃。舊版有這個「省事」的條件，
        # 結果是 f(t) = (t−1)·((t−1.35)² − 0.01) 這種形狀：t=1 的根在第一輪就
        # 被收下，於是 t=1.25 與 t=1.45 這兩個藏在同一步長裡的根永遠不會被找到
        # （RT-BACKEND-9-E-004）。重複收斂同一個根是無害的——最後的去重會處理；
        # 漏掉相鄰的另一個根則是靜默的錯誤答案。
        finer_step = (right_t - left_t) / (2 * _REFINEMENT_FACTOR)
        if finer_step <= tolerance_days:
            continue
        _collect(
            separation_at,
            _sample(separation_at, left_t, right_t - left_t, finer_step),
            roots,
            tolerance_days=tolerance_days,
            tangency_probe_degrees=tangency_probe_degrees,
            collection_cap=collection_cap,
            depth=depth + 1,
        )
