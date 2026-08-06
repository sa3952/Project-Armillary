"""黃道相位：整宮配置層（doctrine）與逐度層（doctrine + 容許度）分開輸出。

本模組刻意分成兩層，因為兩層需要的方法承諾強度不同：

**第一層——整宮配置 (whole-sign configuration)。**
只看兩個星體各自落在第幾個星座，兩者相隔幾個星座。相隔 0/2/3/4/6 個星座分別
稱為 合/六分/刑/拱/沖，相隔 1/5/7/11 個星座則互不見（aversion, ἀσύνδετος）。
「相隔幾個星座」是算術；「相隔三個星座叫刑」是希臘化占星的成文教義。這一層
**不需要任何容許度裁決**，因此預設即計算。

**第二層——逐度相位 (degree-based aspect)。**
兩個星體的實際角距離距離精確相位差幾度，以及該差距是否落在容許度內。
「差幾度」是算術；**「容許度是幾度」則是一個尚未裁決的方法選擇**，
而且歷史上的數字彼此不一致。因此本模組不設預設容許度表：
未明確指定具名 `orb_profile` 或使用者固定配對門檻時，`in_orb` 一律回傳 null 並附 reason_code，
角距離等純幾何量照常輸出。憲法 0.1 條：AI 不得自行創設裁決。

**第二批擴展（Sebastian 2026-08-03，MTH-Q-014／019／020）：** 逐度層可改選
現代小相位具名集合、縮放具名七政 orb 表、或使用明示固定配對門檻；ASC／MC 可 opt-in，
但不互配、不加入整宮 doctrine、不推測 applying。現代小相位與使用者覆寫均不繼承古典來源宣稱。

**預設開啟（Sebastian 2026-08-03 裁決 MTH-Q-016）：** 本模組預設計算、可 opt-out。
方法收據仍為 `provisional_pending_method_audit`，Dossier 也仍會為此發出
`provisional_method_result` 通知——預設開啟不等於方法已獲採用。

---

## 容許度表的來源（皆為 moiety 制：兩星各有自己的 orb，相位成立條件為
   「距離精確相位的差 ≤ 兩星 orb 的一半之和」）

### ⚠ 關於證據強度的更正（RT-BACKEND-9-E-011）

本模組初版把 `abu_mashar_consensus_v1`（現已更名為 `abu_mashar_lineage_v1`）的來源標為
「**五個彼此獨立**的前現代來源一致」。**該陳述無法成立，已撤回。** 兩個理由：

1. **它們不是彼此獨立的證人，而是同一條傳承鏈。** Holden 本人即指出
   Porphyry 該章與 Sahl 之間可能存在衍生關係；Pingree 更認為第 53–55 章
   是後人竄入。中世紀阿拉伯與拉丁作者之間大量互相轉引，「同一組數字重複
   出現」證明的是**傳承**，不是**獨立驗證**。
2. **本實作未曾轉錄任何一手文本。** 下列表格與頁碼皆轉引自 Steven
   Birchfield, "Orbs of Influence"（二版 2013），一份二手彙編。
   Lilly p.107 的原頁未經獨立轉錄。

因此 provenance 改標為 `recurring_value_in_one_transmission_lineage_secondary_tabulation`，
並在 `source_verification` 欄明示「一手文本未經獨立轉錄」。
數值本身未變——被撤回的是關於**證據強度**的宣稱，不是數字。

Profile 的鍵值一併從 `abu_mashar_consensus_v1` 改為 `abu_mashar_lineage_v1`：
「consensus」一詞本身就是在說「各方一致同意」，若只改 provenance 而留著舊鍵值，
被撤回的宣稱等於還留在識別碼裡。

**證據標準（Sebastian 2026-08-03 裁決 E-011 甲）：** 只有二手彙編為依據的數值表
**可以**出貨，但必須像現在這樣在 `source_verification` 明示未轉錄一手文本。

**`abu_mashar_lineage_v1`** — Sun 15、Moon 12、Saturn 9、Jupiter 9、Mars 8、
Venus 7、Mercury 7（每顆星前後各這麼多度）。此組數字重複出現於下列來源
（依 Birchfield 彙編轉引，未逐一核對一手）：

- Porphyry, *Introduction to the Tetrabiblos* §55「行星的光芒」，trans. James
  Herschel Holden, AFA 2009：太陽 30 度（前 15 後 15）、月亮 24（12/12）、
  土星與木星 18（9/9）、火星 16（8/8）、金星與水星 14（7/7）。
- Abu Ma'shar, *The Abbreviation of the Introduction to Astrology* II.[11]–[12],
  ed./trans. Charles Burnett, ARHAT 2000。
- Sahl ibn Bishr (Zahel), *Introduction to Astrology*：稱為「光之球」(orb of light)。
- Ibn Ezra, *The Beginning of Wisdom* ch. IV, trans. Meira B. Epstein, ARHAT 1998。
- Bonatti, *Liber Astronomiae* Tract III, chs. LXV–LXXI, trans. Robert Zoller,
  Spica 1994：「凡行星之球，皆為其前與其後」。

**`lilly_1647_experience_v1`** — Saturn 10、Jupiter 12、Mars 7°30′、Sun 17、
Venus 8、Mercury 7、Moon 12°30′。出自 William Lilly, *Christian Astrology* (1647)
Book I, ch. XIX, p.107 的表格第一欄，Lilly 明言其為「the best Authors and my own
Experience」。同頁的 moiety 規則與實例亦出自此處：

> 「金星在金牛 10 度、土星在處女 18 度……因為她在兩者 orb 的 moiety 之內；
> 土星的 moiety 為 5、金星為 4，而兩者距離精確相位為 8 度。」

（5 + 4 = 9 ≥ 8，故成立。此式實作於本模組的逐對門檻 `(orb_a + orb_b) / 2`，
並以此例入測試——見 `tests/backend/test_aspects.py` 的 Lilly p.107 案例。）

**未實作的第三欄。** Lilly 同頁另列一欄註明「According to others / All consent」
（Saturn 9、Jupiter 9、Mars 7、Sun 15、Venus 8→7、Mercury 7、Moon 12）。
本模組刻意不把它做成一個 profile：它是 Lilly 對前人的轉述而非獨立來源，
且其火星值 7 與上列五個具名來源的 8 相矛盾。要採用它應是一次裁決，不是實作細節。

**未提供現代固定容許度。** 現代常見的「合 8 度、拱 8 度、刑 7 度、六分 6 度」
沒有可具名引用的權威來源，各家數字不同。憑空給一組數字並標為「現代」會是一個
可被證偽的權威宣稱，故不提供；若需要，應由裁決指定並附來源。
"""

