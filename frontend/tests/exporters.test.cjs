const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const Exporters = require("../zh-TW/exporters.js");
const compatibilityFixture = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "fixtures", "response-compatibility.json"),
    "utf8"
  )
);

// 版本一律取自 response-compatibility.json 的 accepted 組合，不在此重複字面值。
// 這兩個字串曾各自釘死在 0.10.0／0.3.2，而契約已走到 0.13.0／0.6.0，
// 使八個與版本無關的 serializer 測試一起變紅。單一出處讓它不會再漂第二次。
function fixture() {
  const response = {
    schema_version: compatibilityFixture.accepted.api_schema_version,
    calculation_dossier: {
      dossier_version: compatibilityFixture.accepted.dossier_version,
      authority: "backend-authored calculation receipt",
      input_receipt: {
        datetime: { year: 1997, month: 8, day: 17, hour: 9, minute: 42, second: 0 },
        location: { latitude: 24.1477, longitude: 120.6736, altitude_m: 80 },
      },
      time_conversion: { utc_iso_8601: "1997-08-17T01:42:00.000Z" },
      warnings: [{ code: "TEST_WARNING", message: "保留, 逗號與 \"引號\"" }],
      privacy: {
        privacy_attestation_version: "1.2.0",
        attestation_status: "provisional_pending_external_review",
        claims: [{
          id: "application_chart_path_no_persistence",
          status: "implemented_in_application_layer",
          enforcement_layer: "application_request_path",
          control: { id: "application-no-persistence-v1" },
          evidence: [{ reference: "backend/tests/test_privacy_logging.py" }],
          scope: { applies_to: "/api/chart" },
          limitations: ["No secure memory erasure claim"],
        }],
      },
    },
    library_info: { swiss_ephemeris_library_version: "2.10.03" },
    astronomical_data: {
      bodies: [{ name: "木星, Jupiter", longitude: 321.123456789 }],
    },
    derived_geometry: {},
    derived_methods: {},
    calculation_trace: [{ title: "測試步驟", result: { longitude: 321.123456789 } }],
  };
  const sections = [
    {
      id: "time",
      title: "時間轉換",
      layer_label: "天文原始資料",
      notes: ["以下時間均標明時間尺度。"],
      tables: [
        {
          title: "時間",
          columns: ["項目", "數值"],
          rows: [["UTC", "1997-08-17T01:42:00.000Z"], ["備註", "say \"hi\", then check"]],
        },
      ],
      blocks: [],
    },
    {
      id: "bodies",
      title: "七政",
      layer_label: "天文原始資料",
      notes: [],
      tables: [
        {
          title: "黃道／赤道座標",
          columns: ["星體", "黃經"],
          rows: [["木星, Jupiter", "水瓶 21°07'24\""]],
        },
      ],
      blocks: [],
    },
  ];
  return { response, sections };
}

test("one canonical document keeps the dossier, raw precision, and display sections", () => {
  const { response, sections } = fixture();
  const document = Exporters.createDocument(response, sections);

  assert.equal(document.export_contract_version, "0.1.2");
  assert.equal(document.calculation_dossier, response.calculation_dossier);
  assert.equal(document.source_response, response);
  // 正規化會補上預設收據狀態；沒有 status 的 section 一律視為「已計算」。
  assert.deepEqual(
    document.sections,
    sections.map((section) => ({
      ...section,
      status: {
        state: "present",
        label: Exporters.SECTION_STATES.present,
        reason_code: "",
      },
    }))
  );
  assert.equal(
    document.source_response.astronomical_data.bodies[0].longitude,
    321.123456789
  );
});

