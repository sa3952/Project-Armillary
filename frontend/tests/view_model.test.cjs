const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ViewModel = require("../zh-TW/view-model.js");
const Exporters = require("../zh-TW/exporters.js");

function loadFixture(name) {
  return JSON.parse(
    fs.readFileSync(path.join(__dirname, "fixtures", `${name}.json`), "utf8")
  );
}

const EXACT = loadFixture("chart-exact");
const SIDEREAL = loadFixture("chart-sidereal-dignity-refused");
const DATE_ONLY = loadFixture("chart-date-only");

function documentFor(response) {
  return Exporters.createDocument(response, ViewModel.buildSections(response));
}

function sectionById(sections, id) {
  const found = sections.find((section) => section.id === id);
  assert.ok(found, `找不到 section: ${id}`);
  return found;
}

// ---------------------------------------------------------------------------
// 收據狀態：本檔最重要的部分
// ---------------------------------------------------------------------------

test("四個布林的每一種組合都映射到不同的顯示狀態", () => {
  assert.equal(
    ViewModel.receiptStatus({ requested: false }).state,
    "not_requested"
  );
  assert.equal(
    ViewModel.receiptStatus({ requested: true, applicable: false, reason_code: "x" }).state,
    "refused"
  );
  assert.equal(
    ViewModel.receiptStatus({
      requested: true, applicable: true, executed: true, available: false,
    }).state,
    "executed_unavailable"
  );
  assert.equal(
    ViewModel.receiptStatus({
      requested: true, applicable: true, executed: true, available: true,
      defaulted: true, requested_explicitly: false,
    }).state,
    "defaulted"
  );
  assert.equal(
    ViewModel.receiptStatus({
      requested: true, applicable: true, executed: true, available: true,
      defaulted: false, requested_explicitly: true,
    }).state,
    "present"
  );
});

test("『明確拒絕』與『使用者沒要求』不得映射成同一個狀態（§13.2 第 24 項）", () => {
  const refused = sectionById(
    ViewModel.buildSections(SIDEREAL), "dignities"
  );
  const notRequested = sectionById(
    ViewModel.buildSections(EXACT), "antiscia"
  );

  assert.equal(refused.status.state, "refused");
  assert.equal(notRequested.status.state, "not_requested");
  assert.notEqual(refused.status.state, notRequested.status.state);

  // 拒絕必須帶著後端的原因代碼，否則畫面說不出「為什麼被拒絕」。
  assert.equal(
    refused.status.reason_code,
    "sidereal_dignity_basis_not_authorized"
  );
});

test("預設帶入與使用者主動選取是不同狀態", () => {
  const dignities = sectionById(ViewModel.buildSections(EXACT), "dignities");
  assert.equal(dignities.status.state, "defaulted");
  assert.ok(
    dignities.notes.some((note) => note.includes("產品預設帶入的，不是你勾選的")),
    "預設帶入必須在畫面上說出來"
  );
});

test("未計算的陷落與外來不得呈現為『沒有』", () => {
  const dignities = sectionById(ViewModel.buildSections(EXACT), "dignities");
  const joined = dignities.notes.join("\n");
  assert.ok(joined.includes("未評估"), "必須明說是未評估");
  assert.ok(joined.includes("不是「沒有」"), "必須否定『沒有』的讀法");
});

// ---------------------------------------------------------------------------
// birth_time_precision 三態
// ---------------------------------------------------------------------------

test("date_only 的錨點不得呈現得像出生時刻（§13.2 第 29 項）", () => {
  const sections = ViewModel.buildSections(DATE_ONLY);
  const time = sectionById(sections, "time");
  const label = time.tables[0].rows[0][0];
  assert.ok(
    label.includes("錨點") && label.includes("非出生時刻"),
    `第一列的標籤必須自稱錨點，實際為：${label}`
  );
  assert.ok(
    time.notes.some((note) => note.includes("不是出生時刻")),
    "備註必須明說這不是出生時刻"
  );
  // 這兩個語義字串來自不同欄位，值也不同，不可互相取代：
  //   astronomical_data.time.input_semantics       -> 這次計算餵給引擎的是什麼
  //   birth_time_sensitivity.representative_semantics -> 取樣區間裡挑了哪一刻當代表
  assert.ok(
    time.notes.some((note) =>
      note.includes("representative_computational_anchor_not_birth_time")
    ),
    "time.input_semantics 必須可達"
  );

  const sensitivity = sectionById(sections, "sensitivity");
  assert.equal(sensitivity.status.state, "present");
  assert.ok(
    sensitivity.notes.some((note) =>
      note.includes("local_noon_computational_anchor_not_birth_time")
    ),
    "birth_time_sensitivity.representative_semantics 必須可達"
  );
  assert.ok(
    sensitivity.tables.some((table) => table.title === "未評估路徑"),
    "not_evaluated_paths 必須逐條可見"
  );
});

