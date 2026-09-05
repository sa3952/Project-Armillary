const assert = require("node:assert/strict");
const test = require("node:test");
const {
  PROFILES,
  validateClientConfiguration,
  formatApiError,
  apiErrorActions,
  networkErrorMessage,
  placeQueryNotice,
  resolveProfileBounded,
} = require("../zh-TW/client-context.js");

assert.match(
  formatApiError({ code: "request_capacity_exhausted" }, 503),
  /不是.*輸入|忙碌/
);
assert.match(
  formatApiError({ code: "compute_capacity_exhausted" }, 503),
  /稍後|重試/
);
assert.deepStrictEqual(
  apiErrorActions(
    503, "5", PROFILES.PRIVATE_ALPHA
  ),
  ["約 5 秒後重試。", "若持續發生，請聯絡邀請者。"]
);
assert.ok(
  !apiErrorActions(
    503, null, PROFILES.PRIVATE_ALPHA
  ).some((value) => value.includes("修正輸入"))
);

assert.deepEqual(validateClientConfiguration({ profile: "private_alpha" }), {
  profile: "private_alpha",
});
assert.deepEqual(validateClientConfiguration({ profile: "public" }), {
  profile: "public",
});
for (const invalid of [null, {}, { profile: "production" }]) {
  assert.throws(() => validateClientConfiguration(invalid));
}

test("client configuration timeout settles even when fetch never does", async () => {
  let timeout;
  let cancelled = 0;
  const result = resolveProfileBounded(
    () => new Promise(() => {}),
    () => { cancelled += 1; },
    5000,
    (callback) => { timeout = callback; return 7; },
    () => {}
  );
  timeout();
  assert.equal(await result, null);
  assert.equal(cancelled, 1);
});

test("client configuration success clears the timeout and returns public", async () => {
  const cleared = [];
  const result = resolveProfileBounded(
    () => Promise.resolve({ profile: "public" }),
    () => assert.fail("successful load must not cancel"),
    5000,
    () => 9,
    (token) => cleared.push(token)
  );
  assert.equal(await result, "public");
  assert.deepEqual(cleared, [9]);
});

const timezoneError = formatApiError(
  [
    {
      type: "value_error",
      loc: ["body", "timezone"],
      msg: "Value error, raw framework detail with Not/AZone",
      input: { iana_name: "Not/AZone" },
    },
  ],
  422
);
assert.equal(
  timezoneError,
  "時區名稱無效；請使用例如 Asia/Taipei 的 IANA 時區名稱，或改選固定 UTC 偏移。"
);
assert.ok(!timezoneError.includes("Not/AZone"));
assert.ok(!timezoneError.includes("raw framework detail"));

assert.equal(
  formatApiError(
    [{ type: "missing", loc: ["body", "datetime", "year"] }],
    422
  ),
  "年份尚未填寫。"
);
assert.equal(
  formatApiError(
    [{ type: "less_than_equal", loc: ["body", "location", "latitude"] }],
    422
  ),
  "緯度無效；請輸入 -90 到 90 的十進位度數。"
);
assert.equal(
  formatApiError(
    {
      code: "house_system_unavailable",
      message: "Placidus 在緯度 89.123456° 無法計算",
      latitude: 89.123456,
    },
    422
  ),
  "所選宮位制無法在這個緯度計算；請改選 Whole Sign 或其他可用宮位制。"
);
assert.ok(
  !formatApiError(
    {
      code: "house_system_unavailable",
      message: "Placidus 在緯度 89.123456° 無法計算",
      latitude: 89.123456,
    },
    422
  ).includes("89.123456")
);
assert.equal(
  formatApiError(
    { code: "internal_server_error", message: "raw traceback detail" },
    500
  ),
  "服務暫時無法完成計算。請稍後重試；若持續發生，請聯絡服務管理者。"
);
assert.ok(!formatApiError("caller supplied secret", 400).includes("secret"));
assert.match(formatApiError(null, 500), /服務暫時無法完成計算/);

const hostedNetworkError = networkErrorMessage(PROFILES.PRIVATE_ALPHA);
assert.match(hostedNetworkError, /Private Alpha/);
assert.match(hostedNetworkError, /聯絡邀請者/);
assert.ok(!hostedNetworkError.includes("127.0.0.1"));

console.log("client context tests passed");

// 契約 §9：ambiguous_local_time_choice_required 先前不在封閉對照表內，
// 因此掉進通用的「部分輸入無法驗證」——對這個情況完全誤導。
// local profile 的回應帶 message，內容含本地時刻與兩個 UTC 偏移；
// 指引必須由對照表產生，不得回顯後端原文。
const ambiguous = formatApiError(
  {
    code: "ambiguous_local_time_choice_required",
    message:
      "此本地時間在 America/New_York 為模糊時刻；已採用 fold=0，"
      + "對應 UTC-04.00；另一種解讀為 UTC-05.00。",
  },
  422
);
assert.ok(ambiguous.includes("出現了兩次"));
// SD-32：這則訊息原本叫使用者「改用固定偏移輸入」，而本頁沒有那個控制項。
// 一則指向不存在控制項的「可行動訊息」比沒有訊息更糟——使用者會去找。
assert.ok(!ambiguous.includes("固定偏移"),
  "訊息又指向了本頁不存在的固定偏移輸入");