test("compatibility fixture accepts only the supported API and Dossier pair", () => {
  const { response, sections } = fixture();
  const accepted = compatibilityFixture.accepted;
  response.schema_version = accepted.api_schema_version;
  response.calculation_dossier.dossier_version = accepted.dossier_version;

  const document = Exporters.createDocument(response, sections);
  assert.equal(
    document.export_contract_version,
    accepted.export_contract_version
  );

  compatibilityFixture.rejected.forEach((entry) => {
    const rejectedResponse = structuredClone(response);
    if (entry.api_schema_version === null) {
      delete rejectedResponse.schema_version;
    } else {
      rejectedResponse.schema_version = entry.api_schema_version;
    }
    if (entry.dossier_version === null) {
      delete rejectedResponse.calculation_dossier.dossier_version;
    } else {
      rejectedResponse.calculation_dossier.dossier_version =
        entry.dossier_version;
    }
    assert.throws(
      () => Exporters.createDocument(rejectedResponse, sections),
      /不相容|缺少.*版本/
    );
  });
});

test("section and full copy are rendered from the canonical section model", () => {
  const { response, sections } = fixture();
  const document = Exporters.createDocument(response, sections);
  const sectionText = Exporters.renderSectionText(document.sections[0]);
  const fullText = Exporters.renderPlainText(document);

  assert.match(sectionText, /時間轉換/);
  assert.match(sectionText, /項目\t數值/);
  assert.match(sectionText, /UTC\t1997-08-17T01:42:00\.000Z/);
  assert.ok(
    fullText.includes(
      `Calculation Dossier ${compatibilityFixture.accepted.dossier_version}`
    ),
    "全文複製必須印出目前 accepted 的 Dossier 版本"
  );
  assert.match(fullText, /TEST_WARNING/);
  assert.match(fullText, /木星, Jupiter/);
  assert.match(fullText, /"latitude": 24\.1477/);
  assert.match(fullText, /Calculation Dossier（機器可讀）/);
  assert.match(fullText, /application_chart_path_no_persistence/);
});

test("section renderer explicitly rejects missing or non-record models", () => {
  for (const invalidSection of [null, undefined, [], "section"]) {
    assert.throws(
      () => Exporters.renderSectionText(invalidSection),
      /canonical section/
    );
  }
  const { response, sections } = fixture();
  const document = Exporters.createDocument(response, sections);
  assert.match(
    Exporters.renderSectionText(document.sections[0]),
    /## 時間轉換/
  );
});

test("CSV uses a stable long-form schema and RFC 4180 escaping", () => {
  const { response, sections } = fixture();
  const csv = Exporters.renderCsv(Exporters.createDocument(response, sections));
  const lines = csv.split("\r\n");

  assert.equal(lines[0], "section_id,section,table,row,field,value");
  assert.match(csv, /input_receipt\.location\.latitude,24\.1477/);
  assert.match(csv, /warnings\[0\]\.code,TEST_WARNING/);
  assert.match(
    csv,
    /privacy\.claims\[0\]\.id,application_chart_path_no_persistence/
  );
  assert.match(csv, /"say ""hi"", then check"/);
  assert.match(csv, /"木星, Jupiter"/);
  assert.ok(lines.every((line) => !line.includes("undefined")));
});