from __future__ import annotations

import math
from collections.abc import Callable

from .root_finding import SOLVER_NAME, find_crossings_detailed, wrap_to_signed_180
from .trace import Trace


WHOLE_SIGN_METHOD_NAME = "whole_sign_configuration_v1"
DEGREE_ASPECT_METHOD_NAME = "ptolemaic_degree_aspect_moiety_v1"
EXTENDED_DEGREE_ASPECT_METHOD_NAME = "named_degree_aspect_set_with_explicit_orb_policy_v1"
APPLYING_METHOD_NAME = "relative_longitude_speed_sign_v1"
METHOD_STATUS = "provisional_pending_method_audit"

# Partile 的預設慣例。與 orb 表不同，這一項有預設值：三種慣例中「同一整數度」
# 是 Houlding 記為最通行者，也是本模組原本的行為，故沿用為預設而非留空。
# 使用者可經 `partile_profile` 改選；Sebastian 可另行裁決改預設。
DEFAULT_PARTILE_PROFILE_KEY = "same_degree_number_v1"

# 托勒密五大相位。此清單與 config.PTOLEMAIC_ASPECTS 的數值相同，但這裡需要名稱與
# 星座距離，故獨立定義；兩者的一致性由測試看守。
PTOLEMAIC_ASPECTS = (
    {"key": "conjunction", "zh": "合", "angle": 0.0, "sign_distance": 0, "classification": "classical_major"},
    {"key": "sextile", "zh": "六分", "angle": 60.0, "sign_distance": 2, "classification": "classical_major"},
    {"key": "square", "zh": "刑", "angle": 90.0, "sign_distance": 3, "classification": "classical_major"},
    {"key": "trine", "zh": "拱", "angle": 120.0, "sign_distance": 4, "classification": "classical_major"},
    {"key": "opposition", "zh": "沖", "angle": 180.0, "sign_distance": 6, "classification": "classical_major"},
)

MODERN_MINOR_ASPECTS = (
    {"key": "semisextile", "zh": "半六分", "angle": 30.0, "classification": "modern_minor"},
    {"key": "semisquare", "zh": "半刑", "angle": 45.0, "classification": "modern_minor"},
    {"key": "quintile", "zh": "五分", "angle": 72.0, "classification": "modern_minor"},
    {"key": "sesquiquadrate", "zh": "倍半刑", "angle": 135.0, "classification": "modern_minor"},
    {"key": "biquintile", "zh": "倍五分", "angle": 144.0, "classification": "modern_minor"},
    {"key": "quincunx", "zh": "梅花／補十二分", "angle": 150.0, "classification": "modern_minor"},
)

_ASPECT_BY_KEY = {
    aspect["key"]: aspect for aspect in PTOLEMAIC_ASPECTS + MODERN_MINOR_ASPECTS
}

# Sebastian 2026-08-03 本輪裁決：小相位只擴充逐度幾何層；整宮 doctrine 仍固定為
# 托勒密五相。常用 30/45/135/150 與五分相家族 72/144 分開具名，並提供一個
# 明示的聯合集合；所有現代集合都包含五大相位，避免「選小相位後反而遺失主相位」。
ASPECT_SET_PROFILES: dict[str, dict] = {
    "ptolemaic_major_v1": {
        "display_name": "托勒密五大逐度相位",
        "aspect_keys": ["conjunction", "sextile", "square", "trine", "opposition"],
        "method_classification": "classical_major_degree_aspects",
        "source": "existing_ptolemaic_degree_aspect_contract",
    },
    "modern_common_minor_v1": {
        "display_name": "五大相位＋常用幾何小相位",
        "aspect_keys": [
            "conjunction", "semisextile", "semisquare", "sextile", "square",
            "trine", "sesquiquadrate", "quincunx", "opposition",
        ],
        "method_classification": "modern_degree_aspect_geometry",
        "source": "sebastian_selected_product_profile_2026_08_03",
    },
    "modern_quintile_family_v1": {
        "display_name": "五大相位＋五分相家族",
        "aspect_keys": [
            "conjunction", "sextile", "quintile", "square", "trine",
            "biquintile", "opposition",
        ],
        "method_classification": "modern_degree_aspect_geometry",
        "source": "sebastian_selected_product_profile_2026_08_03",
    },
    "modern_minor_combined_v1": {
        "display_name": "五大相位＋常用小相位＋五分相家族",
        "aspect_keys": [
            "conjunction", "semisextile", "semisquare", "sextile", "quintile",
            "square", "trine", "sesquiquadrate", "biquintile", "quincunx",
            "opposition",
        ],
        "method_classification": "modern_degree_aspect_geometry",
        "source": "sebastian_selected_product_profile_2026_08_03",
    },
}

_SIGN_DISTANCE_TO_ASPECT = {
    aspect["sign_distance"]: aspect for aspect in PTOLEMAIC_ASPECTS
}

