const form = document.getElementById("chart-form");
const calculateButton = document.getElementById("calculate-button");
const loadExampleButton = document.getElementById("load-example-button");
const clearResultsButton = document.getElementById("clear-results-button");
const panicClearButton = document.getElementById("panic-clear-button");
const profileBadge = document.getElementById("profile-badge");
const resultsEl = document.getElementById("results");
const errorEl = document.getElementById("error-box");
const privacyStatusEl = document.getElementById("privacy-status");
const sensitiveLifecycle = PrivacyLifecycle.createSensitiveDataLifecycle({
  revokeObjectUrl: (url) => URL.revokeObjectURL(url),
});
let sectionSequence = 0;
let applicationProfile = null;
let applicationReady = false;
const SECTION_IDS = Object.freeze({
  "Calculation Dossier 計算收據": "calculation_dossier",
  "計算模式 / 函式庫版本": "calculation_mode",
  "時間轉換": "time_conversion",
  "七政": "core_bodies",
  "南北交點": "lunar_nodes",
  "角點與宮位": "angles_and_houses",
  "恆星": "fixed_stars",
  "月相與食事件": "lunar_events",
  "升降與過中天": "horizon_events",
  "對蹠點 / 反對蹠點": "antiscia",
  "日夜盤判定 (Sect)": "sect",
  "阿拉伯點 (Lots)": "lots",
  "月空亡 (Void of Course)": "void_of_course",
  "赤緯相位 (平行 / 反平行)": "declination_aspects",
  "完整計算過程": "calculation_trace",
});

function syncTimezoneControls() {
  const mode = document.querySelector('input[name="tz-mode"]:checked').value;
  const ianaName = document.getElementById("iana-name");
  const fixedOffset = document.getElementById("fixed-offset");
  ianaName.disabled = mode !== "iana";
  ianaName.required = mode === "iana";
  fixedOffset.disabled = mode !== "fixed_offset";
  fixedOffset.required = mode === "fixed_offset";
}

function syncZodiacControls() {
  const mode = document.querySelector('input[name="zodiac"]:checked').value;
  document.getElementById("ayanamsa-select").disabled = mode !== "sidereal";
}

document.querySelectorAll('input[name="tz-mode"]').forEach((radio) => {
  radio.addEventListener("change", syncTimezoneControls);
});

document.querySelectorAll('input[name="zodiac"]').forEach((radio) => {
  radio.addEventListener("change", syncZodiacControls);
});

function applyApplicationProfile(profile) {
  applicationProfile = profile;
  document.documentElement.dataset.appProfile = profile;
  document.querySelectorAll("[data-profile-only]").forEach((element) => {
    element.classList.toggle(
      "hidden",
      element.getAttribute("data-profile-only") !== profile
    );
  });

  if (profile === ClientContext.PROFILES.LOCAL) {
    profileBadge.textContent = "本機運算";
    document.title = "古典西洋占星天文計算";
  } else {
    profileBadge.textContent = "Private Alpha";
    document.title = "Private Alpha｜古典西洋占星天文計算";
  }
}

async function initializeApplicationProfile() {
  const response = await fetch("/api/client-config", {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`client configuration HTTP ${response.status}`);
  }
  const payload = await response.json();
  const configuration = ClientContext.validateClientConfiguration(payload);
  applyApplicationProfile(configuration.profile);
}

// --- 複製到剪貼簿 ---------------------------------------------------------
// navigator.clipboard 需要「安全情境」，http://localhost / http://127.0.0.1
// 瀏覽器一律視為安全情境；但若透過區網 IP 或其他非 localhost 主機名稱開啟，
// 或瀏覽器政策擋下 clipboard-write 權限，Clipboard API 會不存在或被拒絕。
// execCommand 後備方案本身在部分瀏覽器/情境下也可能靜默失敗（回傳 false 而不丟例外），
// 之前版本沒檢查回傳值，導致「顯示已複製，但剪貼簿其實是空的」。這裡明確檢查每一層
// 是否真的成功，兩者都失敗時最後用 prompt() 讓使用者手動選取複製，絕不假裝成功。
function copyViaExecCommand(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.top = "0";
  ta.style.left = "0";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try {
    ta.setSelectionRange(0, text.length);
  } catch (err) {
    // 某些瀏覽器的 textarea 不支援 setSelectionRange，忽略即可，select() 已足夠
  }
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch (err) {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

function copyText(text) {
  const viaClipboardApi =
    navigator.clipboard && navigator.clipboard.writeText
      ? navigator.clipboard.writeText(text)
      : Promise.reject(new Error("Clipboard API 無法使用"));

  return viaClipboardApi.catch(() => {
    if (copyViaExecCommand(text)) return;
    throw new Error("自動複製失敗");
  });
}

// 自動複製兩種方式都失敗時的最後手段：跳出 prompt 讓使用者自行全選複製
// （prompt 的預設值文字通常會被瀏覽器自動選取，方便直接按 Ctrl/Cmd+C）。
function fallbackManualCopy(text) {
  window.prompt("自動複製失敗，請手動複製以下內容（已預先選取）：", text);
}

// 小型「複製」按鈕，點擊後複製 getTextFn() 的回傳字串，並短暫改成「已複製!」；
// 若複製實際失敗，改跳出手動複製的提示，不顯示假成功訊息。
function copyButton(getTextFn, label = "複製") {
  const btn = el("button", { type: "button", class: "copy-btn", text: label });
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    let text;
    try {
      text = getTextFn();
    } catch (_error) {
      errorEl.textContent = "敏感計算資料已清除，請重新計算後再複製。";
      errorEl.classList.remove("hidden");
      return;
    }
    copyText(text)
      .then(() => {
        const original = btn.textContent;
        btn.textContent = "已複製!";
        btn.classList.add("copied");
        setTimeout(() => {
          btn.textContent = original;
          btn.classList.remove("copied");
        }, 900);
      })
      .catch(() => fallbackManualCopy(text));
  });
  return btn;
}

function setSubmitLabel(button, label) {
  const labelNode = button.querySelector(".submit-label");
  if (labelNode) labelNode.textContent = label;
  else button.textContent = label;
}

