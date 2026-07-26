(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ClientContext = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PROFILES = Object.freeze({
    LOCAL: "local",
    PRIVATE_ALPHA: "private_alpha",
  });

  const FIELD_LABELS = Object.freeze({
    datetime: "出生日期時間",
    year: "年份",
    month: "月份",
    day: "日期",
    hour: "小時",
    minute: "分鐘",
    second: "秒數",
    timezone: "時區",
    iana_name: "IANA 時區名稱",
    utc_offset_hours: "UTC 偏移",
    location: "出生地點",
    latitude: "緯度",
    longitude: "經度",
    altitude_m: "海拔",
    atmosphere: "大氣設定",
    pressure_hpa: "氣壓",
    temperature_c: "溫度",
  });

  const ERROR_CODE_MESSAGES = Object.freeze({
    nonexistent_local_time: (
      "這個本地時間在所選時區不存在；請核對出生紀錄，或在確知 UTC 偏移時改用固定偏移。"
    ),
    house_system_unavailable: (
      "所選宮位制無法在這個緯度計算；請改選 Whole Sign 或其他可用宮位制。"
    ),
    full_ephemeris_required: (
      "這次計算超出目前完整 Swiss Ephemeris 資料範圍；服務已拒絕改用較低精度替代。"
    ),
    unsupported_media_type: "送出的資料格式不受支援，請重新載入頁面後再試。",
    invalid_content_length: "送出的資料長度資訊無效，請重新載入頁面後再試。",
    request_body_too_large: "送出的資料超過服務允許大小，請重新載入頁面後再試。",
    swiss_ephemeris_error: (
      "天文計算服務暫時無法完成這次計算；請稍後重試。"
    ),
    internal_server_error: (
      "服務暫時無法完成計算。請稍後重試；若持續發生，請聯絡服務管理者。"
    ),
  });

  function validateClientConfiguration(payload) {
    if (!payload || typeof payload !== "object") {
      throw new Error("client configuration 不是物件");
    }
    if (!Object.values(PROFILES).includes(payload.profile)) {
      throw new Error("client configuration 含有不支援的 profile");
    }
    return Object.freeze({ profile: payload.profile });
  }

  function fieldLabel(location) {
    if (!Array.isArray(location)) return "輸入資料";
    for (let index = location.length - 1; index >= 0; index -= 1) {
      const key = String(location[index]);
      if (FIELD_LABELS[key]) return FIELD_LABELS[key];
    }
    return "輸入資料";
  }

  function validationGuidance(issue) {
    const location = Array.isArray(issue && issue.loc) ? issue.loc : [];
    const locationKeys = new Set(location.map(String));
    const label = fieldLabel(location);
    const type = issue && typeof issue.type === "string" ? issue.type : "";

    if (type === "missing") return `${label}尚未填寫。`;
    if (type === "extra_forbidden") {
      return "頁面送出了目前版本不支援的欄位，請重新載入後再試。";
    }
    if (locationKeys.has("timezone") || locationKeys.has("iana_name")) {
      return "時區名稱無效；請使用例如 Asia/Taipei 的 IANA 時區名稱，或改選固定 UTC 偏移。";
    }
    if (locationKeys.has("datetime")) {
      return "出生日期時間無效；請確認日期確實存在，且時間數字在允許範圍內。";
    }
    if (locationKeys.has("latitude")) {
      return "緯度無效；請輸入 -90 到 90 的十進位度數。";
    }
    if (locationKeys.has("longitude")) {
      return "經度無效；請輸入 -180 到 180 的十進位度數。";
    }
    if (
      type.includes("greater_than")
      || type.includes("less_than")
      || type.includes("finite_number")
    ) {
      return `${label}超出允許範圍；請檢查欄位旁的格式說明。`;
    }
    return `${label}格式無效；請檢查後再試。`;
  }

  function formatApiError(detail, statusCode) {
    if (Array.isArray(detail) && detail.length > 0) {
      return validationGuidance(detail[0]);
    }
    if (
      detail
      && typeof detail === "object"
      && typeof detail.code === "string"
      && ERROR_CODE_MESSAGES[detail.code]
    ) {
      return ERROR_CODE_MESSAGES[detail.code];
    }
    if (statusCode === 413) return "送出的資料超過服務允許大小，請重新載入頁面後再試。";
    if (statusCode === 415) return "送出的資料格式不受支援，請重新載入頁面後再試。";
    if (statusCode === 422) return "部分輸入無法驗證；請檢查必填欄位與格式。";
    if (statusCode >= 500) {
      return "服務暫時無法完成計算。請稍後重試；若持續發生，請聯絡服務管理者。";
    }
    return `服務拒絕本次請求（HTTP ${statusCode}）。請檢查輸入後再試。`;
  }

  function networkErrorMessage(profile) {
    if (profile === PROFILES.LOCAL) {
      return "無法連線到本機命盤計算後端。請關閉這個頁面後，重新啟動 Classical Astrology App。";
    }
    if (profile === PROFILES.PRIVATE_ALPHA) {
      return "無法連線到 Private Alpha 命盤計算服務。請確認網路連線後再試；若持續發生，請聯絡邀請者。";
    }
    return "無法確認目前執行環境或連線到計算服務。請重新載入頁面後再試。";
  }

  return Object.freeze({
    PROFILES,
    validateClientConfiguration,
    formatApiError,
    networkErrorMessage,
  });
});