ORB_PROFILES: dict[str, dict] = {
    # 舊名 `abu_mashar_consensus_v1`。「consensus」一詞本身就是在說「各方一致同意」，
    # 正是 RT-BACKEND-9-E-011 撤回的那個宣稱——只改 provenance 而留著這個鍵值，
    # 等於把被撤回的宣稱留在識別碼裡。Sebastian 2026-08-03 裁決一併更名。
    "abu_mashar_lineage_v1": {
        "display_name": "Abu Ma'shar 一系傳承的重複數值",
        "rule": "moiety",
        "orbs_degrees": {
            "sun": 15.0,
            "moon": 12.0,
            "mercury": 7.0,
            "venus": 7.0,
            "mars": 8.0,
            "jupiter": 9.0,
            "saturn": 9.0,
        },
        "provenance": (
            "recurring_value_in_one_transmission_lineage_secondary_tabulation"
        ),
        "source_verification": {
            "primary_texts_independently_transcribed": False,
            "tabulated_from": (
                "Steven Birchfield, 'Orbs of Influence', 2nd ed. 2013 (secondary)"
            ),
            "independence_claim_withdrawn": (
                "Holden notes a probable derivation relationship between the "
                "Porphyry chapter and Sahl; Pingree regards chs. 53-55 as a "
                "later insertion. Repetition across medieval authors evidences "
                "transmission, not independent attestation."
            ),
            "retracted_claim": "five_independent_pre_modern_sources_in_agreement",
            "renamed_from": "abu_mashar_consensus_v1",
            "evidence_standard": "secondary_tabulation_permitted_when_disclosed",
            "ruling": "Sebastian 2026-08-03, E-011 甲",
            "finding": "RT-BACKEND-9-E-011",
        },
        "sources": [
            "Porphyry, Introduction to the Tetrabiblos §55 (trans. Holden, AFA 2009)",
            "Abu Ma'shar, Abbreviation of the Introduction to Astrology II.11-12 "
            "(ed./trans. Burnett, ARHAT 2000)",
            "Sahl ibn Bishr, Introduction to Astrology",
            "Ibn Ezra, The Beginning of Wisdom ch. IV (trans. Epstein, ARHAT 1998)",
            "Bonatti, Liber Astronomiae Tract III chs. LXV-LXXI (trans. Zoller, Spica 1994)",
        ],
    },
    "lilly_1647_experience_v1": {
        "display_name": "William Lilly 1647 自述經驗值",
        "rule": "moiety",
        "orbs_degrees": {
            "sun": 17.0,
            "moon": 12.5,
            "mercury": 7.0,
            "venus": 8.0,
            "mars": 7.5,
            "jupiter": 12.0,
            "saturn": 10.0,
        },
        "provenance": "single_named_author_stated_as_personal_experience",
        "source_verification": {
            "primary_texts_independently_transcribed": False,
            "tabulated_from": (
                "Steven Birchfield, 'Orbs of Influence', 2nd ed. 2013 (secondary), "
                "quoting Lilly p.107"
            ),
            "note": (
                "A scan of Christian Astrology exists at archive.org/details/b30338724; "
                "the page was not independently transcribed for this implementation."
            ),
            "evidence_standard": "secondary_tabulation_permitted_when_disclosed",
            "ruling": "Sebastian 2026-08-03, E-011 甲",
            "finding": "RT-BACKEND-9-E-011",
        },
        "sources": [
            "William Lilly, Christian Astrology (1647) Book I ch. XIX p.107, first column",
        ],
    },
}

# 逐度層只對「來源明確給了 orb 的星體」下容許度判斷。三王星、南北交點與阿拉伯點
# 在上列任何一個來源中都沒有 orb 數值——三王星在來源成書時尚未發現，交點與阿拉伯點
# 不是有「光芒」的星體。替它們編一個 orb 會是憑空創設，因此這些參與者的
# `in_orb` 在歷史 profile 下為 null，但角距離、入相位／出相位、成相時刻照常輸出。
# 使用者明示固定配對門檻時可以另行得到 verdict；其 source 固定是 user_override，
# 不可被描述成下列古籍替這些參與者提供了 orb。
ORB_ELIGIBLE_CATEGORIES = frozenset({"classical_planet"})

SIGN_KEYS = (
    "aries", "taurus", "gemini", "cancer", "leo", "virgo",
    "libra", "scorpio", "sagittarius", "capricorn", "aquarius", "pisces",
)
SIGN_ZH = (
    "牡羊", "金牛", "雙子", "巨蟹", "獅子", "處女",
    "天秤", "天蠍", "射手", "摩羯", "水瓶", "雙魚",
)

# 成相時刻搜尋的預設視窗與粗掃描步長。步長 0.125 日（3 小時）之下，黃經相對速度
# 最快的組合（月亮對逆行水星，約 16.8 度/日）單步最多移動約 2.1 度，遠小於
# root_finding 用來區分「真過零」與「環繞不連續」所需的 180 度界線。
DEFAULT_PERFECTION_WINDOW_DAYS = 40.0
PERFECTION_COARSE_STEP_DAYS = 0.125
# 收斂容差 1e-6 日約合 0.086 秒，遠細於出生時刻本身的不確定度。
PERFECTION_TOLERANCE_DAYS = 1e-6


def sign_index(longitude: float) -> int:
    return int(math.floor((longitude % 360.0) / 30.0)) % 12


def _sign_record(longitude: float) -> dict:
    index = sign_index(longitude)
    return {
        "sign_index": index,
        "sign_key": SIGN_KEYS[index],
        "sign_zh": SIGN_ZH[index],
        "degree_in_sign": (longitude % 360.0) - index * 30.0,
    }


def _whole_sign_configuration(lon_a: float, lon_b: float) -> dict:
    """相隔幾個星座，以及該距離對應的希臘化配置名稱。

    星座距離取兩個方向的較小者（0–6），因為配置本身無方向性：
    相隔 3 個星座與相隔 9 個星座是同一個刑。
    """

    index_a = sign_index(lon_a)
    index_b = sign_index(lon_b)
    forward = (index_b - index_a) % 12
    distance = min(forward, 12 - forward)
    aspect = _SIGN_DISTANCE_TO_ASPECT.get(distance)
    return {
        "sign_distance": distance,
        "signed_sign_distance": forward,
        "configuration_key": aspect["key"] if aspect else "aversion",
        "configuration_zh": aspect["zh"] if aspect else "不合意",
        "in_aspect": aspect is not None,
    }