function discardComputedSensitiveState() {
  const receipt = sensitiveLifecycle.clear();
  resultsEl.replaceChildren();
  resultsEl.classList.add("hidden");
  errorEl.textContent = "";
  errorEl.classList.add("hidden");
  sectionSequence = 0;
  return receipt;
}

function clearSensitiveData({
  clearForm,
  announcement,
  stripUrl = true,
  focusForm = true,
}) {
  const receipt = discardComputedSensitiveState();
  if (clearForm) {
    form.reset();
    document.querySelectorAll("[data-sensitive-input]").forEach((input) => {
      input.value = "";
    });
    syncTimezoneControls();
    syncZodiacControls();
  }
  const submitButton = calculateButton;
  submitButton.disabled = !applicationReady;
  setSubmitLabel(submitButton, "開始計算");
  if (stripUrl && (window.location.search || window.location.hash)) {
    window.history.replaceState(null, "", window.location.pathname);
  }
  privacyStatusEl.textContent = announcement || "";
  privacyStatusEl.classList.toggle("hidden", !announcement);
  if (clearForm && focusForm) document.getElementById("year").focus();
  return receipt;
}

function loadExampleData() {
  discardComputedSensitiveState();
  form.reset();
  document.querySelectorAll("[data-sensitive-input]").forEach((input) => {
    input.value = "";
  });
  const exampleValues = {
    year: "2000",
    month: "1",
    day: "1",
    hour: "12",
    minute: "0",
    second: "0",
    "iana-name": "Asia/Taipei",
    latitude: "25.0330",
    longitude: "121.5654",
    altitude: "10",
    "temperature-c": "0",
  };
  Object.entries(exampleValues).forEach(([elementId, value]) => {
    document.getElementById(elementId).value = value;
  });
  syncTimezoneControls();
  syncZodiacControls();
  privacyStatusEl.textContent = (
    "已載入明確標示的範例資料；送出前可任意修改。這不是你的出生資料。"
  );
  privacyStatusEl.classList.remove("hidden");
  document.getElementById("year").focus();
}

function fmt(n, digits = 4) {
  if (n === null || n === undefined) return "—";
  if (typeof n === "number") return n.toFixed(digits);
  return String(n);
}

function fmtDeg(n, digits = 3) {
  if (n === null || n === undefined) return "—";
  return n.toFixed(digits) + "°";
}

// 注意：後端 core/formatting.py 的 to_dms() 也做同一套進位運算。兩者刻意並存——
// 後端輸出的是給 API 消費者看的純度分秒字串，前端這支要輸出的是給人看的「星座+度分秒」，
// 且星座名稱是 UI 語系字串，屬於呈現層。兩者曾經因為只修了一邊而分歧（後端漏了 360° 進位
// 保護），所以 backend/tests/test_chart_api.py 有一個跨實作一致性測試釘住邊界行為，
// 修改任一邊的進位邏輯時請同步檢查另一邊。
function deg2dms(lon) {
  if (lon === null || lon === undefined) return "—";
  const signs = ["牡羊", "金牛", "雙子", "巨蟹", "獅子", "處女", "天秤", "天蠍", "射手", "摩羯", "水瓶", "雙魚"];
  // 先四捨五入到整數弧秒，再用整數除法逐級進位（度/星座都用整數運算），
  // 避免對度/分/秒各自的浮點數分開四捨五入時，秒數顯示成不存在的 "60"
  // （例如 29°59'59.9997" 應進位成下個星座 0°00'00"，而不是 29°59'60"）。
  const norm = ((lon % 360) + 360) % 360;
  let totalArcsec = Math.round(norm * 3600);
  totalArcsec = ((totalArcsec % 1296000) + 1296000) % 1296000; // 360*3600，防止進位溢出 360°
  const signIndex = Math.floor(totalArcsec / 108000) % 12; // 30*3600
  const remInSign = totalArcsec - Math.floor(totalArcsec / 108000) * 108000;
  const d = Math.floor(remInSign / 3600);
  const m = Math.floor((remInSign - d * 3600) / 60);
  const s = remInSign - d * 3600 - m * 60;
  return `${signs[signIndex]} ${d}°${String(m).padStart(2, "0")}'${String(s).padStart(2, "0")}"`;
}

function el(tag, attrs = {}, children = []) {
  // 刻意只提供 text（textContent）路徑，不提供 html/innerHTML——所有渲染的資料都來自
  // 我們自己的 API 回應，用不到插入任意 HTML 的能力，移除這條路徑可避免日後不小心
  // 誤用而開出 DOM XSS 缺口。
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.appendChild(c);
  return node;
}

function section(title, layerLabel) {
  const s = el("div", { class: "section" });
  sectionSequence += 1;
  s._exportMeta = {
    id: SECTION_IDS[title] || `section-${sectionSequence}`,
    title,
    layer_label: layerLabel || "",
    blocks: [],
  };
  if (layerLabel) s.appendChild(el("div", { class: "layer-label", text: layerLabel }));
  const headingRow = el("div", { class: "section-heading-row" });
  headingRow.appendChild(el("h2", { text: title }));
  headingRow.appendChild(copyButton(
    () => ChartExport.renderSectionText(s._canonicalExportSection),
    "複製本節"
  ));
  s.appendChild(headingRow);
  return s;
}

function methodLine(methodResult) {
  const line = el("div", {
    class: "method-name",
    text: methodResult && methodResult.method ? `method = ${methodResult.method}` : "",
  });
  if (methodResult && methodResult.method_status === "provisional_pending_method_audit") {
    line.appendChild(el("span", { class: "method-status", text: "待方法審閱" }));
  }
  return line;
}

// cellText()：不論儲存格是純字串/數字還是一個 Node（motionBadge/yesNo 產生的 span 等），
// 都取出它實際顯示的文字，供「複製表格」使用。
function cellText(cell) {
  return cell instanceof Node ? cell.textContent : String(cell);
}

