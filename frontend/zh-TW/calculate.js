(function calculatePage() {
  "use strict";

  // 這一層只做接線：收表單、打同源 API、把 view tree 變成元素、接匯出與清除。
  // 「回應長什麼樣」在 view-model.js，「有哪些選項」在 options-catalogue.js，
  // 序列化在 exporters.js。這裡不得自行讀 source_response 組畫面（契約 §10）。

  const Catalogue = window.OptionsCatalogue;
  const LocationReceipt = window.LocationReceipt;
  const lifecycle = window.PrivacyLifecycle.createSensitiveDataLifecycle({
    revokeObjectUrl: (url) => URL.revokeObjectURL(url),
  });

  const el = (id) => document.getElementById(id);
  const form = el("chart-form");
  const statusLine = el("status-line");
  const errorPanel = el("error-panel");
  const errorMessage = el("error-message");
  const errorActions = el("error-actions");
  const results = el("results");
  const sectionsHost = el("sections");
  const warningsHost = el("warnings");
  const versionsHost = el("versions");
  const submitButton = el("submit-button");
  const cancelButton = el("cancel-button");

  // 讀值、範圍檢查與模糊時刻判定都在 RequestInput，理由見該模組開頭：舊版把
  // 「沒填」與「填了但不合法」壓成同一個回傳值，於是 12.5 靜默變 12
  // （Math.trunc），而 `|| 0` 讓 NaN 靜默變 0——兩者都會算出使用者沒有輸入
  // 的時刻。宣告放在這裡而不是 buildPayload 附近，因為 refreshFoldChoice
  // 也用它，而那個函式定義得更早（const 有 TDZ，宣告必須先於任何呼叫）。
  const RI = window.RequestInput;

  let profile = null;
  let optionValues = Catalogue.defaults();
  let selectedPlace = null;            // 目錄選定的地點，含收據欄位
  const sectionSnapshots = new Map();  // section id -> canonical section

  const profileReady = resolveProfile();

  function resolveProfile() {
    return fetch("/api/client-config", { headers: { Accept: "application/json" } })
      .then((response) => {
        if (!response.ok) throw new Error(`client-config HTTP ${response.status}`);
        return response.json();
      })
      .then((payload) => {
        profile = window.ClientContext.validateClientConfiguration(payload).profile;
      })
      .catch(() => {
        // 解析失敗不顯示在畫面上；它只影響錯誤訊息要說「本機後端沒開」還是「網路不通」。
        profile = null;
      });
  }

  // ══ 選項介面：由目錄產生，不手寫 ══════════════════════
  //
  // 摺疊只有一層：「進階選項」開或關。展開後所有選項一次攤在畫面上，
  // 分組只是視覺分隔線，不是要點開的容器，也沒有搜尋框——
  // 使用者是看到 Lilith 才想起要看它，不是先想起名字再去搜尋（辨識優於回憶）。
  // 相依子項縮排在父項底下，不另外摺疊。

  function buildOptionUi() {
    const host = el("option-groups");
    host.replaceChildren();

    Catalogue.GROUPS.filter((group) => group.in_advanced).forEach((group) => {
      const block = document.createElement("section");
      block.className = "option-block";
      block.dataset.group = group.key;

      const heading = document.createElement("h3");
      heading.append(group.label_zh);
      const en = document.createElement("span");
      en.className = "en";
      en.textContent = group.label_en;
      heading.appendChild(en);
      block.appendChild(heading);

      Catalogue.OPTIONS
        .filter((option) => option.group === group.key && !option.depends_on)
        .forEach((option) => block.appendChild(buildOptionRow(option)));

      host.appendChild(block);
    });

    // 宮位兩項已升到主表單，插進口徑區而不是進階選項。
    el("house-system-slot").replaceChildren(buildHouseControl());

    refreshOptionUi();
  }

  // 宮位以單一下拉表達兩個欄位：選「不計算宮位」等於 include_houses=false。
  // 使用者不需要先勾一個開關才能選制度。
  const NO_HOUSES = "__none__";

  function buildHouseControl() {
    const option = Catalogue.BY_KEY.house_system;
    const select = markNoRestore(document.createElement("select"));
    select.id = "opt-house_system";
    select.className = "option-control";

    const none = document.createElement("option");
    none.value = NO_HOUSES;
    none.textContent = "不計算宮位　No houses";
    select.appendChild(none);

    option.values.forEach((value) => {
      const opt = document.createElement("option");
      opt.value = String(value.value);
      opt.textContent = `${value.label_zh}　${value.label_en}`;
      select.appendChild(opt);
    });

    select.addEventListener("change", () => {
      if (select.value === NO_HOUSES) {
        optionValues = { ...optionValues, include_houses: false };
      } else {
        optionValues = {
          ...optionValues, include_houses: true, house_system: select.value,
        };
      }
      refreshOptionUi();
    });

    // include_houses 仍需一個具名節點，讓覆蓋率檢查與測試找得到它。
    const mirror = document.createElement("input");
    mirror.type = "hidden";
    mirror.id = "opt-include_houses";
    mirror.className = "option-control";
    const wrap = document.createElement("div");
    wrap.className = "house-control";
    wrap.appendChild(select);
    wrap.appendChild(mirror);
    return wrap;
  }

  function buildOptionRow(option) {
    const row = document.createElement("div");
    row.className = "option-row";
    row.dataset.option = option.key;

    const label = document.createElement("label");
    label.className = "option-label";
    label.htmlFor = `opt-${option.key}`;
    label.append(option.label_zh);
    const en = document.createElement("span");
    en.className = "en";
    en.textContent = option.label_en;
    label.appendChild(en);

    row.appendChild(label);
    row.appendChild(buildControl(option));

    if (option.help_zh) {
      const help = document.createElement("p");
      help.className = "option-help";
      help.textContent = option.help_zh;
      row.appendChild(help);
    }

    const children = Catalogue.OPTIONS.filter((child) => child.depends_on === option.key);
    if (children.length) {
      const nest = document.createElement("div");
      nest.className = "option-children";
      nest.dataset.childrenOf = option.key;
      children.forEach((child) => nest.appendChild(buildOptionRow(child)));
      row.appendChild(nest);
    }
    return row;
  }

  // 每個動態產生的控制項都必須關閉瀏覽器的表單還原。
  //
  // 實測（2026-08-05，Chromium）：不關閉時，重新載入會讓瀏覽器把 checkbox 復原成
  // 上一次的狀態並觸發 change，於是送出的模組組合與畫面初始狀態不符——
  // 那正是 PRODUCT_CHARTER 禁止的「隱藏預設值影響結果」。
  function markNoRestore(control) {
    control.setAttribute("autocomplete", "off");
    return control;
  }

  function buildControl(option) {
    if (option.type === "boolean" && option.render === "select") {
      const select = markNoRestore(document.createElement("select"));
      select.id = `opt-${option.key}`;
      select.className = "option-control";
      option.value_labels.forEach((entry) => {
        const opt = document.createElement("option");
        opt.value = entry.value ? "true" : "false";
        opt.textContent = `${entry.label_zh}　${entry.label_en}`;
        select.appendChild(opt);
      });
      select.addEventListener("change", () => setOption(option.key, select.value === "true"));
      return select;
    }
    if (option.type === "boolean") {
      const input = markNoRestore(document.createElement("input"));
      input.type = "checkbox";
      input.id = `opt-${option.key}`;
      input.className = "option-control";
      input.addEventListener("change", () => setOption(option.key, input.checked));
      return input;
    }
    if (option.type === "choice") {
      const select = markNoRestore(document.createElement("select"));
      select.id = `opt-${option.key}`;
      select.className = "option-control";
      option.values.forEach((value) => {
        const opt = document.createElement("option");
        // null 在 DOM 只能是字串，用固定哨兵往返，避免與真實值 "null" 混淆。
        opt.value = value.value === null ? "\u0000null" : String(value.value);
        opt.textContent = `${value.label_zh}　${value.label_en}`;
        select.appendChild(opt);
      });
      select.addEventListener("change", () => {
        setOption(option.key, select.value === "\u0000null" ? null : select.value);
      });
      return select;
    }
    const input = markNoRestore(document.createElement("input"));
    input.type = "number";
    input.id = `opt-${option.key}`;
    input.className = "option-control";
    if (option.min !== undefined) input.min = String(option.min);
    if (option.max !== undefined) input.max = String(option.max);
    if (option.step !== undefined) input.step = String(option.step);
    input.addEventListener("change", () => {
      const raw = input.value.trim();
      setOption(option.key, raw === "" ? null : Number(raw));
    });
    return input;
  }

  function setOption(key, value) {
    optionValues = { ...optionValues, [key]: value };
    refreshOptionUi();
  }

  function refreshOptionUi() {
    Catalogue.OPTIONS.forEach((option) => {
      const control = document.getElementById(`opt-${option.key}`);
      if (!control) return;
      const value = optionValues[option.key];
      if (option.type === "boolean" && option.render === "select") {
        control.value = value === true ? "true" : "false";
      } else if (option.type === "boolean") control.checked = value === true;
      else if (option.type === "choice") {
        control.value = value === null ? "\u0000null" : String(value);
      } else control.value = value === null || value === undefined ? "" : String(value);

      const row = control.closest(".option-row");
      if (row) row.classList.toggle("is-changed", Catalogue.isEnabled(option, value));

      // 互斥：停用並就地說明，不隱藏。隱藏會讓人以為選項不存在。
      const conflict = Catalogue.conflictFor(option, optionValues);
      control.disabled = !!conflict;
      if (row) {
        row.classList.toggle("is-blocked", !!conflict);
        let note = row.querySelector(".conflict-note");
        if (conflict) {
          if (!note) {
            note = document.createElement("p");
            note.className = "conflict-note";
            row.appendChild(note);
          }
          note.textContent = `已由「${conflict.source.label_zh}」停用：${conflict.reason_zh}`;
        } else if (note) {
          note.remove();
        }
      }

      const nest = document.querySelector(`[data-children-of="${option.key}"]`);
      if (nest) {
        const active = option.type === "boolean"
          ? value === true
          : value !== null && value !== undefined;
        nest.hidden = !active;
      }
    });

    const houseSelect = document.getElementById("opt-house_system");
    if (houseSelect) {
      houseSelect.value = optionValues.include_houses === false
        ? NO_HOUSES : String(optionValues.house_system);
      const mirror = document.getElementById("opt-include_houses");
      if (mirror) mirror.value = String(optionValues.include_houses === true);
    }

    const counts = Catalogue.enabledCountByGroup(optionValues);
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    el("advanced-summary").textContent = total
      ? `進階選項：已調整 ${total} 項`
      : "進階選項：全部為產品預設";
    el("advanced-summary").dataset.state = total ? "changed" : "default";
  }

  el("reset-options").addEventListener("click", () => {
    optionValues = Catalogue.defaults();
    refreshOptionUi();
    setStatus("進階選項已回到產品預設。", "info");
  });

  // ══ 地點：離線目錄查詢 ════════════════════════════════
  const placeQuery = el("place-query");
  const placeResults = el("place-results");
  const placeStatus = el("place-status");
  const PLACE_SEARCH_TIMEOUT_MS = 10000;
  let activePlaceSearchController = null;
  let activePlaceSearchGeneration = 0;

  el("place-search-button").addEventListener("click", searchPlaces);
  placeQuery.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); searchPlaces(); }
  });

  function searchPlaces() {
    if (activePlaceSearchController) activePlaceSearchController.abort();
    const generation = ++activePlaceSearchGeneration;
    activePlaceSearchController = null;
    const query = placeQuery.value.trim();
    placeResults.replaceChildren();
    placeResults.hidden = true;
    if (query.length < 2) {
      placeStatus.textContent = "請輸入至少兩個字再查詢。";
      placeStatus.dataset.tone = "error";
      return;
    }
    placeStatus.textContent = "查詢中…";
    placeStatus.dataset.tone = "info";
    const controller = new AbortController();
    activePlaceSearchController = controller;
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, PLACE_SEARCH_TIMEOUT_MS);

    fetch("/api/places/search", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ query, limit: 10 }),
      signal: controller.signal,
    })
      .then((response) =>
        response.json().catch(() => null).then((body) => ({ response, body }))
      )
      .then(({ response, body }) => {
        if (generation !== activePlaceSearchGeneration) return;
        if (!response.ok) {
          placeStatus.textContent = window.ClientContext.formatApiError(
            body && body.detail, response.status
          );
          placeStatus.dataset.tone = "error";
          return;
        }
        const found = (body && body.results) || [];
        // 後端會回一份 query 收據，說明有沒有詞被丟掉。不讀它，等於把一個
        // 被改寫過的查詢報成使用者自己的查詢（PIA-2026-08-06-009）。
        const truncation = window.ClientContext.placeQueryNotice(body && body.query);
        if (!found.length) {
          placeStatus.textContent = truncation
            ? `內建目錄沒有符合的地名。${truncation}`
            : "內建目錄沒有符合的地名。可以改用鄰近的較大地點，或在下方手動輸入座標與時區。";
          placeStatus.dataset.tone = "error";
          return;
        }
        placeStatus.textContent = truncation
          ? `找到 ${found.length} 筆。座標為目錄的代表點，不是出生地址。${truncation}`
          : `找到 ${found.length} 筆。座標為目錄的代表點，不是出生地址。`;
        // 被截斷時是警告而不是單純資訊：結果不涵蓋使用者打的全部內容。
        placeStatus.dataset.tone = truncation ? "warning" : "info";
        found.forEach((place) => placeResults.appendChild(buildPlaceRow(place)));
        placeResults.hidden = false;
      })
      .catch((error) => {
        if (generation !== activePlaceSearchGeneration) return;
        placeStatus.textContent = timedOut && error && error.name === "AbortError"
          ? "地名查詢逾時。請重試，或在下方手動輸入座標與時區。"
          : "地名查詢沒有完成。這個查詢只讀取內建目錄，不會對外連線；請重試，或在下方手動輸入座標與時區。";
        placeStatus.dataset.tone = "error";
      })
      .finally(() => {
        window.clearTimeout(timer);
        if (generation === activePlaceSearchGeneration) {
          activePlaceSearchController = null;
        }
      });
  }

  function buildPlaceRow(place) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "place-option";

    const name = document.createElement("b");
    name.textContent = place.display_name || place.name;
    button.appendChild(name);

    // 同名地點必須靠這一行分辨，因此經緯度、時區、來源與人口都要出現。
    const meta = document.createElement("span");
    meta.className = "place-meta mono";
    meta.textContent = [
      `${Number(place.latitude).toFixed(4)}, ${Number(place.longitude).toFixed(4)}`,
      place.timezone,
      place.country_code,
      place.population ? `人口 ${place.population.toLocaleString("en-US")}` : "人口資料無",
      place.source,
    ].filter(Boolean).join("　·　");
    button.appendChild(meta);

    button.addEventListener("click", () => selectPlace(place));
    item.appendChild(button);
    return item;
  }

  function selectPlace(place) {
    selectedPlace = place;
    el("latitude").value = String(place.latitude);
    el("longitude").value = String(place.longitude);
    if (!el("altitude").value.trim()) el("altitude").value = "0";
    el("timezone").value = place.timezone || "";

    const body = el("place-receipt-body");
    body.replaceChildren();
    [
      ["地點", place.display_name || place.name],
      ["緯度 latitude", Number(place.latitude).toFixed(6)],
      ["經度 longitude", Number(place.longitude).toFixed(6)],
      ["時區 timezone", place.timezone || "—"],
      ["目錄來源 source", place.source],
      ["目錄記錄 id", place.source_record_id],
      ["座標語義", place.coordinate_semantics],
      ["位置精度", place.location_precision],
      ["比對層級", place.match_tier],
    ].forEach(([term, value]) => {
      const dt = document.createElement("dt");
      dt.textContent = term;
      const dd = document.createElement("dd");
      dd.textContent = value === undefined || value === null ? "—" : String(value);
      body.appendChild(dt);
      body.appendChild(dd);
    });

    el("place-caveat").textContent =
      "這組座標是目錄對該地點的代表點，不是你的出生地址。上升點對經緯度敏感——"
      + "若你知道更精確的座標，請在下方手動輸入取代它。時區已依目錄帶入，仍請確認出生當時實際適用的時區。";
    el("place-receipt").hidden = false;
    placeResults.hidden = true;
    placeStatus.textContent = "已選定地點，座標與時區已填入。";
    placeStatus.dataset.tone = "info";
  }

  // 手動改動座標或時區時，目錄收據不再成立，必須撤下。
  ["latitude", "longitude", "timezone"].forEach((id) => {
    el(id).addEventListener("input", () => {
      if (!selectedPlace) return;
      const drifted =
        !LocationReceipt.sameCoordinate(el("latitude").value, selectedPlace.latitude)
        || !LocationReceipt.sameCoordinate(el("longitude").value, selectedPlace.longitude)
        || String(el("timezone").value) !== String(selectedPlace.timezone || "");
      if (drifted) {
        selectedPlace = null;
        el("place-receipt").hidden = true;
        placeStatus.textContent = "座標或時區已手動修改，目錄收據不再適用。";
        placeStatus.dataset.tone = "info";
      }
    });
  });

  // ══ 表單連動 ══════════════════════════════════════════
  const precisionConsequence = el("precision-consequence");
  const zodiacConsequence = el("zodiac-consequence");
  const hourInput = el("hour");
  const minuteInput = el("minute");
  const secondInput = el("second");
  const zodiacSelect = el("zodiac");
  const ayanamsaSelect = el("ayanamsa");

  function currentPrecision() {
    const checked = form.querySelector('input[name="precision"]:checked');
    return checked ? checked.value : "exact";
  }

  function applyPrecisionConsequences() {
    const precision = currentPrecision();
    minuteInput.disabled = precision !== "exact";
    secondInput.disabled = precision !== "exact";
    hourInput.disabled = precision === "date_only";
    if (precision !== "exact") { minuteInput.value = ""; secondInput.value = ""; }
    if (precision === "date_only") hourInput.value = "";
    const messages = {
      exact: "",
      approximate_hour:
        "分與秒會以 0 送出，因為後端只接受已知的民用小時，並會自行對整個小時取樣。這些 0 不代表你出生在整點。",
      date_only:
        "時、分、秒都會以 0 送出，那只是 API 的日期容器，不代表你出生在午夜。"
        + "後端在這個模式下會關閉宮位計算，因為宮位需要已知時刻；結果中的宮位區塊會標示為未請求。",
    };
    precisionConsequence.hidden = !messages[precision];
    precisionConsequence.textContent = messages[precision];
  }

  function applyZodiacConsequences() {
    const sidereal = zodiacSelect.value === "sidereal";
    ayanamsaSelect.disabled = !sidereal;
    zodiacConsequence.hidden = !sidereal;
    zodiacConsequence.textContent = sidereal
      ? "恆星黃道下，必然尊貴會被產品明確拒絕而不是算錯——尊貴的判準建立在回歸黃道上，"
        + "尚未有授權的恆星黃道版本。結果中會顯示拒絕的原因代碼。"
      : "";
  }

  // ── 重複的民用小時（SD-32 / PIA-2026-08-06-005）──────────
  //
  // 秋季調慢當天，同一個時鐘時間出現兩次。後端支援 fold 指明是哪一次，
  // 但介面送不出去，於是永遠採用第一次——盤可能整個差一小時，而畫面不說。
  // 這裡在偵測到模糊時把選擇交還給使用者，並在沒選之前 fail closed。
  let foldChoice = null;   // null = 尚未選；0/1 = 使用者選定的那一次

  function currentAmbiguity() {
    const dateValue = String(el("date").value).trim();
    const timezone = String(el("timezone").value).trim();
    if (!dateValue || !timezone) return null;
    const [year, month, day] = dateValue.split("-").map(Number);
    const precision = currentPrecision();
    // date_only 由後端對整個民用日取樣，approximate_hour 則會自己 fail closed
    // 並要求 fold；只有 exact 需要在這裡把選擇呈現出來。
    if (precision !== "exact") return null;
    const hour = RI.readInteger(String(hourInput.value));
    const minute = RI.readInteger(String(minuteInput.value));
    if (hour.state !== RI.STATES.VALUE || minute.state !== RI.STATES.VALUE) return null;
    const second = RI.readSecond(String(secondInput.value));
    const result = RI.civilTimeOccurrences(
      {
        year, month, day,
        hour: hour.value,
        minute: minute.value,
        second: second.state === RI.STATES.VALUE ? second.value : 0,
      },
      timezone
    );
    return result && result.state === "ambiguous" ? result : null;
  }

  function refreshFoldChoice() {
    const ambiguity = currentAmbiguity();
    const host = el("fold-choice");
    if (!ambiguity) {
      host.hidden = true;
      el("fold-choice-options").replaceChildren();
      // signature 必須一起清掉。留著的話，使用者清空表單後再輸入**同一個**
      // 模糊時刻，下面的早退會判定「選項沒變」而不重建——於是選擇區永遠不再
      // 出現，但送出仍被 fail-closed 擋下，使用者陷入無法完成計算的死路。
      delete host.dataset.signature;
      foldChoice = null;
      return;
    }
    const signature = ambiguity.occurrences.map((o) => o.utcIso).join("|");
    if (host.dataset.signature === signature) return;   // 同一組選項不重建
    host.dataset.signature = signature;
    foldChoice = null;

    el("fold-choice-title").textContent =
      "這個時間在所選時區出現了兩次，請指明是哪一次。";
    const options = el("fold-choice-options");
    options.replaceChildren();
    ambiguity.occurrences.forEach((occurrence, index) => {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "fold";
      input.value = String(occurrence.fold);
      input.addEventListener("change", () => { foldChoice = occurrence.fold; });
      const caption = document.createElement("span");
      const which = index === 0 ? "第一次（較早）" : "第二次（較晚）";
      const sign = occurrence.offsetHours >= 0 ? "+" : "−";
      const magnitude = Math.abs(occurrence.offsetHours);
      const bold = document.createElement("b");
      bold.textContent = `${which}　UTC${sign}${magnitude}`;
      const detail = document.createElement("span");
      detail.textContent = `對應世界時 ${occurrence.utcIso.replace(".000Z", "Z")}`;
      caption.append(bold, detail);
      label.append(input, caption);
      options.appendChild(label);
    });
    el("fold-choice-note").textContent =
      "日光節約時間結束當天，時鐘會往回撥，於是同一個鐘面時間重複一次。"
      + "兩者相差一小時，算出來的上升點可能落在不同星座，所以這裡不替你猜。"
      + "若真的無法確定，任選一個，並把這件事記在結果之外——收據會記下採用了哪一次。";
    host.hidden = false;
  }

  form.addEventListener("change", (event) => {
    if (event.target.name === "precision") applyPrecisionConsequences();
    if (event.target.id === "zodiac") applyZodiacConsequences();
    if (event.target.name !== "fold") refreshFoldChoice();
  });
  form.addEventListener("input", (event) => {
    if (event.target.name !== "fold") refreshFoldChoice();
  });

  el("example-button").addEventListener("click", () => {
    form.querySelector('input[name="precision"][value="exact"]').checked = true;
    applyPrecisionConsequences();
    el("date").value = "1990-05-15";
    hourInput.value = "14";
    minuteInput.value = "30";
    el("latitude").value = "24.1477";
    el("longitude").value = "120.6736";
    el("altitude").value = "80";
    el("timezone").value = "Asia/Taipei";
    selectedPlace = null;
    el("place-receipt").hidden = true;
    el("manual-place").open = true;
    setStatus("已載入合成範例。這不是任何真實人物的出生資料。", "info");
  });

  // ══ 送出 ══════════════════════════════════════════════
  /**
   * 讀一個欄位，把問題分流到 missing 或 invalid。回傳數值或 null。
   * 兩者給使用者的下一步不同：一個是「去填」，一個是「你填的那個值不對」。
   */
  function collect(reader, input, label, required, missing, invalid) {
    const read = reader(String(input.value));
    if (read.state === RI.STATES.EMPTY) {
      if (required) missing.push(label);
      return null;
    }
    if (read.state === RI.STATES.INVALID) {
      invalid.push(`${label}${read.reason}`);
      return null;
    }
    return read.value;
  }

  function buildPayload() {
    const missing = [];
    const invalid = [];
    const dateValue = String(el("date").value).trim();
    if (!dateValue) missing.push("出生日期");
    const timezone = String(el("timezone").value).trim();
    if (!timezone) missing.push("時區（查詢地點或手動輸入）");

    const precision = currentPrecision();
    const hour = precision === "date_only"
      ? 0
      : collect(RI.readInteger, hourInput, "小時", true, missing, invalid);
    const minute = precision === "exact"
      ? collect(RI.readInteger, minuteInput, "分", true, missing, invalid)
      : 0;
    // 秒不填視為 0；非 exact 模式下後端要求必須為 0。
    const secondRead = precision === "exact"
      ? collect(RI.readSecond, secondInput, "秒", false, missing, invalid)
      : 0;

    const latitude = collect(RI.readDecimal, el("latitude"), "緯度", true, missing, invalid);
    const longitude = collect(RI.readDecimal, el("longitude"), "經度", true, missing, invalid);
    const altitude = collect(RI.readDecimal, el("altitude"), "海拔", true, missing, invalid);

    // 數字選項的上下界在這裡檢查，不倚賴 HTML min/max：表單帶 novalidate，
    // 原生驗證不會執行，所以那些屬性只是提示，不是守衛。
    invalid.push(...RI.numericOptionProblems(Catalogue, optionValues));

    // 模糊時刻沒選就 fail closed（SD-32）。與其猜一個再附帶告知，不如
    // 停下來問——猜錯的代價是整張盤的上升點，而使用者當場就能回答。
    const ambiguity = currentAmbiguity();
    if (ambiguity && foldChoice === null) {
      invalid.push("這個時間在所選時區出現了兩次，請先指明是哪一次");
    }

    if (missing.length || invalid.length) return { ok: false, missing, invalid };

    const [year, month, day] = dateValue.split("-").map(Number);
    return {
      ok: true,
      payload: {
        birth_time_precision: precision,
        datetime: {
          year, month, day, hour, minute,
          second: secondRead === null ? 0 : secondRead,
        },
        // fold 只在真的模糊時送出。平時省略，讓收據忠實反映「使用者沒有
        // 需要做這個選擇」，而不是「他選了第一次」。
        timezone: ambiguity
          ? { mode: "iana", iana_name: timezone, fold: foldChoice }
          : { mode: "iana", iana_name: timezone },
        location: LocationReceipt.buildLocationInput({
          latitude,
          longitude,
          altitudeM: altitude,
          selectedPlace,
        }),
        atmosphere: { pressure_hpa: null, temperature_c: 15 },
        computation_mode: zodiacSelect.value === "sidereal"
          ? { zodiac: "sidereal", ayanamsa: ayanamsaSelect.value }
          : { zodiac: "tropical" },
        options: Catalogue.toRequestOptions(optionValues),
      },
    };
  }

  function setStatus(message, tone) {
    statusLine.textContent = message || "";
    statusLine.dataset.tone = tone || "info";
  }

  // FPI-2026-08-06-E-010。匯出的成功／失敗訊息全都寫進 #status-line，而那個元素
  // 在表單的 action 區；匯出工具列在結果區，「複製本區」更散布在其下二十多個區塊
  // 之中。使用者捲到結果深處按下按鈕時，訊息更新在視窗外——`aria-live` 對螢幕
  // 報讀有效，明眼使用者看到的是「按了沒反應」。
  //
  // 這裡在按下的那個按鈕旁補一則同文的短訊，全域那條保留不動：它承載 aria-live，
  // 移掉會把已經有效的無障礙回饋換成另一個問題。訊息內容只有固定字串與
  // error.message，不含使用者輸入。
  const INLINE_STATUS_TIMEOUT_MS = 6000;
  const inlineStatusTimers = new WeakMap();

  function reportAt(anchor, message, tone) {
    setStatus(message, tone);
    if (!anchor || !anchor.parentNode) return;
    let note = anchor.nextElementSibling;
    if (!note || !note.classList.contains("inline-status")) {
      note = document.createElement("span");
      note.className = "inline-status";
      anchor.insertAdjacentElement("afterend", note);
    }
    note.textContent = message;
    note.dataset.tone = tone || "info";
    window.clearTimeout(inlineStatusTimers.get(note));
    inlineStatusTimers.set(
      note,
      window.setTimeout(() => {
        note.textContent = "";
      }, INLINE_STATUS_TIMEOUT_MS)
    );
  }

  function showError(message, actions) {
    errorMessage.textContent = message;
    errorActions.replaceChildren();
    (actions || []).forEach((action) => {
      const item = document.createElement("li");
      item.textContent = action;
      errorActions.appendChild(item);
    });
    errorPanel.hidden = false;
  }

  function hideError() {
    errorPanel.hidden = true;
    errorMessage.textContent = "";
    errorActions.replaceChildren();
  }

  let submissionQueued = false;

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (submissionQueued) return;
    hideError();
    const built = buildPayload();
    if (!built.ok) {
      // 缺漏與不合法分開陳述。把兩者混成一句「必填欄位沒有填」，會讓
      // 「12.5 不是整數」讀起來像是那一格空著，使用者就會去填一個已經填了的欄位。
      const parts = [];
      if (built.missing.length) parts.push(`還有必填欄位沒有填：${built.missing.join("、")}。`);
      if (built.invalid.length) parts.push(`這些值送不出去：${built.invalid.join("；")}。`);
      const actions = [];
      if (built.missing.length) actions.push("補齊上列欄位後再送出。");
      if (built.invalid.length) actions.push("修正上列數值後再送出。");
      actions.push("若只知道大約時辰或只知道日期，改選對應的把握程度。");
      showError(parts.join(" "), actions);
      setStatus("尚未送出。", "error");
      return;
    }
    submissionQueued = true;
    submitButton.disabled = true;
    profileReady.then(() => submitPayload(built.payload));
  });

  // 守衛裝好之後才解除送出鈕。
  //
  // 表單沒有 action 也沒有 method，所以原生送出是「GET 回同一個網址」——
  // 出生日期、時刻與精確座標會變成 query string，違反契約 §4「不得把出生資料
  // 放入 URL、query string」。腳本還沒載入完就按下去正是那條路徑。
  // 因此 HTML 端讓它 disabled 起手，這裡是唯一解除它的地方，且必須排在
  // addEventListener 之後——先解除再裝守衛會留下一個同樣的時間窗。
  submitButton.disabled = false;
  submitButton.removeAttribute("title");

  /**
   * 送出後的等待有上限（PIA-2026-08-06-007）。
   *
   * 原本只建了 AbortController 卻沒有任何計時器，而送出鈕立刻 disabled，
   * 兩個會 abort 的清除鈕又躲在 hidden 的 #results 裡。半開連線或伺服器
   * 停住時，畫面會永遠停在「計算中…」，使用者只能重新載入整頁——而重新
   * 載入會清掉他剛輸入的出生資料。
   *
   * 30 秒：目前最重的組合（全恆星＋成相推算）在本機是數百毫秒等級，30 秒
   * 已遠超正常範圍；再長只是延長使用者對著沒有反應的畫面枯等。
   */
  const REQUEST_TIMEOUT_MS = 30000;

  function isJsonResponse(response) {
    const mediaType = (response.headers.get("Content-Type") || "")
      .split(";", 1)[0].trim().toLowerCase();
    return mediaType === "application/json" || mediaType.endsWith("+json");
  }

  function submitPayload(payload) {
    dropResultsForNewAttempt();
    const controller = new AbortController();
    const token = lifecycle.beginRequest(controller);
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      try { controller.abort(); } catch (_error) { /* generation 已失效即可 */ }
    }, REQUEST_TIMEOUT_MS);
    const finishAttempt = () => {
      window.clearTimeout(timer);
      cancelButton.hidden = true;
    };

    submitButton.disabled = true;
    cancelButton.hidden = false;
    setStatus("計算中…", "info");

    fetch("/api/chart", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    })
      .then((response) => {
        if (response.ok && !isJsonResponse(response)) {
          return { response, body: null, invalidJsonMedia: true };
        }
        return response.json().catch(() => null)
          .then((body) => ({ response, body, invalidJsonMedia: false }));
      })
      .then(({ response, body, invalidJsonMedia }) => {
        if (!lifecycle.isCurrentRequest(token)) return;   // late response 丟棄
        if (invalidJsonMedia) {
          showError("計算服務的回應格式不是 JSON。", ["請稍後重試。"]);
          setStatus("服務回應格式錯誤。", "error");
          return;
        }
        if (!response.ok) { handleApiError(response, body); return; }
        renderResponse(body);
      })
      .catch((error) => {
        if (!lifecycle.isCurrentRequest(token)) return;
        if (error && error.name === "AbortError") {
          // 逾時與使用者主動中止都走 AbortError，但兩者要說不同的話：
          // 一個是「服務沒回應」，一個是「你叫停了」。混為一談會讓逾時
          // 看起來像使用者自己按到的。
          if (timedOut) {
            showError(
              `等待計算結果超過 ${REQUEST_TIMEOUT_MS / 1000} 秒，已中止這次請求。`,
              ["表單內容仍在，可以直接再送出一次。",
               "若持續逾時，改用較少的選項組合，或稍後重試。"]
            );
            setStatus("計算逾時。", "error");
          } else {
            setStatus("已中止這次計算。表單內容保留。", "info");
          }
          return;
        }
        showError(window.ClientContext.networkErrorMessage(profile),
          ["確認服務仍在執行後重試。", "重新載入頁面。"]);
        setStatus("沒有送達計算服務。", "error");
      })
      .finally(() => {
        finishAttempt();
        submissionQueued = false;
        if (lifecycle.isCurrentRequest(token)) {
          lifecycle.finishRequest(token);
          submitButton.disabled = false;
        }
      });
  }

  /**
   * 只讀 `detail`，且只交給 ClientContext 的封閉對照表。
   * local profile 的 422 會把整份 request body 放進 `detail[].input`——
   * 那裡面有出生日期、時刻與精確座標，一個字都不會進入畫面（契約 §9）。
   */
  function handleApiError(response, body) {
    const statusCode = response.status;
    const detail = body && typeof body === "object" ? body.detail : null;
    const message = window.ClientContext.formatApiError(detail, statusCode);
    const actions = window.ClientContext.apiErrorActions(
      statusCode,
      response.headers.get("Retry-After"),
      profile
    );
    showError(message, actions);
    setStatus("服務拒絕了這次計算。", "error");
  }

  // ══ 渲染 ══════════════════════════════════════════════
  function renderResponse(response) {
    let canonical;
    try {
      canonical = window.ChartExport.createDocument(
        response, window.ChartViewModel.buildSections(response)
      );
    } catch (_error) {
      // 回應裡的 schema/status 值不可信。即使以 textContent 顯示不會造成 XSS，
      // 把原始例外文字放進 role=alert 仍會讓對方的文字看起來像本站公告。
      showError("這個版本的頁面無法安全呈現本次回應。",
        ["重新載入頁面以取得對應版本。", "若重新載入後仍失敗，請聯絡服務管理者。"]);
      setStatus("回應無法呈現。", "error");
      return;
    }

    lifecycle.setCanonicalDocument(canonical);
    sectionSnapshots.clear();
    canonical.sections.forEach((section) => sectionSnapshots.set(section.id, section));

    const tree = window.ChartViewModel.buildViewTree(canonical);
    versionsHost.textContent =
      `API ${tree.header.api_schema_version} · Dossier ${tree.header.dossier_version}`
      + ` · Export ${tree.header.export_contract_version}`;

    warningsHost.replaceChildren();
    warningsHost.hidden = tree.header.warnings.length === 0;
    tree.header.warnings.forEach((warning) => {
      const item = document.createElement("li");
      const code = document.createElement("b");
      code.textContent = warning.code;
      item.appendChild(code);
      item.appendChild(document.createTextNode(warning.message));
      warningsHost.appendChild(item);
    });

    // 結果頁有二十一個區塊、三百多列，沒有索引就只能一路捲。
    // 索引同時顯示每一區的環與收據狀態，讓「哪些被拒絕、哪些沒請求」在捲之前就看得到。
    buildSectionIndex(tree.sections);

    sectionsHost.replaceChildren();
    tree.sections.forEach((section) => sectionsHost.appendChild(materializeSection(section)));

    results.hidden = false;
    setStatus("計算完成。", "info");
  }

  function buildSectionIndex(sections) {
    const host = el("section-index");
    host.replaceChildren();
    sections.forEach((section) => {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = `#section-${section.id}`;
      link.className = "index-link";
      link.dataset.ring = section.ring;

      const name = document.createElement("span");
      name.textContent = section.title;
      link.appendChild(name);

      if (section.status.state !== "present") {
        const flag = document.createElement("span");
        flag.className = "index-state";
        flag.dataset.state = section.status.state;
        flag.textContent = section.status.label;
        link.appendChild(flag);
      }
      item.appendChild(link);
      host.appendChild(item);
    });
    el("section-index-wrap").hidden = sections.length === 0;
  }

  function materializeSection(section) {
    const host = document.createElement("section");
    host.className = "result-section";
    host.id = `section-${section.id}`;
    lifecycle.registerSectionNode(host);

    const head = document.createElement("div");
    head.className = "head";

    const ring = document.createElement("span");
    ring.className = "ring-tag";
    ring.dataset.ring = section.ring;
    ring.textContent = section.layer_label;
    head.appendChild(ring);

    const title = document.createElement("h3");
    title.textContent = section.title;
    head.appendChild(title);

    const state = document.createElement("span");
    state.className = "state";
    state.dataset.state = section.status.state;
    state.textContent = section.status.label;
    head.appendChild(state);

    if (section.status.reason_code) {
      const reason = document.createElement("span");
      reason.className = "reason-code";
      reason.textContent = `reason_code: ${section.status.reason_code}`;
      head.appendChild(reason);
    }

    // section copy：讀建立時綁定的 canonical 快照，不在點擊時重掃畫面。
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "ghost small section-copy";
    copy.textContent = "複製本區";
    copy.addEventListener("click", () => copySection(section.id, copy));
    head.appendChild(copy);

    host.appendChild(head);

    // 長區塊改為摺疊：一頁四百多列，全部攤開等於沒有結構。
    // 門檻式而非點名式——新的長區塊會自動適用，不必記得回來加。
    const rowCount = section.children
      .filter((child) => child.type === "table")
      .reduce((total, table) => total + table.rows.length, 0);
    const collapsible = rowCount > LONG_SECTION_ROWS;
    let body = host;
    if (collapsible) {
      const details = document.createElement("details");
      details.className = "long-section";
      const summary = document.createElement("summary");
      summary.textContent = `展開這一區（${rowCount} 列）`;
      details.appendChild(summary);
      host.appendChild(details);
      body = details;
    }

    section.children.forEach((child) => {
      const target = child.type === "note" && !collapsible ? host : body;
      if (child.type === "note") {
        const note = document.createElement("p");
        note.className = "section-note";
        note.textContent = child.text;
        // 備註留在摺疊外面：使用者要在展開前就知道這一區在講什麼。
        (collapsible ? host : target).insertBefore(note, collapsible ? body : null);
      } else if (child.type === "block") {
        const block = document.createElement("p");
        block.className = "section-block";
        block.textContent = child.text;
        body.appendChild(block);
      } else if (child.type === "table") {
        body.appendChild(materializeTable(child, section.id));
      }
    });
    return host;
  }

  const LONG_SECTION_ROWS = 20;

  function materializeTable(model, sectionId) {
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", model.title || "可橫向捲動的資料表");
    const table = document.createElement("table");
    if (sectionId) table.dataset.section = sectionId;
    if (model.title) {
      const caption = document.createElement("caption");
      caption.textContent = model.title;
      table.appendChild(caption);
    }
    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    model.columns.forEach((column) => {
      const th = document.createElement("th");
      th.scope = "col";
      th.textContent = column;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = document.createElement("tbody");
    model.rows.forEach((row) => {
      const tr = document.createElement("tr");
      row.forEach((cell) => {
        const td = document.createElement("td");
        td.textContent = cell;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  // ══ 匯出 ══════════════════════════════════════════════
  function writeToClipboard(text, okMessage, anchor) {
    const clipboard = navigator.clipboard;
    if (!clipboard || typeof clipboard.writeText !== "function") {
      reportAt(anchor, "這個瀏覽器不允許頁面寫入剪貼簿；請改用下載。", "error");
      return;
    }
    clipboard.writeText(text).then(
      () => reportAt(anchor, okMessage, "info"),
      // 2026-08-06：原本把 error.message 直接放進畫面。實測到的其中一種是
      // "Failed to execute 'writeText' on 'Clipboard': Document is not focused"
      // ——一段英文的平台內部訊息，出現在一個中文介面裡，而且沒有告訴使用者
      // 該怎麼辦。這裡的失敗原因對使用者只有一種意義：這條路走不通，改用下載。
      // 原始訊息不丟掉，但留在 console 給回報用，不佔畫面。
      (error) => {
        if (error) console.warn("clipboard.writeText 失敗", error);
        reportAt(anchor, "瀏覽器這次沒有允許寫入剪貼簿；請改用下載。", "error");
      }
    );
  }

  function copySection(sectionId, anchor) {
    let text;
    try {
      lifecycle.requireCanonicalDocument();          // 清除後這裡就會拋
      const snapshot = sectionSnapshots.get(sectionId);
      if (!snapshot) throw new Error("找不到這個區段的 canonical 快照。");
      text = window.ChartExport.renderSectionText(snapshot);
    } catch (error) {
      reportAt(anchor, `複製失敗：${error.message}`, "error");
      return;
    }
    writeToClipboard(text, "已複製本區。", anchor);
  }

  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", () => {
      let text;
      try {
        const canonical = lifecycle.requireCanonicalDocument();
        text = window.ChartExport.renderPlainText(
          window.ChartExport.projectOutputDocument(
            canonical,
            el("export-detail-mode").value,
          )
        );
      } catch (error) {
        reportAt(button, `複製失敗：${error.message}`, "error");
        return;
      }
      writeToClipboard(text, "已複製全文。", button);
    });
  });

  document.querySelectorAll("[data-download]").forEach((button) => {
    button.addEventListener("click", () => {
      let canonical;
      try {
        canonical = lifecycle.requireCanonicalDocument();
      } catch (error) {
        reportAt(button, `下載失敗：${error.message}`, "error");
        return;
      }
      const outcome = window.ChartExport.runDownloadAction(
        canonical,
        button.dataset.download,
        deliverArtifact,
        el("export-detail-mode").value,
      );
      reportAt(
        button,
        outcome.ok
          ? `已產生 ${button.dataset.download.toUpperCase()} 檔案。`
          : `下載失敗：${outcome.error_message}`,
        outcome.ok ? "info" : "error"
      );
    });
  });

  function deliverArtifact(artifact) {
    const blob = new Blob([artifact.content], { type: artifact.mime_type });
    const url = URL.createObjectURL(blob);
    lifecycle.registerObjectUrl(url);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = artifact.filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    // 同一個 task 內 revoke 會讓部分瀏覽器取消下載；排到下一個 task。
    // release 是冪等的，因此使用者若先按清除也不會重複撤銷。
    setTimeout(() => lifecycle.releaseObjectUrl(url), 0);
  }

  el("print-button").addEventListener("click", () => {
    try { lifecycle.requireCanonicalDocument(); }
    catch (error) { setStatus(`無法列印：${error.message}`, "error"); return; }
    window.print();
  });

  // ══ 兩種清除（匯出契約驗收 16）══════════════════════════
  function dropResultsForNewAttempt() {
    lifecycle.clear();
    sectionSnapshots.clear();
    sectionsHost.replaceChildren();
    warningsHost.replaceChildren();
    versionsHost.textContent = "";
    results.hidden = true;
    hideError();
  }

  function dropResults() {
    dropResultsForNewAttempt();
    submitButton.disabled = false;
  }

  // 送出中唯一可達的中止入口。只停這一次請求，不動已算出的結果——
  // 那是「清除」的語意，不是「中止」的。
  cancelButton.addEventListener("click", () => {
    const aborted = lifecycle.abortActiveRequest();
    cancelButton.hidden = true;
    submitButton.disabled = false;
    setStatus(aborted ? "已中止這次計算。表單內容保留。" : "目前沒有進行中的計算。", "info");
  });

  el("clear-results").addEventListener("click", () => {
    dropResults();
    setStatus("已清除計算結果。表單保留，可以直接改一個欄位再算一次。", "info");
  });

  el("clear-sensitive").addEventListener("click", () => {
    dropResults();
    form.reset();
    optionValues = Catalogue.defaults();
    selectedPlace = null;
    el("place-receipt").hidden = true;
    el("place-results").replaceChildren();
    el("place-results").hidden = true;
    placeQuery.value = "";
    secondInput.value = "";
    placeStatus.textContent = "";
    applyPrecisionConsequences();
    applyZodiacConsequences();
    refreshOptionUi();
    // 模糊時刻的選擇區是從已清空的輸入推導出來的，必須跟著重算——否則表單
    // 空了，畫面卻還留著「這個時間出現了兩次」，等於洩漏上一筆輸入的性質。
    refreshFoldChoice();
    // URL 不應留下任何輸入痕跡。
    if (window.location.search || window.location.hash) {
      window.history.replaceState(null, "", window.location.pathname);
    }
    setStatus("已清除本頁的輸入與結果。已複製到剪貼簿或已下載的檔案無法由本頁收回。", "info");
  });

  // 離開頁面時盡力清除；這不是記憶體安全抹除，也不控制瀏覽器當機還原。
  window.addEventListener("pagehide", () => { try { lifecycle.clear(); } catch (_) {} });

  // ══ 起始狀態 ══════════════════════════════════════════
  buildOptionUi();
  applyPrecisionConsequences();
  applyZodiacConsequences();

})();