def _nearest_aspect(
    separation_degrees: float,
    aspect_definitions: tuple[dict, ...] = PTOLEMAIC_ASPECTS,
) -> tuple[dict, float, bool]:
    """回傳距離最近的托勒密相位、帶號的「還差幾度」，以及是否為並列。

    五個相位角把 0–180 度切成寬度 60/30/30/60 的四段，**並非等距**，因此
    「最近的相位唯一」這句話有兩個例外：角距離恰為 30 度時，距合與距六分
    都是 30；恰為 150 度時，距拱與距沖都是 30。

    這兩點的取捨過去由 `PTOLEMAIC_ASPECTS` 的排列順序默默決定
    （RT-BACKEND-9-E-002）。行為本身是確定的，但沒有寫出來就等於沒有政策。
    現在明確回傳 `tie` 旗標，讓輸出說出「這裡有並列，我取了角度較小的那個」。
    兩個並列點都距離精確相位 30 度，遠在任何具名來源的容許度之外，
    因此取哪一個都不會改變 `in_orb`；差別只在顯示哪個名稱。
    """

    best: dict | None = None
    best_offset: float | None = None
    tie = False
    for aspect in aspect_definitions:
        offset = separation_degrees - aspect["angle"]
        if best_offset is None or abs(offset) < abs(best_offset):
            best, best_offset, tie = aspect, offset, False
        elif abs(offset) == abs(best_offset):
            tie = True
    if best is None or best_offset is None:
        # Only reachable with an empty definition set, which no caller produces.
        # Saying so here beats the TypeError the caller would otherwise raise
        # one frame later on aspect["angle"].
        raise ValueError("aspect_definitions must not be empty")
    return best, best_offset, tie


def _signed_target(delta: float, angle: float) -> float:
    """挑出 ±angle 中與目前帶號角差 `delta` 較接近者。

    合 (0°) 與沖 (180°) 兩者的 +angle 與 −angle 在 (-180, 180] 上是同一個值，
    自動退化，不需特別處理。
    """

    candidates = {wrap_to_signed_180(angle), wrap_to_signed_180(-angle)}
    return min(candidates, key=lambda target: abs(wrap_to_signed_180(delta - target)))


# Partile 的三種具名慣例（Sebastian 2026-08-03 裁決 E-012：做成 profile）。
#
# 初版只實作「同一整數度」並在 docstring 裡寫成「這是傳統定義」，屬過度宣稱
# （RT-BACKEND-9-E-012）。三種用法真的並存，且**其中兩種都出自 Lilly 本人**——
# 他在相隔三十年的兩本書裡給了互相矛盾的定義：
#
#   Christian Astrology (1647) p.107：
#       "as if Venus be in nine degrees of Aries, and Jupiter in nine degrees of
#        Leo, this is a Partill Trine aspect"        → 同一整數度
#   Merlini Anglici (1677)：
#       "A Partile Aspect comes to pass within the difference of three degrees"
#                                                     → 三度以內
#
# 第三種「1 度以內」則被記為「traditional authors have used the term partile to
# indicate an aspect that is within 1 degree of exactness」。
#
# 三者會給出不同答案：金牛 29.9° 與處女 0.1° 差距僅 0.2 度，在「1 度以內」與
# 「3 度以內」下是 partile，在「同一整數度」下不是。
#
# 來源：Deborah Houlding, Skyscript glossary 'Partile / Platick'
# （https://www.skyscript.co.uk/glossary/partile/，CC BY-NC-SA 4.0）。
# 依 E-011 甲 的裁決，二手轉引可用但必須明示：**Merlini Anglici 與 CA p.107
# 的原文本實作皆未獨立轉錄**，見各 profile 的 source_verification。
#
# Houlding 在同一條目附了一句編輯意見（建議把三度以內者稱為 'close' 而把
# partile 保留給同度者）。那是意見而非來源，故不編入任何 profile 的權威欄位。
PARTILE_PROFILES: dict[str, dict] = {
    "same_degree_number_v1": {
        "display_name": "同一整數度",
        "rule": "same_integer_degree_of_sign",
        "threshold_degrees": None,
        "provenance": "lilly_christian_astrology_1647_and_general_usage",
        "sources": [
            "William Lilly, Christian Astrology (1647) Book I p.107",
            "Deborah Houlding, Skyscript glossary 'Partile / Platick'",
        ],
        "note": (
            "Houlding 記為最通行的用法。兩星必須落在各自星座的同一個整數度，"
            "與角距離無關。"
        ),
    },
    "within_one_degree_v1": {
        "display_name": "距精確 1 度以內",
        "rule": "absolute_offset_within_threshold",
        "threshold_degrees": 1.0,
        "provenance": "unattributed_traditional_usage",
        "sources": [
            "Deborah Houlding, Skyscript glossary 'Partile / Platick'"
            "（記為 traditional authors 的用法，未點名個別作者）",
        ],
        "note": "只看距離精確相位的角差，與整數度無關。",
    },
    "lilly_1677_three_degrees_v1": {
        "display_name": "Lilly 1677：距精確 3 度以內",
        "rule": "absolute_offset_within_threshold",
        "threshold_degrees": 3.0,
        "provenance": "lilly_merlini_anglici_1677_contradicting_his_own_1647",
        "sources": [
            "William Lilly, Merlini Anglici (1677)",
            "Deborah Houlding, Skyscript glossary 'Partile / Platick'",
        ],
        "note": (
            "Lilly 本人在 1677 年改寫了自己 1647 年的定義。兩者並非同一套系統的"
            "細節差異，而是同一位作者前後不一致，故分別具名並列。"
        ),
    },
}

_PARTILE_SOURCE_VERIFICATION = {
    "primary_texts_independently_transcribed": False,
    "tabulated_from": (
        "Deborah Houlding, Skyscript glossary 'Partile / Platick' (secondary), "
        "quoting Lilly's Christian Astrology 1647 p.107 and Merlini Anglici 1677"
    ),
    "evidence_standard": "secondary_tabulation_permitted_when_disclosed",
    "ruling": "Sebastian 2026-08-03, E-011 甲 and E-012",
    "finding": "RT-BACKEND-9-E-012",
}


