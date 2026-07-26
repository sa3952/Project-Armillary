const test = require("node:test");
const assert = require("node:assert/strict");

const Exporters = require("../exporters.js");

function fixture() {
  const response = {
    schema_version: "0.9.0",
    calculation_dossier: {
      dossier_version: "0.3.0",
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
  assert.deepEqual(document.sections, sections);
  assert.equal(
    document.source_response.astronomical_data.bodies[0].longitude,
    321.123456789
  );
});

test("section and full copy are rendered from the canonical section model", () => {
  const { response, sections } = fixture();
  const document = Exporters.createDocument(response, sections);
  const sectionText = Exporters.renderSectionText(document.sections[0]);
  const fullText = Exporters.renderPlainText(document);

  assert.match(sectionText, /時間轉換/);
  assert.match(sectionText, /項目\t數值/);
  assert.match(sectionText, /UTC\t1997-08-17T01:42:00\.000Z/);
  assert.match(fullText, /Calculation Dossier 0\.3\.0/);
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
