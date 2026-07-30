"""數值格式化輔助：度分秒 (DMS)、時分秒 (HMS) 字串表示，以及方位角座標慣例轉換。"""


def swiss_azimuth_to_standard(az_south_based: float) -> float:
    """swe.azalt() 回傳的方位角是「以南點為 0°、向西遞增」的天文傳統慣例
    （實測驗證：冬至真太陽正午時 swe.azalt 回傳 ~0°，而非慣用的 ~180°）。
    這裡轉換成一般地圖/羅盤慣用的「北點為 0°、順時針向東遞增」，避免使用者誤讀。"""
    return (az_south_based + 180.0) % 360.0


def to_dms(value_degrees: float, signed: bool = False, wrap_360: bool = False) -> str:
    """將十進位度數轉換為度分秒字串。signed=True 時前面加上 +/- 符號（緯度/赤緯用）。

    先四捨五入到「百分之一角秒」的整數，再用 divmod 逐級進位，而不是分別對度/分/秒
    三個浮點數各自四捨五入——後者在秒數接近 60 時會顯示成「29°59'60.00"」而不是
    正確進位後的「30°00'00.00"」。

    wrap_360=True 時（黃經、赤經這類 0–360 循環量）會在進位後再對 360° 取餘數，
    否則 359.9999999 會進位成不存在的「360°00'00.00"」——這是同一類進位錯誤只是
    發生在最上層。緯度/赤緯是 ±90 的有界量，不該套用環繞，故預設關閉。
    """
    v = abs(value_degrees)
    total_centi_arcsec = round(v * 3600 * 100)
    if wrap_360:
        total_centi_arcsec %= 360 * 3600 * 100
    total_arcsec, centi = divmod(total_centi_arcsec, 100)
    d, rem = divmod(total_arcsec, 3600)
    m, s = divmod(rem, 60)
    # 符號依「進位後」是否真的非零決定：極小的負值（如 -1e-7）進位後就是 0，
    # 這時再冠上負號會變成沒有意義的「-0°00'00.00"」，讓人誤以為有方向性。
    is_zero = total_centi_arcsec == 0
    sign = "" if is_zero and not signed else ("-" if value_degrees < 0 and not is_zero else ("+" if signed else ""))
    return f"{sign}{d}°{m:02d}'{s:02d}.{centi:02d}\""


def to_hms(ra_degrees: float) -> str:
    """將赤經(度)轉換為時分秒字串。同 to_dms，先進位到整數百分之一秒再逐級 divmod，
    避免顯示出「60.00s」這種非法進位。"""
    hours_total = (ra_degrees % 360.0) / 15.0
    total_centi_sec = round(hours_total * 3600 * 100)
    total_sec, centi = divmod(total_centi_sec, 100)
    h, rem = divmod(total_sec, 3600)
    h %= 24
    m, s = divmod(rem, 60)
    return f"{h:02d}h{m:02d}m{s:02d}.{centi:02d}s"