def _partile(lon_a: float, lon_b: float, offset_from_exact: float, profile: dict) -> bool:
    """依所選 profile 判定 partile。

    `offset_from_exact` 是距離最近精確相位的帶號角差；同度慣例不使用它，
    兩種門檻慣例則只使用它。
    """

    if profile["rule"] == "same_integer_degree_of_sign":
        return int((lon_a % 360.0) % 30.0) == int((lon_b % 360.0) % 30.0)
    return abs(offset_from_exact) <= profile["threshold_degrees"]


def _partile_reason_code(
    *,
    is_modern_minor: bool,
    partile_profile: dict,
    whole_sign: dict,
    partile: bool | None,
) -> str | None:
    """說明本組 partile 判定的性質，只在需要提醒時才給碼。

    `FPI-2026-08-06-E-006`：同度慣例在互不見的組合上也會回 True，而消費端會把它
    讀成「精確相位」。回一個具名的理由碼，比讓對方自己去比對 `whole_sign` 可靠。
    """

    if is_modern_minor:
        return "partile_profile_not_applicable_to_modern_minor_aspect"
    if partile is not True:
        return None
    if partile_profile["rule"] != "same_integer_degree_of_sign":
        return None
    if whole_sign.get("in_aspect") is True:
        return None
    # 角點組合沒有整宮教義，無法判斷是否成相；照樣提醒，因為同度制在那裡同樣不蘊含相位。
    return "same_integer_degree_without_a_whole_sign_aspect"


def _applying(offset_from_exact: float, relative_speed: float | None) -> bool | None:
    """|距離精確相位的差| 正在縮小即為入相位。

    以帶號差與相對黃經速度的乘積判斷：兩者異號代表差值朝 0 移動。乘積為零
    （速度相等，或已精確成相）時方向未定義，回傳 null 而非硬指定一邊。
    """

    if relative_speed is None:
        return None
    product = offset_from_exact * relative_speed
    if product == 0.0:
        return None
    return product < 0.0


def _perfection_times(
    *,
    participant_a: dict,
    participant_b: dict,
    signed_target: float,
    jd_ut: float,
    longitude_at: Callable[[int, float], float],
    window_days: float,
) -> list[dict]:
    # find_crossings_detailed returns crossing records, not bare times; the
    # caller reads crossing["t"]. The old list[float] annotation described
    # something this function never returned.
    body_a = participant_a.get("body_id")
    body_b = participant_b.get("body_id")
    if body_a is None or body_b is None:
        return []

    def separation_at(offset_days: float) -> float:
        moment = jd_ut + offset_days
        return wrap_to_signed_180(
            longitude_at(body_a, moment)
            - longitude_at(body_b, moment)
            - signed_target
        )

    return find_crossings_detailed(
        separation_at,
        window_days=window_days,
        coarse_step_days=PERFECTION_COARSE_STEP_DAYS,
        tolerance_days=PERFECTION_TOLERANCE_DAYS,
    )


def _orb_receipt(
    participants: list[dict],
    *,
    profile_key: str | None,
    profile: dict | None,
    orb_scale_percent: float | None,
    fixed_orb_degrees: float | None,
    angle_orb_degrees: float | None,
) -> dict:
    """Return the effective orb table independently of pair rendering.

    Pair records keep their local threshold for convenient inspection; this receipt is
    the request-level table needed to reproduce why a verdict was or was not available.
    """

    scale = (orb_scale_percent if orb_scale_percent is not None else 100.0) / 100.0
    participant_orbs = []
    for participant in participants:
        eligible = participant["category"] in ORB_ELIGIBLE_CATEGORIES
        orb = (
            profile["orbs_degrees"].get(participant["key"]) * scale
            if profile is not None and eligible
            and profile["orbs_degrees"].get(participant["key"]) is not None
            else None
        )
        if participant["category"] == "angle" and angle_orb_degrees is not None:
            reason_code = "angle_pair_threshold_recorded_separately"
        elif fixed_orb_degrees is not None:
            reason_code = "fixed_pair_threshold_has_no_participant_orbs"
        elif profile is None:
            reason_code = "orb_profile_not_selected"
        elif not eligible or orb is None:
            reason_code = "no_sourced_orb_for_participant"
        else:
            reason_code = None
        participant_orbs.append(
            {
                "key": participant["key"],
                "category": participant["category"],
                "orb_eligible": eligible,
                "orb_degrees": orb,
                "reason_code": reason_code,
            }
        )

    # profile.get("provenance") may be absent, so the chain's type is str | None
    # even though the literal branches all assign a string.
    source: str | None
    if fixed_orb_degrees is not None:
        configuration_mode = "user_fixed_pair_threshold"
        source = "user_override"
        rule = "fixed_pair_threshold"
    elif profile is not None and orb_scale_percent is not None:
        configuration_mode = "named_profile_scaled"
        source = "user_scaled_named_profile"
        rule = profile["rule"]
    elif profile is not None:
        configuration_mode = "named_profile"
        source = profile.get("provenance")
        rule = profile["rule"]
    elif angle_orb_degrees is not None:
        configuration_mode = "angle_pair_override_only"
        source = "user_override"
        rule = "angle_pair_threshold"
    else:
        configuration_mode = "none"
        source = None
        rule = None

    available = profile is not None or fixed_orb_degrees is not None
    return {
        "requested": available or angle_orb_degrees is not None,
        "executed": available or angle_orb_degrees is not None,
        "applicable": True,
        "available": available or angle_orb_degrees is not None,
        "source": source,
        "configuration_mode": configuration_mode,
        "profile_key": profile_key,
        "rule": rule,
        "scale_percent": orb_scale_percent,
        "fixed_pair_threshold_degrees": fixed_orb_degrees,
        "angle_pair_threshold_degrees": angle_orb_degrees,
        "participant_orbs": participant_orbs,
        "pair_threshold_formula": (
            "fixed_pair_threshold_degrees"
            if fixed_orb_degrees is not None
            else "(orb_a + orb_b) / 2"
            if profile is not None
            else None
        ),
        "reason_code": (
            None
            if available or angle_orb_degrees is not None
            else "orb_profile_not_selected"
        ),
        **(
            {"source_verification": dict(profile["source_verification"])}
            if profile is not None
            else {}
        ),
    }


