#!/usr/bin/env node
/**
 * 產生 /validation 內嵌的範例卷宗（SD-24：內嵌完整範例卷宗，須點選才展開）。
 *
 * 為什麼在 commit 時產生靜態 HTML，而不是在瀏覽器裡渲染：
 *
 * 1. 零 JS。/validation 因此不必新增任何 script 檔；frontend release manifest
 *    會記錄成品並作為 runtime 允許清單。`<details>` 原生提供「點選才展開」。
 * 2. 單一出處。畫面仍然是 view-model.js 的下游——本腳本呼叫的是
 *    /calculate 用的同一組 buildSections / createDocument / buildViewTree，
 *    不另寫一套 section 邏輯。契約 §10 要求 render 與 export 同為 canonical
 *    sections 的兄弟，若在 Python 裡重寫一遍就會出現第二個出處。
 * 3. 與 SD-27 一致：commit 時產生、把產出物 commit 進去，View Source 仍可讀。
 *
 * 輸出為 fragment，由 scripts/frontend/build_pages.py 代入 {{example_dossier}}。
 */

"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..", "..");
const ViewModel = require(path.join(ROOT, "frontend", "zh-TW", "view-model.js"));
const Exporters = require(path.join(ROOT, "frontend", "zh-TW", "exporters.js"));

// 全部模組啟用的那一份：範例要示範「明示指定界表、面、三分性與容許度表」之後
// 的完整輸出，用預設請求會出現一整排空值，讀者會誤以為功能壞了。
const FIXTURE = path.join(
  ROOT, "frontend", "tests", "fixtures", "chart-all-modules.json"
);
const OUT = path.join(ROOT, "scripts", "frontend", "pages", "_example-dossier.html");

const esc = (value) =>
  String(value === null || value === undefined ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

// section.status 是物件（exporters.js normalizeStatus）：{state, label, reason_code}。
// state 的字彙是封閉的；未知值一律失敗關閉，不得退化成「已計算」。
const KNOWN_STATES = new Set([
  "present",
  "defaulted",
  "refused",
  "not_requested",
  "executed_unavailable",
]);

function renderTable(table) {
  const head = table.columns.map((c) => `<th>${esc(c)}</th>`).join("");
  const body = table.rows
    .map((row) => `<tr>${row.map((cell) => `<td>${esc(cell)}</td>`).join("")}</tr>`)
    .join("\n        ");
  const caption = table.title
    ? `\n      <caption>${esc(table.title)}</caption>`
    : "";
  return `    <div class="dossier-table-wrap">
      <table class="log dossier-table">${caption}
        <thead><tr>${head}</tr></thead>
        <tbody>
        ${body}
        </tbody>
      </table>
    </div>`;
}

function renderSection(section) {
  const status = section.status || {};
  if (!KNOWN_STATES.has(status.state)) {
    // 失敗關閉：未知狀態不得退化成「已計算」。
    throw new Error(`未知的 section 狀態：${status.state}（${section.id}）`);
  }
  const reason = status.reason_code
    ? ` <code>${esc(status.reason_code)}</code>`
    : "";
  const parts = [];
  parts.push(`  <section class="dossier-section" data-ring="${esc(section.ring)}">
    <h3>${esc(section.title)}
      <span class="dossier-status" data-state="${esc(status.state)}">${esc(status.label)}${reason}</span>
    </h3>
    <p class="dossier-layer">${esc(section.layer_label)}</p>`);
  for (const child of section.children) {
    if (child.type === "note") {
      parts.push(`    <p class="dossier-note">${esc(child.text)}</p>`);
    } else if (child.type === "table") {
      parts.push(renderTable(child));
    }
  }
  parts.push("  </section>");
  return parts.join("\n");
}

function main() {
  const response = JSON.parse(fs.readFileSync(FIXTURE, "utf-8"));
  const doc = Exporters.createDocument(response, ViewModel.buildSections(response));
  const tree = ViewModel.buildViewTree(doc);

  const rowCount = tree.sections.reduce(
    (total, section) =>
      total +
      section.children.reduce(
        (n, child) => n + (child.type === "table" ? child.rows.length : 0),
        0
      ),
    0
  );

  const warnings = tree.header.warnings.length
    ? `\n  <h3 class="dossier-warnings-title">本次回應附帶的警告（${tree.header.warnings.length}）</h3>
    <ul class="dossier-warnings">${tree.header.warnings
        .map((w) => `<li><code>${esc(w.code)}</code> ${esc(w.message)}</li>`)
        .join("")}</ul>`
    : "";

  const html = `<!-- 由 scripts/frontend/render_example_dossier.cjs 產生，請勿手改。 -->
<details class="dossier">
  <summary>展開完整範例卷宗（${tree.sections.length} 節、${rowCount} 列）</summary>
  <div class="dossier-body">
  <p class="dossier-lede"><strong>這是範例，不是任何人的真實出生資料。</strong>
  底下每一個數值都由後端實際計算產生，不是手寫的；使用的是一組標示為範例的輸入。</p>
  <p class="dossier-lede">本次請求<strong>明示指定</strong>了界表、面、三分性與容許度表。
  這不是預設行為——不指定時那幾節會留空並說明原因，因為選一套表就是替你做方法裁決。</p>
  <dl class="legal-terms dossier-meta">
    <dt>匯出契約版本</dt><dd class="mono">${esc(tree.header.export_contract_version)}</dd>
    <dt>API schema 版本</dt><dd class="mono">${esc(tree.header.api_schema_version)}</dd>
    <dt>卷宗版本</dt><dd class="mono">${esc(tree.header.dossier_version)}</dd>
  </dl>${warnings}
${tree.sections.map(renderSection).join("\n")}
  </div>
</details>`;

  const rendered = html + "\n";
  const summary =
    `${tree.sections.length} sections, ${rowCount} rows, ` +
    `${(rendered.length / 1024).toFixed(0)} KB`;

  if (process.argv.includes("--check")) {
    const current = fs.existsSync(OUT) ? fs.readFileSync(OUT, "utf-8") : "";
    if (current !== rendered) {
      process.stderr.write(
        "EXAMPLE DOSSIER DRIFT: 產出物與 view-model／fixture 不一致。\n" +
          "re-run: node scripts/frontend/render_example_dossier.cjs\n"
      );
      process.exit(1);
    }
    process.stdout.write(`EXAMPLE DOSSIER CHECKED: ${summary}\n`);
    return;
  }

  fs.writeFileSync(OUT, rendered, "utf-8");
  process.stdout.write(`EXAMPLE DOSSIER: ${summary}\n`);
}

main();