test("exact 下的敏感度是『未取樣』而不是『穩定』", () => {
  const sensitivity = sectionById(
    ViewModel.buildSections(EXACT), "sensitivity"
  );
  assert.equal(sensitivity.status.state, "not_requested");
  assert.ok(
    sensitivity.notes.join("").includes("沒有進行區間取樣"),
    "不得讓使用者以為已經驗證過穩定性"
  );
});

// ---------------------------------------------------------------------------
// 相位與容許度（§13.2 第 28 項）
// ---------------------------------------------------------------------------

test("未選定 orb profile 時，畫面必須說出少了哪一層判定", () => {
  const aspects = sectionById(ViewModel.buildSections(EXACT), "aspects");
  const joined = aspects.notes.join("\n");
  assert.ok(joined.includes("沒有選定容許度"), "必須明說沒有 orb 表");
  assert.ok(
    joined.includes("orb_profile_not_selected"),
    "後端原因代碼必須可達"
  );
  // 相位本身有算出來，不能因為少了 orb 就被標成模組被拒絕。
  assert.equal(aspects.status.state, "present");
});

// ---------------------------------------------------------------------------
// 數值呈現
// ---------------------------------------------------------------------------

test("度分秒換算保留正負號且不進位到 60", () => {
  assert.equal(ViewModel.dms(0), "0°00′00.00″");
  assert.equal(ViewModel.dms(-18.8177226), "-18°49′03.80″");
  assert.equal(ViewModel.dms(23.44216310179027), "23°26′31.79″");
  assert.equal(ViewModel.dms(null), ViewModel.DASH);
});

// 上面那個 case 的名稱宣稱「不進位到 60」，但它挑的三個值都離秒的進位邊界很遠，
// 所以那句宣稱從來沒有被檢驗過。秒位是 toFixed(2)，落在每度最後 5 毫弧秒內的
// 黃經會四捨五入成 60.00 而分位不跟著進位，輸出 `29°59′60.00″` —— 一個不存在的
// 六十進位值。黃經是連續量，這個區間是可達的。
test("秒四捨五入到 60 時必須進位到上一階，不得輸出 59′60.00″", () => {
  assert.equal(ViewModel.dms(23.99999899), "24°00′00.00″");
  assert.equal(ViewModel.dms(0.9999988888888889), "1°00′00.00″");
  // 負值走同一條進位路徑，正負號不得因進位而改變。
  assert.equal(ViewModel.dms(-23.99999899), "-24°00′00.00″");
  // 相鄰控制：只差一點點、不該進位的值必須維持原樣。
  assert.equal(ViewModel.dms(23.9999), "23°59′59.64″");
});

test("星座度數在秒進位時必須跨到下一個星座，不得停在 29°59′60.00″", () => {
  // 359.9999999° 四捨五入到百分之一秒就是 0°00′00.00″ 的牡羊座起點。
  assert.equal(ViewModel.signPosition(359.9999999), "牡羊 0°00′00.00″");
  // 相鄰控制：確實還在雙魚座的值不得被推進牡羊座。
  assert.equal(ViewModel.signPosition(359.999), "雙魚 29°59′56.40″");
});

test("motion_sign 缺值不得被斷言為順行", () => {
  // heliocentric/barycentric 下後端對太陽與月交點回傳 motion_sign: null
  // （backend/app/core/bodies.py 的 degenerate 分支）。同一列的黃經正確顯示
  // 為 DASH，行進欄卻不能因此變成一句肯定的天文陳述。
  const response = JSON.parse(JSON.stringify(EXACT));
  const sun = response.astronomical_data.bodies.find((b) => b.key === "sun");
  sun.motion_sign = null;
  sun.longitude = null;
  const bodies = sectionById(ViewModel.buildSections(response), "bodies");
  const table = bodies.tables[0];
  const column = table.columns.indexOf("行進");
  assert.ok(column >= 0, "必須找得到行進欄");
  const row = table.rows.find((item) => item[0] === sun.name || item[0] === "太陽");
  assert.equal(row[column], ViewModel.DASH);
  // 相鄰控制：有值的天體仍必須讀得出順逆，修復不得把整欄改成 DASH。
  const moving = table.rows.find((item) => item[column] !== ViewModel.DASH);
  assert.ok(
    moving && ["順行", "逆行"].includes(moving[column]),
    "有 motion_sign 的天體仍必須顯示順行或逆行"
  );
});