def compute_aspects(
    participants: list[dict],
    trace: Trace,
    *,
    orb_profile_key: str | None = None,
    aspect_set_profile_key: str = "ptolemaic_major_v1",
    orb_scale_percent: float | None = None,
    fixed_orb_degrees: float | None = None,
    angle_orb_degrees: float | None = None,
    angles_requested: bool = False,
    angles_applicable: bool = True,
    angle_inapplicable_reason_code: str = (
        "angle_frame_incompatible_with_body_longitudes"
    ),
    partile_profile_key: str = DEFAULT_PARTILE_PROFILE_KEY,
    include_perfection: bool = False,
    jd_ut: float | None = None,
    longitude_at: Callable[[int, float], float] | None = None,
    perfection_window_days: float = DEFAULT_PERFECTION_WINDOW_DAYS,
) -> dict:
    """對所有參與者的兩兩組合計算整宮配置與逐度相位。

    `participants` 的每一筆需要 key、name、longitude、category，
    可選 speed_longitude（缺少時入相位／出相位為 null）與 body_id
    （缺少時不計算成相時刻——阿拉伯點沒有對應的星曆天體）。
    """

    profile = ORB_PROFILES.get(orb_profile_key) if orb_profile_key else None
    if orb_scale_percent is not None and profile is None:
        raise ValueError("orb_scale_percent requires a named orb profile")
    if fixed_orb_degrees is not None and profile is not None:
        raise ValueError("fixed pair orb and named orb profile are mutually exclusive")
    aspect_set_profile = ASPECT_SET_PROFILES.get(aspect_set_profile_key)
    if aspect_set_profile is None:
        raise ValueError(f"unknown aspect set profile: {aspect_set_profile_key}")
    aspect_definitions = tuple(
        _ASPECT_BY_KEY[key] for key in aspect_set_profile["aspect_keys"]
    )
    degree_method_name = (
        DEGREE_ASPECT_METHOD_NAME
        if aspect_set_profile_key == "ptolemaic_major_v1"
        and fixed_orb_degrees is None
        and angle_orb_degrees is None
        else EXTENDED_DEGREE_ASPECT_METHOD_NAME
    )
    partile_profile = PARTILE_PROFILES.get(partile_profile_key)
    if partile_profile is None:
        raise ValueError(f"unknown partile profile: {partile_profile_key}")
    usable = [
        participant
        for participant in participants
        if participant.get("longitude") is not None
    ]
    skipped = [
        participant["key"]
        for participant in participants
        if participant.get("longitude") is None
    ]

    receipt: dict = {
        # 頂層的 method 三欄位讓本模組與其餘 derived_methods 具有相同形狀，
        # dossier 的 methodology receipt 才能一視同仁地收錄。名稱取整宮層，
        # 因為那是預設就會產生判斷的那一層；逐度層的方法名另見 degree_based。
        "method": WHOLE_SIGN_METHOD_NAME,
        "method_status": METHOD_STATUS,
        "method_authority": None,
        "whole_sign": {
            "method": WHOLE_SIGN_METHOD_NAME,
            "method_status": METHOD_STATUS,
            "method_authority": None,
            "method_provenance": "hellenistic_whole_sign_configuration_doctrine",
            "requires_orb": False,
        },
        "degree_based": {
            "method": degree_method_name,
            "method_status": METHOD_STATUS,
            "method_authority": None,
            "aspect_set_profile": aspect_set_profile_key,
            "aspect_set_profile_detail": dict(aspect_set_profile),
            "aspect_set_profiles_available": sorted(ASPECT_SET_PROFILES),
            "aspect_keys": list(aspect_set_profile["aspect_keys"]),
            "partile_profile": partile_profile_key,
            "partile_profile_detail": dict(partile_profile),
            "partile_source_verification": dict(_PARTILE_SOURCE_VERIFICATION),
            "partile_profiles_available": sorted(PARTILE_PROFILES),
            "partile_note": (
                "三種慣例並存且會給出不同答案，其中兩種都出自 Lilly 本人"
                "（1647 同度、1677 三度以內）。預設採最通行的同度慣例，可經 "
                "partile_profile 改選。"
            ),
            "application_method": APPLYING_METHOD_NAME,
            "orb_profile": orb_profile_key,
            "orb_profile_detail": (
                {
                    key: value
                    for key, value in profile.items()
                    if key != "orbs_degrees"
                }
                | {"orbs_degrees": dict(profile["orbs_degrees"])}
                if profile
                else None
            ),
            "orb_verdict_available": (
                profile is not None
                or fixed_orb_degrees is not None
                or angle_orb_degrees is not None
            ),
            "orb_unavailable_reason_code": (
                None
                if profile is not None
                or fixed_orb_degrees is not None
                or angle_orb_degrees is not None
                else "orb_profile_not_selected"
            ),
            "orb_receipt": _orb_receipt(
                usable,
                profile_key=orb_profile_key,
                profile=profile,
                orb_scale_percent=orb_scale_percent,
                fixed_orb_degrees=fixed_orb_degrees,
                angle_orb_degrees=angle_orb_degrees,
            ),
            "angle_participation": {
                "requested": angles_requested or any(
                    item["category"] == "angle" for item in participants
                ),
                "executed": any(item["category"] == "angle" for item in usable),
                "applicable": angles_applicable,
                "available": any(item["category"] == "angle" for item in usable),
                "source": (
                    "astronomical_data.angles.asc_and_mc"
                    if any(item["category"] == "angle" for item in usable)
                    else None
                ),
                "participant_keys": [
                    item["key"] for item in usable if item["category"] == "angle"
                ],
                "angle_angle_pairs_included": False,
                "applying_semantics": "not_applicable_angle_motion_not_modeled",
                "angle_pair_orb_degrees": angle_orb_degrees,
                "angle_pair_orb_source": (
                    "user_override" if angle_orb_degrees is not None else None
                ),
                "reason_code": (
                    None
                    if angles_applicable
                    else angle_inapplicable_reason_code
                ),
            },
        },
        "perfection": {
            "requested": include_perfection,
            "solver": SOLVER_NAME if include_perfection else None,
            "search_window_days": (
                perfection_window_days if include_perfection else None
            ),
            "semantics": (
                "forward_search_from_chart_moment_bounded_by_search_window"
            ),
        },
        "participants": [
            {
                "key": participant["key"],
                "name": participant["name"],
                "category": participant["category"],
                "orb_eligible": participant["category"] in ORB_ELIGIBLE_CATEGORIES,
                **_sign_record(participant["longitude"]),
            }
            for participant in usable
        ],
        "excluded_participant_keys": skipped,
        "pairs": [],
    }

    if orb_profile_key and profile is None:
        # 未知的 profile 名稱不靜默退回無容許度模式：那會讓使用者以為自己選到的表
        # 生效了。呼叫端已由 schema 的 Literal 擋下，此處是深度防禦。
        raise ValueError(f"unknown orb profile: {orb_profile_key}")

    for index, participant_a in enumerate(usable):
        for participant_b in usable[index + 1:]:
            if (
                participant_a["category"] == "angle"
                and participant_b["category"] == "angle"
            ):
                continue
            receipt["pairs"].append(
                _pair_record(
                    participant_a,
                    participant_b,
                    profile=profile,
                    aspect_definitions=aspect_definitions,
                    orb_scale_percent=orb_scale_percent,
                    fixed_orb_degrees=fixed_orb_degrees,
                    angle_orb_degrees=angle_orb_degrees,
                    partile_profile=partile_profile,
                    include_perfection=include_perfection,
                    jd_ut=jd_ut,
                    longitude_at=longitude_at,
                    perfection_window_days=perfection_window_days,
                )
            )

    _trace_summary(receipt, trace, orb_profile_key=orb_profile_key)
    return receipt


