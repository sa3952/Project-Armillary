const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ViewModel = require("../zh-TW/view-model.js");

const ALL = JSON.parse(
  fs.readFileSync(path.join(__dirname, "fixtures", "chart-all-modules.json"), "utf8")
);

const LAYERS = ["astronomical_data", "derived_geometry", "derived_methods"];

function modulePaths(response) {
  // 頂層鍵與三層底下的模組都要納入。
  // 先前只掃三層，calculation_trace 是頂層鍵因而完全沒被檢查過。
  const paths = Object.keys(response);
  LAYERS.forEach((layer) => {
    Object.keys(response[layer] || {}).forEach((key) => paths.push(`${layer}.${key}`));
  });
  return paths;
}

test("後端回的每一個模組都必須在覆蓋宣告裡有交代", () => {
  const declared = new Set(Object.keys(ViewModel.MODULE_COVERAGE));
  const actual = modulePaths(ALL);

  const undeclared = actual.filter((p) => !declared.has(p));
  assert.deepEqual(undeclared, [],
    "這些模組後端會回，但覆蓋宣告沒有交代它們是被哪個 section 承接、還是刻意不顯示");

  const stale = [...declared].filter((p) => !actual.includes(p));
  assert.deepEqual(stale, [], "覆蓋宣告提到後端已不再回傳的模組");
});

test("每個宣告不是指向 section 就是寫明不顯示的理由", () => {
  Object.entries(ViewModel.MODULE_COVERAGE).forEach(([path_, entry]) => {
    const hasSection = typeof entry.section === "string" && entry.section.length > 0;
    const hasReason = typeof entry.not_rendered === "string" && entry.not_rendered.length > 0;
    assert.ok(hasSection !== hasReason,
      `${path_} 必須擇一：由某個 section 承接，或寫明刻意不顯示的理由`);
  });
});

test("宣告承接的 section 必須真的存在於全開回應的輸出中", () => {
  // 這一項才是真正防止「勾了沒東西」的閘門：
  // 宣告說某模組由 section X 承接，那麼在全部模組開啟的回應下，X 必須真的被產生。
  const sections = ViewModel.buildSections(ALL);
  const ids = new Set(sections.map((s) => s.id));

  const promised = [...new Set(
    Object.values(ViewModel.MODULE_COVERAGE)
      .map((entry) => entry.section)
      .filter(Boolean)
  )];
  const missing = promised.filter((id) => !ids.has(id));
  assert.deepEqual(missing, [],
    "覆蓋宣告承諾由這些 section 承接，但全開回應下它們沒有被產生");
});

test("全開回應下，沒有任何 section 是空殼", () => {
  // 「有 section 但裡面沒東西」與「沒有 section」對使用者是同一件事。
  const sections = ViewModel.buildSections(ALL);
  const hollow = sections
    .filter((s) => s.status.state === "present")
    .filter((s) => s.tables.every((t) => t.rows.length === 0) && s.blocks.length === 0)
    .map((s) => s.id);
  assert.deepEqual(hollow, [],
    "這些 section 標為已計算，但一列資料都沒有——那是靜默的空表");
});

test("每一張天體表都用同一組欄位定義，不得有一處漏接", () => {
  // 2026-08-05：BODY_COLUMNS / bodyRows 寫好後只接上了新區塊，
  // bodiesSection 本身仍是舊的七欄，而我卻回報「已補到十欄」。
  // 空表閘門抓不到這種錯——表格有資料，只是欄位少。
  const sections = ViewModel.buildSections(ALL);
  const bodyTables = sections
    .flatMap((s) => s.tables.map((t) => ({ id: s.id, table: t })))
    // 以「赤經 α」判別：只有完整的三套座標表才有它。
    // 落宮表也以「天體」開頭並含黃經，但它不是座標表，不該被要求同寬。
    .filter(({ table }) => table.columns.includes("赤經 α"));

  assert.ok(bodyTables.length >= 3, "天體類表格應該不只一處");
  const widths = new Set(bodyTables.map(({ table }) => table.columns.length));
  assert.equal(widths.size, 1,
    `天體類表格欄數不一致：${bodyTables.map(({ id, table }) => `${id}=${table.columns.length}`).join(", ")}`);
  bodyTables.forEach(({ id, table }) => {
    table.rows.forEach((row) => {
      assert.equal(row.length, table.columns.length, `${id} 有一列的欄數與表頭不符`);
    });
  });
});

test("所有表格的每一列欄數都等於表頭欄數", () => {
  ViewModel.buildSections(ALL).forEach((section) => {
    section.tables.forEach((table) => {
      table.rows.forEach((row, index) => {
        assert.equal(row.length, table.columns.length,
          `${section.id} / ${table.title} 第 ${index + 1} 列欄數不符`);
      });
    });
  });
});
