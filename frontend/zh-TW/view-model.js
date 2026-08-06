(function attachViewModel(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ChartViewModel = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function buildViewModel() {
  "use strict";

  // 本檔是純函式：不碰 DOM、不發 request、不讀全域狀態。
  //
  // 契約 §10 要求的資料流是
  //   backend response -> canonical export document -> sections -> {render, exporters}
  // 渲染與匯出必須是 sections 的兩個平行下游，而不是渲染完再回頭刮畫面。
  // 把「回應 -> sections」與「sections -> 可渲染樹」都放在這裡，
  // 是為了讓這兩步能在 Node 測試裡直接斷言，不必先有瀏覽器。

  const RING = Object.freeze({
    ASTRONOMICAL: { key: "ring-1", label: "天文事實", ordinal: "RING I" },
    GEOMETRY: { key: "ring-2", label: "幾何推導", ordinal: "RING II" },
    METHOD: { key: "ring-3", label: "方法判斷", ordinal: "RING III" },
    VERIFICATION: { key: "ring-v", label: "驗證", ordinal: "VERIFICATION" },
  });

  /**
   * 回應側覆蓋宣告：後端能回的每一個模組，這裡都要有交代。
   *
   * 2026-08-05：一版 /calculate 上線時有 14 個模組完全沒有承接——使用者勾了
   * 「恆星」，後端算了 34 顆，畫面一顆都不顯示，而 33 個測試全綠。原因是
   * view-model 是照一份 `options:{}` 的樣本回應寫的，而那份樣本裡多數模組是空的。
   *
   * 這份宣告與 `frontend/tests/fixtures/chart-all-modules.json`（全部模組開啟的
   * 真實回應）做 exact-set 比對。後端新增模組而這裡沒交代，測試就紅。
   * `section` 表示由哪個 section 承接；`not_rendered` 必須寫出理由，
   * 不接受空白——「忘了做」與「刻意不顯示」必須在這裡就分得開。
   */
  const MODULE_COVERAGE = Object.freeze({
    "astronomical_data.time": { section: "time" },
    "astronomical_data.atmosphere": { section: "time" },
    "astronomical_data.bodies": { section: "bodies" },
    "astronomical_data.nodes": { section: "nodes" },
    "astronomical_data.fixed_stars": { section: "fixed_stars" },
    "astronomical_data.fixed_star_policy": { section: "fixed_stars" },
    "astronomical_data.lunar_apsides": { section: "lunar_apsides" },
    "astronomical_data.parallax_moon": { section: "parallax_moon" },
    "astronomical_data.angles": { section: "angles" },
    "astronomical_data.extra_angles": { section: "extra_angles" },
    "astronomical_data.lunar_events": { section: "lunar_events" },
    "astronomical_data.horizon_events": { section: "horizon_events" },
    "derived_geometry.antiscia": { section: "antiscia" },
    "derived_methods.house_division": { section: "houses" },
    "derived_methods.planet_in_house": { section: "planet_in_house" },
    "derived_methods.sect": { section: "sect" },
    "derived_methods.lots": { section: "lots" },
    "derived_methods.void_of_course": { section: "void_of_course" },
    "derived_methods.declination_aspects": { section: "declination_aspects" },
    "derived_methods.aspects": { section: "aspects" },
    "derived_methods.essential_dignities": { section: "dignities" },

    // 頂層鍵。2026-08-05 第二次擴充：先前這份宣告只涵蓋三個資料層，
    // 而 calculation_trace 是頂層鍵，於是 98 步的逐步軌跡從來沒有被閘門看過，
    // 畫面上也只印了一個步數。範圍是我自己畫的，不是回應的實際範圍——
    // 同一個錯誤換一層又犯一次，所以這裡改成涵蓋回應的每一個頂層鍵。
    "schema_version": { section: "receipt" },
    "output_contract": { section: "contract" },
    "requested_options": { section: "requested_options" },
    "library_info": { section: "receipt" },
    "computation_mode": { section: "receipt" },
    "calculation_dossier": { section: "receipt" },
    "astronomical_data": { not_rendered: "層容器本身；其下每個模組各自宣告。" },
    "derived_geometry": { not_rendered: "層容器本身；其下每個模組各自宣告。" },
    "derived_methods": { not_rendered: "層容器本身；其下每個模組各自宣告。" },
    "birth_time_sensitivity": { section: "sensitivity" },
    "calculation_trace": { section: "trace" },
  });

  const SIGNS = Object.freeze([
    "牡羊", "金牛", "雙子", "巨蟹", "獅子", "處女",
    "天秤", "天蠍", "射手", "摩羯", "水瓶", "雙魚",
  ]);

  const DASH = "—";

  /** 後端在尊貴模組回英文星座鍵；其餘模組回黃經。統一成同一套中文名。 */
  const SIGN_BY_KEY = Object.freeze({
    aries: "牡羊", taurus: "金牛", gemini: "雙子", cancer: "巨蟹",
    leo: "獅子", virgo: "處女", libra: "天秤", scorpio: "天蠍",
    sagittarius: "射手", capricorn: "摩羯", aquarius: "水瓶", pisces: "雙魚",
  });

  function signName(key) {
    if (!key) return DASH;
    return SIGN_BY_KEY[String(key).toLowerCase()] || String(key);
  }

  function isRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  /** 模組是否根本沒有內容：null、缺席或空物件都算。 */
  function isEmptyModule(module) {
    if (module === null || module === undefined) return true;
    if (isRecord(module) && Object.keys(module).length === 0) return true;
    return false;
  }

  function num(value, digits) {
    if (typeof value !== "number" || !Number.isFinite(value)) return DASH;
    return value.toFixed(digits);
  }

  /* ── 結構化收據欄位的攤平 ─────────────────────────────────
     後端有幾個欄位是物件或物件陣列，不是純量。把它們直接放進表格
     儲存格會被字串化成 [object Object]——不會報錯，只是讀不到。
     2026-08-06 實測發現四處這樣的儲存格，其中一處是隱私未涵蓋層，
     那是 L1 宣稱。所以攤平必須具名，而不是丟一個泛用的 JSON.stringify。 */

  /** 時區資料庫：版本 ＋ 來源；不可用時說出原因代碼。 */
  function tzDatabase(value) {
    if (!isRecord(value)) return value === undefined || value === null ? DASH : String(value);
    if (value.available === false) {
      return `不可用（${value.reason_code || "未提供原因代碼"}）`;
    }
    const version = value.version || DASH;
    return value.source ? `${version}（${value.source}）` : String(version);
  }

  /** 星曆譜系：狀態、JPL 基礎與適用檔案。 */
  function ephemerisLineage(value) {
    if (!isRecord(value)) return value === undefined || value === null ? DASH : String(value);
    const parts = [];
    if (value.jpl_ephemeris_basis) parts.push(`JPL ${value.jpl_ephemeris_basis}`);
    if (value.representation) parts.push(value.representation);
    if (Array.isArray(value.applies_to) && value.applies_to.length) {
      parts.push(value.applies_to.join("、"));
    }
    if (value.status) parts.push(`狀態：${value.status}`);
    return parts.length ? parts.join("；") : DASH;
  }

  /** 旗標政策：每組旗標的名稱與數值，名稱在前因為數值不可讀。 */
  function flagPolicy(value) {
    if (!isRecord(value)) return value === undefined || value === null ? DASH : String(value);
    const parts = [];
    for (const [group, entry] of Object.entries(value)) {
      if (!isRecord(entry)) {
        parts.push(`${group}：${entry}`);
        continue;
      }
      const names = Array.isArray(entry.names) ? entry.names.join("＋") : DASH;
      parts.push(`${group}：${names}（${entry.value}）`);
    }
    return parts.length ? parts.join("；") : DASH;
  }

  /** 未涵蓋層：層名 ＋ 狀態；備註另行成列，不擠進同一格。 */
  function uncoveredLayers(value) {
    if (!Array.isArray(value) || !value.length) return DASH;
    return value
      .map((entry) => {
        if (!isRecord(entry)) return String(entry);
        return entry.status ? `${entry.layer}（${entry.status}）` : String(entry.layer);
      })
      .join("、");
  }

  /**
   * 座標政策（月球拱點）：軌道參考 ＋ 站心視差是否套用 ＋ 理由。
   *
   * 後端回的是物件，不是字串。理由那一段必須留著：讀者看到「未套用站心視差」
   * 的第一個反應是「那是不是漏做了」，而答案是 Swiss Ephemeris 對這類軌道點
   * 根本不區分站心與地心——那是不能做，不是沒做。
   */
  function coordinatePolicy(value) {
    if (!isRecord(value)) return value === undefined || value === null ? DASH : String(value);
    const parts = [];
    if (value.orbital_reference) parts.push(`軌道參考 ${value.orbital_reference}`);
    if (value.topocentric_parallax_applied === false) {
      parts.push("未套用站心視差");
    } else if (value.topocentric_parallax_applied === true) {
      parts.push("已套用站心視差");
    }
    if (value.reason) parts.push(`理由：${value.reason}`);
    return parts.length ? parts.join("；") : DASH;
  }

  /**
   * 範圍（反照點）：納入與排除的類別、語意與裁決。
   *
   * included_keys 刻意不列——同一份資料下面的表格已經逐列列出那些天體，
   * 在註記裡再抄一次只會把真正需要讀的「語意」推到看不見的地方。
   */
  function scopeSummary(value) {
    if (!isRecord(value)) return value === undefined || value === null ? DASH : String(value);
    const parts = [];
    if (Array.isArray(value.included) && value.included.length) {
      parts.push(`納入 ${value.included.join("、")}`);
    }
    if (Array.isArray(value.excluded) && value.excluded.length) {
      parts.push(`排除 ${value.excluded.join("、")}`);
    }
    if (value.semantics) parts.push(`語意：${value.semantics}`);
    if (value.ruling) parts.push(`裁決：${value.ruling}`);
    return parts.length ? parts.join("；") : DASH;
  }

  /**
   * 地平事件契約：後端回一份巢狀物件，先前整包被當成一條註記推進去，
   * 渲染成 `[object Object]`。
   *
   * 攤平成數條而非一條：這裡面有四件互相獨立、而且都會被質疑的事——
   * 座標一律站心（不跟隨整體計算口徑）、位置一律視位置且折射已套用、
   * 圓面取上緣、中天含上下中天。擠成一行等於沒說。
   * frame.reason 說明「為什麼升起必然含折射」，那是最容易被誤判為錯誤的一項，
   * 必須留全文。
   */
  function horizonContractNotes(value) {
    if (!isRecord(value)) return value ? [String(value)] : [];
    const frame = isRecord(value.frame) ? value.frame : {};
    const notes = [];

    const geometry = [];
    if (value.coordinate_origin) geometry.push(`座標原點 ${value.coordinate_origin}`);
    if (frame.position_mode) geometry.push(`位置口徑 ${frame.position_mode}`);
    if (frame.follows_computation_mode === false) {
      geometry.push("不跟隨整體計算口徑");
    }
    if (value.disc_position) geometry.push(`圓面取 ${value.disc_position}`);
    if (value.transit_definition) geometry.push(`中天定義 ${value.transit_definition}`);
    if (geometry.length) notes.push(`${geometry.join("；")}。`);

    const refraction = [];
    if (value.refraction) refraction.push(`折射 ${value.refraction}`);
    if (typeof value.temperature_c === "number") {
      refraction.push(`溫度 ${value.temperature_c} °C`);
    }
    refraction.push(
      value.pressure_hpa === null || value.pressure_hpa === undefined
        ? `氣壓 ${value.pressure_mode || DASH}`
        : `氣壓 ${value.pressure_hpa} hPa`
    );
    if (refraction.length) notes.push(`${refraction.join("；")}。`);
    if (frame.reason) notes.push(frame.reason);

    const provenance = [];
    if (value.reference_time) provenance.push(`參考時刻 ${value.reference_time}`);
    if (Array.isArray(value.directions) && value.directions.length) {
      provenance.push(`方向 ${value.directions.join("、")}`);
    }
    if (isRecord(value.ephemeris_source)
      && value.ephemeris_source.actual_source_verified === false) {
      provenance.push(
        `星曆來源未經回傳確認（${value.ephemeris_source.evidence || "未提供證據欄位"}）`
      );
    }
    if (frame.ruling) provenance.push(`裁決：${frame.ruling}`);
    if (frame.finding) provenance.push(`紅隊發現：${frame.finding}`);
    if (provenance.length) notes.push(`${provenance.join("；")}。`);

    return notes;
  }

  // 輸出精度是百分之一角秒；一度 = 3600 秒 × 100。
  const TICKS_PER_DEGREE = 360000;
  const TICKS_PER_MINUTE = 6000;

  /** 度轉度分秒。保留正負號，供赤緯與黃緯使用。 */
  function dms(value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return DASH;
    const sign = value < 0 ? "-" : "";
    // 先把整個值量化到輸出精度，再切分度／分／秒。反過來做——先用 floor 切分、
    // 最後才對秒 toFixed(2)——會讓 59.996″ 印成 60.00″ 而分位不跟著進位，產出
    // `29°59′60.00″` 這種不存在的六十進位值。黃經是連續量，落在每度最後五毫弧秒
    // 的輸入是可達的，且同一個錯值會經 canonical sections 進入畫面與四種匯出。
    const ticks = Math.round(Math.abs(value) * TICKS_PER_DEGREE);
    const d = Math.floor(ticks / TICKS_PER_DEGREE);
    const m = Math.floor((ticks % TICKS_PER_DEGREE) / TICKS_PER_MINUTE);
    const s = (ticks % TICKS_PER_MINUTE) / 100;
    return `${sign}${d}°${String(m).padStart(2, "0")}′${s.toFixed(2).padStart(5, "0")}″`;
  }

  /**
   * 由後端給的黃經取星座與宮內度數。
   *
   * 這不是第二次計算：它把同一個數字換一種寫法，30° 一段的分割在回歸與恆星黃道下
   * 都成立，因為兩者用的是同一個經度慣例，差別在原點，而原點已經反映在數值裡了。
   */
  function signPosition(longitude) {
    if (typeof longitude !== "number" || !Number.isFinite(longitude)) return DASH;
    // 量化必須發生在分段之前。先分段再交給 dms()，359.9999999° 會先被歸進雙魚座，
    // 然後段內度數進位成 30°00′00.00″——雙魚座沒有第 30 度。星座歸屬本身就是
    // 這個產品會對使用者示警的邊界，顯示層不能在那裡自相矛盾。
    const ticks =
      Math.round((((longitude % 360) + 360) % 360) * TICKS_PER_DEGREE)
      % (360 * TICKS_PER_DEGREE);
    const signTicks = 30 * TICKS_PER_DEGREE;
    const index = Math.floor(ticks / signTicks) % 12;
    return `${SIGNS[index]} ${dms((ticks - index * signTicks) / TICKS_PER_DEGREE)}`;
  }

  /**
   * 把後端收據翻成 section 狀態。
   *
   * 四個布林的組合語義各不相同，這個函式是唯一決定它們如何映射到畫面狀態的地方。
   * 特別是「requested=true 但 applicable=false」必須成為 refused 而不是 not_requested——
   * 前者是產品明確拒絕並附理由，後者只是使用者沒勾。
   */
  function receiptStatus(receipt) {
    if (!isRecord(receipt)) return { state: "not_requested" };
    const requested = receipt.requested === true;
    if (!requested) return { state: "not_requested" };
    if (receipt.applicable === false) {
      return { state: "refused", reason_code: receipt.reason_code || "" };
    }
    if (receipt.executed === true && receipt.available === false) {
      return {
        state: "executed_unavailable",
        reason_code: receipt.reason_code || "",
      };
    }
    if (receipt.defaulted === true && receipt.requested_explicitly === false) {
      return { state: "defaulted" };
    }
    return { state: "present" };
  }

  /** 未請求的模組仍然要出現在畫面上，帶著「未請求」而不是被靜默省略。 */
  function absentSection(id, title, ring, note) {
    return {
      id,
      title,
      layer_label: `${ring.ordinal} ${ring.label}`,
      status: { state: "not_requested" },
      notes: [note],
      tables: [],
      blocks: [],
    };
  }

  function timeSection(response) {
    const time = (response.astronomical_data || {}).time || {};
    const anchored = time.input_semantics
      && time.input_semantics !== "exact_birth_time";
    const notes = [
      "所有時刻均標明時間尺度；UTC 為換算結果，不是輸入。",
    ];
    if (anchored) {
      notes.unshift(
        "這一欄的本地時間是計算用的代表性錨點，不是出生時刻。"
        + `後端語義：${time.input_semantics}。`
      );
    }
    return {
      id: "time",
      title: "時間轉換",
      layer_label: `${RING.ASTRONOMICAL.ordinal} ${RING.ASTRONOMICAL.label}`,
      status: { state: "present" },
      notes,
      tables: [{
        title: "時間",
        columns: ["項目", "數值"],
        rows: [
          [anchored ? "代表性錨點（非出生時刻）" : "本地時間", time.input_local_time],
          ["時區", time.timezone_label],
          ["UTC 偏移（小時）", num(time.utc_offset_hours, 2)],
          ["UTC", time.utc_time],
          ["儒略日 JD(UT)", num(time.jd_ut, 6)],
          ["ΔT（秒）", num(time.delta_t_seconds, 3)],
          ["地方真恆星時 LAST（小時）", num(time.last_hours, 6)],
          ["真黃赤交角 ε", dms(time.true_obliquity)],
        ],
      }],
      blocks: [],
    };
  }

  function bodiesSection(response) {
    const bodies = (response.astronomical_data || {}).bodies || [];
    return {
      id: "bodies",
      title: "天體位置",
      layer_label: `${RING.ASTRONOMICAL.ordinal} ${RING.ASTRONOMICAL.label}`,
      status: { state: bodies.length ? "present" : "executed_unavailable" },
      notes: [
        "黃道、赤道、地平三套座標一次輸出，不必換工具重填生辰。",
        "星座與宮內度數由後端黃經以 30° 分段換寫，非另一次計算；完整精度在 JSON 匯出中。",
      ],
      tables: [{
        title: "本命天體",
        columns: BODY_COLUMNS,
        rows: bodyRows(bodies),
      }],
      blocks: [],
    };
  }

  function anglesSection(response) {
    const angles = (response.astronomical_data || {}).angles || {};
    const rows = [
      ["上升 ASC", angles.asc],
      ["中天 MC", angles.mc],
      ["下降 DSC", angles.desc],
      ["天底 IC", angles.ic],
    ].map(([label, value]) => [label, num(value, 6), signPosition(value)]);
    return {
      id: "angles",
      title: "軸點",
      layer_label: `${RING.ASTRONOMICAL.ordinal} ${RING.ASTRONOMICAL.label}`,
      status: { state: "present" },
      notes: ["軸點由地方恆星時與觀測地緯度決定，與宮位制的選擇無關。"],
      tables: [{
        title: "四軸",
        columns: ["軸點", "黃經", "星座度數"],
        rows,
      }],
      blocks: [],
    };
  }

  function antisciaSection(response) {
    const antiscia = (response.derived_geometry || {}).antiscia;
    if (isEmptyModule(antiscia)) {
      return absentSection(
        "antiscia",
        "反照點 Antiscia",
        RING.GEOMETRY,
        "本次未請求。這是純幾何鏡射，不需要任何技法選擇；勾選後公式會直接印在數值上方。"
      );
    }
    const direct = antiscia.antiscia || [];
    const contra = antiscia.contra_antiscia || [];
    const byKey = new Map();
    direct.forEach((p) => byKey.set(p.key, { name: p.name || p.key, antiscion: p.longitude }));
    contra.forEach((p) => {
      const entry = byKey.get(p.key) || { name: p.name || p.key };
      entry.contra = p.longitude;
      byKey.set(p.key, entry);
    });
    return {
      id: "antiscia",
      title: "反照點 Antiscia",
      layer_label: `${RING.GEOMETRY.ordinal} ${RING.GEOMETRY.label}`,
      status: { state: "present" },
      notes: [
        "λ′ = 180° − λ（以 0° 巨蟹—0° 摩羯 為鏡軸）。",
        `範圍：${scopeSummary(antiscia.scope)}。`,
      ],
      tables: [{
        title: "反照點與反向反照點",
        columns: ["天體", "反照點 λ′", "星座度數", "反向反照點", "星座度數"],
        rows: [...byKey.values()].map((e) => [
          e.name,
          num(e.antiscion, 6), signPosition(e.antiscion),
          num(e.contra, 6), signPosition(e.contra),
        ]),
      }],
      blocks: [],
    };
  }

  function housesSection(response) {
    const houses = (response.derived_methods || {}).house_division;
    if (isEmptyModule(houses)) {
      return absentSection(
        "houses", "宮位分割", RING.METHOD, "本次未請求宮位。"
      );
    }
    const cusps = houses.cusps || [];
    return {
      id: "houses",
      title: "宮位分割",
      layer_label: `${RING.METHOD.ordinal} ${RING.METHOD.label}`,
      status: {
        state: houses.execution_status === "computed" ? "present" : "executed_unavailable",
      },
      notes: [
        `方法：${houses.method || DASH}（${houses.system_name || houses.system_code || DASH}）。`,
        `方法審閱狀態：${houses.method_status || DASH}；權威：${houses.method_authority || "未確立"}。`,
      ],
      tables: [{
        title: "宮始點",
        columns: ["宮", "黃經", "星座度數"],
        rows: cusps.map((cusp, index) => [
          `第 ${index + 1} 宮`,
          num(typeof cusp === "number" ? cusp : cusp && cusp.longitude, 6),
          signPosition(typeof cusp === "number" ? cusp : cusp && cusp.longitude),
        ]),
      }],
      blocks: [],
    };
  }

  function aspectsSection(response) {
    const aspects = (response.derived_methods || {}).aspects;
    if (isEmptyModule(aspects)) {
      return absentSection("aspects", "相位", RING.METHOD, "本次未請求相位。");
    }
    const degree = aspects.degree_based || {};
    const orbReceipt = degree.orb_receipt || {};
    const orbUnavailable = degree.orb_verdict_available === false;
    const notes = [
      `方法：${aspects.method || DASH}；審閱狀態：${aspects.method_status || DASH}。`,
    ];
    if (orbUnavailable) {
      notes.push(
        "目前沒有選定容許度（orb）表，因此每一組相位只有幾何角距離，"
        + "沒有「是否在容許度內」的判定。"
        + `後端原因代碼：${degree.orb_unavailable_reason_code || DASH}。`
      );
    }
    // pairs 在 aspects 的**頂層**，不在 degree_based 底下。
    // 先前寫成 `degree.pairs || degree.aspects || []`，兩個鍵都不存在，
    // 於是永遠回空陣列且永遠不報錯——逐度相位表因此一直是空的。
    const pairs = aspects.pairs || [];
    if (!Array.isArray(pairs)) {
      throw new Error("aspects.pairs 不是陣列；相位表無法安全呈現。");
    }
    const tables = [];
    if (pairs.length) {
      tables.push({
        title: "逐度相位",
        columns: ["組合", "整宮配置", "最近相位", "角距離", "是否在容許度內"],
        rows: pairs.map((pair) => {
          const ws = pair.whole_sign || {};
          const near = pair.nearest_aspect || {};
          return [
            `${pair.body_a_name || pair.body_a || DASH} — ${pair.body_b_name || pair.body_b || DASH}`,
            ws.in_aspect ? (ws.configuration_zh || ws.configuration_key || DASH) : "無",
            near.zh || near.key || DASH,
            num(pair.separation_degrees, 4),
            pair.in_orb === null || pair.in_orb === undefined
              ? "未套用容許度"
              : (pair.in_orb ? "是" : "否"),
          ];
        }),
      });
    }
    return {
      id: "aspects",
      title: "相位",
      layer_label: `${RING.METHOD.ordinal} ${RING.METHOD.label}`,
      // orb 未選定不是「相位模組被拒絕」；相位算出來了，少的是一層判定。
      status: orbReceipt.requested === false && orbUnavailable
        ? { state: "present" }
        : receiptStatus(orbReceipt),
      notes,
      tables,
      blocks: [],
    };
  }

  function dignitiesSection(response) {
    const dignities = (response.derived_methods || {}).essential_dignities;
    if (isEmptyModule(dignities)) {
      return absentSection(
        "dignities", "必然尊貴", RING.METHOD, "本次未請求必然尊貴。"
      );
    }
    const status = receiptStatus(dignities);
    const notes = [];
    if (dignities.defaulted === true && dignities.requested_explicitly === false) {
      notes.push("這個模組是產品預設帶入的，不是你勾選的；可以明示關閉。");
    }
    if (status.state === "refused") {
      notes.push(
        "本次計算的口徑下，產品拒絕輸出必然尊貴，而不是算不出來。"
        + `後端原因代碼：${status.reason_code || DASH}。`
      );
    }
    notes.push(
      "本模組只評估廟與旺（domicile／exaltation）。"
      + "陷、落、外來與互容尚未評估——那是「未評估」，不是「沒有」。"
    );
    const selected = dignities.selected_profiles || {};
    const tables = [{
      title: "採用的具名 profile",
      columns: ["技法", "profile", "狀態"],
      rows: [
        ["廟旺 domicile／exaltation", selected.domicile_exaltation, null],
        ["界 bounds", selected.bounds, null],
        ["面／旬 face／decan", selected.face_decan, null],
        ["三分性 triplicity", selected.triplicity, null],
      ].map(([technique, profile]) => [
        technique,
        profile || DASH,
        profile ? "已選定" : "未選定",
      ]),
    }];
    // 每一套已選定的 profile 都要列出實際判定，不能只列名稱。
    // 各技法另外拆成獨立 section（見 dignityProfileSections），這裡只留選定清單。
    [].forEach((profile) => {
      const objects = profile.objects || [];
      if (!objects.length) return;
      tables.push({
        title: `${profile.technique || DASH} · ${profile.profile_id || DASH}`,
        columns: ["天體", "星座", "宮內度數", "廟", "旺", "判定"],
        rows: objects.map((object) => [
          object.name || object.key,
          signName(object.sign),
          num(object.degree_in_sign, 4),
          (object.domicile_signs || []).map(signName).join("、") || DASH,
          signName(object.exaltation_sign),
          object.domicile_matched ? "入廟"
            : (object.exaltation_matched ? "入旺" : "無廟旺"),
        ]),
      });
      if (profile.debility_evaluated === false) {
        notes.push(
          `${profile.profile_id}：陷、落、外來與互容標記為未評估`
          + `（${(profile.not_evaluated || []).join("、") || "not_evaluated"}）——`
          + "那是「未評估」，不是「沒有」。"
        );
      }
    });
    const comparison = dignities.research_comparison_profiles || [];
    if (comparison.length) {
      notes.push(
        `另有 ${comparison.length} 套並列比較用的 profile；並列不會改變你選定的那一套。`
      );
    }

    return {
      id: "dignities",
      title: "必然尊貴",
      layer_label: `${RING.METHOD.ordinal} ${RING.METHOD.label}`,
      status,
      notes,
      tables,
      blocks: [
        `評估基礎：${dignities.zodiac_basis || DASH} / `
        + `${dignities.coordinate_center || DASH} / ${dignities.ecliptic_frame || DASH}`,
      ],
    };
  }

  // ── 以下區塊補於 2026-08-05：先前這些模組後端算了但畫面完全沒有承接 ──

  /** 共用：把一組天體列成三套座標的表。 */
  function bodyRows(list) {
    return (list || []).map((b) => [
      b.name || b.key,
      num(b.longitude, 6),
      signPosition(b.longitude),
      dms(b.latitude),
      num(b.right_ascension, 6),
      dms(b.declination),
      num(b.azimuth, 3),
      num(b.altitude_true, 3),
      num(b.altitude_apparent, 3),
      // 三態，不是二態。heliocentric/barycentric 下後端對太陽與月交點回傳
      // motion_sign: null，因為那些點在該中心沒有物理意義。把 null 摺進「順行」
      // 會在同一列印出「黃經 —、行進 順行」——一句沒有依據的天文陳述，而且會經
      // canonical sections 進入四種匯出。
      // "zero" 維持既有的「順行」顯示：速度為零是否構成「停滯」屬方法裁決，
      // 後端 core/bodies.py 明示不做該判斷，這裡也不代它做。
      b.motion_sign === "negative"
        ? "逆行"
        : b.motion_sign === "positive" || b.motion_sign === "zero"
          ? "順行"
          : DASH,
    ]);
  }
  const BODY_COLUMNS = [
    "天體", "黃經 λ", "星座度數", "黃緯 β", "赤經 α", "赤緯 δ",
    "方位角 Az", "真高度", "視高度", "行進",
  ];

  function methodNotes(module) {
    return [
      `方法：${module.method || DASH}；審閱狀態：${module.method_status || DASH}；`
      + `權威：${module.method_authority || "未確立"}。`,
    ];
  }

  function nodesSection(response) {
    const nodes = (response.astronomical_data || {}).nodes || [];
    if (!nodes.length) {
      return absentSection("nodes", "月交點", RING.ASTRONOMICAL, "本次未請求交點。");
    }
    return {
      id: "nodes", title: "月交點",
      layer_label: `${RING.ASTRONOMICAL.ordinal} ${RING.ASTRONOMICAL.label}`,
      status: { state: "present" },
      notes: ["真交點與平交點何者進入正式技法尚待考據；兩者以名稱區分，不擇一。"],
      tables: [{ title: "交點", columns: BODY_COLUMNS, rows: bodyRows(nodes) }],
      blocks: [],
    };
  }

  function fixedStarsSection(response) {
    const stars = (response.astronomical_data || {}).fixed_stars || [];
    const policy = (response.astronomical_data || {}).fixed_star_policy || {};
    if (!stars.length) {
      return absentSection("fixed_stars", "恆星", RING.ASTRONOMICAL,
        "本次未請求恆星。34 顆全部標為 research_only，勾選才計算。");
    }
    return {
      id: "fixed_stars", title: "恆星",
      layer_label: `${RING.ASTRONOMICAL.ordinal} ${RING.ASTRONOMICAL.label}`,
      status: { state: "present" },
      notes: [
        `分類：${policy.method_classification || DASH}；`
        + `裁決：${policy.classification_ruling || DASH}。`,
        `目錄收錄 ${policy.catalog_size || DASH} 顆，本次輸出 ${stars.length} 顆。`,
      ],
      tables: [{
        title: "恆星位置",
        columns: ["恆星", "目錄名", "黃經 λ", "星座度數", "赤緯 δ"],
        rows: stars.map((star) => [
          star.name || star.key, star.catalog_name || DASH,
          num(star.longitude, 6), signPosition(star.longitude), dms(star.declination),
        ]),
      }],
      blocks: [],
    };
  }

  function lunarApsidesSection(response) {
    const module = (response.astronomical_data || {}).lunar_apsides;
    if (isEmptyModule(module)) {
      return absentSection("lunar_apsides", "Lilith 與 Priapus", RING.ASTRONOMICAL,
        "本次未請求。");
    }
    const status = receiptStatus(module);
    return {
      id: "lunar_apsides", title: "Lilith 與 Priapus",
      layer_label: `${RING.ASTRONOMICAL.ordinal} ${RING.ASTRONOMICAL.label}`,
      status,
      notes: methodNotes(module).concat([
        "三個點皆非物理天體，是月球軌道的幾何點。",
        `座標政策：${coordinatePolicy(module.coordinate_policy)}；`
        + `相位參與：${module.aspect_participation || DASH}。`,
      ]),
      tables: [{ title: "月球拱點", columns: BODY_COLUMNS, rows: bodyRows(module.points) }],
      blocks: [],
    };
  }

  function parallaxMoonSection(response) {
    const module = (response.astronomical_data || {}).parallax_moon;
    if (isEmptyModule(module)) {
      return absentSection("parallax_moon", "站心月亮", RING.ASTRONOMICAL,
        "本次未請求。月亮位置口徑維持跟隨整體計算中心。");
    }
    return {
      id: "parallax_moon", title: "站心月亮",
      layer_label: `${RING.ASTRONOMICAL.ordinal} ${RING.ASTRONOMICAL.label}`,
      status: receiptStatus(module),
      notes: methodNotes(module).concat([
        "畫面上同時出現兩個月亮不是錯誤，是明示的座標政策："
        + "地心參考值保留，站心值另計。",
        `整體中心：${module.global_center || DASH}；`
        + `生效的月亮中心：${module.effective_moon_center || DASH}。`,
      ]),
      tables: [{
        title: "兩個月亮",
        columns: ["來源", "黃經 λ"],
        rows: [
          ["地心參考 geocentric", num((module.geocentric_reference || {}).longitude, 6)],
          ["站心生效 topocentric", num((module.topocentric_effective || {}).longitude, 6)],
          ["差值（度）", num(module.longitude_delta_degrees, 6)],
        ],
      }],
      blocks: [],
    };
  }

  function extraAnglesSection(response) {
    const module = (response.astronomical_data || {}).extra_angles;
    if (isEmptyModule(module)) {
      return absentSection("extra_angles", "額外角點", RING.ASTRONOMICAL, "本次未請求。");
    }
    const angles = module.angles || {};
    return {
      id: "extra_angles", title: "額外角點",
      layer_label: `${RING.ASTRONOMICAL.ordinal} ${RING.ASTRONOMICAL.label}`,
      status: { state: "present" },
      notes: [module.semantics, module.note].filter(Boolean),
      tables: [{
        title: "角點",
        columns: ["角點", "黃經 λ", "星座度數"],
        rows: Object.entries(angles).map(([key, value]) => [
          key, num(value, 6), signPosition(value),
        ]),
      }],
      blocks: [],
    };
  }

  function eventInstant(entry) {
    if (!isRecord(entry)) return DASH;
    const next = entry.next || entry;
    return next.utc_time || DASH;
  }

  function lunarEventsSection(response) {
    const module = (response.astronomical_data || {}).lunar_events;
    if (isEmptyModule(module)) {
      return absentSection("lunar_events", "朔望與日月食", RING.ASTRONOMICAL,
        "本次未請求朔望或日月食。");
    }
    const phases = module.primary_phases || {};
    const rows = Object.entries(phases).flatMap(([key, entry]) => [
      [`${key}（前一次）`, ((entry || {}).previous || {}).utc_time || DASH],
      [`${key}（下一次）`, ((entry || {}).next || {}).utc_time || DASH],
    ]);
    const syzygy = module.prenatal_syzygy || {};
    if (syzygy.utc_time || syzygy.phase) {
      rows.push([`產前朔望（${syzygy.phase || DASH}）`, syzygy.utc_time || DASH]);
    }
    return {
      id: "lunar_events", title: "朔望與日月食",
      layer_label: `${RING.ASTRONOMICAL.ordinal} ${RING.ASTRONOMICAL.label}`,
      status: { state: "present" },
      notes: [module.contract].filter(Boolean),
      tables: [{ title: "朔望時刻（UTC）", columns: ["事件", "時刻"], rows }],
      blocks: [],
    };
  }

  function horizonEventsSection(response) {
    const module = (response.astronomical_data || {}).horizon_events;
    if (isEmptyModule(module)) {
      return absentSection("horizon_events", "升降與中天", RING.ASTRONOMICAL,
        "本次未請求升降與中天。");
    }
    const bodies = module.bodies || [];
    return {
      id: "horizon_events", title: "升降與中天",
      layer_label: `${RING.ASTRONOMICAL.ordinal} ${RING.ASTRONOMICAL.label}`,
      status: { state: "present" },
      notes: horizonContractNotes(module.contract),
      tables: [{
        title: "地平事件（UTC）",
        columns: ["天體", "可見性", "上升", "下降", "上中天"],
        rows: bodies.map((body) => {
          const events = body.events || {};
          return [
            body.name || body.key,
            body.visibility || DASH,
            eventInstant(events.rise),
            eventInstant(events.set),
            eventInstant(events.upper_culmination || events.transit),
          ];
        }),
      }],
      blocks: [],
    };
  }

  function planetInHouseSection(response) {
    const module = (response.derived_methods || {}).planet_in_house;
    if (isEmptyModule(module)) {
      return absentSection("planet_in_house", "行星落宮", RING.METHOD, "本次未請求。");
    }
    const placements = module.placements || [];
    return {
      id: "planet_in_house", title: "行星落宮",
      layer_label: `${RING.METHOD.ordinal} ${RING.METHOD.label}`,
      status: {
        state: module.execution_status === "computed" ? "present" : "executed_unavailable",
      },
      notes: methodNotes(module).concat([
        `宮位制：${module.house_system_name || module.house_system_code || DASH}；`
        + `區間語義：${module.interval_semantics || DASH}。`,
        "距最近宮頭的角距一律列出，讓你自己判斷這個落宮對時間有多敏感。",
      ]),
      tables: [{
        title: "落宮",
        columns: ["天體", "黃經 λ", "宮", "距最近宮頭（度）", "在宮頭上"],
        rows: placements.map((place) => [
          place.name || place.key,
          num(place.longitude, 6),
          place.house === null || place.house === undefined ? DASH : String(place.house),
          num(place.distance_to_nearest_cusp_degrees, 4),
          place.on_cusp === true ? "是" : "否",
        ]),
      }],
      blocks: [],
    };
  }

  function sectSection(response) {
    const module = (response.derived_methods || {}).sect;
    if (isEmptyModule(module)) {
      return absentSection("sect", "日夜盤", RING.METHOD, "本次未計算日夜盤。");
    }
    return {
      id: "sect", title: "日夜盤",
      layer_label: `${RING.METHOD.ordinal} ${RING.METHOD.label}`,
      status: { state: "present" },
      notes: methodNotes(module).concat(
        module.near_critical === true
          ? [`此盤接近臨界（容差 ${num(module.near_critical_tolerance_degrees, 4)}°）。`
             + "臨界時日夜盤會翻轉，並連帶翻轉阿拉伯點的公式。"]
          : []
      ),
      tables: [{
        title: "判定",
        columns: ["項目", "數值"],
        rows: [
          ["判定", module.is_day === true ? "日生盤" : (module.is_day === false ? "夜生盤" : DASH)],
          ["依據", module.method_provenance || module.method || DASH],
          ["所用太陽高度", num(module.sun_altitude_used, 6)],
          ["接近臨界", module.near_critical === true ? "是" : "否"],
        ],
      }],
      blocks: [],
    };
  }

  function lotsSection(response) {
    const module = (response.derived_methods || {}).lots;
    if (isEmptyModule(module)) {
      return absentSection("lots", "阿拉伯點", RING.METHOD, "本次未請求阿拉伯點。");
    }
    return {
      id: "lots", title: "阿拉伯點",
      layer_label: `${RING.METHOD.ordinal} ${RING.METHOD.label}`,
      status: { state: "present" },
      notes: methodNotes(module).concat([
        `公式是否依日夜盤反轉：${module.depends_on_sect === true ? "是" : "否"}。`,
        "目前僅命運點與精神點。",
      ]),
      tables: [{
        title: "點位",
        columns: ["點", "黃經 λ", "星座度數"],
        rows: [
          ["命運點 Fortune", num(module.fortune, 6), signPosition(module.fortune)],
          ["精神點 Spirit", num(module.spirit, 6), signPosition(module.spirit)],
        ],
      }],
      blocks: [],
    };
  }

  function voidOfCourseSection(response) {
    const module = (response.derived_methods || {}).void_of_course;
    if (isEmptyModule(module)) {
      return absentSection("void_of_course", "月空亡", RING.METHOD, "本次未請求月空亡。");
    }
    const next = module.next_completing_aspect || {};
    return {
      id: "void_of_course", title: "月空亡",
      layer_label: `${RING.METHOD.ordinal} ${RING.METHOD.label}`,
      status: { state: "present" },
      notes: methodNotes(module).concat([
        `求解器：${module.solver || DASH}（狀態 ${module.solver_status || DASH}）。`,
        "本模組目前以線性外插求相位完成時刻，那是已知的正確性缺陷："
        + "月亮速度會變、被相位的行星可能站留、站留附近可能出現多重根。"
        + "方向已定為兩段式求根，尚未實作。",
      ]),
      tables: [{
        title: "判定",
        columns: ["項目", "數值"],
        rows: [
          ["是否空亡", module.is_void_of_course === true ? "是"
            : (module.is_void_of_course === false ? "否" : DASH)],
          ["離開本星座尚需（小時）", num(module.time_to_sign_exit_hours, 4)],
          ["下一個完成的相位", next.body || DASH],
          ["該相位角度", num(next.aspect_angle, 2)],
          ["尚需（日）", num(next.time_days, 6)],
        ],
      }],
      blocks: [],
    };
  }

  function declinationAspectsSection(response) {
    const module = (response.derived_methods || {}).declination_aspects;
    if (isEmptyModule(module)) {
      return absentSection("declination_aspects", "赤緯平行與反平行", RING.GEOMETRY,
        "本次未請求。這是近現代技法，非古典傳統。");
    }
    const pairs = module.aspects || [];
    return {
      id: "declination_aspects", title: "赤緯平行與反平行",
      layer_label: `${RING.GEOMETRY.ordinal} ${RING.GEOMETRY.label}`,
      status: { state: "present" },
      notes: methodNotes(module).concat([
        `分類：${module.method_classification || DASH}——`
        + `${module.classification_ruling || DASH}。這是近現代技法，不是古典傳統。`,
        `容許度：${num(module.orb_degrees, 3)}°。`,
      ]),
      tables: [{
        title: "赤緯相位",
        columns: ["類型", "天體 A", "赤緯 A", "天體 B", "赤緯 B", "差值（度）"],
        rows: pairs.map((pair) => [
          pair.type === "contra_parallel" ? "反平行 contra-parallel" : "平行 parallel",
          pair.body_a, dms(pair.declination_a),
          pair.body_b, dms(pair.declination_b),
          num(pair.diff, 4),
        ]),
      }],
      blocks: [],
    };
  }

  const TECHNIQUE_LABELS = Object.freeze({
    domicile_exaltation: "廟與旺 Domicile / Exaltation",
    bounds: "界 Bounds",
    face_decan: "面／旬 Face / Decan",
    triplicity: "三分性 Triplicity",
  });

  /**
   * 每一套尊貴技法自成一個區塊。
   *
   * 先前把六套 profile 的表格全部塞進同一個 section，結果廟旺、界、面、三分性
   * 混在一起分不出段落。技法是使用者心裡的單位，區塊就該照技法切。
   */
  function dignityProfileSections(response) {
    const dignities = (response.derived_methods || {}).essential_dignities;
    if (isEmptyModule(dignities)) return [];
    const selected = dignities.selected_profiles || {};
    const results = dignities.profile_results || {};
    const comparison = new Set(dignities.research_comparison_profiles || []);

    return Object.keys(TECHNIQUE_LABELS).map((technique) => {
      const profileId = selected[technique];
      const sectionId = `dignity_${technique}`;
      if (!profileId) {
        return absentSection(sectionId, TECHNIQUE_LABELS[technique], RING.METHOD,
          "本次未選定這套技法的 profile；未選定不是「沒有這個技法」。");
      }
      const profile = results[profileId] || {};
      const objects = profile.objects || [];
      const notes = [
        `採用 profile：${profileId}`,
        profile.source ? `來源：${profile.source}` : null,
        profile.debility_evaluated === false
          ? "陷、落、外來與互容標記為未評估——那是「未評估」，不是「沒有」。"
          : null,
      ].filter(Boolean);

      const tables = [{
        title: `${TECHNIQUE_LABELS[technique]}　判定`,
        columns: technique === "domicile_exaltation"
          ? ["天體", "星座", "宮內度數", "廟", "旺", "判定"]
          : ["天體", "星座", "宮內度數", "守護", "判定"],
        rows: objects.map((object) => (technique === "domicile_exaltation"
          ? [
            object.name || object.key, signName(object.sign),
            num(object.degree_in_sign, 4),
            (object.domicile_signs || []).map(signName).join("、") || DASH,
            signName(object.exaltation_sign),
            object.domicile_matched ? "入廟"
              : (object.exaltation_matched ? "入旺" : "無廟旺"),
          ]
          : [
            object.name || object.key, signName(object.sign),
            num(object.degree_in_sign, 4),
            object.ruler_name || object.ruler || DASH,
            object.matched === true ? "相合" : (object.matched === false ? "不合" : DASH),
          ])),
      }];

      // 並列比較的 profile 附在同一個技法底下，但明示不影響選定的那一套。
      Object.values(results).forEach((other) => {
        if (other.technique !== technique) return;
        if (other.profile_id === profileId) return;
        if (!comparison.has(other.profile_id)) return;
        tables.push({
          title: `並列比較（不影響選定）：${other.profile_id}`,
          columns: ["天體", "星座", "守護"],
          rows: (other.objects || []).map((object) => [
            object.name || object.key, signName(object.sign),
            object.ruler_name || object.ruler || DASH,
          ]),
        });
      });

      return {
        id: sectionId, title: TECHNIQUE_LABELS[technique],
        layer_label: `${RING.METHOD.ordinal} ${RING.METHOD.label}`,
        status: receiptStatus(dignities),
        notes, tables, blocks: [],
      };
    });
  }

  /**
   * 逐步計算軌跡。
   *
   * 這是產品的頭號賣點——「每一步的函式、輸入、旗標、結果與當時的假設都攤開」——
   * 而先前畫面上只有一個「步數 98」。它是主要內容，不是附錄，因此預設就在頁面上，
   * 不藏在摺疊後面。
   */
  function traceSection(response) {
    const trace = response.calculation_trace || [];
    if (!trace.length) {
      return absentSection("trace", "逐步計算軌跡", RING.VERIFICATION,
        "本次回應沒有軌跡。");
    }
    const receipt = (response.calculation_dossier || {}).trace_receipt || {};
    return {
      id: "trace", title: "逐步計算軌跡",
      layer_label: `${RING.VERIFICATION.ordinal} ${RING.VERIFICATION.label}`,
      status: { state: "present" },
      notes: [
        `本次共 ${trace.length} 步。步數隨你勾選的模組而變，不是固定值。`,
        "整份可複製貼出去逐步對照；每一步都列出算式、輸入與結果。",
        receipt.python_json_serialization_sha256
          ? `序列化雜湊 ${receipt.python_json_serialization_sha256}`
            + "（對 Python 固定參數序列化的結果計算，不跨語言可攜，"
            + "證明的是完整性而非跨實作指紋）"
          : null,
      ].filter(Boolean),
      tables: [{
        title: "軌跡",
        columns: ["#", "步驟", "算式", "輸入", "結果", "備註"],
        rows: trace.map((step, index) => [
          String(index + 1),
          step.title || DASH,
          step.formula || DASH,
          flattenPairs(step.inputs),
          flattenPairs(step.result),
          step.note || DASH,
        ]),
      }],
      blocks: [],
    };
  }

  /** 把 inputs／result 的物件壓成一行可讀文字，數值不改精度。 */
  function flattenPairs(value) {
    if (value === null || value === undefined) return DASH;
    if (!isRecord(value)) return String(value);
    const parts = Object.entries(value).map(([key, item]) => {
      const shown = isRecord(item) || Array.isArray(item)
        ? JSON.stringify(item) : String(item);
      return `${key}=${shown}`;
    });
    return parts.length ? parts.join("；") : DASH;
  }

  /**
   * 本次實際生效的選項。重現靠的是這份回聲，不是使用者記得自己勾了什麼。
   */
  function requestedOptionsSection(response) {
    const requested = response.requested_options || {};
    const keys = Object.keys(requested);
    if (!keys.length) {
      return absentSection("requested_options", "本次生效的選項", RING.VERIFICATION,
        "回應沒有回報生效選項。");
    }
    return {
      id: "requested_options", title: "本次生效的選項",
      layer_label: `${RING.VERIFICATION.ordinal} ${RING.VERIFICATION.label}`,
      status: { state: "present" },
      notes: [
        "這是後端回報的本次實際生效值，含你沒有明示送出、由後端帶入的預設。",
        "拿這份表就能重現同一次計算，不必記得自己勾了什麼。",
      ],
      tables: [{
        title: "生效選項",
        columns: ["選項", "生效值"],
        rows: keys.sort().map((key) => [
          key,
          requested[key] === null ? "null（未選定）" : String(requested[key]),
        ]),
      }],
      blocks: [],
    };
  }

  /** 輸出契約：三層的定義與其 provisional 狀態。 */
  function contractSection(response) {
    const contract = response.output_contract || {};
    const layers = contract.layers || {};
    return {
      id: "contract", title: "輸出契約",
      layer_label: `${RING.VERIFICATION.ordinal} ${RING.VERIFICATION.label}`,
      status: { state: "present" },
      notes: [
        `契約狀態：${contract.status || DASH}——`
        + "provisional 表示欄位仍可能變動，不是已凍結的公開 API。",
        `相容性政策：${contract.compatibility || DASH}`,
      ],
      tables: [{
        title: "三層的定義（後端原文）",
        columns: ["層", "定義"],
        rows: Object.entries(layers).map(([key, text_]) => [key, text_]),
      }],
      blocks: [],
    };
  }

  function sensitivitySection(response) {
    const sensitivity = response.birth_time_sensitivity || {};
    if (sensitivity.precision === "exact") {
      return {
        id: "sensitivity",
        title: "出生時刻敏感度",
        layer_label: `${RING.VERIFICATION.ordinal} ${RING.VERIFICATION.label}`,
        status: { state: "not_requested" },
        notes: [
          "出生時刻標記為精確到分鐘，因此沒有進行區間取樣。"
          + "若你其實只記得大約時辰，改選較寬的把握度，這裡會列出哪些判定會跟著改變。",
        ],
        tables: [],
        blocks: [],
      };
    }
    const ranges = sensitivity.position_ranges || [];
    const notEvaluated = sensitivity.not_evaluated_paths || [];
    const notes = [
      `取樣區間：${sensitivity.interval_start_local || DASH} 至 `
      + `${sensitivity.interval_end_exclusive_local || DASH}（不含結尾）。`,
      `代表性時刻：${sensitivity.representative_local_time || DASH}——`
      + `後端語義為 ${sensitivity.representative_semantics || DASH}，這不是出生時刻。`,
    ];
    (sensitivity.limitations || []).forEach((limitation) => notes.push(limitation));
    if (notEvaluated.length) {
      notes.push(`未在此區間評估的路徑共 ${notEvaluated.length} 條，逐條列於下表。`);
    }
    const tables = [{
      title: "位置範圍",
      columns: ["天體", "代表性黃經", "可能落入的星座數", "取樣狀態"],
      rows: ranges.map((range) => [
        range.name || range.key,
        num(range.representative_longitude, 6),
        String((range.possible_sign_indices || []).length),
        range.status || DASH,
      ]),
    }];
    if (notEvaluated.length) {
      tables.push({
        title: "未評估路徑",
        columns: ["路徑"],
        rows: notEvaluated.map((path) => [path]),
      });
    }
    return {
      id: "sensitivity",
      title: "出生時刻敏感度",
      layer_label: `${RING.VERIFICATION.ordinal} ${RING.VERIFICATION.label}`,
      status: { state: "present" },
      notes,
      tables,
      blocks: [],
    };
  }

  function receiptSection(response) {
    const dossier = response.calculation_dossier || {};
    const engine = dossier.engine || {};
    const privacy = dossier.privacy || {};
    const receipt = dossier.input_receipt || {};
    const place = dossier.location_resolution || {};
    const policy = dossier.calculation_policy || {};
    const traceReceipt = dossier.trace_receipt || {};
    const time = (response.astronomical_data || {}).time || {};
    const trace = response.calculation_trace || [];
    return {
      id: "receipt",
      title: "計算收據",
      layer_label: `${RING.VERIFICATION.ordinal} ${RING.VERIFICATION.label}`,
      status: { state: "present" },
      notes: [
        "這是本次計算的可重現證據，不是占星解讀，也不是任何外部天文、方法、"
        + "資安或法規認證。",
        `隱私聲明狀態為後端原值 ${privacy.attestation_status || DASH}——`
        + "它不等於已通過外部審查。",
      ],
      tables: [
        {
          title: "版本與來源",
          columns: ["項目", "數值"],
          rows: [
            ["API schema", response.schema_version],
            ["Calculation Dossier", dossier.dossier_version],
            ["Dossier 狀態", dossier.status],
            ["Dossier 權威", dossier.authority],
            ["建置識別", (dossier.build_identity || {}).revision
              || (dossier.build_identity || {}).identity],
            ["pyswisseph", engine.pyswisseph_distribution_version],
            ["Swiss Ephemeris", engine.swiss_ephemeris_library_version],
            ["時區資料庫", tzDatabase(engine.tz_database)],
            ["星曆來源", engine.requested_ephemeris_source],
            ["星曆譜系", ephemerisLineage(engine.ephemeris_dataset_lineage)],
          ],
        },
        {
          title: "已驗證的輸入（input receipt）",
          columns: ["項目", "數值"],
          rows: [
            ["出生時刻把握程度", receipt.birth_time_precision],
            ["本地時間", (time.input_local_time)],
            ["時區", time.timezone_label],
            ["UTC 偏移（小時）", num(time.utc_offset_hours, 2)],
            // SD-32／PIA-2026-08-06-005：後端在模糊時刻已經逐字說明採用了
            // 哪一次、另一次是什麼，前端卻整份丟掉。使用者因此看不出自己
            // 的盤是從兩個可能時刻裡挑出來的。只在真的模糊時出現。
            ...(time.dst_warning
              ? [["重複民用時刻的處置", time.dst_warning]]
              : []),
            ["UTC", time.utc_time],
            ["儒略日 JD(UT)", num(time.jd_ut, 6)],
            ["ΔT（秒）", num(time.delta_t_seconds, 3)],
            ["緯度", num((receipt.location || {}).latitude, 6)],
            ["經度", num((receipt.location || {}).longitude, 6)],
            ["海拔（公尺）", num((receipt.location || {}).altitude_m, 1)],
          ],
        },
        {
          title: "地點解析",
          columns: ["項目", "數值"],
          rows: [
            ["地點標籤", place.place_label],
            ["來源", place.location_source],
            ["目錄記錄 id", place.source_record_id],
            ["位置精度", place.location_precision],
            ["驗證狀態", place.verification_status],
          ],
        },
        {
          title: "計算政策",
          columns: ["項目", "數值"],
          rows: [
            ["黃道", (policy.computation_mode || {}).zodiac],
            ["歲差校正", (policy.computation_mode || {}).ayanamsa],
            ["計算中心", (policy.computation_mode || {}).center],
            ["位置模式", (policy.computation_mode || {}).position_mode],
            ["黃道框架", (policy.computation_mode || {}).ecliptic_frame],
            ["旗標政策", flagPolicy(policy.flag_policy)],
            ["月亮位置", typeof policy.moon_position === "string"
              ? policy.moon_position : (policy.moon_position || {}).profile],
          ],
        },
        {
          title: "軌跡收據",
          columns: ["項目", "數值"],
          rows: [
            ["逐步軌跡步數", String(trace.length)],
            ["後端回報步數", String(traceReceipt.step_count === undefined
              ? DASH : traceReceipt.step_count)],
            ["序列化雜湊 SHA-256", traceReceipt.python_json_serialization_sha256],
            ["跨語言可攜", (traceReceipt.serialization_recipe || {})
              .portable_across_languages === true ? "是" : "否（僅供完整性核對）"],
          ],
        },
        {
          title: "隱私聲明",
          columns: ["項目", "數值"],
          rows: [
            ["聲明版本", privacy.privacy_attestation_version],
            ["聲明狀態（後端原值）", privacy.attestation_status],
            ["含敏感出生資料", privacy.contains_sensitive_birth_data === true ? "是" : "否"],
            ["可匿名分享", privacy.anonymous_share_ready === true ? "是" : "否"],
            ["已列出的未涵蓋層", uncoveredLayers(privacy.uncovered_layers)],
            ["App 持久化邊界", "計算期間不寫檔、不建立帳號或命盤資料庫；"
              + "下載是你主動觸發的本機寫入，本頁無法收回"],
          ],
        },
      ],
      blocks: [],
    };
  }

  /**
   * 回應 -> sections。這是唯一一處把後端形狀翻成顯示投影的地方。
   */
  function buildSections(response) {
    if (!isRecord(response)) throw new Error("缺少可用的 backend response。");
    return [
      timeSection(response),
      bodiesSection(response),
      nodesSection(response),
      lunarApsidesSection(response),
      parallaxMoonSection(response),
      fixedStarsSection(response),
      anglesSection(response),
      extraAnglesSection(response),
      lunarEventsSection(response),
      horizonEventsSection(response),
      antisciaSection(response),
      declinationAspectsSection(response),
      housesSection(response),
      planetInHouseSection(response),
      sectSection(response),
      lotsSection(response),
      voidOfCourseSection(response),
      aspectsSection(response),
      dignitiesSection(response),
      ...dignityProfileSections(response),
      sensitivitySection(response),
      requestedOptionsSection(response),
      traceSection(response),
      contractSection(response),
      receiptSection(response),
    ];
  }

  /**
   * canonical document -> 可渲染的節點樹。
   *
   * 刻意回傳純資料而不是 DOM：瀏覽器端只負責把節點變成元素（一律用 textContent），
   * 因此渲染路徑本身沒有字串拼接，也就沒有 HTML 注入面；
   * 而「sections 改了，畫面就會跟著改」這件事可以在 Node 裡直接斷言。
   */
  function buildViewTree(exportDocument) {
    if (!isRecord(exportDocument) || !Array.isArray(exportDocument.sections)) {
      throw new Error("缺少可用的 canonical export document。");
    }
    const dossier = exportDocument.calculation_dossier || {};
    const warnings = Array.isArray(dossier.warnings) ? dossier.warnings : [];
    return {
      header: {
        export_contract_version: exportDocument.export_contract_version,
        api_schema_version: exportDocument.source_response.schema_version || DASH,
        dossier_version: dossier.dossier_version || DASH,
        warnings: warnings.map((warning) => ({
          code: warning.code || "UNSPECIFIED_WARNING",
          message: warning.message || warning.note || "",
        })),
      },
      sections: exportDocument.sections.map((section) => ({
        type: "section",
        id: section.id,
        title: section.title,
        layer_label: section.layer_label,
        ring: ringKeyFor(section.layer_label),
        status: section.status,
        children: [].concat(
          section.notes.map((note) => ({ type: "note", text: note })),
          section.tables.map((table) => ({
            type: "table",
            title: table.title,
            columns: table.columns,
            rows: table.rows,
          })),
          section.blocks.map((block) => ({ type: "block", text: block }))
        ),
      })),
    };
  }

  function ringKeyFor(layerLabel) {
    const label = String(layerLabel || "");
    // 序數必須連同後面的空白一起比對。"RING III ...".startsWith("RING I") 為真，
    // 若照 I、II、III 的順序前綴比對，方法環會整批被判成天文環——
    // 而畫面只會顯示成顏色不對，不會報錯。
    const ordinals = [
      [RING.ASTRONOMICAL, RING.ASTRONOMICAL.ordinal + " "],
      [RING.GEOMETRY, RING.GEOMETRY.ordinal + " "],
      [RING.METHOD, RING.METHOD.ordinal + " "],
      [RING.VERIFICATION, RING.VERIFICATION.ordinal + " "],
    ];
    for (const [ring, prefix] of ordinals) {
      if (label.startsWith(prefix)) return ring.key;
    }
    return RING.VERIFICATION.key;
  }

  return Object.freeze({
    RING,
    MODULE_COVERAGE,
    DASH,
    dms,
    signPosition,
    receiptStatus,
    buildSections,
    buildViewTree,
    ringKeyFor,
  });
});