def _pair_record(
    participant_a: dict,
    participant_b: dict,
    *,
    profile: dict | None,
    aspect_definitions: tuple[dict, ...],
    orb_scale_percent: float | None,
    fixed_orb_degrees: float | None,
    angle_orb_degrees: float | None,
    partile_profile: dict,
    include_perfection: bool,
    jd_ut: float | None,
    longitude_at: Callable[[int, float], float] | None,
    perfection_window_days: float,
) -> dict:
    lon_a = participant_a["longitude"]
    lon_b = participant_b["longitude"]
    delta = wrap_to_signed_180(lon_a - lon_b)
    separation = abs(delta)
    aspect, offset, nearest_is_tied = _nearest_aspect(separation, aspect_definitions)

    speed_a = participant_a.get("speed_longitude")
    speed_b = participant_b.get("speed_longitude")
    relative_speed = (
        speed_a - speed_b
        if speed_a is not None and speed_b is not None
        else None
    )
    signed_target = _signed_target(delta, aspect["angle"])
    signed_offset = wrap_to_signed_180(delta - signed_target)

    orb_a = orb_b = None
    in_orb: bool | None = None
    reason_code: str | None = None
    contains_angle = (
        participant_a["category"] == "angle"
        or participant_b["category"] == "angle"
    )
    is_modern_minor = aspect["classification"] == "modern_minor"
    pair_orb_threshold = None
    orb_rule = None
    scale = (orb_scale_percent if orb_scale_percent is not None else 100.0) / 100.0
    if contains_angle and angle_orb_degrees is not None:
        pair_orb_threshold = angle_orb_degrees
        orb_rule = "user_angle_pair_threshold"
        in_orb = abs(offset) <= pair_orb_threshold
    elif contains_angle:
        reason_code = "angle_orb_not_selected"
    elif fixed_orb_degrees is not None:
        pair_orb_threshold = fixed_orb_degrees
        orb_rule = "user_fixed_pair_threshold"
        in_orb = abs(offset) <= pair_orb_threshold
    elif profile is None:
        reason_code = "orb_profile_not_selected"
    elif is_modern_minor:
        reason_code = "historical_orb_profile_not_applicable_to_modern_minor_aspect"
    else:
        eligible_a = participant_a["category"] in ORB_ELIGIBLE_CATEGORIES
        eligible_b = participant_b["category"] in ORB_ELIGIBLE_CATEGORIES
        raw_orb_a = profile["orbs_degrees"].get(participant_a["key"])
        raw_orb_b = profile["orbs_degrees"].get(participant_b["key"])
        orb_a = raw_orb_a * scale if raw_orb_a is not None else None
        orb_b = raw_orb_b * scale if raw_orb_b is not None else None
        if not eligible_a or not eligible_b or orb_a is None or orb_b is None:
            reason_code = "no_sourced_orb_for_participant"
        else:
            pair_orb_threshold = (orb_a + orb_b) / 2.0
            orb_rule = "scaled_moiety" if orb_scale_percent is not None else "moiety"
            in_orb = abs(offset) <= pair_orb_threshold

    applying = None if contains_angle else _applying(signed_offset, relative_speed)

    whole_sign = (
        {
            "applicable": False,
            "reason_code": "angle_not_a_whole_sign_doctrine_participant",
        }
        if contains_angle
        else _whole_sign_configuration(lon_a, lon_b)
    )
    partile = (
        None if is_modern_minor else _partile(lon_a, lon_b, offset, partile_profile)
    )

    record = {
        "body_a": participant_a["key"],
        "body_a_name": participant_a["name"],
        "body_b": participant_b["key"],
        "body_b_name": participant_b["name"],
        "separation_degrees": separation,
        "whole_sign": whole_sign,
        "nearest_aspect": {
            "key": aspect["key"],
            "zh": aspect["zh"],
            "exact_angle_degrees": aspect["angle"],
            "offset_from_exact_degrees": offset,
            "signed_offset_from_exact_degrees": signed_offset,
            # 角距離恰為 30 或 150 度時，兩個相位等距。旗標與規則一併輸出，
            # 讓消費端知道這是一個並列而非唯一解。
            "is_tied": nearest_is_tied,
            "tie_rule": (
                "smaller_exact_angle_wins" if nearest_is_tied else None
            ),
        },
        "in_orb": in_orb,
        "in_orb_reason_code": reason_code,
        "pair_orb_threshold_degrees": pair_orb_threshold,
        "orb_rule": orb_rule,
        "moiety_sum_degrees": (
            (orb_a + orb_b) / 2.0
            if orb_a is not None and orb_b is not None
            else None
        ),
        "orb_degrees": {
            participant_a["key"]: orb_a,
            participant_b["key"]: orb_b,
        },
        "partile": partile,
        "partile_rule": None if is_modern_minor else partile_profile["rule"],
        # `FPI-2026-08-06-E-006`。同度慣例只看兩星是否落在各自星座的同一個整數度，
        # **與角距離無關**，所以任兩個同整數度的星體（約 1/12 機率）都會被標記，
        # 包括教義上互不見的組合——例如牡羊 10 度與金牛 10 度，整宮上是不合意。
        # 消費端把 `partile: true` 讀成「精確相位」是很自然的誤讀。
        #
        # 「partile 是否應以成相為前置條件」是古典方法定義問題，不是工程問題
        # （`AGENTS.md` §7）。這裡不改判定，只讓輸出說出它到底宣稱了什麼：
        # 兩個門檻制 profile 由「距最近精確相位的偏差」導出，蘊含相位；
        # 同度制不蘊含。若 Sebastian 日後裁決加上前置條件，改的是 `_partile`，
        # 本欄仍然成立。
        "partile_rule_implies_aspect": (
            None
            if is_modern_minor
            else partile_profile["rule"] != "same_integer_degree_of_sign"
        ),
        "partile_reason_code": _partile_reason_code(
            is_modern_minor=is_modern_minor,
            partile_profile=partile_profile,
            whole_sign=whole_sign,
            partile=partile,
        ),
        "applying": applying,
        "applying_reason_code": (
            "angle_motion_not_modeled" if contains_angle else None
        ),
        "relative_longitude_speed": relative_speed,
        "perfection": None,
    }

    # Written as one condition rather than a `should_search` flag so the
    # not-None checks actually narrow for the call below. The flag form was
    # correct at runtime and unprovable to a reader or a checker.
    if (
        include_perfection
        and jd_ut is not None
        and longitude_at is not None
        and applying is True
        and in_orb is not False
    ):
        crossings = _perfection_times(
            participant_a=participant_a,
            participant_b=participant_b,
            signed_target=signed_target,
            jd_ut=jd_ut,
            longitude_at=longitude_at,
            window_days=perfection_window_days,
        )
        times = [crossing["t"] for crossing in crossings]
        record["perfection"] = {
            "solver": SOLVER_NAME,
            "search_window_days": perfection_window_days,
            "crossing_count": len(times),
            "days_from_chart_moment": times,
            "hours_from_chart_moment": [value * 24.0 for value in times],
            "julian_day_ut": [jd_ut + value for value in times],
            # 括號端點與迭代次數一併輸出，第三方可據以複算（MTH-Q-003 乙的要求，
            # 該裁決針對 VOC，但同一個求根器在此也適用同樣的可複算標準）。
            "solver_evidence": [
                {
                    "bracket": crossing["bracket"],
                    "bisection_iterations": crossing["iterations"],
                    "residual_degrees": crossing["residual_degrees"],
                }
                for crossing in crossings
            ],
            "not_found_reason_code": (
                None if times else "no_perfection_within_search_window"
            ),
        }
    return record


