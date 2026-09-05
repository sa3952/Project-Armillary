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
import html
import json
from pathlib import Path
import re
import sys
from xml.sax.saxutils import escape as xml_escape


ROOT = Path(__file__).resolve().parents[2]
PAGES = ROOT / "scripts" / "frontend" / "pages"
SHELL = PAGES / "_shell.html"
SURFACE_MANIFEST = ROOT / "frontend" / "surfaces.json"

# Bump when a shared asset changes, so browsers do not serve a stale copy
# alongside freshly generated markup.
ASSET_VERSION = "0.15.0-final-polish"

# /validation 的範例卷宗由 scripts/frontend/render_example_dossier.cjs 產生，
# 因為它必須是 view-model.js 的下游（契約 §10：render 與 export 同為 canonical
# sections 的兄弟）。在 Python 這邊重寫一遍 section 邏輯會造成第二個出處。
EXAMPLE_DOSSIER = PAGES / "_example-dossier.html"
_INDEXNOW_KEY = re.compile(r"^[A-Za-z0-9-]{8,128}$")

_TELEMETRY_FIELD_EXPLANATIONS = {
    "event_schema_version": "固定字串",
    "event": "固定字串",
    "request_id": "伺服器產生的32字元十六進位字串",
    "route": "固定分類，不是原始網址",
    "method": "GET／POST／HEAD／OPTIONS／OTHER",
    "status_code": "100–599",
    "duration_bucket": "固定分組，不記精確耗時",
    "request_size_bucket": "固定區間，不記內容",
    "outcome": "success／rejected／error／unknown",
}


def telemetry_contract_rows(
    contract: dict[str, tuple[str, ...]] | None = None,
) -> str:
    """Render membership from the executable event owner, never a copied list."""

    if contract is None:
        backend = str(ROOT / "backend")
        inserted = backend not in sys.path
        if inserted:
            sys.path.insert(0, backend)
        try:
            from app.privacy_logging import public_event_contract
        finally:
            if inserted:
                sys.path.remove(backend)

        contract = public_event_contract()
    fields = contract.get("fields")
    error_codes = contract.get("error_codes")
    failure_classes = contract.get("failure_classes")
    if not fields or not error_codes or not failure_classes:
        raise ValueError("public telemetry contract is incomplete")
    descriptions = {
        **_TELEMETRY_FIELD_EXPLANATIONS,
        "error_code": "空值，或：" + "、".join(error_codes),
        "failure_class": "空值，或：" + "、".join(failure_classes),
    }
    return "\n".join(
        "          <tr><td class=\"mono\">"
        + html.escape(field)
        + "</td><td>"
        + html.escape(descriptions.get(field, "封閉事件欄位"))
        + "</td></tr>"
        for field in fields
    )


def load_surface_manifest() -> dict:
    payload = json.loads(SURFACE_MANIFEST.read_text(encoding="utf-8"))
    if set(payload) != {
        "schema_version", "origin", "default_locale", "indexnow_key", "surfaces"
    } or payload["schema_version"] != 1:
        raise ValueError("frontend surface manifest root is invalid")
    if payload["origin"] != "https://projectarmillary.com":
        raise ValueError("frontend canonical origin is invalid")
    if payload["default_locale"] != "zh-TW" or not _INDEXNOW_KEY.fullmatch(
        payload["indexnow_key"]
    ):
        raise ValueError("frontend locale or IndexNow key is invalid")
    expected_fields = {
        "key", "fragment", "output", "surface", "locale", "layers",
        "indexable", "title", "description",
    }
    seen: dict[str, set[str]] = {name: set() for name in ("key", "output", "surface")}
    for item in payload["surfaces"]:
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise ValueError("frontend surface row field set is invalid")
        if item["fragment"] is not None and not isinstance(item["fragment"], str):
            raise ValueError("frontend surface fragment is invalid")
        if (
            item["locale"] != "zh-TW"
            or not isinstance(item["indexable"], bool)
            or not isinstance(item["title"], str)
            or not item["title"].strip()
            or not isinstance(item["description"], str)
            or not item["description"].strip()
            or not isinstance(item["layers"], list)
            or not item["layers"]
            or not set(item["layers"]) <= {"L1", "L2", "L3"}
            or not item["surface"].startswith("/zh-TW/")
            or not item["output"].startswith("zh-TW/")
            or ".." in Path(item["output"]).parts
        ):
            raise ValueError(f"frontend surface row is invalid: {item.get('key')}")
        for field in seen:
            value = item[field]
            if not isinstance(value, str) or value in seen[field]:
                raise ValueError(f"frontend surface {field} is invalid or duplicated")
            seen[field].add(value)
    if seen["surface"] != {item["surface"] for item in payload["surfaces"]}:
        raise ValueError("frontend surface set is invalid")
    if {"/zh-TW/", "/zh-TW/calculate"} - seen["surface"]:
        raise ValueError("frontend entry surfaces are missing")
    return payload


