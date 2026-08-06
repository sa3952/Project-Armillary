const test = require("node:test");
const assert = require("node:assert/strict");

const RequestInput = require("../zh-TW/request-input.js");
const Catalogue = require("../zh-TW/options-catalogue.js");

// PIA-2026-08-06-006：時間欄位靜默改值。
// PIA-2026-08-06-004：UI 數字範圍與 API 契約不一致。
//
// 兩者的失敗形狀相同——介面接受了一個它算不出來的值，而且不說。所以下面
// 每一組都同時要求「不合法的值必須被指名」與「合法的值必須原樣通過」；
// 只有前者會被一個什麼都拒絕的實作騙過去。

const { EMPTY, INVALID, VALUE } = RequestInput.STATES;

test("小數填進時或分是錯誤，不是無聲截斷", () => {
  for (const raw of ["12.5", "1.5", "0.9", "-3.2"]) {
    const read = RequestInput.readInteger(raw);
    assert.equal(read.state, INVALID, `${raw} 應被拒絕，而不是 Math.trunc 後送出`);
  }
});

test("整數與空值仍照常通過", () => {
  assert.deepEqual(RequestInput.readInteger("12"), { state: VALUE, value: 12 });
  assert.deepEqual(RequestInput.readInteger(" 0 "), { state: VALUE, value: 0 });
  assert.deepEqual(RequestInput.readInteger("+7"), { state: VALUE, value: 7 });
  assert.equal(RequestInput.readInteger("").state, EMPTY);
  assert.equal(RequestInput.readInteger("   ").state, EMPTY);
});

test("非數字的時或分是錯誤，不得變成 0", () => {
  // 舊版 `readInteger(x) || 0` 會把 NaN 變成 0，也就是把打錯字算成午夜。
  for (const raw of ["abc", "NaN", "Infinity", "1e2", "١٢"]) {
    assert.equal(RequestInput.readInteger(raw).state, INVALID, raw);
  }
});

test("秒保留小數，且守住 0 <= second < 60", () => {
  assert.deepEqual(RequestInput.readSecond("0.25"), { state: VALUE, value: 0.25 });
  assert.deepEqual(RequestInput.readSecond("59.999"), { state: VALUE, value: 59.999 });
  assert.deepEqual(RequestInput.readSecond("0"), { state: VALUE, value: 0 });
  assert.equal(RequestInput.readSecond("").state, EMPTY);
  assert.equal(RequestInput.readSecond("60").state, INVALID);
  assert.equal(RequestInput.readSecond("-0.5").state, INVALID);
  assert.equal(RequestInput.readSecond("abc").state, INVALID);
});

test("緯經高接受有限小數，拒絕非數字", () => {
  assert.deepEqual(RequestInput.readDecimal("24.1477"), { state: VALUE, value: 24.1477 });
  assert.deepEqual(RequestInput.readDecimal("-120.6736"), { state: VALUE, value: -120.6736 });
  assert.equal(RequestInput.readDecimal("").state, EMPTY);
  for (const raw of ["abc", "Infinity", "1/2"]) {
    assert.equal(RequestInput.readDecimal(raw).state, INVALID, raw);
  }
});

// ── PIA-2026-08-06-004 ───────────────────────────────────────
// 這一組是本 finding 的核心：catalogue 宣告的範圍必須是 API 契約的子集。
// 數字直接照抄 backend/app/schemas.py，因為那份才是權威。

const CONTRACT = {
  declination_aspect_orb_degrees: { gt: 0, le: 3 },
  aspect_orb_scale_percent: { gt: 0, le: 300 },
  aspect_fixed_orb_degrees: { gt: 0, le: 30 },
  aspect_angle_orb_degrees: { gt: 0, le: 30 },
};

test("每個數字選項宣告的範圍都落在 API 契約之內", () => {
  Object.entries(CONTRACT).forEach(([key, bound]) => {
    const option = Catalogue.BY_KEY[key];
    assert.ok(option, `${key} 不在 catalogue 裡`);
    assert.equal(option.type, "number", key);
    assert.ok(option.min > bound.gt,
      `${key} 的 min ${option.min} 沒有大於契約下界 ${bound.gt}——UI 會提供伺服器拒絕的值`);
    assert.ok(option.max <= bound.le,
      `${key} 的 max ${option.max} 超過契約上界 ${bound.le}`);
  });
});

test("catalogue 沒有其他數字選項偷偷缺少範圍宣告", () => {
  Catalogue.OPTIONS.filter((option) => option.type === "number").forEach((option) => {
    assert.ok(Number.isFinite(option.min), `${option.key} 缺 min`);
    assert.ok(Number.isFinite(option.max), `${option.key} 缺 max`);
    assert.ok(option.min <= option.max, option.key);
  });
});

test("送出前的範圍檢查會指名越界的選項", () => {
  const values = {
    ...Catalogue.defaults(),
    include_declination_aspects: true,
    declination_aspect_orb_degrees: 30,
  };
  const problems = RequestInput.numericOptionProblems(Catalogue, values);
  assert.equal(problems.length, 1, JSON.stringify(problems));
  assert.match(problems[0], /赤緯容許度/);
  assert.match(problems[0], /不得大於 3/);
});

test("上下界本身是允許的，檢查不得把合法值也擋掉", () => {
  const values = {
    ...Catalogue.defaults(),
    include_declination_aspects: true,
    declination_aspect_orb_degrees: 3,
  };
  assert.deepEqual(RequestInput.numericOptionProblems(Catalogue, values), []);
});

