const test = require("node:test");
const assert = require("node:assert/strict");

const Catalogue = require("../zh-TW/options-catalogue.js");

test("省略的條件是產品預設與後端預設同時符合", () => {
  // 後端以「有沒有明示送出」區分 requested_explicitly 與 defaulted。
  // 實測（2026-08-05，真實 /api/chart）：
  //   options:{}                              -> requested_explicitly=false, defaulted=true
  //   options:{include_domicile_exaltation:true} -> requested_explicitly=true,  defaulted=false
  // 因此把等於預設的值一併送出，會讓「產品預設帶入」這一態永遠不可達，
  // 而 §13.2 第 31 項要求畫面能分辨使用者選取、產品預設與 fail-closed 三者。
  // 尊貴是唯一兩者不同的欄位：產品預設關閉、後端 schema 預設開啟。
  // 不勾時必須明示送出 false，否則後端會自行帶入。
  assert.deepEqual(Catalogue.toRequestOptions(Catalogue.defaults()), {
    include_domicile_exaltation: false,
  });

  // 主動勾選時也必須送出，雖然值剛好等於後端預設——
  // 省略會讓收據把使用者的選擇報成 defaulted，而那是不實的。
  // 實測（2026-08-05，真實 /api/chart）：
  //   送 true  -> requested_explicitly=true,  defaulted=false
  //   不送     -> requested_explicitly=false, defaulted=true
  assert.deepEqual(
    Catalogue.toRequestOptions({
      ...Catalogue.defaults(), include_domicile_exaltation: true,
    }),
    { include_domicile_exaltation: true }
  );
});

test("只送出偏離預設的欄位", () => {
  const values = {
    ...Catalogue.defaults(),
    include_fixed_stars: true,
    bounds_profile: "egyptian_bounds_robbins_1940_v1",
  };
  assert.deepEqual(Catalogue.toRequestOptions(values), {
    include_fixed_stars: true,
    bounds_profile: "egyptian_bounds_robbins_1940_v1",
    include_domicile_exaltation: false,
  });
});

test("父項關閉時，子項不會被送出", () => {
  const values = { ...Catalogue.defaults(), include_aspects: false, partile_profile: "within_one_degree_v1" };
  const payload = Catalogue.toRequestOptions(values);
  assert.equal(payload.partile_profile, undefined, "相位關閉時不得送出相位子項");
  assert.equal(payload.include_aspects, false, "父項本身仍要送出，因為它偏離了預設");
});

test("已啟用計數只計可達且偏離產品預設的項目", () => {
  const base = Catalogue.defaults();
  assert.equal(
    Object.values(Catalogue.enabledCountByGroup(base)).reduce((a, b) => a + b, 0),
    0,
    "產品預設狀態不得顯示任何『已啟用』——即使該欄位需要明示送出"
  );

  const withStar = { ...base, include_fixed_stars: true };
  assert.equal(Catalogue.enabledCountByGroup(withStar).stars, 1);

  // 子項被啟用但父項關閉時不計入：畫面上它根本到不了。
  const unreachable = { ...base, include_antiscia: false, antiscia_include_nodes: true };
  assert.equal(Catalogue.enabledCountByGroup(unreachable).geometry, 0);
});

test("每一個選項都屬於一個已宣告的群組", () => {
  const groupKeys = new Set(Catalogue.GROUPS.map((g) => g.key));
  Catalogue.OPTIONS.forEach((option) => {
    assert.ok(groupKeys.has(option.group), `${option.key} 的群組未宣告`);
  });
});

test("升到主表單的選項不重複出現在進階選項裡", () => {
  const advanced = new Set(
    Catalogue.GROUPS.filter((g) => g.in_advanced).map((g) => g.key)
  );
  const promoted = Catalogue.OPTIONS.filter((o) => !advanced.has(o.group)).map((o) => o.key);
  assert.deepEqual(promoted.sort(), ["house_system", "include_houses"],
    "只有宮位相關的兩項升到主表單；其餘都應留在進階選項");
});

test("每個選項都同時有中文與英文標籤", () => {
  Catalogue.OPTIONS.forEach((option) => {
    assert.ok(option.label_zh && option.label_en, `${option.key} 缺少雙語標籤`);
    (option.values || []).forEach((value) => {
      assert.ok(value.label_zh && value.label_en, `${option.key} 的某個值缺少雙語標籤`);
    });
  });
});


test("相位集合的標籤必須說出它包含五大相位", () => {
  // 後端 aspects.py:134 的設計是「明示的聯合集合；所有現代集合都包含五大相位，
  // 避免『選小相位後反而遺失主相位』」。標籤若只寫「小相位」會讓使用者以為是二選一，
  // 而那正是 2026-08-05 被指出的缺陷。
  const option = Catalogue.BY_KEY.aspect_set_profile;
  option.values
    .filter((value) => value.value !== "ptolemaic_major_v1")
    .forEach((value) => {
      assert.ok(
        value.label_zh.includes("五大相位"),
        `${value.value} 的標籤沒有說出它包含五大相位：${value.label_zh}`
      );
    });
});

test("人名一律使用原文，不用未確立的中文譯名", () => {
  const banned = ["托勒密", "李利", "阿布馬謝", "都勒斯", "曼尼利烏斯",
                  "普拉西德", "雷吉奧蒙塔努斯", "阿卡比修斯", "莉莉絲", "普里阿普斯"];
  const text = JSON.stringify(Catalogue.OPTIONS);
  banned.forEach((name) => {
    assert.ok(!text.includes(name), `目錄仍含未確立的中文人名譯名：${name}`);
  });
});

test("互斥宣告與後端的 check_body_selection_preset 一致", () => {
  // 後端 schemas.py:524 在 classical_seven_v1 下拒絕這四個欄位。
  // 前端若少列一個，使用者仍會撞上 422；多列一個則是無謂的封鎖。
  const rule = Catalogue.BY_KEY.body_selection_preset.conflicts_when;
  assert.equal(rule.value, "classical_seven_v1");
  assert.deepEqual(rule.disables.slice().sort(), [
    "include_chiron", "include_fixed_stars",
    "include_lilith_priapus", "include_outer_planets",
  ]);
  assert.ok(rule.reason_zh && rule.reason_zh.length > 10, "互斥必須寫得出理由");
});

test("被互斥封鎖的欄位不會被送出，否則整個請求會 422", () => {
  const values = {
    ...Catalogue.defaults(),
    body_selection_preset: "classical_seven_v1",
    include_chiron: true, include_fixed_stars: true,
    include_outer_planets: true, include_lilith_priapus: true,
  };
  const payload = Catalogue.toRequestOptions(values);
  Catalogue.BY_KEY.body_selection_preset.conflicts_when.disables.forEach((key) => {
    assert.equal(payload[key], undefined, `${key} 被封鎖時不得送出`);
  });
  assert.equal(payload.body_selection_preset, "classical_seven_v1");
});

test("未觸發互斥時不封鎖任何欄位", () => {
  const values = { ...Catalogue.defaults(), include_chiron: true };
  assert.equal(Catalogue.conflictFor(Catalogue.BY_KEY.include_chiron, values), null);
  assert.equal(Catalogue.toRequestOptions(values).include_chiron, true);
});