test("CSV neutralizes spreadsheet formulas without corrupting numeric literals", () => {
  const { response, sections } = fixture();
  sections.push({
    id: "csv-boundary",
    title: "CSV boundary",
    layer_label: "test",
    notes: [],
    tables: [{
      title: "Injection cases",
      columns: ["case", "value"],
      rows: [
        ["equals", "=1+1"],
        ["at", "@SUM(A1:A2)"],
        ["expression", "-1+2"],
        ["leading tab", "\t=1+1"],
        ["leading carriage return", "\r=1+1"],
        ["leading spaces", "   =1+1"],
        ["negative number", "-2.5"],
        ["positive number", "+123"],
        ["scientific number", "1e-6"],
      ],
    }],
    blocks: [],
  });
  const csv = Exporters.renderCsv(Exporters.createDocument(response, sections));

  assert.match(csv, /,'=1\+1/);
  assert.match(csv, /,'@SUM\(A1:A2\)/);
  assert.match(csv, /,'-1\+2/);
  assert.match(csv, /,'\t=1\+1/);
  assert.match(csv, /"'\r=1\+1"/);
  assert.match(csv, /,'   =1\+1/);
  assert.match(csv, /,-2\.5/);
  assert.match(csv, /,\+123/);
  assert.match(csv, /,1e-6/);
});

test("JSON is lossless and Markdown is labelled as data rather than interpretation", () => {
  const { response, sections } = fixture();
  const document = Exporters.createDocument(response, sections);
  const parsed = JSON.parse(Exporters.renderJson(document));
  const markdown = Exporters.renderMarkdown(document);

  assert.equal(parsed.export_contract_version, "0.1.2");
  assert.equal(
    parsed.source_response.astronomical_data.bodies[0].longitude,
    321.123456789
  );
  assert.equal(
    parsed.source_response.calculation_dossier.input_receipt.location.longitude,
    120.6736
  );
  assert.equal("calculation_dossier" in parsed, false);
  assert.match(markdown, /^# 古典西洋占星天文計算資料/m);
  assert.match(markdown, /這是計算資料與重現收據，不是占星解讀/);
  assert.match(markdown, /\| 木星, Jupiter \| 水瓶 21°07'24" \|/);
  assert.match(markdown, /TEST_WARNING/);
  assert.match(markdown, /application_chart_path_no_persistence/);
});

test("download names are generic and never embed birth data", () => {
  for (const extension of ["csv", "json", "txt", "md"]) {
    const name = Exporters.buildDownloadName(extension);
    assert.equal(name, `classical-astrology-export.${extension}`);
    assert.doesNotMatch(name, /1997|120|24/);
  }
});

test("every download format has one tested renderer, MIME type, filename, and CSV BOM policy", () => {
  const { response, sections } = fixture();
  const document = Exporters.createDocument(response, sections);
  const expected = {
    csv: ["text/csv;charset=utf-8", "classical-astrology-export.csv"],
    json: ["application/json;charset=utf-8", "classical-astrology-export.json"],
    txt: ["text/plain;charset=utf-8", "classical-astrology-export.txt"],
    md: ["text/markdown;charset=utf-8", "classical-astrology-export.md"],
  };

  for (const [format, [mimeType, filename]] of Object.entries(expected)) {
    const artifact = Exporters.buildDownloadArtifact(document, format);
    assert.equal(artifact.mime_type, mimeType);
    assert.equal(artifact.filename, filename);
    assert.ok(artifact.content.length > 100);
    assert.equal(artifact.content.startsWith("\uFEFF"), format === "csv");
  }
  assert.throws(
    () => Exporters.buildDownloadArtifact(document, "pdf"),
    /不支援的匯出格式/
  );
});

test("download action returns an explicit failure instead of claiming success", () => {
  const { response, sections } = fixture();
  const document = Exporters.createDocument(response, sections);

  const outcome = Exporters.runDownloadAction(document, "json", () => {
    throw new Error("simulated Blob delivery failure");
  });

  assert.deepEqual(outcome, {
    ok: false,
    error_message: "simulated Blob delivery failure",
  });
});

// ── PIA-2026-08-06-002 ───────────────────────────────────────
// Markdown 允許 raw HTML，所以未經處理的自由文字在支援 HTML 的 renderer
// 裡就是 active content。匯出物是 L1、會脫離脈絡流傳，因此 escaping 是
// serializer 自己的契約責任，不能倚賴「目前 schema 剛好沒有自由文字」。

function hostileDocument(payload) {
  const { response } = fixture();
  return {
    export_contract_version: Exporters.EXPORT_CONTRACT_VERSION,
    source_response: response,
    calculation_dossier: response.calculation_dossier,
    sections: [{
      id: "probe",
      title: `標題 ${payload}`,
      layer_label: `層級 ${payload}`,
      status: { state: "present" },
      notes: [`備註 ${payload}`],
      tables: [{
        title: `表格標題 ${payload}`,
        columns: ["欄位", "值"],
        rows: [["地點標籤", payload]],
      }],
      blocks: [],
    }],
  };
}

test("markdown escapes HTML metacharacters on every free-text surface", () => {
  const markdown = Exporters.renderMarkdown(
    hostileDocument('<img src=x onerror=alert(1)>')
  );

  assert.ok(!markdown.includes("<img"), "raw <img 進入了 Markdown");
  assert.ok(!markdown.includes("onerror=alert(1)>"), "raw HTML 尾端仍在");
  // 四個面都要涵蓋：先前只有表格儲存格被討論，標題／備註／表格標題同樣沒防。
  for (const surface of ["## 標題", "**層級：**", "> 備註", "### 表格標題"]) {
    const line = markdown.split("\n").find((l) => l.startsWith(surface));
    assert.ok(line, `找不到 ${surface} 這一行`);
    assert.match(line, /&lt;img/, `${surface} 沒有被 escape`);
  }
});

test("markdown escaping does not double-encode an ampersand", () => {
  // & 必須先換。若順序反了，`<` 會變成 `&amp;lt;`，使用者看到的是亂碼。
  const markdown = Exporters.renderMarkdown(hostileDocument("A & B < C"));
  assert.ok(markdown.includes("A &amp; B &lt; C"), markdown.slice(0, 400));
  assert.ok(!markdown.includes("&amp;lt;"), "重複編碼");
});

test("markdown keeps legitimate text and table structure intact", () => {
  // 反向控制：一個把所有東西都吃掉的 escaper 也會讓上面兩個測試通過。
  const markdown = Exporters.renderMarkdown(hostileDocument("台中 24.1469"));
  assert.match(markdown, /## 標題 台中 24\.1469/);
  assert.match(markdown, /\| 地點標籤 \| 台中 24\.1469 \|/);
  assert.ok(markdown.includes("| --- | --- |"), "表格分隔列不見了");
});

test("json and csv are unchanged by the markdown escaping", () => {
  // 相鄰控制：escaping 只屬於 Markdown serializer，不得滲進其他格式。
  const document = hostileDocument('<img src=x>');
  assert.ok(Exporters.renderJson(document).includes('<img src=x>'),
    "JSON 應保存原始位元組，不做 HTML escaping");
  assert.ok(Exporters.renderCsv(document).includes('<img src=x>'),
    "CSV 的守衛是 formula hardening，不是 HTML escaping");
});

// ══ FPI-2026-08-06 盲審 findings ═══════════════════════════════════════
//
// E-008 與 E-009 目前都**不可觸發**：`authority` 是後端硬編碼字串，
// 進入儲存格的自由文字也不含 tab 或換行。列為 finding 而非略過，是因為
// `AGENTS.md` §3A 明文禁止「因為目前 schema 沒有自由文字就假設未來安全」
// 這條推理——而那正是這兩處原本依賴的推理。測試以合成的敵意輸入證明
// serializer 自己就守得住，不依賴上游剛好乾淨。

test("E-008：Markdown 的 authority 與 UTC 兩個插值點都經過 escape", () => {
  const { response } = fixture();
  response.calculation_dossier.authority = "<script>alert(1)</script> & more";
  response.calculation_dossier.time_conversion.utc_iso_8601 = "1997`x`Z";
  const markdown = Exporters.renderMarkdown(
    Exporters.createDocument(response, [])
  );

  const authorityLine = markdown
    .split("\n")
    .find((line) => line.startsWith("- Authority:"));
  assert.ok(authorityLine, "Authority 行不見了");
  assert.ok(
    authorityLine.includes("&lt;script&gt;"),
    `authority 未經 escape 就插入 Markdown：${authorityLine}`
  );
  // 文件末端的 dossier JSON code fence 內仍是原值，那是刻意的：fence 內不解析
  // Markdown，且那份 JSON 就是要逐字重現後端回應。這裡只查散文插值點。
  assert.ok(!authorityLine.includes("<script>"));
  // inline code span 內的反引號會提早關閉 span，後面的文字就落在 code 之外。
  const utcLine = markdown
    .split("\n")
    .find((line) => line.startsWith("- UTC:"));
  assert.ok(utcLine, "UTC 行不見了");
  const fenceMatch = utcLine.match(/`+/);
  assert.ok(
    fenceMatch[0].length >= 2,
    `UTC 的 code span 沒有避開內容裡的反引號：${utcLine}`
  );
});

test("E-008：blocks 的 fence 依內容加長，內容撐不破它", () => {
  const { response } = fixture();
  const document = Exporters.createDocument(response, [
    {
      id: "hostile",
      title: "測試區段",
      blocks: ["```\n偽造的區段外文字\n```"],
    },
  ]);
  const markdown = Exporters.renderMarkdown(document);

  const lines = markdown.split("\n");
  const opening = lines.findIndex((line) => /^`{4,}text$/.test(line));
  assert.ok(
    opening >= 0,
    "含 ``` 的 block 仍使用三個反引號的 fence，內容可以逃出去"
  );
});

test("E-008：一般內容仍使用三個反引號（相鄰控制）", () => {
  const document = Exporters.createDocument(fixture().response, [
    {
      id: "plain",
      title: "一般區段",
      blocks: ["沒有反引號的內容"],
    },
  ]);
  const markdown = Exporters.renderMarkdown(document);
  assert.ok(markdown.includes("```text\n沒有反引號的內容\n```"));
});

test("E-009：TSV 在儲存格邊界中和 tab 與換行", () => {
  const section = {
    id: "tsv",
    title: "測試表",
    tables: [
      {
        title: "含惡意字元",
        columns: ["欄一", "欄二"],
        rows: [["a\tb", "c\nd"]],
      },
    ],
  };
  const document = Exporters.createDocument(fixture().response, [section]);
  const text = Exporters.renderSectionText(document.sections[0]);

  const dataLine = text
    .split("\n")
    .find((line) => line.startsWith("a"));
  assert.ok(dataLine, "資料列不見了");
  assert.equal(
    dataLine.split("\t").length,
    2,
    `儲存格內的 tab 多切出一欄：${JSON.stringify(dataLine)}`
  );
  assert.ok(
    !dataLine.includes("\n"),
    "儲存格內的換行把一列拆成兩列"
  );
});

test("E-009：不含這些字元的表格逐字不變（相鄰控制）", () => {
  const section = {
    id: "clean",
    title: "測試表",
    tables: [
      {
        columns: ["天體", "黃經"],
        rows: [["太陽", "23°26′31.79″"]],
      },
    ],
  };
  const document = Exporters.createDocument(fixture().response, [section]);
  const text = Exporters.renderSectionText(document.sections[0]);
  assert.ok(text.includes("天體\t黃經"));
  assert.ok(text.includes("太陽\t23°26′31.79″"));
});

// ══ Codex 複審打回的兩項 ═══════════════════════════════════════════════
//
// 兩項都是我上一輪修得**不完整**，不是判準嚴苛：E-008 我 escape 了三個插值點
// 漏掉第四個，E-009 我處理了 `\r\n` 卻漏掉單獨的 `\r`。

test("E-008：reason_code 的 code span 不會被內容裡的反引號提前關閉", () => {
  const { response } = fixture();
  const document = Exporters.createDocument(response, [
    {
      id: "s",
      title: "測試區段",
      status: {
        state: "executed_unavailable",
        reason_code: "alpha`偽造散文`omega",
      },
      tables: [],
      notes: [],
      blocks: [],
    },
  ]);
  const line = Exporters.renderMarkdown(document)
    .split("\n")
    .find((row) => row.startsWith("**狀態：**"));

  assert.ok(line, "狀態行不見了");
  // 反引號成對才代表 span 沒有被撐開；奇數個必然有一段內容落在 span 之外。
  const backticks = (line.match(/`/g) || []).length;
  assert.equal(
    backticks % 2,
    0,
    `code span 被內容裡的反引號提前關閉：${line}`
  );
  assert.ok(!/`偽造散文`/.test(line.replace(/^.*?（原因代碼 /, "")) ||
    /``+/.test(line), `分隔沒有加長：${line}`);
});

test("E-008：一般 reason_code 仍使用單一反引號（相鄰控制）", () => {
  const { response } = fixture();
  const document = Exporters.createDocument(response, [
    {
      id: "s",
      title: "測試區段",
      status: { state: "not_requested", reason_code: "house_not_requested" },
      tables: [],
      notes: [],
      blocks: [],
    },
  ]);
  const line = Exporters.renderMarkdown(document)
    .split("\n")
    .find((row) => row.startsWith("**狀態：**"));
  assert.ok(line.includes("`house_not_requested`"), line);
});

test("E-009：單獨的 CR 也在儲存格邊界被中和", () => {
  const { response } = fixture();
  const document = Exporters.createDocument(response, [
    {
      id: "t",
      title: "測試表",
      tables: [{ columns: ["A", "B"], rows: [["left\rright", "ok"]] }],
      notes: [],
      blocks: [],
    },
  ]);
  const text = Exporters.renderSectionText(document.sections[0]);

  // 試算表把裸 CR 當成 record separator，所以它自己就能拆列。
  assert.ok(!text.includes("\r"), `裸 CR 仍在輸出中：${JSON.stringify(text)}`);
  const dataLine = text.split("\n").find((row) => row.startsWith("left"));
  assert.equal(dataLine.split("\t").length, 2, dataLine);
});

test("E-009：CRLF 與 LF 仍如既有行為被折成空白（相鄰控制）", () => {
  const { response } = fixture();
  const document = Exporters.createDocument(response, [
    {
      id: "t",
      title: "測試表",
      tables: [{ columns: ["A"], rows: [["a\r\nb"], ["c\nd"]] }],
      notes: [],
      blocks: [],
    },
  ]);
  const text = Exporters.renderSectionText(document.sections[0]);
  assert.ok(text.includes("a b"), text);
  assert.ok(text.includes("c d"), text);
});

test("E-023：Markdown 表格儲存格的裸 CR 也轉成 <br>", () => {
  const { response } = fixture();
  const document = Exporters.createDocument(response, [
    {
      id: "t",
      title: "測試表",
      tables: [{ columns: ["A", "B"], rows: [["left\rright", "ok"]] }],
      notes: [],
      blocks: [],
    },
  ]);
  const row = Exporters.renderMarkdown(document)
    .split("\n")
    .find((line) => line.includes("left"));

  assert.ok(row, "資料列不見了");
  assert.ok(!row.includes("\r"), `裸 CR 仍在表格列中：${JSON.stringify(row)}`);
  assert.ok(row.includes("left<br>right"), row);
});

test("E-023：CRLF 只產生一個 <br>（相鄰控制）", () => {
  const { response } = fixture();
  const document = Exporters.createDocument(response, [
    {
      id: "t",
      title: "測試表",
      tables: [{ columns: ["A"], rows: [["a\r\nb"], ["c\nd"]] }],
      notes: [],
      blocks: [],
    },
  ]);
  const markdown = Exporters.renderMarkdown(document);
  assert.ok(markdown.includes("a<br>b"), "CRLF 應折成單一 <br>");
  assert.ok(markdown.includes("c<br>d"));
  assert.ok(!markdown.includes("<br><br>"), "CRLF 被算成兩次換行");
});