// 這則訊息一度自己宣稱「只知道約略小時無法指明是哪一次」。那在寫的當天是對的，
// SD-32 的修復讓頁面對該精度也提供了選擇，而這句話沒有任何路徑會被通知——文案與
// 介面矛盾了 27 天。訊息現在只描述情況；出路由 calculate.js 依選擇區是否真的出現
// 在畫面上導出，因為只有它知道。
assert.ok(!ambiguous.includes("無法"),
  "訊息不得自行宣稱頁面能或不能做什麼；那是 calculate.js 才知道的事");
assert.ok(ambiguous.includes("需要知道是哪一次"),
  "訊息必須說出服務要的是什麼");
assert.ok(!ambiguous.includes("fold"));
assert.ok(!ambiguous.includes("America/New_York"));
assert.ok(!ambiguous.includes("UTC-04"));
assert.notEqual(ambiguous, "部分輸入無法驗證；請檢查必填欄位與格式。");

// 契約 §9 ＋ SD-18：地名目錄不可用時，先前掉進 statusCode>=500 的通用訊息
// 「服務暫時無法完成計算」——用詞錯誤（這是地名查詢），且沒有指出手動輸入這條出路。
// local profile 的回應帶 message，內容含 bundled catalog 的內部描述，不得回顯。
const catalogDown = formatApiError(
  {
    code: "place_catalog_unavailable",
    message: "bundled place catalog is unavailable at /app/backend/place_data/places.sqlite3",
  },
  503
);
assert.ok(catalogDown.includes("手動輸入"));
assert.ok(!catalogDown.includes("計算"));
assert.ok(!catalogDown.includes("sqlite"));
assert.ok(!catalogDown.includes("/app/"));
assert.notEqual(
  catalogDown,
  "服務暫時無法完成計算。請稍後重試；若持續發生，請聯絡服務管理者。"
);

// 2026-08-06 實測：送出 1800-01-01（產品支援 1900–2399），後端回
// datetime.year 的 greater_than_equal，畫面卻說「請確認日期確實存在」。
// 那個日期確實存在——指引把使用者送去檢查一個沒有問題的地方。
// datetime 這一支必須先分流範圍違反，再落到「日期不存在」那句。
const outOfRangeYear = formatApiError(
  [{ type: "greater_than_equal", loc: ["body", "datetime", "year"] }],
  422
);
assert.ok(outOfRangeYear.includes("年份"));
assert.ok(outOfRangeYear.includes("範圍"));
assert.ok(
  !outOfRangeYear.includes("確實存在"),
  "年份超範圍不得沿用「請確認日期確實存在」——那句話指向錯誤的原因"
);

for (const [type, expected] of [
  ["approximate_hour_requires_zero_subhour", /整點|精確到分鐘/],
  ["date_only_requires_zero_time", /清除時間|出生時間類型/],
  ["moon_profile_center_conflict", /站心模式|計算中心/],
  ["aspect_orb_scale_requires_profile", /具名容許度來源/],
]) {
  const message = formatApiError([{ type, loc: ["body"] }], 422);
  assert.match(message, expected);
  assert.ok(!message.includes("輸入資料格式無效"));
}
assert.equal(
  formatApiError(
    [{ type: "less_than_equal", loc: ["body", "datetime", "hour"] }],
    422
  ),
  "小時超出允許範圍；請參考該欄位下方的數字範圍說明。"
);
// 真正不存在的日期仍須維持原本那句。
assert.equal(
  formatApiError(
    [{ type: "date_from_datetime_parsing", loc: ["body", "datetime"] }],
    422
  ),
  "出生日期時間無效；請確認日期確實存在，且時間數字在允許範圍內。"
);
console.log("datetime range guidance tests passed");

// ── 地名查詢截斷告知 ──────────────────────────────────────
// 後端逐字交代哪些詞被丟掉，前端必須讀它：一個被改寫過的查詢不得報成
// 使用者自己的查詢。有截斷必說、沒截斷不吵，兩邊都要成立。

const truncated = placeQueryNotice({
  token_count: 8,
  max_search_tokens: 6,
  truncated: true,
  tokens_used: ["新竹縣", "竹北市", "光明", "六路", "東二段", "一號"],
  tokens_ignored: ["附近", "巷口"],
  reason: "token_limit_protects_against_broad_prefix_term_cost",
});
assert.ok(truncated, "截斷了卻沒有任何告知");
assert.match(truncated, /附近 巷口/, "沒有說出被略過的詞");
assert.match(truncated, /新竹縣/, "沒有說出實際用來查詢的詞");
assert.match(truncated, /前 6 個詞/);

// 反向控制：沒有截斷時不得產生告知，否則每次查詢都多一句無意義的警告。
assert.equal(placeQueryNotice({
  token_count: 2, max_search_tokens: 6, truncated: false,
  tokens_used: ["台中"], tokens_ignored: [], reason: null,
}), null);
assert.equal(placeQueryNotice(undefined), null);
assert.equal(placeQueryNotice(null), null);
assert.equal(placeQueryNotice({}), null);

// truncated 為真但沒列出被略過的詞，是自相矛盾的收據；寧可不說，
// 也不要生出一句「略過了「」」。
assert.equal(placeQueryNotice({
  truncated: true, tokens_used: ["台中"], tokens_ignored: [],
}), null);
