#!/usr/bin/env python3
"""Generate frontend pages from fragments at commit time (SD-27).

SD-27 chose "generate at commit time and commit the output" over both a
hand-written set and a build chain: the served files stay readable source, so
AGPL §13's Corresponding Source obligation is discharged by View Source, while
thirteen surfaces (times languages, once SD-16 lands) do not each carry their
own copy of the head, header and footer.

The generated files are committed. `--check` re-runs the generation in memory
and fails if any committed file differs, which is what keeps the output from
drifting away from the fragments — the same shape as
`scripts/publication/generate_static_openapi.py`.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
PAGES = ROOT / "scripts" / "frontend" / "pages"
OUT = ROOT / "frontend" / "zh-TW"
SHELL = PAGES / "_shell.html"

# Bump when a shared asset changes, so browsers do not serve a stale copy
# alongside freshly generated markup.
ASSET_VERSION = "0.9.5-s20"

# surface label is shown in the footer and must match the layer registry entry.
# fragment 名稱 → 輸出檔名。未列出者輸出為同名 .html。
OUTPUT_NAMES = {
    # Sebastian 2026-08-05 裁決把 / 切到本產生器的首頁，因此輸出為 index.html。
    # 原本的 index.html 是 Codex 臨時前端，帶有 protected_work_profiles.json 的
    # 主機語氣 marker；那份保護不隨檔案消失，已改為對全部服務頁面生效，
    # 見 tests/repository/test_contract_maintenance.py。
    "home": "index.html",
    "legal-privacy": "legal/privacy.html",
    "legal-copyright": "legal/copyright.html",
    "legal-terms": "legal/terms.html",
}

SURFACES = {
    "home": ("首頁", "/zh-TW/ · L2 ＋ L3"),
    "trust": ("信任", "/zh-TW/trust · L1 受託宣稱"),
    "validation": ("驗證", "/zh-TW/validation · L1 受託宣稱"),
    "security": ("安全", "/zh-TW/security · L1 受託宣稱"),
    "features": ("功能", "/zh-TW/features · L2 ＋ L3"),
    "roadmap": ("路線", "/zh-TW/roadmap · L1 現況 ＋ 意圖陳述"),
    "methods": ("方法", "/zh-TW/methods · L1 受託宣稱"),
    "blog": ("文章", "/zh-TW/blog · 逐篇宣告分層"),
    "about": ("關於", "/zh-TW/about · L2 ＋ L3"),
    "contact": ("聯絡", "/zh-TW/contact · L1 受託宣稱"),
    "legal-privacy": ("隱私政策", "/zh-TW/legal/privacy · L1 受託宣稱"),
    "legal-copyright": ("著作權與原始碼", "/zh-TW/legal/copyright · L1 受託宣稱"),
    "legal-terms": ("使用條款", "/zh-TW/legal/terms · L1 受託宣稱"),
}
# /zh-TW/calculate 是手寫維護的互動頁，不由本產生器輸出；它需要多個 script 與自己的樣式。


# /validation 的範例卷宗由 scripts/frontend/render_example_dossier.cjs 產生，
# 因為它必須是 view-model.js 的下游（契約 §10：render 與 export 同為 canonical
# sections 的兄弟）。在 Python 這邊重寫一遍 section 邏輯會造成第二個出處。
EXAMPLE_DOSSIER = PAGES / "_example-dossier.html"


def render(name: str) -> str:
    shell = SHELL.read_text(encoding="utf-8")
    body = (PAGES / f"{name}.html").read_text(encoding="utf-8").rstrip("\n")
    if "{{example_dossier}}" in body:
        body = body.replace(
            "{{example_dossier}}",
            EXAMPLE_DOSSIER.read_text(encoding="utf-8").rstrip("\n"),
        )
    title, surface = SURFACES[name]
    # {{body}} 先插入，{{assets}} 後替換，順序不可調換：頁面片段自己也需要
    # 版本鍵。/zh-TW/ 的敏感度示範腳本只屬於該頁，放進 _shell.html 會讓另外
    # 十一頁載入一個對它們無用的檔案；留在片段裡，就必須讓片段也能寫
    # ?v={{assets}}，否則 repository integrity 的 version-contracts 會擋下
    # 「frontend asset lacks version key」。
    return (
        shell.replace("{{title}}", title)
        .replace("{{surface}}", surface)
        .replace("{{body}}", body)
        .replace("{{assets}}", ASSET_VERSION)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    drift = []
    for name in sorted(SURFACES):
        target = OUT / OUTPUT_NAMES.get(name, f"{name}.html")
        rendered = render(name)
        if args.check:
            current = target.read_text(encoding="utf-8") if target.is_file() else ""
            if current != rendered:
                drift.append(name)
        else:
            target.write_text(rendered, encoding="utf-8")

    if args.check and drift:
        print("PAGE BUILD DRIFT: " + ", ".join(drift), file=sys.stderr)
        print("re-run: python -m scripts.frontend.build_pages", file=sys.stderr)
        return 1
    print(f"PAGES {'CHECKED' if args.check else 'BUILT'}: {len(SURFACES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