def _json_ld(item: dict, origin: str) -> str:
    canonical = origin + item["surface"]
    graph: list[dict] = [
        {
            "@type": "WebSite",
            "@id": origin + "/#website",
            "url": origin + "/",
            "name": "渾儀 Armillary",
            "inLanguage": "zh-TW",
        },
        {
            "@type": "WebPage",
            "@id": canonical + "#webpage",
            "url": canonical,
            "name": item["title"],
            "description": item["description"],
            "inLanguage": "zh-TW",
            "isPartOf": {"@id": origin + "/#website"},
        },
    ]
    if item["key"] == "home":
        graph.append({
            "@type": "WebApplication",
            "@id": origin + "/#application",
            "url": canonical,
            "name": "渾儀 Armillary",
            "alternateName": "Armillary",
            "description": item["description"],
            "applicationCategory": "ReferenceApplication",
            "operatingSystem": "Web",
            "creator": {"@type": "Person", "name": "Sebastian"},
        })
        graph[1]["about"] = {"@id": origin + "/#application"}
    return json.dumps(
        {"@context": "https://schema.org", "@graph": graph},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).replace("<", "\\u003c")


def metadata_values(item: dict, origin: str) -> dict[str, str]:
    canonical = origin + item["surface"]
    return {
        "robots": "index, follow" if item["indexable"] else "noindex, follow",
        "title": html.escape(item["title"], quote=True),
        "description": html.escape(item["description"], quote=True),
        "canonical": html.escape(canonical, quote=True),
        "surface": html.escape(
            f"{item['surface']} · {' ＋ '.join(item['layers'])}", quote=True
        ),
        "json_ld": _json_ld(item, origin),
    }


def render(item: dict, origin: str) -> str:
    name = item["fragment"]
    if not isinstance(name, str):
        raise ValueError("hand-maintained surface cannot be rendered")
    shell = SHELL.read_text(encoding="utf-8")
    body = (PAGES / f"{name}.html").read_text(encoding="utf-8").rstrip("\n")
    if "{{example_dossier}}" in body:
        body = body.replace(
            "{{example_dossier}}",
            EXAMPLE_DOSSIER.read_text(encoding="utf-8").rstrip("\n"),
        )
    if "{{telemetry_contract_rows}}" in body:
        body = body.replace(
            "{{telemetry_contract_rows}}",
            telemetry_contract_rows(),
        )
    metadata = metadata_values(item, origin)
    stylesheets = [
        '<link rel="stylesheet" href="/zh-TW/page.css?v={{assets}}">'
    ]
    if item["key"] == "calculate":
        stylesheets.append(
            '<link rel="stylesheet" href="/zh-TW/calculate.css?v={{assets}}">'
        )
    # {{body}} 先插入，{{assets}} 後替換，順序不可調換：頁面片段自己也需要
    # 版本鍵。/zh-TW/ 的敏感度示範腳本只屬於該頁，放進 _shell.html 會讓另外
    # 十一頁載入一個對它們無用的檔案；留在片段裡，就必須讓片段也能寫
    # ?v={{assets}}，否則 repository integrity 的 version-contracts 會擋下
    # 「frontend asset lacks version key」。
    return (
        shell.replace("{{robots}}", metadata["robots"])
        .replace("{{title}}", metadata["title"])
        .replace("{{description}}", metadata["description"])
        .replace("{{canonical}}", metadata["canonical"])
        .replace("{{json_ld}}", metadata["json_ld"])
        .replace("{{surface}}", metadata["surface"])
        .replace("{{stylesheets}}", "\n".join(stylesheets))
        .replace("{{body}}", body)
        .replace("{{assets}}", ASSET_VERSION)
    )


