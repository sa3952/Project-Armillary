(function attachExportPipeline(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ChartExport = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function buildExportPipeline() {
  "use strict";

  const EXPORT_CONTRACT_VERSION = "0.1.2";
  const OUTPUT_MODES = Object.freeze({
    chart_data_only: "chart_data_only",
    reproducibility_detail: "reproducibility_detail",
  });
  const REPRODUCIBILITY_SECTION_IDS = new Set([
    "trace", "requested_options", "contract", "receipt",
  ]);
  const SUPPORTED_RESPONSE_CONTRACTS = Object.freeze({
    "0.13.0": Object.freeze(["0.6.0"]),
  });

  function assertCompatibleSourceResponse(sourceResponse) {
    const apiVersion = sourceResponse && sourceResponse.schema_version;
    if (!apiVersion) {
      throw new Error("缺少 API schema 版本，不能建立可追溯輸出。");
    }
    const acceptedDossiers = SUPPORTED_RESPONSE_CONTRACTS[apiVersion];
    if (!acceptedDossiers) {
      throw new Error(`不相容的 API schema 版本：${apiVersion}。`);
    }
    const dossierVersion =
      sourceResponse &&
      sourceResponse.calculation_dossier &&
      sourceResponse.calculation_dossier.dossier_version;
    if (!dossierVersion) {
      throw new Error("缺少 Calculation Dossier 版本，不能建立可追溯輸出。");
    }
    if (!acceptedDossiers.includes(dossierVersion)) {
      throw new Error(
        `不相容的 Calculation Dossier 版本：${dossierVersion}（API ${apiVersion}）。`
      );
    }
  }

  function text(value) {
    if (value === null || value === undefined) return "—";
    return String(value);
  }

  function plainScalar(value) {
    return text(value).replace(/[\t\r\n]+/g, " ");
  }

  function normalizeTable(table) {
    return {
      title: text(table.title || ""),
      columns: (table.columns || []).map(text),
      rows: (table.rows || []).map((row) => row.map(text)),
    };
  }

  // 收據狀態是 section 模型的第一級欄位，不是備註字串。
  //
  // backend 對每個模組回 requested / executed / applicable / available /
  // source / reason_code，四個布林的組合語義各不相同。若把它們壓平成一句備註，
  // 畫面就只能靠解析字串才能把「產品明確拒絕」與「使用者沒有要求」畫成不同樣子，
  // 而契約 §13.2 第 24 項要求這兩者在畫面上必須不同。狀態進模型，
  // 渲染與各 serializer 才會拿到同一份、且不必再猜。
  const SECTION_STATES = Object.freeze({
    present: "已計算",
    defaulted: "產品預設帶入",
    refused: "產品明確拒絕",
    not_requested: "未請求",
    executed_unavailable: "已執行，無可用結果",
  });

  function normalizeStatus(status) {
    if (status === undefined || status === null) {
      return { state: "present", label: SECTION_STATES.present, reason_code: "" };
    }
    if (typeof status !== "object" || Array.isArray(status)) {
      throw new Error("section status 必須是物件。");
    }
    const state = text(status.state);
    if (!Object.prototype.hasOwnProperty.call(SECTION_STATES, state)) {
      // Fail closed：把未知狀態當成 present 會讓「被拒絕」看起來像「算出來了」。
      throw new Error(`不支援的 section status.state: ${state}`);
    }
    return {
      state,
      label: SECTION_STATES[state],
      reason_code: status.reason_code ? text(status.reason_code) : "",
    };
  }

  function normalizeSection(section, index) {
    return {
      id: text(section.id || `section-${index + 1}`),
      title: text(section.title || `Section ${index + 1}`),
      layer_label: text(section.layer_label || ""),
      status: normalizeStatus(section.status),
      notes: (section.notes || []).map(text).filter(Boolean),
      tables: (section.tables || []).map(normalizeTable),
      blocks: (section.blocks || []).map(text).filter(Boolean),
    };
  }

  function createDocument(sourceResponse, sections) {
    if (!sourceResponse || !sourceResponse.calculation_dossier) {
      throw new Error("Calculation Dossier 缺失，不能建立可追溯輸出。");
    }
    assertCompatibleSourceResponse(sourceResponse);
    return {
      export_contract_version: EXPORT_CONTRACT_VERSION,
      calculation_dossier: sourceResponse.calculation_dossier,
      source_response: sourceResponse,
      sections: (sections || []).map(normalizeSection),
    };
  }

  function projectOutputDocument(document, mode = OUTPUT_MODES.reproducibility_detail) {
    if (!Object.prototype.hasOwnProperty.call(OUTPUT_MODES, mode)) {
      throw new Error(`不支援的輸出詳細度: ${mode}`);
    }
    return {
      ...document,
      output_mode: mode,
      include_reproducibility: mode === OUTPUT_MODES.reproducibility_detail,
      sections: mode === OUTPUT_MODES.reproducibility_detail
        ? document.sections
        : document.sections.filter((section) => !REPRODUCIBILITY_SECTION_IDS.has(section.id)),
    };
  }

  // FPI-2026-08-06-E-009。契約 §2 說 section copy 是「TSV 型純文字，供貼入
  // 筆記或試算表」。TSV 的欄界是 tab、列界是換行，所以儲存格裡的 tab 會多切出
  // 一欄、換行會拆成兩列——而使用者貼進試算表後**不會察覺資料已錯位**。
  // markdownCell 早就對表格做了對應處理（換行轉 <br>），TSV 沒有。
  //
  // 折成空白會失去換行這個字形，但保住欄列結構。兩害相權取結構：錯位是靜默的，
  // 少一個換行不是。目前後端進入儲存格的自由文字都不含這些字元，這裡不依賴那件事。
  function tsvCell(value) {
    // Codex 複審打回：上一版寫的是 `.replace(/\r?\n/g, " ")`，只涵蓋 CRLF 與 LF。
    // **單獨的 CR 對試算表本身就是 record separator**，貼上後照樣拆列，
    // 而那正是這個 finding 要防的靜默錯位。改為逐一中和三個控制字元。
    return text(value).replace(/[\t\r\n]+/g, " ");
  }

  function renderTableText(table) {
    const lines = [];
    if (table.title) lines.push(`[${table.title}]`);
    lines.push(table.columns.map(tsvCell).join("\t"));
    table.rows.forEach((row) => lines.push(row.map(tsvCell).join("\t")));
    return lines.join("\n");
  }

  function renderSectionText(section) {
    if (!section || typeof section !== "object" || Array.isArray(section)) {
      throw new Error("缺少可用的 canonical section。");
    }
    const lines = [`## ${plainScalar(section.title)}`];
    if (section.layer_label) lines.push(`層級: ${plainScalar(section.layer_label)}`);
    if (section.status && section.status.state !== "present") {
      lines.push(`狀態: ${plainScalar(section.status.label)}`);
      if (section.status.reason_code) {
        lines.push(`原因代碼: ${plainScalar(section.status.reason_code)}`);
      }
    }
    section.notes.forEach((note) => lines.push(`備註: ${plainScalar(note)}`));
    section.tables.forEach((table) => {
      lines.push("", renderTableText(table));
    });
    section.blocks.forEach((block) => lines.push("", plainScalar(block)));
    return lines.join("\n").trim();
  }

  function warningLines(dossier) {
    const warnings = Array.isArray(dossier.warnings) ? dossier.warnings : [];
    if (!warnings.length) return ["Warnings: 無"];
    return warnings.map((warning) => {
      const code = warning.code || "UNSPECIFIED_WARNING";
      const message = warning.message || warning.note || JSON.stringify(warning);
      return `Warning [${code}]: ${message}`;
    });
  }

  function renderPlainText(document) {
    const dossier = document.calculation_dossier;
    const lines = [
      "古典西洋占星天文計算資料",
      "這是計算資料與重現收據，不是占星解讀。",
      `Export Contract ${document.export_contract_version}`,
      `API Schema ${document.source_response.schema_version || "—"}`,
      `Calculation Dossier ${dossier.dossier_version || "—"}`,
      `Authority: ${dossier.authority || "—"}`,
    ];
    const utc = dossier.time_conversion && dossier.time_conversion.utc_iso_8601;
    if (utc) lines.push(`UTC: ${utc}`);
    lines.push(...warningLines(dossier));
    document.sections.forEach((section) => {
      lines.push("", "============================================================", "");
      lines.push(renderSectionText(section));
    });
    if (document.include_reproducibility !== false) {
      lines.push(
        "",
        "============================================================",
        "",
        "## Calculation Dossier（機器可讀）",
        JSON.stringify(dossier, null, 2)
      );
    }
    return lines.join("\n").trim() + "\n";
  }

  function csvCell(value) {
    let raw = text(value);
    const formulaLike = /^[\t\r\n \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]*[=+\-@]/u.test(raw);
    const numericLiteral = /^[\t\r\n ]*[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?[\t\r\n ]*$/.test(raw);
    if (formulaLike && !numericLiteral) raw = "'" + raw;
    return /[",\r\n]/.test(raw) ? `"${raw.replace(/"/g, '""')}"` : raw;
  }

  function renderCsv(document) {
    const rows = [["section_id", "section", "table", "row", "field", "value"]];
    const dossier = document.calculation_dossier;
    const flattenedDossier = [];
    function flatten(value, path) {
      if (Array.isArray(value)) {
        if (!value.length) flattenedDossier.push([path, "[]"]);
        value.forEach((item, index) => flatten(item, `${path}[${index}]`));
      } else if (value !== null && typeof value === "object") {
        const entries = Object.entries(value);
        if (!entries.length) flattenedDossier.push([path, "{}"]);
        entries.forEach(([key, item]) => {
          flatten(item, path ? `${path}.${key}` : key);
        });
      } else {
        const scalar = value === null ? "null" : String(value);
        flattenedDossier.push([path, scalar]);
      }
    }
    if (document.include_reproducibility !== false) {
      flatten(dossier, "");
      flattenedDossier.unshift(
        ["export_contract_version", document.export_contract_version],
        ["api_schema_version", document.source_response.schema_version || "—"]
      );
      flattenedDossier.forEach(([field, value], index) => {
        rows.push([
          "_dossier", "Calculation Dossier", "JSON paths",
          index + 1, field, value,
        ]);
      });
    }
    document.sections.forEach((section) => {
      rows.push([
        section.id,
        section.title,
        "Status",
        1,
        "state",
        section.status.state,
      ]);
      rows.push([
        section.id,
        section.title,
        "Status",
        2,
        "reason_code",
        section.status.reason_code || "—",
      ]);
      section.notes.forEach((note, index) => {
        rows.push([section.id, section.title, "Notes", index + 1, "note", note]);
      });
      section.tables.forEach((table, tableIndex) => {
        const tableName = table.title || `Table ${tableIndex + 1}`;
        table.rows.forEach((row, rowIndex) => {
          table.columns.forEach((field, columnIndex) => {
            rows.push([
              section.id,
              section.title,
              tableName,
              rowIndex + 1,
              field,
              row[columnIndex] === undefined ? "—" : row[columnIndex],
            ]);
          });
        });
      });
      section.blocks.forEach((block, index) => {
        rows.push([section.id, section.title, "Blocks", index + 1, "text", block]);
      });
    });
    return rows.map((row) => row.map(csvCell).join(",")).join("\r\n");
  }

  function renderJson(document) {
    const sourceResponse = { ...document.source_response };
    if (document.include_reproducibility === false) {
      for (const field of [
        "calculation_dossier", "calculation_trace",
        "requested_options", "output_contract",
      ]) delete sourceResponse[field];
    }
    return JSON.stringify({
      export_contract_version: document.export_contract_version,
      output_mode: document.output_mode || OUTPUT_MODES.reproducibility_detail,
      source_response: sourceResponse,
      display_sections: document.sections,
    }, null, 2) + "\n";
  }

  /**
   * Markdown 允許 raw HTML，所以任何未經處理就插進 Markdown 的自由文字，
   * 在支援 HTML 的 renderer 裡都是 active content（PIA-2026-08-06-002）。
   *
   * 這是 serializer 自己的責任，不是上游的：契約 §3A 明文規定「不得因目前
   * schema 沒有自由文字就假設未來輸入永遠安全」。同一個值在 DOM 走
   * `textContent`、在 CSV 有 formula hardening，只有 Markdown 沒有守衛。
   *
   * `&` 必須先換，否則後面兩個換出來的 `&lt;` 會被自己再換一次。
   */
  function escapeMarkdownText(value) {
    return text(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/([\\`*_{}\[\]()#+|])/g, "\\$1")
      .replace(/\r\n?|\n/g, "<br>");
  }

  function markdownCell(value) {
    // 先 escape 再插 <br>：反過來會把我們自己刻意產生的換行標記也吃掉。
    //
    // `FPI-2026-08-06-E-023`：上一版寫 `/\r?\n/`，與 tsvCell 修好之前一模一樣的
    // 錯誤——**單獨的 CR 走不到那個分支**，於是裸 CR 直接進 Markdown 表格列。
    // 我在同一輪修好了 tsvCell（E-009）卻沒有回頭看它旁邊這個姊妹函式。
    // 三種行結束符（CRLF／CR／LF）現在一起處理。
    return escapeMarkdownText(value)
      .replace(/\\/g, "\\\\")
      .replace(/\|/g, "\\|")
      .replace(/\r\n?|\n/g, "<br>");
  }

  function renderMarkdownTable(table) {
    if (!table.columns.length) return "";
    const lines = [
      `| ${table.columns.map(markdownCell).join(" | ")} |`,
      `| ${table.columns.map(() => "---").join(" | ")} |`,
    ];
    table.rows.forEach((row) => {
      const padded = table.columns.map((_, index) => markdownCell(row[index]));
      lines.push(`| ${padded.join(" | ")} |`);
    });
    return lines.join("\n");
  }

  // 依內容選一段比內容裡最長的反引號串還長的 fence。原本只判斷「有沒有 ```」，
  // 對 ```` 之類更長的串就不夠；一併補上。
  function longestBacktickRun(value) {
    const runs = String(value).match(/`+/g);
    return runs ? Math.max(...runs.map((run) => run.length)) : 0;
  }

  function fenceFor(value) {
    return "`".repeat(Math.max(3, longestBacktickRun(value) + 1));
  }

  // FPI-2026-08-06-E-008。inline code span 內的反引號會提早關閉 span，
  // 後面的文字就落在 code 之外、不受 escape。加長分隔並在必要時補空白
  // （CommonMark：首尾為反引號時各補一個空白，解析時會被去掉）。
  function markdownInlineCode(value) {
    const content = text(value);
    const fence = fenceFor(content);
    const pad =
      content.startsWith("`") || content.endsWith("`") ? " " : "";
    return `${fence}${pad}${content}${pad}${fence}`;
  }

  function markdownCodeFence(value) {
    const serialized = JSON.stringify(value, null, 2);
    const fence = fenceFor(serialized);
    return `${fence}json\n${serialized}\n${fence}`;
  }

  function renderMarkdown(document) {
    const dossier = document.calculation_dossier;
    const lines = [
      "# 古典西洋占星天文計算資料",
      "",
      "> 這是計算資料與重現收據，不是占星解讀。數值應連同單位、時間尺度、座標系統與 Calculation Dossier 一起使用。",
      "",
      "## 輸出契約",
      "",
      `- Export contract: \`${document.export_contract_version}\``,
      `- API schema: \`${document.source_response.schema_version || "—"}\``,
      `- Calculation Dossier: \`${dossier.dossier_version || "—"}\``,
      // FPI-2026-08-06-E-008：這一個插值點漏了 escape。同函式其餘六處都有，
      // 上方註解也寫著「每一個把自由文字插進 Markdown 的位置都要經過 escape」。
      `- Authority: ${escapeMarkdownText(dossier.authority || "—")}`,
    ];
    const utc = dossier.time_conversion && dossier.time_conversion.utc_iso_8601;
    if (utc) lines.push(`- UTC: ${markdownInlineCode(utc)}`);
    lines.push("", "### Warnings", "");
    const warnings = warningLines(dossier);
    warnings.forEach((warning) => lines.push(`- ${escapeMarkdownText(warning)}`));

    // 每一個把自由文字插進 Markdown 的位置都要經過 escape，不只是表格儲存格。
    // 原本標題、備註與表格標題三處都是直接插值，raw HTML 一樣照過。
    document.sections.forEach((section) => {
      lines.push("", `## ${escapeMarkdownText(section.title)}`, "");
      if (section.layer_label) {
        lines.push(`**層級：** ${escapeMarkdownText(section.layer_label)}`, "");
      }
      if (section.status && section.status.state !== "present") {
        // Codex 複審打回：這是同一輪裡我漏掉的第四個插值點。上面三處都改用了
        // markdownInlineCode，這裡還留著固定的單一反引號，於是
        // `alpha\`x\`omega` 會提前關閉 span，中間那段變成不受保護的散文。
        const reason = section.status.reason_code
          ? `（原因代碼 ${markdownInlineCode(section.status.reason_code)}）`
          : "";
        lines.push(`**狀態：** ${escapeMarkdownText(section.status.label)}${reason}`, "");
      }
      section.notes.forEach((note) => lines.push(`> ${escapeMarkdownText(note)}`, ""));
      section.tables.forEach((table) => {
        if (table.title) lines.push(`### ${escapeMarkdownText(table.title)}`, "");
        lines.push(renderMarkdownTable(table), "");
      });
      // blocks 進的是 ```text fence，fence 內不解析 HTML，因此不 escape——
      // 那會讓使用者在程式碼區塊裡看到 &lt;。
      // 固定長度的 fence 會被含 ``` 的內容撐破，後續文字就落在 fence 之外。
      // 同檔的 markdownCodeFence 早就示範了正確做法，這裡沿用同一個 fenceFor。
      section.blocks.forEach((block) => {
        const fence = fenceFor(block);
        lines.push(`${fence}text`, block, fence, "");
      });
    });

    if (document.include_reproducibility !== false) {
      lines.push(
        "", "## Calculation Dossier（機器可讀）", "",
        "以下區塊是本次回應內同一份後端收據；可用來核對輸入、時間轉換、計算政策、星曆來源與警告。",
        "", markdownCodeFence(dossier), ""
      );
    }
    return lines.join("\n").replace(/\n{3,}/g, "\n\n");
  }

  function buildDownloadName(extension) {
    const clean = String(extension).toLowerCase().replace(/^\./, "");
    if (!["csv", "json", "txt", "md"].includes(clean)) {
      throw new Error(`不支援的匯出格式: ${extension}`);
    }
    return `classical-astrology-export.${clean}`;
  }

  function buildDownloadArtifact(
    document,
    format,
    mode = OUTPUT_MODES.reproducibility_detail,
  ) {
    const definitions = {
      csv: {
        renderer: renderCsv,
        mime_type: "text/csv;charset=utf-8",
        prefix: "\uFEFF",
      },
      json: {
        renderer: renderJson,
        mime_type: "application/json;charset=utf-8",
        prefix: "",
      },
      txt: {
        renderer: renderPlainText,
        mime_type: "text/plain;charset=utf-8",
        prefix: "",
      },
      md: {
        renderer: renderMarkdown,
        mime_type: "text/markdown;charset=utf-8",
        prefix: "",
      },
    };
    const definition = definitions[format];
    if (!definition) throw new Error(`不支援的匯出格式: ${format}`);
    const projected = projectOutputDocument(document, mode);
    return {
      filename: buildDownloadName(format),
      mime_type: definition.mime_type,
      content: definition.prefix + definition.renderer(projected),
    };
  }

  function runDownloadAction(document, format, deliverArtifact, mode) {
    try {
      deliverArtifact(buildDownloadArtifact(document, format, mode));
      return { ok: true, error_message: null };
    } catch (error) {
      return {
        ok: false,
        error_message: error instanceof Error ? error.message : String(error),
      };
    }
  }

  return {
    EXPORT_CONTRACT_VERSION,
    OUTPUT_MODES,
    SECTION_STATES,
    SUPPORTED_RESPONSE_CONTRACTS,
    assertCompatibleSourceResponse,
    createDocument,
    projectOutputDocument,
    normalizeTable,
    renderSectionText,
    renderPlainText,
    renderCsv,
    renderJson,
    renderMarkdown,
    buildDownloadName,
    buildDownloadArtifact,
    runDownloadAction,
  };
});