function table(headers, rows) {
  const wrap = el("div", { class: "table-wrap", tabindex: "0" });
  wrap._exportTable = {
    title: "",
    columns: headers.map(String),
    rows: rows.map((row) => row.map(cellText)),
  };

  const t = el("table");
  const thead = el("thead");
  const trh = el("tr");
  headers.forEach((h) => trh.appendChild(el("th", { scope: "col", text: h })));
  thead.appendChild(trh);
  t.appendChild(thead);
  const tbody = el("tbody");
  rows.forEach((r) => {
    const tr = el("tr");
    r.forEach((cell) => {
      const td = el("td");
      if (cell instanceof Node) td.appendChild(cell);
      else td.textContent = cell;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  t.appendChild(tbody);
  wrap.appendChild(t);
  return wrap;
}

function inferredTableTitle(tableWrap, index) {
  const previous = tableWrap.previousElementSibling;
  if (previous && previous.tagName === "H3") return previous.textContent;
  const parent = tableWrap.parentElement;
  if (parent && parent.tagName === "DETAILS") {
    const summary = parent.querySelector(":scope > summary");
    if (summary) return summary.textContent;
  }
  return `表 ${index + 1}`;
}

// UI、section copy 與所有檔案格式共用的呈現模型。表格列在建立 DOM 時只正規化一次，
// 此處只收集同一份模型，不再從畫面文字反向猜測數值。
function collectSectionModel(sectionNode) {
  const meta = sectionNode._exportMeta;
  const tableNodes = Array.from(sectionNode.querySelectorAll(".table-wrap"));
  const notes = Array.from(sectionNode.querySelectorAll(".method-name, p"))
    .map((node) => node.textContent.trim())
    .filter(Boolean);
  return {
    id: meta.id,
    title: meta.title,
    layer_label: meta.layer_label,
    notes,
    tables: tableNodes.map((tableNode, index) => ({
      ...tableNode._exportTable,
      title: tableNode._exportTable.title || inferredTableTitle(tableNode, index),
    })),
    blocks: meta.blocks.slice(),
  };
}

function downloadArtifact(artifact) {
  const blob = new Blob([artifact.content], { type: artifact.mime_type });
  let url = null;
  let anchor = null;
  try {
    url = URL.createObjectURL(blob);
    sensitiveLifecycle.registerObjectUrl(url);
    anchor = el("a", {
      href: url,
      download: artifact.filename,
    });
    document.body.appendChild(anchor);
    anchor.click();
    // Safari/WebKit 有時會在事件迴圈下一拍才真正開始讀取 blob；立即 revoke 可能產生空檔。
    setTimeout(() => sensitiveLifecycle.releaseObjectUrl(url), 1000);
  } catch (error) {
    if (url) sensitiveLifecycle.releaseObjectUrl(url);
    throw error;
  } finally {
    if (anchor && anchor.isConnected) document.body.removeChild(anchor);
  }
}

function exportDownloadButton(label, format, errorNode) {
  const button = el("button", {
    type: "button",
    class: "export-btn",
    text: label,
  });
  button.addEventListener("click", () => {
    errorNode.textContent = "";
    errorNode.classList.add("hidden");
    let outcome;
    try {
      outcome = ChartExport.runDownloadAction(
        sensitiveLifecycle.requireCanonicalDocument(),
        format,
        downloadArtifact
      );
    } catch (error) {
      outcome = {
        ok: false,
        error_message: error instanceof Error ? error.message : String(error),
      };
    }
    if (!outcome.ok) {
      errorNode.textContent = `無法建立 ${label} 下載檔：${outcome.error_message}。請改用「複製全部」，或重新計算後再試。`;
      errorNode.classList.remove("hidden");
    }
  });
  return button;
}

function renderExportPanel(documentModel) {
  const dossier = documentModel.calculation_dossier;
  const panel = el("div", { class: "export-panel" });
  const header = el("div", { class: "export-panel-heading" });
  const headingText = el("div");
  headingText.appendChild(el("div", { class: "layer-label", text: "UNIFIED EXPORT · DOSSIER-BOUND" }));
  headingText.appendChild(el("h2", { text: "複製與下載" }));
  header.appendChild(headingText);
  header.appendChild(el("div", {
    class: "export-contract",
    text: `Export ${documentModel.export_contract_version} · API ${documentModel.source_response.schema_version || "—"} · Dossier ${dossier.dossier_version || "—"}`,
  }));
  panel.appendChild(header);

  const errorNode = el("p", {
    class: "export-error hidden",
    role: "alert",
    "aria-live": "assertive",
  });
  const actions = el("div", { class: "export-actions" });
  actions.appendChild(copyButton(
    () => ChartExport.renderPlainText(
      sensitiveLifecycle.requireCanonicalDocument()
    ),
    "複製全部"
  ));
  actions.appendChild(exportDownloadButton(
    "CSV", "csv", errorNode
  ));
  actions.appendChild(exportDownloadButton(
    "JSON", "json", errorNode
  ));
  actions.appendChild(exportDownloadButton(
    "純文字 .txt", "txt", errorNode
  ));
  actions.appendChild(exportDownloadButton(
    "AI-friendly .md", "md", errorNode
  ));
  panel.appendChild(actions);
  panel.appendChild(errorNode);
  panel.appendChild(el("p", {
    class: "export-privacy",
    text: "隱私提醒：下載檔包含出生時間與精確座標。只有在你按下下載時，瀏覽器才會建立本機檔案；內容不會由 App 保存。",
  }));
  return panel;
}

function renderDossier(dossier) {
  const s = section(
    "Calculation Dossier 計算收據",
    "後端權威收據 · 已驗證輸入、實際政策與 provenance"
  );
  const receipt = dossier.input_receipt || {};
  const datetime = receipt.datetime || {};
  const timezone = receipt.timezone || {};
  const location = receipt.location || {};
  const atmosphere = receipt.atmosphere || {};
  const policy = dossier.calculation_policy || {};
  const mode = policy.computation_mode || {};
  const engine = dossier.engine || {};
  const provenance = dossier.provenance || {};
  const privacy = dossier.privacy || {};
  const localClock = [
    String(datetime.year || "").padStart(4, "0"),
    String(datetime.month || "").padStart(2, "0"),
    String(datetime.day || "").padStart(2, "0"),
  ].join("-") + " " + [
    String(datetime.hour ?? "").padStart(2, "0"),
    String(datetime.minute ?? "").padStart(2, "0"),
    String(datetime.second ?? "").padStart(2, "0"),
  ].join(":");

  s.appendChild(el("h3", { text: "已驗證出生資料" }));
  s.appendChild(table(
    ["欄位", "值"],
    [
      ["本地民用時間", localClock],
      ["時區請求", JSON.stringify(timezone)],
      ["緯度（北正南負）", `${fmt(location.latitude, 6)}°`],
      ["經度（東正西負）", `${fmt(location.longitude, 6)}°`],
      ["海拔", `${fmt(location.altitude_m, 2)} m`],
      ["大氣輸入", `pressure_hpa=${atmosphere.pressure_hpa ?? "null"}; temperature_c=${atmosphere.temperature_c ?? "—"}`],
    ]
  ));

  s.appendChild(el("h3", { text: "實際計算收據" }));
  s.appendChild(table(
    ["欄位", "值"],
    [
      ["Dossier version", dossier.dossier_version || "—"],
      ["Status / authority", `${dossier.status || "—"} / ${dossier.authority || "—"}`],
      ["UTC", dossier.time_conversion?.utc_iso_8601 || "—"],
      ["曆法", `${dossier.time_conversion?.calendar?.system || "—"} / ${dossier.time_conversion?.calendar?.swiss_flag || "—"}`],
      ["計算模式", `center=${mode.center || "—"}; zodiac=${mode.zodiac || "—"}; position=${mode.position_mode || "—"}; frame=${mode.ecliptic_frame || "—"}; nutation=${mode.nutation ?? "—"}`],
      ["pyswisseph / Swiss library", `${engine.pyswisseph_distribution_version || "—"} / ${engine.swiss_ephemeris_library_version || "—"}`],
      ["星曆來源請求", engine.requested_ephemeris_source || "—"],
      [
        "核心物件完整 Swiss files",
        provenance.all_core_calculation_sources_used_full_ephemeris === true
          ? "是"
          : (provenance.all_core_calculation_sources_used_full_ephemeris === false ? "否" : "—"),
      ],
      [
        "Privacy attestation",
        `${privacy.privacy_attestation_version || "—"} / ${privacy.attestation_status || "—"}`,
      ],
      ["Privacy evidence semantics", privacy.evidence_semantics || "—"],
    ]
  ));

  const privacyClaims = Array.isArray(privacy.claims) ? privacy.claims : [];
  s.appendChild(el("h3", { text: "隱私控制聲明（分層證據）" }));
  s.appendChild(table(
    ["Claim", "Statement", "Enforcement layer", "Status", "Control", "Scope", "Evidence", "Limitations"],
    privacyClaims.length
      ? privacyClaims.map((claim) => [
          claim.id || "—",
          claim.statement || "—",
          claim.enforcement_layer || "—",
          claim.status || "—",
          claim.control?.id || "—",
          Array.isArray(claim.scope?.applies_to)
            ? claim.scope.applies_to.join("；")
            : "—",
          Array.isArray(claim.evidence)
            ? claim.evidence.map((item) => item.reference || "—").join("；")
            : "—",
          Array.isArray(claim.limitations)
            ? claim.limitations.join("；")
            : "—",
        ])
      : [["—", "—", "—", "—", "—", "—", "—", "沒有隱私 attestation claim"]]
  ));

  const uncoveredLayers = Array.isArray(privacy.uncovered_layers)
    ? privacy.uncovered_layers
    : [];
  s.appendChild(el("h3", { text: "未涵蓋的隱私層" }));
  s.appendChild(table(
    ["Layer", "Status", "說明"],
    uncoveredLayers.length
      ? uncoveredLayers.map((item) => [
          item.layer || "—",
          item.status || "—",
          item.note || "—",
        ])
      : [["—", "—", "沒有列出未涵蓋層；不得據此推定已有保證"]]
  ));

  const modules = Object.entries(policy.modules || {});
  if (modules.length) {
    s.appendChild(el("h3", { text: "模組執行狀態" }));
    s.appendChild(table(
      ["模組", "狀態"],
      modules.map(([name, status]) => [name, status])
    ));
  }

  s.appendChild(el("h3", { text: "結構化警告" }));
  const warnings = Array.isArray(dossier.warnings) ? dossier.warnings : [];
  s.appendChild(table(
    ["Code", "Severity", "訊息", "來源"],
    warnings.length
      ? warnings.map((warning) => [
          warning.code || "—",
          warning.severity || "—",
          warning.message || "—",
          warning.source || "—",
        ])
      : [["—", "—", "無結構化警告", "—"]]
  ));
  return s;
}

function motionBadge(sign) {
  if (sign === null || sign === undefined) return el("span", { text: "—", class: "null-value" });
  const map = { positive: "順行", negative: "逆行(R)", zero: "靜止(0)" };
  const span = el("span", { text: map[sign] || sign });
  span.className = sign === "negative" ? "flag-yes" : "flag-no";
  return span;
}

function renderLibraryBanner(libInfo, mode, ayanamsa) {
  const s = section("計算模式 / 函式庫版本");
  const rows = [
    ["pyswisseph 套件版本", libInfo.pyswisseph_distribution_version || "—"],
    ["Swiss Ephemeris 函式庫版本", libInfo.swiss_ephemeris_library_version],
    ["黃道系統", mode.zodiac === "sidereal" ? `sidereal (${mode.ayanamsa}, ayanamsa=${fmt(ayanamsa, 5)}°)` : "tropical"],
    ["計算中心", mode.center + (mode.center !== "geocentric" ? "（進階，非古典預設）" : "")],
    ["位置／黃道參考框架／章動", `${mode.position_mode} / ${mode.ecliptic_frame} / nutation=${mode.nutation}`],
  ];
  s.appendChild(table(["項目", "數值"], rows));
  return s;
}

function renderTimeConversion(tc, atmosphere) {
  const s = section("時間轉換", "天文原始資料");
  const rows = [
    ["輸入本地時間", tc.input_local_time + " (" + tc.timezone_label + ")"],
    ["UTC 時間", tc.utc_time],
    ["日光節約時間(DST)", tc.dst_warning ? "⚠ 有疑慮，見上方警告" : "無疑慮"],
    ["JD (UT1)", fmt(tc.jd_ut, 9)],
    ["JD (ET/TT)", fmt(tc.jd_et, 9)],
    ["ΔT (秒)", fmt(tc.delta_t_seconds, 3)],
    ["格林威治視恆星時 GAST (小時)", fmt(tc.gast_hours, 8)],
    ["格林威治平均恆星時 GMST (小時)", fmt(tc.gmst_hours, 8)],
    ["當地視恆星時 LAST (小時)", fmt(tc.last_hours, 8)],
    ["當地平均恆星時 LMST (小時)", fmt(tc.lmst_hours, 8)],
    ["真黃赤交角 ε", fmtDeg(tc.true_obliquity, 8)],
    ["平黃赤交角 ε₀", fmtDeg(tc.mean_obliquity, 8)],
    ["黃經章動 Δψ", fmtDeg(tc.nutation_longitude, 8)],
    ["交角章動 Δε", fmtDeg(tc.nutation_obliquity, 8)],
    ["均時差 (分鐘)", fmt(tc.equation_of_time_minutes, 6)],
    ["真太陽時（參考）", tc.apparent_solar_time],
  ];
  if (tc.ayanamsa !== null && tc.ayanamsa !== undefined) {
    rows.push(["Ayanāṃśa", fmtDeg(tc.ayanamsa, 8)]);
  }
  rows.push(
    ["視高度氣壓", atmosphere.pressure_mode === "user_supplied"
      ? `${fmt(atmosphere.pressure_hpa, 2)} hPa（使用者指定）`
      : "Swiss 依海拔估算"],
    ["視高度溫度", `${fmt(atmosphere.temperature_c, 1)} °C`],
    ["折射模型", atmosphere.refraction]
  );
  s.appendChild(table(
    ["項目", "數值"],
    rows
  ));
  return s;
}

function renderBodies(title, bodies) {
  const s = section(title, "天文原始資料");
  s.appendChild(el("h3", { text: "黃道／赤道座標" }));
  s.appendChild(table(
    ["星體", "黃經", "黃緯", "赤經(HMS)", "赤緯", "距離(AU)", "黃經速度(度/日)", "順逆"],
    bodies.map((b) => [
      b.name,
      deg2dms(b.longitude),
      fmtDeg(b.latitude),
      b.right_ascension_hms || "—",
      fmtDeg(b.declination),
      fmt(b.distance_au, 5),
      fmt(b.speed_longitude, 4),
      motionBadge(b.motion_sign),
    ])
  ));

  const details = el("details", { class: "trace" });
  details.appendChild(el("summary", { text: "顯示地平座標與物理量" }));
  details.appendChild(table(
    ["星體", "方位角(北0°順時針)", "Swiss原始方位角", "真高度", "視高度(含折射)",
     "相位角", "照明比例", "距日角", "視直徑(度)", "視星等"],
    bodies.map((b) => [
      b.name,
      fmtDeg(b.azimuth, 4),
      fmtDeg(b.azimuth_swiss_raw, 4),
      fmtDeg(b.altitude_true, 4),
      fmtDeg(b.altitude_apparent, 4),
      fmtDeg(b.phase_angle, 1),
      b.illuminated_fraction === null || b.illuminated_fraction === undefined
        ? "—"
        : `${fmt(b.illuminated_fraction, 6)} (${(b.illuminated_fraction * 100).toFixed(2)}%)`,
      fmtDeg(b.elongation, 4),
      fmtDeg(b.apparent_diameter, 6),
      fmt(b.apparent_magnitude, 2),
    ])
  ));
  s.appendChild(details);
  return s;
}

function renderHouses(angles, division) {
  const s = section("角點與宮位", "角點＝天文資料 · 宮頭＝方法判定");
  s.appendChild(methodLine(division));
  s.appendChild(el("h3", {
    text: `${division.system_name}　ASC ${deg2dms(angles.asc)}　MC ${deg2dms(angles.mc)}`,
  }));
  s.appendChild(table(
    ["宮位", "宮頭黃經"],
    division.cusps.map((c, i) => [`第 ${i + 1} 宮`, deg2dms(c)])
  ));
  s.appendChild(table(
    ["角點", "黃經"],
    [
      ["ASC", deg2dms(angles.asc)],
      ["DESC", deg2dms(angles.desc)],
      ["MC", deg2dms(angles.mc)],
      ["IC", deg2dms(angles.ic)],
      ["ARMC", fmtDeg(angles.armc)],
    ]
  ));
  return s;
}

function renderFixedStars(stars) {
  if (!stars.length) return null;
  const s = section("恆星", "天文原始資料");
  s.appendChild(table(
    ["恆星", "黃經", "黃緯", "赤經(HMS)", "赤緯", "方位角(北0°順時針)",
     "Swiss原始方位角", "真高度", "視高度(含折射)", "星等"],
    stars.map((st) => st.error
      ? [st.name + "（查詢失敗）", st.error, "", "", "", "", "", "", "", ""]
      : [
        `${st.name} (${st.catalog_name})`,
        deg2dms(st.longitude),
        fmtDeg(st.latitude),
        st.right_ascension_hms || "—",
        fmtDeg(st.declination),
        fmtDeg(st.azimuth, 4),
        fmtDeg(st.azimuth_swiss_raw, 4),
        fmtDeg(st.altitude_true, 4),
        fmtDeg(st.altitude_apparent, 4),
        fmt(st.magnitude, 2),
      ])
  ));
  return s;
}

function renderLunarEvents(lunar) {
  if (!lunar || Object.keys(lunar).length === 0) return null;
  const s = section("月相與食事件", "天文事件 · 不含古典技法解讀");
  const phaseNames = {
    new_moon: "朔／新月",
    first_quarter: "上弦",
    full_moon: "望／滿月",
    last_quarter: "下弦",
  };

  if (lunar.primary_phases) {
    s.appendChild(el("h3", { text: "出生時刻前後的主要月相" }));
    s.appendChild(table(
      ["事件", "前一次 (UTC)", "後一次 (UTC)"],
      Object.entries(lunar.primary_phases).map(([key, pair]) => [
        phaseNames[key] || key,
        pair.previous.utc_time,
        pair.next.utc_time,
      ])
    ));
  }

  if (lunar.prenatal_syzygy) {
    const event = lunar.prenatal_syzygy;
    s.appendChild(el("h3", { text: "出生前最近朔／望（Prenatal Syzygy 的天文事件）" }));
    s.appendChild(table(
      ["項目", "數值"],
      [
        ["類型", phaseNames[event.phase] || event.phase],
        ["UTC", event.utc_time],
        ["JD (UT1)", fmt(event.jd_ut, 9)],
        ["定義", event.definition],
        ["解讀", "未提供（只回報天文事件）"],
      ]
    ));
  }

  if (lunar.eclipses) {
    const eclipses = lunar.eclipses;
    s.appendChild(el("h3", { text: "出生前最近的全球食事件" }));
    s.appendChild(table(
      ["事件", "類型", "中心性", "最大食 UTC", "Swiss retflag"],
      [eclipses.previous_solar, eclipses.previous_lunar].map((event) => [
        event.kind === "solar" ? "日食" : "月食",
        event.type,
        event.centrality || "—",
        event.utc_time_maximum,
        String(event.retflag),
      ])
    ));
  }
  return s;
}

function renderHorizonEvents(horizon) {
  if (!horizon || !Array.isArray(horizon.bodies)) return null;
  const s = section("升降與過中天", "天文事件 · 觀測者地平系");
  const contract = horizon.contract;
  s.appendChild(table(
    ["設定", "數值"],
    [
      ["搜尋方向", "出生時刻前一次／後一次"],
      ["升降基準", contract.disc_position === "upper_limb" ? "上緣 (upper limb)" : contract.disc_position],
      ["折射", contract.refraction],
      ["氣壓", contract.pressure_mode === "user_supplied"
        ? `${fmt(contract.pressure_hpa, 2)} hPa`
        : "Swiss 依海拔估算"],
      ["溫度", `${fmt(contract.temperature_c, 1)} °C`],
      ["過中天定義", contract.transit_definition],
    ]
  ));

  const eventNames = {
    rise: "升",
    set: "降",
    upper_transit: "上中天",
    lower_transit: "下中天",
  };
  const rows = [];
  horizon.bodies.forEach((body) => {
    Object.entries(body.events).forEach(([eventKey, event]) => {
      rows.push([
        body.name,
        eventNames[eventKey] || eventKey,
        event.status === "found" ? "找到" : `${event.status} (${event.reason || "—"})`,
        event.previous ? event.previous.utc_time : "—",
        event.next ? event.next.utc_time : "—",
        body.visibility,
      ]);
    });
  });
  s.appendChild(table(
    ["星體", "事件", "狀態", "前一次 UTC", "後一次 UTC", "可見性分類"],
    rows
  ));
  return s;
}

function renderAntiscia(antiscia) {
  if (!antiscia || !antiscia.antiscia) return null;
  const s = section("對蹠點 / 反對蹠點", "衍生幾何（純鏡射轉換，非技法判斷）");
  s.appendChild(table(
    ["星體", "Antiscia", "Contra-antiscia"],
    antiscia.antiscia.map((a, i) => [a.name, deg2dms(a.longitude), deg2dms(antiscia.contra_antiscia[i].longitude)])
  ));
  return s;
}

function renderSect(sect) {
  if (!sect) return null;
  const s = section("日夜盤判定 (Sect)", "方法判定");
  s.appendChild(methodLine(sect));
  s.appendChild(table(
    ["項目", "數值"],
    [
      ["盤性", sect.is_day === null ? "—(heliocentric/barycentric 模式下無意義)" : (sect.is_day ? "日生盤" : "夜生盤")],
      ["採用的太陽真高度", fmtDeg(sect.sun_altitude_used, 3)],
    ]
  ));
  return s;
}

function renderLots(lots) {
  if (!lots || lots.fortune === undefined) return null;
  const s = section("阿拉伯點 (Lots)", "方法判定");
  s.appendChild(methodLine(lots));
  s.appendChild(table(
    ["項目", "黃經"],
    [
      ["Lot of Fortune", deg2dms(lots.fortune)],
      ["Lot of Spirit", deg2dms(lots.spirit)],
    ]
  ));
  return s;
}

function renderVoc(voc) {
  if (!voc || voc.is_void_of_course === undefined) return null;
  const s = section("月空亡 (Void of Course)", "方法判定");
  s.appendChild(methodLine(voc));
  s.appendChild(table(
    ["項目", "數值"],
    [
      ["是否空亡", voc.is_void_of_course === null ? "—(無地平座標基礎)" : (voc.is_void_of_course ? "是" : "否")],
      ["距離星座邊界(小時)", fmt(voc.time_to_sign_exit_hours, 2)],
      ["離開星座前最快的入相位", voc.next_completing_aspect ? `${voc.next_completing_aspect.body} ${voc.next_completing_aspect.aspect_angle}°（約 ${(voc.next_completing_aspect.time_days*24).toFixed(2)} 小時後）` : "無"],
    ]
  ));
  if (voc.all_candidates && voc.all_candidates.length > 1) {
    s.appendChild(el("h3", { text: "全部候選入相位事件" }));
    s.appendChild(table(
      ["星體", "相位角", "時間(小時後)"],
      voc.all_candidates.map((c) => [c.body, fmtDeg(c.aspect_angle, 0), fmt(c.time_days * 24, 2)])
    ));
  }
  return s;
}

function renderDeclinationAspects(decl) {
  if (!decl || !Array.isArray(decl.aspects)) return null;
  const s = section("赤緯相位 (平行 / 反平行)", "方法判定");
  s.appendChild(methodLine(decl));
  s.appendChild(el("div", { class: "t-kv method-name", text: `orb = ${decl.orb_degrees}°` }));
  if (!decl.aspects.length) {
    s.appendChild(el("p", { text: "無符合容許誤差的赤緯相位。" }));
    return s;
  }
  s.appendChild(table(
    ["星體 A", "星體 B", "類型", "誤差(度)"],
    decl.aspects.map((a) => [a.body_a, a.body_b, a.type === "parallel" ? "平行" : "反平行", fmt(a.diff, 3)])
  ));
  return s;
}

// 把單一 trace 步驟轉成適合貼到其他地方（筆記、Excel、聊天視窗）的純文字區塊。
function formatStepText(step) {
  const lines = [step.title];
  if (step.formula) lines.push("公式: " + step.formula);
  if (step.inputs && Object.keys(step.inputs).length) lines.push("輸入: " + JSON.stringify(step.inputs));
  if (step.result && Object.keys(step.result).length) lines.push("結果: " + JSON.stringify(step.result));
  if (step.note) lines.push("備註: " + step.note);
  return lines.join("\n");
}

function renderTrace(steps) {
  const s = section("完整計算過程", "Calculation trace · 可重現技術收據");
  const details = el("details", { class: "trace" });
  const summary = el("summary", { text: `顯示完整計算過程 (${steps.length} 步，含 Vertex 等技術性角點的原始數值)` });
  details.appendChild(summary);
  s._exportMeta.blocks = steps.map(formatStepText);

  steps.forEach((step) => {
    const box = el("div", { class: "trace-step" });
    box.appendChild(el("div", { class: "t-title", text: step.title }));
    if (step.formula) box.appendChild(el("div", { class: "t-formula", text: step.formula }));
    if (step.inputs && Object.keys(step.inputs).length) {
      box.appendChild(el("div", { class: "t-kv", text: "輸入: " + JSON.stringify(step.inputs) }));
    }
    if (step.result && Object.keys(step.result).length) {
      box.appendChild(el("div", { class: "t-kv", text: "結果: " + JSON.stringify(step.result) }));
    }
    if (step.note) box.appendChild(el("div", { class: "t-note", text: step.note }));
    details.appendChild(box);
  });
  s.appendChild(details);
  return s;
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!applicationReady || !applicationProfile) {
    errorEl.textContent = "尚未完成執行環境確認，請重新載入頁面後再試。";
    errorEl.classList.remove("hidden");
    return;
  }
  const submitButton = calculateButton;
  discardComputedSensitiveState();
  privacyStatusEl.textContent = "";
  privacyStatusEl.classList.add("hidden");
  const abortController = new AbortController();
  const requestToken = sensitiveLifecycle.beginRequest(abortController);
  submitButton.disabled = true;
  setSubmitLabel(submitButton, "計算中");
  errorEl.classList.add("hidden");
  resultsEl.classList.remove("hidden");
  resultsEl.innerHTML = "";
  resultsEl.appendChild(el("div", { class: "loading", text: "計算中..." }));

  const tzMode = document.querySelector('input[name="tz-mode"]:checked').value;

  const payload = {
    datetime: {
      year: Number(document.getElementById("year").value),
      month: Number(document.getElementById("month").value),
      day: Number(document.getElementById("day").value),
      hour: Number(document.getElementById("hour").value),
      minute: Number(document.getElementById("minute").value),
      second: Number(document.getElementById("second").value),
    },
    // fold 只在 mode='iana' 時送出——後端會拒絕 fixed_offset 搭配非零 fold（該組合沒有意義）
    timezone: tzMode === "iana"
      ? {
          mode: "iana",
          iana_name: document.getElementById("iana-name").value.trim(),
          fold: Number(document.querySelector('input[name="fold"]:checked').value),
        }
      : { mode: "fixed_offset", utc_offset_hours: Number(document.getElementById("fixed-offset").value) },
    location: {
      latitude: Number(document.getElementById("latitude").value),
      longitude: Number(document.getElementById("longitude").value),
      altitude_m: Number(document.getElementById("altitude").value),
    },
    atmosphere: {
      pressure_hpa: document.getElementById("pressure-hpa").value.trim() === ""
        ? null
        : Number(document.getElementById("pressure-hpa").value),
      temperature_c: Number(document.getElementById("temperature-c").value),
    },
    computation_mode: {
      center: document.querySelector('input[name="center"]:checked').value,
      zodiac: document.querySelector('input[name="zodiac"]:checked').value,
      ayanamsa: document.getElementById("ayanamsa-select").value,
      position_mode: document.querySelector('input[name="position_mode"]:checked').value,
      ecliptic_frame: document.querySelector('input[name="ecliptic_frame"]:checked').value,
      nutation: document.getElementById("nutation").checked,
    },
    options: {
      house_system: document.querySelector('input[name="house_system"]:checked').value,
      include_fixed_stars: document.getElementById("opt-fixed-stars").checked,
      include_lots: document.getElementById("opt-lots").checked,
      include_antiscia: document.getElementById("opt-antiscia").checked,
      include_void_of_course: document.getElementById("opt-voc").checked,
      include_declination_aspects: document.getElementById("opt-declination-aspects").checked,
      include_outer_planets: document.getElementById("opt-outer").checked,
      include_lunar_phases: document.getElementById("opt-lunar-phases").checked,
      include_eclipses: document.getElementById("opt-eclipses").checked,
      include_rise_set_transits: document.getElementById("opt-rise-set-transits").checked,
    },
  };

  try {
    let resp;
    try {
      resp = await fetch("/api/chart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: abortController.signal,
      });
    } catch (networkError) {
      throw new Error(ClientContext.networkErrorMessage(applicationProfile));
    }

    let data;
    try {
      data = await resp.json();
    } catch (parseError) {
      const recovery = applicationProfile === ClientContext.PROFILES.LOCAL
        ? "請重新啟動 Classical Astrology App。"
        : "請稍後重試；若持續發生，請聯絡邀請者。";
      throw new Error(`服務回傳了非預期格式（HTTP ${resp.status}）。${recovery}`);
    }
    if (!sensitiveLifecycle.isCurrentRequest(requestToken)) return;
    if (!resp.ok) {
      throw new Error(ClientContext.formatApiError(data.detail, resp.status));
    }
    resultsEl.innerHTML = "";
    sectionSequence = 0;

    const astro = data.astronomical_data;
    const geom = data.derived_geometry;
    const methods = data.derived_methods;

    // 星曆檔警告分兩層檢查：(1) 啟動時的目錄檔案存在性檢查（粗略，只看有沒有檔案）
    // (2) 每個星體/恆星實際計算時的 retflag（精確，能抓到「檔案存在但涵蓋範圍不足/損毀」
    // 這種粗略檢查抓不到的情況）——只看 (1) 會漏掉 (2) 才會出現的個別退回 Moshier 狀況。
    // DST 警告放在最前面且獨立樣式：這是唯一會讓整張命盤的出生時間差一小時（進而改變
    // ASC/宮位/月亮）的情況，絕不能只埋在預設收合的 calculation_trace 裡。
    if (astro.time.dst_warning) {
      resultsEl.appendChild(el("div", { class: "warn-banner dst", text: "⚠ " + astro.time.dst_warning }));
    }

    const startupNote = data.calculation_trace.find((s) => s.title.includes("星曆檔檢查"));
    if (startupNote) {
      resultsEl.appendChild(el("div", { class: "warn-banner", text: startupNote.note }));
    }
    const fallbackNames = [...astro.bodies, ...astro.nodes, ...astro.fixed_stars]
      .filter((b) => b.used_full_ephemeris === false)
      .map((b) => b.name);
    if (fallbackNames.length) {
      resultsEl.appendChild(el("div", {
        class: "warn-banner",
        text: `⚠ 以下星體實際計算時退回 Moshier 半分析模型（精度較低，可能是星曆檔涵蓋範圍不足或已損毀）：${fallbackNames.join("、")}`,
      }));
    }

    const sectionNodes = [
      renderDossier(data.calculation_dossier),
      renderLibraryBanner(data.library_info, data.computation_mode, astro.time.ayanamsa),
      renderTimeConversion(astro.time, astro.atmosphere),
      renderBodies("七政", astro.bodies),
      renderBodies("南北交點", astro.nodes),
      renderHouses(astro.angles, methods.house_division),
    ];
    const starsSection = renderFixedStars(astro.fixed_stars);
    if (starsSection) sectionNodes.push(starsSection);
    const lunarSection = renderLunarEvents(astro.lunar_events);
    if (lunarSection) sectionNodes.push(lunarSection);
    const horizonSection = renderHorizonEvents(astro.horizon_events);
    if (horizonSection) sectionNodes.push(horizonSection);

    const antisciaSection = renderAntiscia(geom.antiscia);
    if (antisciaSection) sectionNodes.push(antisciaSection);

    const sectSection = renderSect(methods.sect);
    if (sectSection) sectionNodes.push(sectSection);
    const lotsSection = renderLots(methods.lots);
    if (lotsSection) sectionNodes.push(lotsSection);
    const vocSection = renderVoc(methods.void_of_course);
    if (vocSection) sectionNodes.push(vocSection);
    const declSection = renderDeclinationAspects(methods.declination_aspects);
    if (declSection) sectionNodes.push(declSection);

    sectionNodes.push(renderTrace(data.calculation_trace));
    const exportDocument = ChartExport.createDocument(
      data,
      sectionNodes.map(collectSectionModel)
    );
    sensitiveLifecycle.setCanonicalDocument(exportDocument);
    const canonicalSectionsById = new Map(
      exportDocument.sections.map((sectionModel) => [sectionModel.id, sectionModel])
    );
    sectionNodes.forEach((node) => {
      const canonicalSection = canonicalSectionsById.get(node._exportMeta.id);
      if (!canonicalSection) {
        throw new Error(`找不到輸出區段的 canonical snapshot：${node._exportMeta.id}`);
      }
      node._canonicalExportSection = canonicalSection;
      sensitiveLifecycle.registerSectionNode(node);
    });
    resultsEl.appendChild(renderExportPanel(exportDocument));
    sectionNodes.forEach((node) => resultsEl.appendChild(node));
  } catch (err) {
    if (!sensitiveLifecycle.isCurrentRequest(requestToken)) return;
    discardComputedSensitiveState();
    resultsEl.classList.add("hidden");
    errorEl.classList.remove("hidden");
    errorEl.textContent = "計算失敗：\n" + err.message;
    submitButton.disabled = false;
    setSubmitLabel(submitButton, "開始計算");
  } finally {
    if (sensitiveLifecycle.isCurrentRequest(requestToken)) {
      sensitiveLifecycle.finishRequest(requestToken);
      submitButton.disabled = false;
      setSubmitLabel(submitButton, "開始計算");
    }
  }
});

loadExampleButton.addEventListener("click", loadExampleData);

clearResultsButton.addEventListener("click", () => {
  clearSensitiveData({
    clearForm: false,
    announcement: "已清除計算結果、匯出快照與尚未完成的 request；表單輸入仍保留。",
  });
});

panicClearButton.addEventListener("click", () => {
  clearSensitiveData({
    clearForm: true,
    announcement: "已清除本頁表單、結果、錯誤、匯出快照與暫存 Blob URL。",
  });
});

window.addEventListener("pagehide", () => {
  clearSensitiveData({
    clearForm: true,
    announcement: "",
    stripUrl: false,
    focusForm: false,
  });
});

// HTML 先把原生 submit 鎖住；只有整份程式執行完、上方 preventDefault
// handler 已安裝後才解鎖。如此首次載入較慢或 app.js 載入失敗時，
// 使用者的第一下點擊不會退回 GET /?...、重載頁面並清空已填資料。
async function initializeApplication() {
  syncTimezoneControls();
  syncZodiacControls();
  try {
    await initializeApplicationProfile();
    applicationReady = true;
    calculateButton.disabled = false;
    setSubmitLabel(calculateButton, "開始計算");
  } catch (_error) {
    applicationReady = false;
    profileBadge.textContent = "環境驗證失敗";
    calculateButton.disabled = true;
    setSubmitLabel(calculateButton, "無法開始");
    errorEl.textContent = (
      "無法確認本頁連到哪一種執行環境。請重新載入頁面；若持續發生，"
      + "請重新啟動本機 App 或聯絡 Private Alpha 邀請者。"
    );
    errorEl.classList.remove("hidden");
  }
}

initializeApplication();
