"""出生時間相關轉換：本地時間 -> UTC -> 儒略日(UT/ET)，以及 ΔT、恆星時、黃赤交角、均時差。"""

import datetime as dtmod
from zoneinfo import ZoneInfo

import swisseph as swe

from .trace import Trace


class NonexistentLocalTimeError(ValueError):
    """Raised when an IANA-zone wall-clock time has no corresponding UTC instant."""

    code = "nonexistent_local_time"


class AmbiguousLocalTimeChoiceRequiredError(ValueError):
    """Raised when an approximate repeated civil hour lacks an explicit fold."""

    code = "ambiguous_local_time_choice_required"


def _required_utc_offset_hours(value: dtmod.datetime) -> float:
    offset = value.utcoffset()
    if offset is None:
        raise RuntimeError("timezone-aware datetime has no UTC offset")
    return offset.total_seconds() / 3600.0


def _to_utc_datetime(
    dt_input,
    tz_input,
    *,
    require_explicit_fold: bool = False,
):
    # 用 timedelta 疊加秒數（而非手動 round 微秒），讓 59.9999999 這類合法邊界值
    # 正確進位到下一分鐘，不會產生 microsecond=1000000 這種非法值。
    naive = dtmod.datetime(
        dt_input.year, dt_input.month, dt_input.day,
        dt_input.hour, dt_input.minute, 0, 0,
    ) + dtmod.timedelta(seconds=dt_input.second)

    dst_warning = None

    if tz_input.mode == "iana":
        tz = ZoneInfo(tz_input.iana_name)
        fold = tz_input.fold
        local_dt = naive.replace(tzinfo=tz, fold=fold)
        utc_dt = local_dt.astimezone(dtmod.timezone.utc)
        offset_hours = _required_utc_offset_hours(local_dt)
        tz_label = tz_input.iana_name

        # PEP 495：同一本地時間在兩個 fold 下若解出不同 UTC 偏移，代表這個本地時間
        # 落在 DST 轉換附近（模糊或不存在），需要進一步分辨是哪一種、並讓使用者知道。
        alt_local_dt = naive.replace(tzinfo=tz, fold=1 - fold)
        offsets_differ = local_dt.utcoffset() != alt_local_dt.utcoffset()

        if offsets_differ:
            # 把選定 fold 算出的 UTC 轉回本地時間；若對不回原本輸入的「完整 datetime」，
            # 代表這個本地時間根本不存在（DST 春季跳錶的空隙），而非單純模糊。
            # 必須比對完整 datetime 而非只比 (hour, minute)：像 Pacific/Apia 2011-12-30
            # 這種整天被跳過的跨換日線變更，時分會恰好相同、只有日期差一天，
            # 只比時分會把「不存在」誤判成「模糊」並給出錯誤的 fold 建議。
            round_trip = utc_dt.astimezone(tz)
            if round_trip.replace(tzinfo=None) != naive:
                alternative_utc = alt_local_dt.astimezone(dtmod.timezone.utc)
                alternative_round_trip = alternative_utc.astimezone(tz)
                raise NonexistentLocalTimeError(
                    f"此本地時間在 {tz_label} 因日光節約時間或時區規則變更而不存在（該時鐘從未指向 "
                    f"{naive.strftime('%Y-%m-%d %H:%M')}）。fold=0 與 fold=1 只會強制正規化成 "
                    f"{round_trip.strftime('%Y-%m-%d %H:%M %Z')} 或 "
                    f"{alternative_round_trip.strftime('%Y-%m-%d %H:%M %Z')}，兩者都不是原輸入。"
                    "請改正出生時間；若出生紀錄明確記載 UTC 偏移，才改用 fixed_offset。"
                )
            else:
                if (
                    require_explicit_fold
                    and "fold" not in tz_input.model_fields_set
                ):
                    raise AmbiguousLocalTimeChoiceRequiredError(
                        f"{tz_label} 的這個民用小時重複兩次；approximate_hour 必須明確"
                        "提供 timezone.fold=0 或 1，不能由系統替使用者猜測。"
                    )
                alt_offset_hours = _required_utc_offset_hours(alt_local_dt)
                dst_warning = (
                    f"此本地時間在 {tz_label} 為日光節約時間轉換造成的模糊時刻（同一本地時間對應兩個不同的 "
                    f"UTC 時刻，秋季調慢時會重複一次）。已採用 fold={fold}，對應 UTC{offset_hours:+.2f}；"
                    f"另一種解讀為 UTC{alt_offset_hours:+.2f}。如果您知道確切是哪一次，"
                    f"請在請求中指定 timezone.fold={1 - fold} 以取得另一種結果。"
                )
    else:
        offset_hours = tz_input.utc_offset_hours
        local_dt = naive.replace(tzinfo=dtmod.timezone(dtmod.timedelta(hours=offset_hours)))
        utc_dt = local_dt.astimezone(dtmod.timezone.utc)
        tz_label = f"UTC{offset_hours:+.2f}"

    return utc_dt, offset_hours, tz_label, dst_warning