test("星座度數由黃經換寫，且跨 360° 仍正確", () => {
  assert.equal(ViewModel.signPosition(0), "牡羊 0°00′00.00″");
  assert.equal(ViewModel.signPosition(359.5), "雙魚 29°30′00.00″");
  assert.equal(ViewModel.signPosition(-0.5), "雙魚 29°30′00.00″");
  assert.equal(ViewModel.signPosition(54.17513298166507).startsWith("金牛"), true);
});

test("原始精度不因顯示格式化而流失", () => {
  const document = documentFor(EXACT);
  const json = JSON.parse(Exporters.renderJson(document));
  assert.equal(
    json.source_response.astronomical_data.bodies[0].longitude,
    EXACT.astronomical_data.bodies[0].longitude
  );
});

// ---------------------------------------------------------------------------
// §10 要求的可執行驗收：sections 改動時，畫面與各匯出必須同步改變
// ---------------------------------------------------------------------------

test("sections 改變時，view tree 與每一種匯出都同步改變（§10）", () => {
  const response = EXACT;
  const sections = ViewModel.buildSections(response);
  const before = Exporters.createDocument(response, sections);

  const mutated = sections.map((section) =>
    section.id === "bodies"
      ? {
          ...section,
          notes: section.notes.concat("SENTINEL_NOTE_FOR_SYNC_TEST"),
        }
      : section
  );
  const after = Exporters.createDocument(response, mutated);

  const renderers = {
    view_tree: (doc) => JSON.stringify(ViewModel.buildViewTree(doc)),
    text: Exporters.renderPlainText,
    csv: Exporters.renderCsv,
    json: Exporters.renderJson,
    markdown: Exporters.renderMarkdown,
  };

  for (const [name, render] of Object.entries(renderers)) {
    const beforeOut = render(before);
    const afterOut = render(after);
    assert.notEqual(beforeOut, afterOut, `${name} 未隨 sections 改變`);
    assert.ok(
      afterOut.includes("SENTINEL_NOTE_FOR_SYNC_TEST"),
      `${name} 沒有帶上新的 section 內容`
    );
    assert.ok(
      !beforeOut.includes("SENTINEL_NOTE_FOR_SYNC_TEST"),
      `${name} 在改動前就已經含有哨兵字串，這個測試證明不了同步`
    );
  }
});

test("view tree 只含純資料，不含函式或 DOM 節點", () => {
  const tree = ViewModel.buildViewTree(documentFor(EXACT));
  const serialized = JSON.stringify(tree);
  assert.equal(typeof serialized, "string");
  assert.deepEqual(JSON.parse(serialized), tree);
});

test("每個 section 都帶著環的指派，未分類即為失敗", () => {
  const tree = ViewModel.buildViewTree(documentFor(EXACT));
  const rings = new Set(tree.sections.map((section) => section.ring));
  tree.sections.forEach((section) => {
    assert.ok(
      ["ring-1", "ring-2", "ring-3", "ring-v"].includes(section.ring),
      `${section.id} 沒有有效的環指派`
    );
  });
  assert.ok(rings.has("ring-1") && rings.has("ring-2")
    && rings.has("ring-3") && rings.has("ring-v"),
    "四個環都必須在畫面上出現，否則分層是空話");
});

// ---------------------------------------------------------------------------
// 收據狀態必須進入每一種匯出
// ---------------------------------------------------------------------------

