(function attachOptionsCatalogue(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.OptionsCatalogue = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function buildCatalogue() {
  "use strict";

  // 三十五個 OptionsInput 欄位的完整目錄。
  //
  // 介面原則：辨識優於回憶。使用者是看到「Lilith」才想起要看它，
  // 不是先想起名字再去搜尋，所以選項一律攤開可掃視，不做搜尋框、不做分類導覽。
  // 分組只是視覺分隔，摺疊只有一層（進階選項開／關）。
  //
  // 兩條約束寫在資料裡而不是寫在畫面程式裡，否則會漂：
  //   1. 具名 profile 一律平等提供，不排序、不推薦、不標示建議值。因此每個
  //      profile 選項的 `values` 沒有 recommended 旗標，只有
  //      `label_zh` / `label_en`。
  //   2. 中文術語不得隱藏英文技術詞，所以每一項都有 label_zh 與 label_en
  //      兩個欄位，畫面必須同時呈現。
  //
  // `depends_on` 讓相依選項可以做第二層摺疊：父項關閉時子項不該佔版面，
  // 但也不得因此消失得無影無蹤——畫面要說「這些跟著誰」。

  // 分組只是視覺分隔線，不是要點開的容器。順序照「使用者掃視時最可能在找什麼」，
  // 不照後端欄位順序：先是看得見的天體與點，再是幾何與相位，最後是需要選流派的判斷。
  const GROUPS = Object.freeze([
    // main_form 的兩項不出現在進階選項裡；它們已升到主表單，與黃道、歲差並排。
    { key: "main_form", label_zh: "（主表單）", label_en: "(main form)", in_advanced: false },
    { key: "bodies",    label_zh: "天體",       label_en: "Bodies", in_advanced: true },
    { key: "points", in_advanced: true,    label_zh: "點與軸",     label_en: "Points and angles" },
    { key: "stars", in_advanced: true,     label_zh: "恆星",       label_en: "Fixed stars" },
    { key: "lunar", in_advanced: true,     label_zh: "月相、食與月空亡", label_en: "Lunar" },
    { key: "horizon", in_advanced: true,   label_zh: "升降與中天", label_en: "Rise, set and transits" },
    { key: "geometry", in_advanced: true,  label_zh: "幾何推導",   label_en: "Derived geometry" },
    { key: "aspects", in_advanced: true,   label_zh: "相位",       label_en: "Aspects" },
    { key: "dignities", in_advanced: true, label_zh: "必然尊貴",   label_en: "Essential dignities" },
  ]);

  /**
   * 每一欄的宣告。
   * type: boolean | choice | number
   * `values` 內不含建議或預設標記；預設值單獨放在 `default`，
   * 讓「產品預設」與「較好的選擇」在資料上就分得開。
   */
  const OPTIONS = Object.freeze([
    // ── 天體集合 ─────────────────────────────────────────
    { key: "body_selection_preset", group: "bodies", type: "choice",
      label_zh: "天體集合預設", label_en: "Body selection preset", default: "custom",
      values: [
        { value: "custom", label_zh: "自訂", label_en: "Custom" },
        { value: "classical_seven_v1", label_zh: "古典七政", label_en: "Classical seven" },
      ],
      // 後端 schemas.py:524 的 check_body_selection_preset：選 classical_seven_v1
      // 時，這四個非古典星體選項一律不得為 true，否則整個請求 422。
      // 宣告在資料裡，畫面才能在送出前就講清楚，而不是讓使用者撞牆。
      conflicts_when: {
        value: "classical_seven_v1",
        disables: [
          "include_outer_planets", "include_fixed_stars",
          "include_chiron", "include_lilith_priapus",
        ],
        reason_zh: "古典七政是封閉集合：太陽、月亮、水星、金星、火星、木星、土星。"
          + "選它就不能同時要求非古典星體；要那些請改回「自訂」。",
      },
      help_zh: "古典七政為太陽、月亮、水星、金星、火星、木星、土星。" },
    { key: "include_outer_planets", group: "bodies", type: "boolean",
      label_zh: "外行星", label_en: "Outer planets", default: false,
      help_zh: "天王星、海王星、冥王星。古典技法不使用，列為可選。" },
    { key: "include_chiron", group: "bodies", type: "boolean",
      label_zh: "凱龍", label_en: "Chiron", default: false },
    { key: "include_lilith_priapus", group: "bodies", type: "boolean",
      label_zh: "Lilith 與 Priapus（目前一起計算）", label_en: "Lilith and Priapus", default: false,
      help_zh: "後端目前以單一欄位同時計算三個非物理天體點，尚無法只取其一：平均黑月 Lilith（平均月球遠地點）、自然／插值遠地點與其對蹠近地點 Priapus。本產品不提供第三方軟體常稱 True Lilith 的密切／擺動遠地點（實測可相差 9.16°）。" },
    { key: "include_south_nodes", group: "bodies", type: "boolean",
      label_zh: "南交點", label_en: "South nodes", default: false },
    { key: "moon_position_profile", group: "bodies", type: "choice",
      label_zh: "月亮位置口徑", label_en: "Moon position profile",
      default: "global_computation_mode",
      values: [
        { value: "global_computation_mode", label_zh: "跟隨整體計算中心", label_en: "Follow global center" },
        { value: "moon_only_topocentric_v1", label_zh: "僅月亮採站心", label_en: "Moon-only topocentric" },
      ],
      help_zh: "選「僅月亮採站心」時，回應會同時保留地心參考值並標示混合來源警告。畫面上出現兩個月亮不是錯誤，是明示的座標政策。" },

    // ── 宮位 ─────────────────────────────────────────────
    // 不單獨呈現：由 house_system 的下拉一併表達（第一個選項是「不計算宮位」）。
    { key: "include_houses", group: "main_form", type: "boolean",
      rendered_by: "house_system",
      label_zh: "計算宮位", label_en: "Include houses", default: true,
      help_zh: "出生時刻只知道日期時，後端會關閉宮位計算，因為宮位需要已知時刻。" },
    { key: "house_system", group: "main_form", type: "choice",
      label_zh: "宮位制", label_en: "House system", default: "W", depends_on: "include_houses",
      values: [
        { value: "W", label_zh: "整宮", label_en: "Whole Sign" },
        { value: "P", label_zh: "Placidus", label_en: "Placidus" },
        { value: "R", label_zh: "Regiomontanus", label_en: "Regiomontanus" },
        { value: "B", label_zh: "Alcabitius", label_en: "Alcabitius" },
      ],
      help_zh: "四種平等提供，本產品不排序其歷史適用性。一次計算一種。" },

    // ── 點與軸 ───────────────────────────────────────────
    { key: "include_extra_angles", group: "points", type: "boolean",
      label_zh: "額外角點", label_en: "Extra angles", default: false },
    { key: "include_anti_vertex", group: "points", type: "boolean",
      label_zh: "反宿命點", label_en: "Anti-Vertex", default: false },
    { key: "include_lots", group: "points", type: "boolean",
      label_zh: "阿拉伯點", label_en: "Arabic lots", default: false,
      help_zh: "目前僅命運點與精神點。日夜盤臨界時兩種結果並列，不擇一。" },

    // ── 恆星 ─────────────────────────────────────────────
    { key: "include_fixed_stars", group: "stars", type: "boolean",
      label_zh: "恆星", label_en: "Fixed stars", default: false,
      help_zh: "34 顆，全部標為 research_only；勾選才計算。" },

    // ── 月相與食 ─────────────────────────────────────────
    { key: "include_lunar_phases", group: "lunar", type: "boolean",
      label_zh: "朔望", label_en: "Lunar phases", default: false,
      help_zh: "朔望會搜尋出生時刻前後的事件。接近 2399 範圍尾端時，若星曆無法支撐完整搜尋窗，核心星盤仍會完成，本模組會標為不可用並說明原因。" },
    { key: "include_eclipses", group: "lunar", type: "boolean",
      label_zh: "日月食", label_en: "Eclipses", default: false },
    { key: "include_void_of_course", group: "lunar", type: "boolean",
      label_zh: "月空亡", label_en: "Void of course", default: false,
      help_zh: "完成時刻的求解方式與其限制隨每次回應交代在結果的月空亡區塊裡。詳見方法頁。" },

    // ── 地平事件 ─────────────────────────────────────────
    { key: "include_rise_set_transits", group: "horizon", type: "boolean",
      label_zh: "升降與中天", label_en: "Rise, set and transits", default: false },

    // ── 幾何推導 ─────────────────────────────────────────
    { key: "include_antiscia", group: "geometry", type: "boolean",
      label_zh: "反照點", label_en: "Antiscia", default: false,
      help_zh: "純幾何鏡射，不需要任何技法選擇，公式會直接印在數值上方。" },
    { key: "antiscia_include_nodes", group: "geometry", type: "boolean",
      label_zh: "反照點含交點", label_en: "Antiscia include nodes", default: false,
      depends_on: "include_antiscia" },
    { key: "include_declination_aspects", group: "geometry", type: "boolean",
      label_zh: "赤緯平行與反平行", label_en: "Declination aspects", default: false,
      help_zh: "近現代技法，非古典傳統，預設關閉。" },
    { key: "declination_aspect_orb_degrees", group: "geometry", type: "number",
      label_zh: "赤緯容許度（度）", label_en: "Declination orb (degrees)", default: 1.0,
      // 範圍照抄 backend/app/schemas.py 的 `gt=0.0, le=3.0`：UI 宣告的區間
      // 必須是契約的子集，否則使用者會在送出後才被伺服器回 422。
      // 下界取 step 對齊的最小正值，因為契約是 gt 0。
      depends_on: "include_declination_aspects", min: 0.1, max: 3, step: 0.1 },

    // ── 相位 ─────────────────────────────────────────────
    { key: "include_aspects", group: "aspects", type: "boolean",
      label_zh: "計算相位", label_en: "Include aspects", default: true },
    { key: "aspect_set_profile", group: "aspects", type: "choice",
      label_zh: "相位集合", label_en: "Aspect set", default: "ptolemaic_major_v1",
      depends_on: "include_aspects",
      values: [
        { value: "ptolemaic_major_v1", label_zh: "Ptolemy 五大相位", label_en: "Ptolemaic major" },
        { value: "modern_common_minor_v1", label_zh: "五大相位 ＋ 常用幾何小相位", label_en: "Modern common minor" },
        { value: "modern_quintile_family_v1", label_zh: "五大相位 ＋ 五分相家族", label_en: "Quintile family" },
        { value: "modern_minor_combined_v1", label_zh: "五大相位 ＋ 常用小相位 ＋ 五分相家族", label_en: "Modern minor combined" },
      ] },
    { key: "aspect_orb_profile", group: "aspects", type: "choice",
      label_zh: "容許度表", label_en: "Orb profile", default: null,
      depends_on: "include_aspects",
      conflicts_when: {
        any_non_null: true,
        disables: ["aspect_fixed_orb_degrees"],
        reason_zh: "已選容許度表，固定容許度不能同時指定。要用固定值，請先把容許度表改回「不套用」。",
      },
      values: [
        { value: null, label_zh: "不套用容許度", label_en: "None applied" },
        { value: "abu_mashar_lineage_v1", label_zh: "Abu Ma'shar 傳承", label_en: "Abū Ma'shar lineage" },
        { value: "lilly_1647_experience_v1", label_zh: "Lilly 1647 經驗值", label_en: "Lilly 1647 experience" },
      ],
      help_zh: "目前沒有預設容許度表。不選時，相位只有幾何角距離，沒有「是否在容許度內」的判定，回應中該欄位會是 null，而不是「否」。" },
    { key: "aspect_orb_scale_percent", group: "aspects", type: "number",
      label_zh: "容許度縮放（%）", label_en: "Orb scale (percent)", default: null,
      // `gt=0.0, le=300.0`。
      depends_on: "aspect_orb_profile", min: 1, max: 300, step: 1 },
    { key: "aspect_fixed_orb_degrees", group: "aspects", type: "number",
      label_zh: "固定容許度（度）", label_en: "Fixed orb (degrees)", default: null,
      // `gt=0.0, le=30.0`；下界 0 會被拒絕。
      depends_on: "include_aspects", min: 0.1, max: 30, step: 0.1,
      help_zh: "指定後會取代容許度表的逐星體數值。" },
    { key: "partile_profile", group: "aspects", type: "choice",
      label_zh: "同度相位判準", label_en: "Partile profile", default: "same_degree_number_v1",
      depends_on: "include_aspects",
      values: [
        { value: "same_degree_number_v1", label_zh: "同度數編號", label_en: "Same degree number" },
        { value: "within_one_degree_v1", label_zh: "一度以內", label_en: "Within one degree" },
        { value: "lilly_1677_three_degrees_v1", label_zh: "Lilly 1677 三度", label_en: "Lilly 1677 three degrees" },
      ] },
    { key: "include_aspect_perfection", group: "aspects", type: "boolean",
      label_zh: "相位完成推算", label_en: "Aspect perfection", default: false,
      depends_on: "include_aspects" },
    { key: "aspect_include_nodes", group: "aspects", type: "boolean",
      label_zh: "相位納入交點", label_en: "Aspects include nodes", default: false,
      depends_on: "include_aspects" },
    { key: "aspect_include_angles", group: "aspects", type: "boolean",
      label_zh: "相位納入軸點", label_en: "Aspects include angles", default: false,
      depends_on: "include_aspects" },
    { key: "aspect_angle_orb_degrees", group: "aspects", type: "number",
      label_zh: "軸點容許度（度）", label_en: "Angle orb (degrees)", default: null,
      // `gt=0.0, le=30.0`；下界 0 會被拒絕。
      depends_on: "aspect_include_angles", min: 0.1, max: 30, step: 0.1 },

    // ── 必然尊貴 ─────────────────────────────────────────
    // 產品與後端預設皆依 Sebastian 2026-08-05 裁決關閉。
    { key: "include_domicile_exaltation", group: "dignities", type: "boolean",
      // 以下拉呈現，與界／面旬／三分性一致：那三個本來就是「不計算 ＋ 具名 profile」。
      render: "select",
      value_labels: [
        { value: false, label_zh: "不計算", label_en: "Not calculated" },
        { value: true,  label_zh: "計算",   label_en: "Calculated" },
      ],
      label_zh: "廟與旺", label_en: "Domicile and exaltation",
      default: false,
      help_zh: "只評估廟與旺；陷、落、外來與互容尚未評估，那是「未評估」，不是「沒有」。恆星黃道下產品會明確拒絕輸出並附原因代碼。" },
    { key: "bounds_profile", group: "dignities", type: "choice",
      label_zh: "界", label_en: "Bounds", default: null,
      values: [
        { value: null, label_zh: "不計算", label_en: "Not calculated" },
        { value: "egyptian_bounds_robbins_1940_v1", label_zh: "埃及界", label_en: "Egyptian" },
        { value: "chaldaean_bounds_ptolemy_i_21_v1", label_zh: "Chaldaean 界", label_en: "Chaldaean" },
        { value: "ptolemy_bounds_robbins_1940_v1", label_zh: "Ptolemy 界", label_en: "Ptolemy" },
        { value: "lilly_received_bounds_1647_v1", label_zh: "Lilly 1647 承襲界", label_en: "Lilly 1647 received" },
      ],
      help_zh: "四套平等提供。迦勒底界在宗派未知時無法選定守護星，回應會同時給出日盤與夜盤候選，由你判讀，本產品不替你選一個。" },
    { key: "decan_profile", group: "dignities", type: "choice",
      label_zh: "面／旬", label_en: "Face / decan", default: null,
      values: [
        { value: null, label_zh: "不計算", label_en: "Not calculated" },
        { value: "chaldean_planetary_faces_firmicus_ii_4_v1", label_zh: "Chaldean 行星面", label_en: "Chaldean planetary faces" },
        { value: "manilius_sign_decans_astronomica_iv_v1", label_zh: "Manilius 星座旬", label_en: "Manilius sign decans" },
      ],
      help_zh: "兩套的守護者型別不同：一套的守護者是行星，另一套是星座。畫面會分開標示，不混用同一種標籤。" },
    { key: "triplicity_profile", group: "dignities", type: "choice",
      label_zh: "三分性", label_en: "Triplicity", default: null,
      values: [
        { value: null, label_zh: "不計算", label_en: "Not calculated" },
        { value: "dorothean_triplicity_three_rulers_v1", label_zh: "Dorotheus 三守護", label_en: "Dorothean three rulers" },
        { value: "ptolemy_triplicity_textual_corulership_v1", label_zh: "Ptolemy 共治", label_en: "Ptolemy co-rulership" },
        { value: "lilly_triplicity_compact_1647_v1", label_zh: "Lilly 1647 簡表", label_en: "Lilly 1647 compact" },
      ] },
    { key: "triplicity_include_research_comparison", group: "dignities", type: "boolean",
      label_zh: "並列其餘三分性 profile 供比較", label_en: "Triplicity research comparison",
      default: false, depends_on: "triplicity_profile",
      help_zh: "並列比較不會改變你選定的那一套；回應中 selected_profiles 與 research_comparison_profiles 分開。" },
  ]);

  const BY_KEY = Object.freeze(OPTIONS.reduce((acc, option) => {
    acc[option.key] = option;
    return acc;
  }, {}));

  /** 目前值是否偏離產品預設。摺疊處的「已啟用」計數用它。 */
  function isEnabled(option, value) {
    if (option.type === "boolean") return value === true && option.default !== true;
    return value !== option.default && value !== undefined && value !== null;
  }

  /**
   * 某選項是否因互斥而被封鎖。回傳封鎖它的來源與理由，未被封鎖則回 null。
   *
   * 與 depends_on 不同：depends_on 是「父項關了它就沒意義」，
   * 互斥是「兩者同時成立會被後端拒絕」。前者隱藏，後者要停用並說明——
   * 隱藏會讓使用者以為選項不存在，而它其實只是這一刻不能用。
   */
  function conflictFor(option, values) {
    for (const source of OPTIONS) {
      const rule = source.conflicts_when;
      if (!rule) continue;
      if (!ruleMatches(rule, values[source.key])) continue;
      if (!rule.disables.includes(option.key)) continue;
      return { source, reason_zh: rule.reason_zh };
    }
    return null;
  }

  function ruleMatches(rule, current) {
    const forms = ["value", "values", "any_non_null"]
      .filter((key) => Object.prototype.hasOwnProperty.call(rule, key));
    if (forms.length !== 1) {
      throw new Error("conflicts_when 必須恰好宣告一種比對形式");
    }
    if (rule.any_non_null) {
      return current !== null && current !== undefined && current !== "";
    }
    if (rule.values) return rule.values.includes(current);
    return current === rule.value;
  }

  /** 相依鏈是否全部成立；任一祖先關閉即為 false。 */
  function isReachable(option, values) {
    let current = option;
    const seen = new Set();
    while (current && current.depends_on) {
      if (seen.has(current.key)) return false;
      seen.add(current.key);
      const parent = BY_KEY[current.depends_on];
      if (!parent) return false;
      const parentValue = values[parent.key];
      const parentActive = parent.type === "boolean"
        ? parentValue === true
        : parentValue !== null && parentValue !== undefined;
      if (!parentActive) return false;
      current = parent;
    }
    return true;
  }

  /** 每個群組的「已啟用」計數，供摺疊標題顯示（競品實測法則第 1 條）。 */
  function enabledCountByGroup(values) {
    const counts = {};
    GROUPS.forEach((group) => { counts[group.key] = 0; });
    OPTIONS.forEach((option) => {
      if (!isReachable(option, values)) return;
      if (isEnabled(option, values[option.key])) counts[option.group] += 1;
    });
    return counts;
  }

  /**
   * 目前值 -> 送給 backend 的 options 物件。
   *
   * **等於產品預設的值一律不送。** 後端就是靠「有沒有明示送出」來區分
   * `requested_explicitly` 與 `defaulted`：把 `include_domicile_exaltation: true`
   * 明示送出，收據就會從「產品預設帶入」變成「使用者選取」，
   * 而 §13.2 第 31 項要求畫面能分辨這兩者。無條件送出全部欄位會讓其中一態永遠不可達。
   *
   * 這不犧牲可重現性：回應的 `requested_options` 會回報本次實際生效的全部值，
   * 重現靠的是那份回聲，不是靠請求把預設值再說一次。
   */
  function toRequestOptions(values) {
    const payload = {};
    OPTIONS.forEach((option) => {
      if (!isReachable(option, values)) return;
      // 被互斥封鎖的欄位一律不送；送了整個請求會 422。
      if (conflictFor(option, values)) return;
      const value = values[option.key];
      if (value === undefined) return;
      if (value === option.default) return;
      payload[option.key] = value;
    });
    return payload;
  }

  function defaults() {
    return OPTIONS.reduce((acc, option) => {
      acc[option.key] = option.default;
      return acc;
    }, {});
  }

  return Object.freeze({
    GROUPS, OPTIONS, BY_KEY,
    isEnabled, isReachable, conflictFor, enabledCountByGroup, toRequestOptions, defaults,
  });
});
