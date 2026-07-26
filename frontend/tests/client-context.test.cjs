const assert = require("node:assert/strict");
const {
  PROFILES,
  validateClientConfiguration,
  formatApiError,
  networkErrorMessage,
} = require("../client-context.js");

assert.deepEqual(validateClientConfiguration({ profile: "local" }), {
  profile: "local",
});
assert.deepEqual(validateClientConfiguration({ profile: "private_alpha" }), {
  profile: "private_alpha",
});
for (const invalid of [null, {}, { profile: "production" }]) {
  assert.throws(() => validateClientConfiguration(invalid));
}

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

const localNetworkError = networkErrorMessage(PROFILES.LOCAL);
const hostedNetworkError = networkErrorMessage(PROFILES.PRIVATE_ALPHA);
assert.match(localNetworkError, /重新啟動 Classical Astrology App/);
assert.match(hostedNetworkError, /Private Alpha/);
assert.match(hostedNetworkError, /聯絡邀請者/);
assert.ok(!hostedNetworkError.includes("127.0.0.1"));

console.log("client context tests passed");
