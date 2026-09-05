(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ClientContext = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PROFILES = Object.freeze({
    PRIVATE_ALPHA: "private_alpha",
    PUBLIC: "public",
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
      // 後端的訊息會提到 fixed_offset，那對 API 消費者是對的；本頁沒有那個
      // 控制項，所以這裡只指向本頁真的能做的事。
      "這個本地時間在所選時區不存在（該時鐘從未指向這個時刻）；請核對出生紀錄的日期、"
      + "時刻與時區。"
    ),
    // 這裡只描述情況，不描述頁面能做什麼。曾經它寫著「只知道約略小時無法指明
    // 是哪一次」，那在寫的當天是對的；SD-32 的修復讓頁面對這個精度也提供了選擇，
    // 而這句話沒有任何路徑會被通知，於是文案與介面互相矛盾了 27 天。
    // 出路由 calculate.js 依「選擇區實際有沒有出現」導出。
    ambiguous_local_time_choice_required: (
      "這個民用小時在所選時區出現了兩次（日光節約時間結束當天會重複一小時），"
      + "服務需要知道是哪一次。"
    ),
    house_system_unavailable: (
      "所選宮位制無法在這個緯度計算；請改選 Whole Sign 或其他可用宮位制。"
    ),
    full_ephemeris_required: (
      "這次計算超出目前完整 Swiss Ephemeris 資料範圍；服務已拒絕改用較低精度替代。"
    ),
    // 內建地名目錄開不起來時由 /api/places/search 回 503。通用的 5xx 措辭在
    // 這裡是錯的：這是地名查詢，不是計算。出路指向手動輸入，依 SD-18：
    // 「找不到地點時保留手動座標／時區入口並提供可行動錯誤」。不建議重新
    // 載入——那是伺服器端的資料檔問題，重載不會改變它。
    place_catalog_unavailable: (
      "內建地名目錄暫時無法讀取，這次查不了地點。這不是你的輸入有問題；"
      + "請改用下方的手動輸入座標與時區，或稍後重試。"
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
    request_capacity_exhausted: (
      "服務目前忙碌，這不是你的輸入有問題；請稍後重試。"
    ),
    compute_capacity_exhausted: (
      "計算佇列仍在忙碌，暫時無法開始這次計算；請依下方提示稍後重試。"
    ),
  });

  const VALIDATION_REASON_MESSAGES = Object.freeze({
    approximate_hour_requires_zero_subhour: (
      "「只知道約略小時」只能輸入整點；請將分鐘與秒數改為 0，或改選「精確到分鐘」。"
    ),
    date_only_requires_zero_time: (
      "「只知道日期」不能同時指定時分秒；請清除時間，或改選較精確的出生時間類型。"
    ),
    moon_profile_center_conflict: (
      "月亮出生地站心模式只能搭配地心全域中心；請調整月亮模式或計算中心。"
    ),
    body_selection_conflict: (
      "古典七政預設不能同時加入三王星、凱龍星、固定星或莉莉絲／普里阿普斯。"
    ),
    aspect_orb_scale_requires_profile: (
      "縮放相位容許度前，請先選擇一套具名容許度來源。"
    ),
    aspect_orb_sources_conflict: (
      "固定相位容許度與具名容許度來源不能同時使用；請保留其中一種。"
    ),
    aspect_angle_orb_requires_angles: (
      "設定角點相位容許度前，請先開啟角點參與相位。"
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

  function resolveProfileBounded(
    load,
    cancel,
    timeoutMs,
    schedule,
    cancelSchedule
  ) {
    if (typeof load !== "function" || typeof cancel !== "function") {
      throw new TypeError("client configuration loader and cancel must be functions");
    }
    if (!Number.isInteger(timeoutMs) || timeoutMs <= 0) {
      throw new RangeError("client configuration timeout must be positive");
    }
    const startTimer = schedule || setTimeout;
    const stopTimer = cancelSchedule || clearTimeout;
    return new Promise((resolve) => {
      let finished = false;
      let timer;
      const finish = (value) => {
        if (finished) return;
        finished = true;
        stopTimer(timer);
        resolve(value);
      };
      timer = startTimer(() => {
        try { cancel(); } catch (_error) { /* timeout仍必須完成 */ }
        finish(null);
      }, timeoutMs);
      Promise.resolve()
        .then(load)
        .then(
          (payload) => finish(validateClientConfiguration(payload).profile),
          () => finish(null)
        );
    });
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

    if (VALIDATION_REASON_MESSAGES[type]) return VALIDATION_REASON_MESSAGES[type];

    if (type === "missing") return `${label}尚未填寫。`;
    if (type === "extra_forbidden") {
      return "頁面送出了目前版本不支援的欄位，請重新載入後再試。";
    }
    if (locationKeys.has("timezone") || locationKeys.has("iana_name")) {
      return "時區名稱無效；請使用例如 Asia/Taipei 的 IANA 時區名稱，或改選固定 UTC 偏移。";
    }
    if (locationKeys.has("datetime")) {
      // 範圍違反必須先分流出去。2026-08-06 實測：送出 1800-01-01 時後端回的是
      // datetime.year 的 greater_than_equal，畫面卻說「請確認日期確實存在」——
      // 那個日期存在，問題是產品只算 1900–2399。指引把使用者送去檢查一個沒有
      // 問題的地方，比不給指引更糟。
      if (type.includes("greater_than") || type.includes("less_than")) {
        if (locationKeys.has("year")) {
          return "出生年份超出本產品目前計算的範圍；日期欄位下方註明了支援的西元年區間。";
        }
        return `${label}超出允許範圍；請參考該欄位下方的數字範圍說明。`;
      }
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
    if (statusCode === 429) return "服務目前忙碌，這不是你的輸入有問題；請依提示稍後重試。";
    if (statusCode >= 500) {
      return "服務暫時無法完成計算。請稍後重試；若持續發生，請聯絡服務管理者。";
    }
    return `服務拒絕本次請求（HTTP ${statusCode}）。請檢查輸入後再試。`;
  }

  function networkErrorMessage(profile) {
    if (profile === PROFILES.PRIVATE_ALPHA) {
      return "無法連線到 Private Alpha 命盤計算服務。請確認網路連線後再試；若持續發生，請聯絡邀請者。";
    }
    if (profile === PROFILES.PUBLIC) {
      return "無法連線到命盤計算服務。請確認網路連線後再試；若持續發生，請透過聯絡頁回報。";
    }
    return "無法確認目前執行環境或連線到計算服務。請重新載入頁面後再試。";
  }

  function apiErrorActions(statusCode, retryAfterHeader, profile) {
    const actions = [];
    if (statusCode === 429 || statusCode >= 500) {
      const raw = typeof retryAfterHeader === "string" ? retryAfterHeader.trim() : "";
      const seconds = /^\d+$/.test(raw) ? Number(raw) : 0;
      actions.push(
        seconds >= 1 && seconds <= 3600
          ? `約 ${seconds} 秒後重試。`
          : "稍後重試。"
      );
    } else {
      actions.push("修正輸入後再送出一次。");
    }
    if (statusCode === 413 || statusCode === 415) actions.push("重新載入頁面。");
    if (profile === PROFILES.PRIVATE_ALPHA) {
      actions.push("若持續發生，請聯絡邀請者。");
    } else if (profile === PROFILES.PUBLIC) {
      actions.push("若持續發生，請透過聯絡頁回報。");
    }
    return actions;
  }

  /**
   * 地名查詢的 token 截斷告知。
   *
   * 後端會把超過上限的詞丟掉，並在 `query` 收據裡逐字交代
   * （`truncated`、`tokens_used`、`tokens_ignored`）。只讀 `results` 是不夠
   * 的：一個被砍過詞的查詢無論回「找到 N 筆」還是「沒有符合的地名」，都是
   * **在使用者不知情的狀況下，把一個被改寫過的查詢報成他自己的查詢**。
   *
   * 回傳 null 表示沒有截斷，呼叫端不必顯示任何東西。
   *
   * 這串文字只進畫面，不得進 log：它是使用者輸入的自由文字，屬
   * `PRIVACY_LOGGING_POLICY` 的最高敏感類別。
   */
  function placeQueryNotice(queryReceipt) {
    if (!queryReceipt || typeof queryReceipt !== "object") return null;
    if (queryReceipt.truncated !== true) return null;
    const ignored = Array.isArray(queryReceipt.tokens_ignored)
      ? queryReceipt.tokens_ignored
      : [];
    if (!ignored.length) return null;
    const used = Array.isArray(queryReceipt.tokens_used)
      ? queryReceipt.tokens_used
      : [];
    const limit = queryReceipt.max_search_tokens;
    const limitText = Number.isFinite(limit) ? `前 ${limit} 個詞` : "前幾個詞";
    return (
      `這次只用了${limitText}查詢：「${used.join(" ")}」。`
      + `略過了「${ignored.join(" ")}」，所以結果不涵蓋這些字。`
      + "想更精確，請改用較短的地名，或直接在下方手動輸入座標與時區。"
    );
  }

  return Object.freeze({
    PROFILES,
    validateClientConfiguration,
    resolveProfileBounded,
    formatApiError,
    apiErrorActions,
    networkErrorMessage,
    placeQueryNotice,
  });
});
