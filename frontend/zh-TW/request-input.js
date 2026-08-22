/**
 * 把表單的原始字串變成請求輸入，或明確說出哪裡不對。
 *
 * 這一層原本散在 `calculate.js` 的兩個小 helper 裡，而它們把「沒填」和
 * 「填了但不合法」壓成同一個回傳值，於是：
 *
 *   `Math.trunc` 讓「12.5 時」靜默變成 12 時（PIA-2026-08-06-006）；
 *   `readInteger(x) || 0` 讓 NaN 靜默變成 0，也讓秒的小數整個消失；
 *   數字選項的 UI 範圍與 API 契約不一致，送出後才由伺服器回 422
 *   （PIA-2026-08-06-004）。
 *
 * 共同的形狀是「介面接受了一個它算不出來的值，而且不說」。因此這裡的回傳
 * 一律是封閉三態 empty / invalid / value：呼叫端無法再把 invalid 當成 0。
 *
 * 表單帶 `novalidate`（送出時不得讓瀏覽器原生驗證接手，否則出生資料會落進
 * query string），所以 min/max 必須在這裡自己檢查，不能倚賴 HTML 屬性。
 */
(function attachRequestInput(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.RequestInput = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function buildRequestInput() {
  "use strict";

  const EMPTY = "empty";
  const INVALID = "invalid";
  const VALUE = "value";

  const INTEGER_PATTERN = /^[+-]?\d+$/;

  function empty() {
    return { state: EMPTY };
  }

  function invalid(reason) {
    return { state: INVALID, reason };
  }

  function value(number) {
    return { state: VALUE, value: number };
  }

  /** 時與分：嚴格整數。`12.5`、`1e2`、`abc` 都是錯誤，不是 12、100 或 0。 */
  function readInteger(raw) {
    const text = String(raw === undefined || raw === null ? "" : raw).trim();
    if (text === "") return empty();
    // 先比對字面形狀再轉數字。Number("12.5") 是合法有限數，只看
    // Number.isFinite 分不出「整數欄位被填了小數」這件事。
    if (!INTEGER_PATTERN.test(text)) return invalid("必須是整數");
    const parsed = Number(text);
    if (!Number.isSafeInteger(parsed)) return invalid("超出可精確表示的整數範圍");
    return value(parsed);
  }

  /**
   * 秒：唯一允許小數的時間欄位；60只可能是後端另行驗證的UTC閏秒。
   * 小數必須保留——截斷會算出使用者沒有輸入的時刻。
   */
  function readSecond(raw) {
    const text = String(raw === undefined || raw === null ? "" : raw).trim();
    if (text === "") return empty();
    const parsed = Number(text);
    if (!Number.isFinite(parsed)) return invalid("必須是數字");
    if (parsed < 0 || parsed > 60) return invalid("必須介於 0 與 60 之間");
    return value(parsed);
  }

  /** 緯度、經度、海拔：有限十進位數。 */
  function readDecimal(raw) {
    const text = String(raw === undefined || raw === null ? "" : raw).trim();
    if (text === "") return empty();
    const parsed = Number(text);
    if (!Number.isFinite(parsed)) return invalid("必須是數字");
    return value(parsed);
  }

  /**
   * 數字選項：範圍取自 options catalogue 同一份宣告，介面屬性與這裡的檢查
   * 因此不可能各說各話。回傳 null 表示通過。
   */
  function boundsProblem(option, candidate) {
    if (candidate === null || candidate === undefined) return null;
    if (!Number.isFinite(candidate)) return "必須是數字";
    if (option.min !== undefined && candidate < option.min) {
      return `不得小於 ${option.min}`;
    }
    if (option.max !== undefined && candidate > option.max) {
      return `不得大於 ${option.max}`;
    }
    return null;
  }

  /**
   * 逐一檢查目前**送得出去**的數字選項。
   *
   * 判準是 `isReachable`（相依鏈成立）而不是 `isEnabled`——後者的意思是
   * 「與預設不同」，用它會讓使用者被一個他根本沒開啟的欄位擋下來，也會
   * 漏掉「值剛好等於預設但父項已開啟」的情形。
   */
  function numericOptionProblems(catalogue, values) {
    const problems = [];
    catalogue.OPTIONS.forEach((option) => {
      if (option.type !== "number") return;
      if (!catalogue.isReachable(option, values)) return;
      if (catalogue.conflictFor(option, values)) return;
      const candidate = values[option.key];

      // 清空一個「預設值不是 null」的數字欄位，會送出 null 而不是省略該鍵——
      // 因為 null 與預設不同，`toRequestOptions` 就把它帶上，後端則回
      // `float_type` 422。其餘三個數字選項預設就是 null，清空等於省略，
      // 所以只有這一類會踩到。
      //
      // 不自動填回預設：那是把使用者沒有輸入的值算進結果，正是
      // PIA-2026-08-06-006 在修的那件事。改為明講這欄必填。
      if (candidate === null || candidate === undefined) {
        if (option.default !== null && option.default !== undefined) {
          problems.push(`${option.label_zh} 必須填寫（或關閉這個選項）`);
        }
        return;
      }

      const problem = boundsProblem(option, candidate);
      if (problem) problems.push(`${option.label_zh} ${problem}`);
    });
    return problems;
  }

  // ── 模糊民用時刻（SD-32 / PIA-2026-08-06-005）────────────
  //
  // DST 結束當天，同一個時鐘時間在該時區出現兩次，對應兩個相差一小時的
  // UTC 時刻。後端支援 PEP 495 的 `fold` 讓呼叫端指明是哪一次，前端卻
  // 只送得出 `{mode:"iana", iana_name}`，因此永遠是 fold=0——使用者可能
  // 拿到另一個時刻算出的盤，上升點差約 15°，而畫面不會說。
  //
  // 這不是只影響歐美：Asia/Taipei 在 1937、1945–1961、1974、1975、1979
  // 之間共有 40 個這樣的小時。

  const DAY_MS = 86400000;

  /** 某個 UTC 瞬間在該時區的偏移（分鐘）。 */
  function zoneOffsetMinutes(utcMillis, timeZone) {
    const formatter = new Intl.DateTimeFormat("en-US", {
      timeZone,
      hourCycle: "h23",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    const parts = {};
    formatter.formatToParts(new Date(utcMillis)).forEach((part) => {
      parts[part.type] = part.value;
    });
    const asIfUtc = Date.UTC(
      Number(parts.year), Number(parts.month) - 1, Number(parts.day),
      Number(parts.hour), Number(parts.minute), Number(parts.second)
    );
    return (asIfUtc - utcMillis) / 60000;
  }

  /**
   * 一個時鐘時間在該時區對應到幾個真實瞬間。
   *
   *   `unique`      —— 一般情形，一個瞬間；
   *   `ambiguous`   —— 秋季調慢，出現兩次，使用者必須指明是哪一次；
   *   `nonexistent` —— 春季調快跳過的那一小時，這個時間不存在。
   *
   * 判法：取前後各一天的偏移作為候選（一次轉換至多改變其中之一），再讓
   * 每個候選瞬間自己回答「你的偏移真的是這個嗎」。只挑通過自我驗證的。
   * 直接對 `Date` 做時區運算會踩到宿主時區，所以全程用 UTC 毫秒。
   */
  function civilTimeOccurrences(fields, timeZone) {
    if (!timeZone) return null;
    const { year, month, day, hour, minute, second } = fields;
    for (const part of [year, month, day, hour, minute]) {
      if (!Number.isFinite(part)) return null;
    }
    const wholeSeconds = Math.floor(Number.isFinite(second) ? second : 0);
    let wall;
    try {
      wall = Date.UTC(year, month - 1, day, hour, minute, wholeSeconds);
      // 探一次，時區名稱不合法時 Intl 會丟。
      zoneOffsetMinutes(wall, timeZone);
    } catch (_error) {
      return null;
    }

    const nearby = [
      zoneOffsetMinutes(wall - DAY_MS, timeZone),
      zoneOffsetMinutes(wall + DAY_MS, timeZone),
    ];
    const seen = new Set();
    const occurrences = [];
    nearby.forEach((offset) => {
      const utcMillis = wall - offset * 60000;
      if (seen.has(utcMillis)) return;
      if (zoneOffsetMinutes(utcMillis, timeZone) !== offset) return;
      seen.add(utcMillis);
      occurrences.push({ utcMillis, offsetMinutes: offset });
    });
    // PEP 495：fold=0 是較早的那個瞬間。
    occurrences.sort((a, b) => a.utcMillis - b.utcMillis);

    if (!occurrences.length) return { state: "nonexistent", occurrences: [] };
    return {
      state: occurrences.length > 1 ? "ambiguous" : "unique",
      occurrences: occurrences.map((item, index) => ({
        fold: index,
        offsetHours: item.offsetMinutes / 60,
        utcIso: new Date(item.utcMillis).toISOString(),
      })),
    };
  }

  return Object.freeze({
    STATES: Object.freeze({ EMPTY, INVALID, VALUE }),
    zoneOffsetMinutes,
    civilTimeOccurrences,
    readInteger,
    readSecond,
    readDecimal,
    boundsProblem,
    numericOptionProblems,
  });
});
