// 前端 lint 規則。
//
// 這個 repository 直到 2026-08-06 都沒有任何 JavaScript linter：交付閘門用
// mypy 管 Python，JS 卻只有 `node --check` 的語法檢查與單元測試。語法檢查
// 不會告訴你一個函式從來沒被呼叫，也不會告訴你它引用的 API 不存在。
//
// 第一次跑就抓到 `calculate.js` 裡一整段 presets 死碼：沒有呼叫者、引用了
// catalogue 沒有的 `PRESETS`、操作兩個任何 HTML 都沒有的 DOM 節點。它從
// 建檔起就在那裡，任何 linter 第一次跑都會看見。
//
// 規則集刻意窄：以「能抓到上面那類錯誤」為準，不做風格意見（縮排、引號、
// 分號一律不管）。目標是讓 `--max-warnings 0` 成為可長期維持的閘門，而不是
// 製造一片黃字然後被習慣性忽略。

import globals from "globals";

/** 瀏覽器端模組彼此以 window 全域互相引用，沒有 bundler。 */
const RUNTIME_GLOBALS = {
  OptionsCatalogue: "readonly",
  ClientContext: "readonly",
  ChartExport: "readonly",
  ChartViewModel: "readonly",
  PrivacyLifecycle: "readonly",
  LocationReceipt: "readonly",
  RequestInput: "readonly",
};

/** 每個模組都用 UMD 式的 `typeof module === "object"` 讓 Node 測試 require 得到。 */
const DUAL_TARGET_GLOBALS = {
  module: "writable",
  require: "readonly",
};

const CORRECTNESS_RULES = {
  // 抓「引用了不存在的東西」與「寫了沒人用的東西」——presets 死碼就屬這兩類。
  "no-undef": "error",
  "no-unused-vars": ["error", {
    args: "none",
    // `catch (_error)` 是本 repository 刻意的寫法：例外被吞掉是經過考慮的，
    // 名字加底線就是那個宣告。它不該讓閘門變紅。
    caughtErrorsIgnorePattern: "^_",
    varsIgnorePattern: "^_",
  }],

  // 靜默的錯誤來源。
  "no-constant-binary-expression": "error",
  "no-self-compare": "error",
  "no-unsafe-negation": "error",
  "no-dupe-keys": "error",
  "no-dupe-else-if": "error",
  "no-duplicate-case": "error",
  "no-fallthrough": "error",
  "no-redeclare": "error",
  "no-sparse-arrays": "error",
  "no-unreachable": "error",
  "use-isnan": "error",
  "valid-typeof": "error",

  // 匯出物是 L1，動態求值一律禁止（契約 §3A）。
  "no-eval": "error",
  "no-implied-eval": "error",
  "no-new-func": "error",
  "no-script-url": "error",

  // 可讀性上真的會咬人的少數幾條。
  "no-implicit-globals": "error",
  "no-shadow": "error",
  "eqeqeq": ["error", "smart"],
};

export default [
  {
    files: ["zh-TW/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser, ...RUNTIME_GLOBALS, ...DUAL_TARGET_GLOBALS },
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: CORRECTNESS_RULES,
  },
  {
    files: ["tests/**/*.cjs"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "commonjs",
      globals: { ...globals.node },
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: CORRECTNESS_RULES,
  },
];