def compute_time_conversion(
    dt_input,
    tz_input,
    location,
    trace: Trace,
    *,
    require_explicit_fold: bool = False,
) -> dict:
    utc_dt, offset_hours, tz_label, dst_warning = _to_utc_datetime(
        dt_input,
        tz_input,
        require_explicit_fold=require_explicit_fold,
    )

    if dst_warning:
        trace.add("⚠ 日光節約時間(DST)警告", note=dst_warning)

    utc_sec = utc_dt.second + utc_dt.microsecond / 1_000_000
    jd_et, jd_ut = swe.utc_to_jd(
        utc_dt.year, utc_dt.month, utc_dt.day,
        utc_dt.hour, utc_dt.minute, utc_sec,
        swe.GREG_CAL,
    )

    trace.add(
        "本地時間 -> UTC",
        formula="UTC = 本地時間 - 時區偏移",
        inputs={
            "本地時間": f"{dt_input.year:04d}-{dt_input.month:02d}-{dt_input.day:02d} "
                       f"{dt_input.hour:02d}:{dt_input.minute:02d}:{dt_input.second:05.2f}",
            "時區": tz_label,
            "時區偏移(小時)": round(offset_hours, 4),
        },
        result={"UTC時間": utc_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]},
    )

    trace.add(
        "UTC -> 儒略日 (swe.utc_to_jd)",
        formula="JD_UT, JD_ET = swe.utc_to_jd(UTC年,月,日,時,分,秒, 格里曆)",
        inputs={"UTC": utc_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]},
        result={"JD(UT)": jd_ut, "JD(ET/TT)": jd_et},
    )

    delta_t_seconds = (jd_et - jd_ut) * 86400.0
    trace.add(
        "地球時與世界時之差 ΔT",
        formula="ΔT(秒) = (JD_ET - JD_UT) * 86400",
        inputs={"JD(ET)": jd_et, "JD(UT)": jd_ut},
        result={"ΔT(秒)": round(delta_t_seconds, 4)},
    )

    ecl_nut, retflag = swe.calc_ut(jd_ut, swe.ECL_NUT, 0)
    true_obliquity, mean_obliquity, nut_lon, nut_obl = ecl_nut[0], ecl_nut[1], ecl_nut[2], ecl_nut[3]
    trace.add(
        "黃赤交角與章動 (swe.calc_ut ECL_NUT)",
        formula="ECL_NUT 回傳 [真黃赤交角, 平黃赤交角, 黃經章動, 交角章動]",
        inputs={"JD(UT)": jd_ut},
        result={
            "真黃赤交角ε(度)": true_obliquity,
            "平黃赤交角(度)": mean_obliquity,
            "黃經章動(度)": nut_lon,
            "交角章動(度)": nut_obl,
        },
    )

    # swe.sidtime() 包含章動，回傳的是 Greenwich apparent sidereal time (GAST)，
    # 不是名稱含混的「GST」。平均恆星時則以 sidtime0(..., mean_obliquity, 0)
    # 明確移除章動。保留 gst_hours/lst_hours 作舊 API alias，但它們的語義固定為
    # apparent，不能在未改版的情況下偷偷改成 mean。
    gast_hours = swe.sidtime(jd_ut)
    gmst_hours = swe.sidtime0(jd_ut, mean_obliquity, 0.0)
    last_hours = (gast_hours + location.longitude / 15.0) % 24.0
    lmst_hours = (gmst_hours + location.longitude / 15.0) % 24.0
    trace.add(
        "平均／視恆星時 (Sidereal Time)",
        formula=(
            "GAST = swe.sidtime(JD_UT)；GMST = swe.sidtime0(JD_UT, 平黃赤交角, 0)；"
            "LAST/LMST = GAST/GMST + 東經/15"
        ),
        inputs={"JD(UT)": jd_ut, "地理經度(東正西負)": location.longitude},
        result={
            "GAST(小時)": gast_hours,
            "GMST(小時)": gmst_hours,
            "LAST(小時)": last_hours,
            "LMST(小時)": lmst_hours,
        },
        note="舊欄位 gst_hours/lst_hours 為向後相容 alias，固定等同 GAST/LAST。",
    )

    eq_of_time_days = swe.time_equ(jd_ut)
    eq_of_time_minutes = eq_of_time_days * 1440.0
    lmt_offset_hours = location.longitude / 15.0
    apparent_solar_offset_hours = lmt_offset_hours + eq_of_time_minutes / 60.0
    true_solar_dt = utc_dt + dtmod.timedelta(hours=apparent_solar_offset_hours)
    trace.add(
        "均時差與真太陽時（僅供參考，不影響本次計算）",
        formula="真太陽時 = UTC + 經度/15(小時) + 均時差；均時差 = swe.time_equ(JD_UT) * 1440 分鐘",
        inputs={"JD(UT)": jd_ut, "地理經度": location.longitude},
        result={
            "均時差(分鐘)": round(eq_of_time_minutes, 4),
            "真太陽時(參考)": true_solar_dt.strftime("%Y-%m-%d %H:%M:%S"),
        },
        note="此為輔助資訊，出生時刻仍以使用者輸入之時鐘時間為準",
    )

    return {
        "input_local_time": f"{dt_input.year:04d}-{dt_input.month:02d}-{dt_input.day:02d} "
                             f"{dt_input.hour:02d}:{dt_input.minute:02d}:{dt_input.second:05.2f}",
        "timezone_label": tz_label,
        "utc_offset_hours": offset_hours,
        "utc_time": utc_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "jd_ut": jd_ut,
        "jd_et": jd_et,
        "delta_t_seconds": delta_t_seconds,
        "gast_hours": gast_hours,
        "gmst_hours": gmst_hours,
        "last_hours": last_hours,
        "lmst_hours": lmst_hours,
        "gst_hours": gast_hours,
        "lst_hours": last_hours,
        "true_obliquity": true_obliquity,
        "mean_obliquity": mean_obliquity,
        "nutation_longitude": nut_lon,
        "nutation_obliquity": nut_obl,
        "equation_of_time_minutes": eq_of_time_minutes,
        "apparent_solar_time": true_solar_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "ecl_nut_retflag": retflag,
        "dst_warning": dst_warning,
    }