def _trace_summary(receipt: dict, trace: Trace, *, orb_profile_key: str | None) -> None:
    whole_sign_hits = [
        pair
        for pair in receipt["pairs"]
        if pair["whole_sign"].get("in_aspect") is True
    ]
    trace.add(
        f"整宮配置 (method={WHOLE_SIGN_METHOD_NAME})",
        formula="星座距離 = min((星座B − 星座A) mod 12, 12 − 同值)；0/2/3/4/6 → 合/六分/刑/拱/沖，其餘不合意",
        inputs={"參與者數": len(receipt["participants"])},
        result={
            "組合總數": len(receipt["pairs"]),
            "成整宮相位者": len(whole_sign_hits),
        },
        note="此層不需容許度：星座距離是算術，配置名稱是希臘化占星的成文教義。",
    )

    orb_receipt = receipt["degree_based"]["orb_receipt"]
    if not orb_receipt["available"]:
        trace.add(
            f"逐度相位 (method={DEGREE_ASPECT_METHOD_NAME})",
            formula="角距離與距離精確相位的差為純幾何量；是否成立需要容許度表",
            note="⚠ 未指定 orb_profile，故 in_orb 全部回傳 null。角距離、入相位／出相位仍為有效輸出。"
                 "容許度表的選擇屬方法裁決，本程式不自行指定。",
        )
        return

    in_orb_hits = [pair for pair in receipt["pairs"] if pair["in_orb"] is True]
    trace.add(
        f"逐度相位 (method={receipt['degree_based']['method']}, orb_configuration={orb_receipt['configuration_mode']})",
        formula=(
            "成立條件由 degree_based.orb_receipt.pair_threshold_formula 與每一 pair 的 "
            "pair_orb_threshold_degrees 完整記錄"
        ),
        inputs={
            "orb收據": orb_receipt,
            "逐度相位集合": receipt["degree_based"]["aspect_set_profile"],
        },
        result={
            "成逐度相位者": len(in_orb_hits),
            "明細": [
                f"{pair['body_a_name']}{pair['nearest_aspect']['zh']}"
                f"{pair['body_b_name']}（差 {pair['nearest_aspect']['offset_from_exact_degrees']:+.4f}°"
                f"，{'入' if pair['applying'] else '出' if pair['applying'] is False else '方向未定義'}相位）"
                for pair in in_orb_hits
            ],
        },
        note="orb 數值出自具名歷史來源，見本模組 docstring；method_status 仍為待審閱，"
             "不表示本產品已採用該表為正式技法。",
    )
