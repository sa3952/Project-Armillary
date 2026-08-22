# Frontend test fixtures

| Metadata | Value |
|---|---|
| Document ID | DEV-FRONTEND-TEST-FIXTURES |
| Owner | Sebastian |
| Author | Claude Opus 5 |
| Status | CURRENT_AUTHORITY |
| Created | 2026-08-05 |
| Last materially updated | 2026-08-05 |
| Scope | 前端測試 fixture 的來源、再生方式與資料性質 |
| Authority | Fixture provenance note |
| Supersedes | 無 |
| Superseded by | 無 |
| Related decisions | SD-24、SD-27 |
| Verification status | 三份 chart fixture 由實際 HTTP 探測落檔，非手寫 |
| Protected / editable | 隨契約變動再生 |
| Canonical router | [`../../README.md`](../../README.md) |

## 資料性質

`chart-*.json` 是**實際後端回應**，不是手寫樣本。輸入為合成生辰
（1990-05-15，臺中，24.1477N／120.6736E），**不是任何真實人物的出生資料**。

保留完整回應而不裁剪，是因為裁剪過的 fixture 會慢慢與真實形狀分家，
而這正是 `exporters.test.cjs` 曾經釘死 `0.10.0` 卻沒人發現的那一類問題。

## 再生方式

Current source of request identity is `chart-requests.json`; current backend-source digest、每份request／
response digest、schema與Dossier version在`chart-fixture-manifest.json`。普通check只讀、不改檔：

```bash
python -m scripts.frontend.regenerate_chart_fixtures --check
```

只有明確要更新current fixtures時才使用：

```bash
python -m scripts.frontend.regenerate_chart_fixtures --write
node scripts/frontend/render_example_dossier.cjs
python -m scripts.frontend.build_pages
```

下列舊curl例保留為單一request的人工重播說明，不再是四份fixture universe或currentness authority。

在 repository 根目錄啟動後端後：

```bash
curl -s -X POST http://127.0.0.1:8124/api/chart -H 'Content-Type: application/json' -d @- <<'JSON' | python3 -m json.tool --sort-keys > frontend/tests/fixtures/chart-exact.json
{"birth_time_precision":"exact","datetime":{"year":1990,"month":5,"day":15,"hour":14,"minute":30,"second":0},"timezone":{"mode":"iana","iana_name":"Asia/Taipei"},"location":{"latitude":24.1477,"longitude":120.6736,"altitude_m":80},"atmosphere":{"pressure_hpa":null,"temperature_c":15},"computation_mode":{},"options":{}}
JSON
```

四份的差異只在請求：

| Fixture | 差異 | 用途 |
|---|---|---|
| `chart-exact.json` | 預設 | 一般成功路徑；`birth_time_sensitivity` 為 `not_applicable` |
| `chart-sidereal-dignity-refused.json` | `computation_mode.zodiac=sidereal`、`ayanamsa=fagan_bradley` | 必然尊貴 fail-closed：`applicable=false`、`reason_code=sidereal_dignity_basis_not_authorized` |
| `chart-date-only.json` | `birth_time_precision=date_only`，時分秒全 0 | 取樣區間、代表性錨點語義、`not_evaluated_paths` |
| `chart-all-modules.json` | 明示開啟全部可選模組與具名方法profiles | example dossier、module／serializer完整consumer |

`date_only` 的時分秒必須全為 0，否則後端回 422：那些零是 API 的日期容器，
不代表午夜出生。

## `response-compatibility.json`

手寫的版本相容表，由 `tests/repository/test_contract_maintenance.py` 釘住。
它是 `exporters.test.cjs` 取用版本字串的**唯一出處**，測試不得自行重複字面值。