test("被拒絕的模組在文字、Markdown 與 CSV 匯出中都帶著原因代碼", () => {
  const document = documentFor(SIDEREAL);
  const reason = "sidereal_dignity_basis_not_authorized";

  const textOut = Exporters.renderPlainText(document);
  assert.ok(textOut.includes("狀態: 產品明確拒絕"), "TXT 缺少狀態");
  assert.ok(textOut.includes(reason), "TXT 缺少原因代碼");

  const markdownOut = Exporters.renderMarkdown(document);
  assert.ok(markdownOut.includes("**狀態：** 產品明確拒絕"), "Markdown 缺少狀態");
  assert.ok(markdownOut.includes(reason), "Markdown 缺少原因代碼");

  const csvOut = Exporters.renderCsv(document);
  assert.ok(csvOut.includes("refused"), "CSV 缺少狀態");
  assert.ok(csvOut.includes(reason), "CSV 缺少原因代碼");
});

test("未知的 section 狀態一律拒絕，不得退化成『已計算』", () => {
  assert.throws(
    () => Exporters.createDocument(EXACT, [
      { id: "x", title: "x", status: { state: "probably_fine" } },
    ]),
    /不支援的 section status\.state/
  );
});

test("三份 fixture 都能建立 canonical document", () => {
  for (const [name, response] of Object.entries({
    exact: EXACT, sidereal: SIDEREAL, date_only: DATE_ONLY,
  })) {
    const document = documentFor(response);
    assert.equal(document.source_response, response, `${name} 未保留原始回應`);
    assert.ok(document.sections.length >= 9, `${name} 的 section 數不足`);
  }
});

test("任何表格儲存格都不得是物件——[object Object] 不會報錯，只會讓人讀不到", () => {
  // 2026-08-06：實測發現四個儲存格印出 [object Object]，其中一個是
  // 隱私未涵蓋層，那是 L1 宣稱。後端把某些收據欄位設計成物件或物件陣列，
  // 而把它們直接放進 rows 不會拋錯，只會靜靜地字串化。
  // 這個檢查掃過三份 fixture 的每一格，因此新增的欄位一旦忘了攤平就會紅。
  // all-modules 必須在這份清單裡：物件型收據欄位（flag_policy、
  // ephemeris_dataset_lineage、uncovered_layers）只在全模組開啟的回應裡出現，
  // 只掃前三份的話這個閘門看不到它們——正是它要防的那個 bug 的形狀。
  for (const [name, response] of [
    ["exact", EXACT],
    ["sidereal", SIDEREAL],
    ["date_only", DATE_ONLY],
    ["all_modules", loadFixture("chart-all-modules")],
  ]) {
    for (const section of ViewModel.buildSections(response)) {
      for (const table of section.tables) {
        table.rows.forEach((row, rowIndex) => {
          row.forEach((cell, cellIndex) => {
            assert.ok(
              cell === null || typeof cell !== "object",
              `${name} / ${section.id} / ${table.title} 第 ${rowIndex + 1} 列`
                + ` 第 ${cellIndex + 1} 欄是物件，會渲染成 [object Object]：`
                + String(JSON.stringify(cell)).slice(0, 120)
            );
            assert.ok(
              String(cell) !== "[object Object]",
              `${name} / ${section.id} 有儲存格字串化為 [object Object]`
            );
          });
        });
      }

      // 2026-08-06 第二輪：只掃儲存格是不夠的。實測在 /validation 上又抓到
      // 三處 [object Object]，全部在 notes——兩處是模板字串內插物件
      // （lunar_apsides 的 coordinate_policy、antiscia 的 scope），一處是
      // 整包契約物件被當成一條註記推進陣列（horizon_events）。
      // 上一版閘門的比對範圍是我自己畫的「儲存格」，漏掉的正好是它以外那些。
      // 這裡改成走完整棵 section，不再挑範圍。
      assert.ok(Array.isArray(section.notes), `${name} / ${section.id} notes 不是陣列`);
      section.notes.forEach((note, index) => {
        assert.equal(
          typeof note, "string",
          `${name} / ${section.id} 第 ${index + 1} 條註記不是字串，會渲染成`
            + ` [object Object]：${String(JSON.stringify(note)).slice(0, 120)}`
        );
      });

      const walk = (value, trail) => {
        if (typeof value === "string") {
          assert.ok(
            !value.includes("[object Object]"),
            `${name} / ${section.id} 的 ${trail} 內插了物件：${value.slice(0, 140)}`
          );
          return;
        }
        if (Array.isArray(value)) {
          value.forEach((item, index) => walk(item, `${trail}[${index}]`));
          return;
        }
        if (value && typeof value === "object") {
          for (const [key, item] of Object.entries(value)) walk(item, `${trail}.${key}`);
        }
      };
      walk(section, "section");
    }
  }
});
