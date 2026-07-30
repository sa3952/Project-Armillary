(function attachExportPipeline(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ChartExport = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function buildExportPipeline() {
  "use strict";

  const EXPORT_CONTRACT_VERSION = "0.1.2";

  function text(value) {
    if (value === null || value === undefined) return "—";
    return String(value);
  }

  function normalizeTable(table) {
    return {
      title: text(table.title || ""),
      columns: (table.columns || []).map(text),
      rows: (table.rows || []).map((row) => row.map(text)),
    };
  }

  function normalizeSection(section, index) {
    return {
      id: text(section.id || `section-${index + 1}`),
      title: text(section.title || `Section ${index + 1}`),
      layer_label: text(section.layer_label || ""),
      notes: (section.notes || []).map(text).filter(Boolean),
      tables: (section.tables || []).map(normalizeTable),
      blocks: (section.blocks || []).map(text).filter(Boolean),
    };
  }

  function createDocument(sourceResponse, sections) {
    if (!sourceResponse || !sourceResponse.calculation_dossier) {
      throw new Error("Calculation Dossier 缺失，不能建立可追溯輸出。");
    }
    return {
      export_contract_version: EXPORT_CONTRACT_VERSION,
      calculation_dossier: sourceResponse.calculation_dossier,
      source_response: sourceResponse,
      sections: (sections || []).map(normalizeSection),
    };
  }

  function renderTableText(table) {
    const lines = [];
    if (table.title) lines.push(`[${table.title}]`);
    lines.push(table.columns.join("\t"));
    table.rows.forEach((row) => lines.push(row.join("\t")));
    return lines.join("\n");
  }

  function renderSectionText(section) {
    if (!section || typeof section !== "object" || Array.isArray(section)) {
      throw new Error("缺少可用的 canonical section。");
    }
    const lines = [`## ${section.title}`];
    if (section.layer_label) lines.push(`層級: ${section.layer_label}`);
    section.notes.forEach((note) => lines.push(`備註: ${note}`));
    section.tables.forEach((table) => {
      lines.push("", renderTableText(table));
    });
    section.blocks.forEach((block) => lines.push("", block));
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
    lines.push(
      "",
      "============================================================",
      "",
      "## Calculation Dossier（機器可讀）",
      JSON.stringify(dossier, null, 2)
    );
    return lines.join("\n").trim() + "\n";
  }

  function csvCell(value) {
    let raw = text(value);
    const formulaLike = /^[\t\r\n ]*[=+\-@]/.test(raw);
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
    flatten(dossier, "");
    flattenedDossier.unshift(
      ["export_contract_version", document.export_contract_version],
      ["api_schema_version", document.source_response.schema_version || "—"]
    );
    flattenedDossier.forEach(([field, value], index) => {
      rows.push([
        "_dossier",
        "Calculation Dossier",
        "JSON paths",
        index + 1,
        field,
        value,
      ]);
    });
    document.sections.forEach((section) => {
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
    return JSON.stringify({
      export_contract_version: document.export_contract_version,
      source_response: document.source_response,
      display_sections: document.sections,
    }, null, 2) + "\n";
  }

  function markdownCell(value) {
    return text(value)
      .replace(/\\/g, "\\\\")
      .replace(/\|/g, "\\|")
      .replace(/\r?\n/g, "<br>");
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

  function markdownCodeFence(value) {
    const serialized = JSON.stringify(value, null, 2);
    const fence = serialized.includes("```") ? "````" : "```";
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
      `- Authority: ${dossier.authority || "—"}`,
    ];
    const utc = dossier.time_conversion && dossier.time_conversion.utc_iso_8601;
    if (utc) lines.push(`- UTC: \`${utc}\``);
    lines.push("", "### Warnings", "");
    const warnings = warningLines(dossier);
    warnings.forEach((warning) => lines.push(`- ${warning}`));

    document.sections.forEach((section) => {
      lines.push("", `## ${section.title}`, "");
      if (section.layer_label) lines.push(`**層級：** ${section.layer_label}`, "");
      section.notes.forEach((note) => lines.push(`> ${note}`, ""));
      section.tables.forEach((table) => {
        if (table.title) lines.push(`### ${table.title}`, "");
        lines.push(renderMarkdownTable(table), "");
      });
      section.blocks.forEach((block) => lines.push("```text", block, "```", ""));
    });

    lines.push(
      "",
      "## Calculation Dossier（機器可讀）",
      "",
      "以下區塊是本次回應內同一份後端收據；可用來核對輸入、時間轉換、計算政策、星曆來源與警告。",
      "",
      markdownCodeFence(dossier),
      ""
    );
    return lines.join("\n").replace(/\n{3,}/g, "\n\n");
  }

  function buildDownloadName(extension) {
    const clean = String(extension).toLowerCase().replace(/^\./, "");
    if (!["csv", "json", "txt", "md"].includes(clean)) {
      throw new Error(`不支援的匯出格式: ${extension}`);
    }
    return `classical-astrology-export.${clean}`;
  }

  function buildDownloadArtifact(document, format) {
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
    return {
      filename: buildDownloadName(format),
      mime_type: definition.mime_type,
      content: definition.prefix + definition.renderer(document),
    };
  }

  function runDownloadAction(document, format, deliverArtifact) {
    try {
      deliverArtifact(buildDownloadArtifact(document, format));
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
    createDocument,
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