def robots_text(payload: dict) -> str:
    """The robots posture, read off the same `indexable` field as the meta tags.

    This file used to restate it as a hard-coded Allow, and the publication
    verifier restated it a third time as a literal to compare against.  One
    declaration, two readers.
    """

    origin = payload["origin"]
    indexable = [item for item in payload["surfaces"] if item["indexable"]]
    disallowed = sorted(
        item["surface"] for item in payload["surfaces"] if not item["indexable"]
    )
    lines = ["User-agent: *"]
    if indexable:
        lines.extend(f"Disallow: {surface}" for surface in disallowed)
        lines.append("Allow: /")
    else:
        lines.append("Disallow: /")
    lines.extend(["", f"Sitemap: {origin}/sitemap.xml", ""])
    return "\n".join(lines)


def _discovery_outputs(payload: dict) -> dict[Path, str]:
    origin = payload["origin"]
    canonical_urls = [
        origin + item["surface"]
        for item in payload["surfaces"]
        if item["indexable"]
    ]
    sitemap = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        *(f"  <url><loc>{xml_escape(url)}</loc></url>" for url in canonical_urls),
        "</urlset>",
        "",
    ]

    key = payload["indexnow_key"]
    return {
        ROOT / "frontend" / "robots.txt": robots_text(payload),
        ROOT / "frontend" / "sitemap.xml": "\n".join(sitemap),
        ROOT / "frontend" / f"{key}.txt": key + "\n",
    }


def _hand_maintained_metadata_current(item: dict, origin: str) -> bool:
    target = ROOT / "frontend" / item["output"]
    if not target.is_file():
        return False
    text = target.read_text(encoding="utf-8")
    metadata = metadata_values(item, origin)
    required = (
        f'<meta name="robots" content="{metadata["robots"]}">',
        f'<title>{metadata["title"]}</title>',
        f'<meta name="description" content="{metadata["description"]}">',
        f'<link rel="canonical" href="{metadata["canonical"]}">',
        f'<meta property="og:url" content="{metadata["canonical"]}">',
        metadata["json_ld"],
    )
    return all(text.count(token) == 1 for token in required)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    payload = load_surface_manifest()
    drift: list[str] = []
    for item in payload["surfaces"]:
        if item["fragment"] is None:
            if not _hand_maintained_metadata_current(item, payload["origin"]):
                drift.append(item["key"])
            continue
        target = ROOT / "frontend" / item["output"]
        rendered = render(item, payload["origin"])
        if args.check:
            current = target.read_text(encoding="utf-8") if target.is_file() else ""
            if current != rendered:
                drift.append(item["key"])
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered, encoding="utf-8")

    for target, rendered in _discovery_outputs(payload).items():
        if args.check:
            current = target.read_text(encoding="utf-8") if target.is_file() else ""
            if current != rendered:
                drift.append(target.name)
        else:
            target.write_text(rendered, encoding="utf-8")

    if args.check and drift:
        print("PAGE BUILD DRIFT: " + ", ".join(drift), file=sys.stderr)
        print("re-run: python -m scripts.frontend.build_pages", file=sys.stderr)
        return 1
    print(
        f"PAGES {'CHECKED' if args.check else 'BUILT'}: "
        f"{len(payload['surfaces'])} surfaces + 3 discovery assets"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