test("未啟用的選項不參與檢查", () => {
  // 使用者不該被一個他沒開啟、也不會送出的欄位擋下來。
  const values = {
    ...Catalogue.defaults(),
    include_declination_aspects: false,
    declination_aspect_orb_degrees: 999,
  };
  assert.deepEqual(RequestInput.numericOptionProblems(Catalogue, values), []);
});

test("預設值本身必須全部通過檢查", () => {
  // 反向控制：若預設值會被自己的檢查擋下，任何人一開頁面就送不出去。
  const defaults = Catalogue.defaults();
  const enabled = { ...defaults };
  Catalogue.OPTIONS.filter((o) => o.type === "boolean").forEach((o) => {
    enabled[o.key] = true;
  });
  assert.deepEqual(RequestInput.numericOptionProblems(Catalogue, defaults), []);
  assert.deepEqual(RequestInput.numericOptionProblems(Catalogue, enabled), []);
});

// ── SD-32 / PIA-2026-08-06-005：模糊民用時刻 ──────────────
// 這一組的 oracle 是 IANA 時區資料本身，不是本模組的另一條路徑。

test("秋季調慢那一小時被判為模糊，並給出兩個相差一小時的解讀", () => {
  const result = RequestInput.civilTimeOccurrences(
    { year: 2025, month: 10, day: 26, hour: 2, minute: 30, second: 0 },
    "Europe/Paris"
  );
  assert.equal(result.state, "ambiguous");
  assert.equal(result.occurrences.length, 2);
  assert.deepEqual(result.occurrences.map((o) => o.fold), [0, 1]);
  assert.deepEqual(result.occurrences.map((o) => o.offsetHours), [2, 1]);
  // fold=0 必須是較早的真實瞬間（PEP 495）。
  assert.equal(result.occurrences[0].utcIso, "2025-10-26T00:30:00.000Z");
  assert.equal(result.occurrences[1].utcIso, "2025-10-26T01:30:00.000Z");
});

test("台灣歷史上的重複小時同樣被抓到", () => {
  // 台灣曾在 1937、1945-1961、1974、1975、1979 實施日光節約時間。
  const result = RequestInput.civilTimeOccurrences(
    { year: 1979, month: 9, day: 30, hour: 23, minute: 30, second: 0 },
    "Asia/Taipei"
  );
  assert.equal(result.state, "ambiguous", JSON.stringify(result));
  assert.deepEqual(result.occurrences.map((o) => o.offsetHours), [9, 8]);
});

test("一般時刻是唯一解，不得誤報成模糊", () => {
  // 反向控制：一個永遠回 ambiguous 的實作會讓上面兩個測試通過。
  const result = RequestInput.civilTimeOccurrences(
    { year: 1997, month: 8, day: 17, hour: 9, minute: 42, second: 0 },
    "Asia/Taipei"
  );
  assert.equal(result.state, "unique");
  assert.equal(result.occurrences.length, 1);
  assert.equal(result.occurrences[0].offsetHours, 8);
  assert.equal(result.occurrences[0].utcIso, "1997-08-17T01:42:00.000Z");
});

test("春季調快跳過的時間被判為不存在", () => {
  const result = RequestInput.civilTimeOccurrences(
    { year: 2025, month: 3, day: 30, hour: 2, minute: 30, second: 0 },
    "Europe/Paris"
  );
  assert.equal(result.state, "nonexistent");
  assert.deepEqual(result.occurrences, []);
});

test("時區未填或無法辨識時回 null，不假裝知道答案", () => {
  const fields = { year: 2025, month: 1, day: 1, hour: 0, minute: 0, second: 0 };
  assert.equal(RequestInput.civilTimeOccurrences(fields, ""), null);
  assert.equal(RequestInput.civilTimeOccurrences(fields, "Not/AZone"), null);
  assert.equal(
    RequestInput.civilTimeOccurrences({ ...fields, hour: NaN }, "Asia/Taipei"),
    null
  );
});

test("固定偏移時區沒有模糊時刻", () => {
  const result = RequestInput.civilTimeOccurrences(
    { year: 2025, month: 10, day: 26, hour: 2, minute: 30, second: 0 },
    "UTC"
  );
  assert.equal(result.state, "unique");
  assert.equal(result.occurrences[0].offsetHours, 0);
});

// ── 第二層矩陣抓到的：清空「預設非 null」的數字欄位 ──────────
// 106 個 UI option 案例中唯一送到伺服器又被拒的一個。清空赤緯容許度會送出
// null（因為 null 與預設 1.0 不同，toRequestOptions 就把它帶上），後端回
// float_type 422，而畫面只會顯示一句非針對性的錯誤。
// 其餘三個數字選項預設本來就是 null，清空等於省略，所以不受影響——
// 下面兩個測試把這個差異釘住。

test("清空預設非 null 的數字欄位，必須被指名為必填", () => {
  const values = {
    ...Catalogue.defaults(),
    include_declination_aspects: true,
    declination_aspect_orb_degrees: null,
  };
  const problems = RequestInput.numericOptionProblems(Catalogue, values);
  assert.equal(problems.length, 1, JSON.stringify(problems));
  assert.match(problems[0], /赤緯容許度/);
  assert.match(problems[0], /必須填寫/);
});

test("預設本來就是 null 的數字欄位，清空是合法的省略", () => {
  // 反向控制：若把「空值」一律當錯，這三個就會被誤擋，使用者連預設狀態
  // 都送不出去。
  const values = {
    ...Catalogue.defaults(),
    include_aspects: true,
    aspect_include_angles: true,
    aspect_fixed_orb_degrees: null,
    aspect_angle_orb_degrees: null,
    aspect_orb_scale_percent: null,
  };
  assert.deepEqual(RequestInput.numericOptionProblems(Catalogue, values), []);
});
