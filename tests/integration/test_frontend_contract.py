"""Static acceptance contracts for the maintained /zh-TW/calculate frontend."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CALCULATE_HTML = PROJECT_ROOT / "frontend" / "zh-TW" / "calculate.html"
CALCULATE_JS = PROJECT_ROOT / "frontend" / "zh-TW" / "calculate.js"
VIEW_MODEL_JS = PROJECT_ROOT / "frontend" / "zh-TW" / "view-model.js"
EXPORTERS_JS = PROJECT_ROOT / "frontend" / "zh-TW" / "exporters.js"
PRIVACY_LIFECYCLE_JS = PROJECT_ROOT / "frontend" / "zh-TW" / "privacy-lifecycle.js"
CLIENT_CONTEXT_JS = PROJECT_ROOT / "frontend" / "zh-TW" / "client-context.js"


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.form: dict[str, str | None] | None = None
        self.inputs: dict[str, dict[str, str | None]] = {}
        self.buttons: dict[str, dict[str, str | None]] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if tag == "form" and element_id == "chart-form":
            self.form = attributes
        elif tag == "input" and element_id:
            self.inputs[element_id] = attributes
        elif tag == "button" and element_id:
            self.buttons[element_id] = attributes


def test_calculate_loads_exact_versioned_dependencies_before_controller():
    html = CALCULATE_HTML.read_text(encoding="utf-8")
    dependencies = (
        "client-context.js",
        "exporters.js",
        "privacy-lifecycle.js",
        "options-catalogue.js",
        "request-input.js",
        "view-model.js",
        "location-receipt.js",
    )

    # The cache key was pinned as a literal here, so every bump turned this
    # contract test red for a reason unrelated to what it guards. What it
    # actually guards is that dependencies load before the controller and that
    # every asset carries the *same* key — a partial bump is the real defect,
    # because it serves a new controller against a stale dependency.
    keys = set(re.findall(r"/zh-TW/[\w.-]+\?v=([\w.-]+)", html))
    assert len(keys) == 1, f"assets disagree on the cache key: {sorted(keys)}"
    version = keys.pop()

    controller_offset = html.index("calculate.js")
    for asset in dependencies:
        assert html.index(asset) < controller_offset
        assert f"/zh-TW/{asset}?v={version}" in html
    assert f"/zh-TW/calculate.css?v={version}" in html
    assert f"/zh-TW/calculate.js?v={version}" in html


def test_sensitive_form_is_transient_and_clear_controls_are_explicit():
    parser = _FormParser()
    parser.feed(CALCULATE_HTML.read_text(encoding="utf-8"))

    assert parser.form is not None
    assert parser.form.get("autocomplete") == "off"
    for element_id in (
        "date",
        "hour",
        "minute",
        "second",
        "place-query",
        "latitude",
        "longitude",
        "altitude",
        "timezone",
    ):
        attributes = parser.inputs[element_id]
        assert attributes.get("autocomplete") == "off", element_id
        assert attributes.get("value") in {None, ""}, element_id
    assert parser.buttons["clear-results"].get("type") == "button"
    assert parser.buttons["clear-sensitive"].get("type") == "button"


def test_privacy_lifecycle_guards_requests_exports_and_page_exit():
    script = CALCULATE_JS.read_text(encoding="utf-8")
    lifecycle = PRIVACY_LIFECYCLE_JS.read_text(encoding="utf-8")
    exporters = EXPORTERS_JS.read_text(encoding="utf-8")
    executable_sources = (script + lifecycle + exporters).lower()

    for required in (
        "PrivacyLifecycle.createSensitiveDataLifecycle",
        "new AbortController()",
        "lifecycle.isCurrentRequest(token)",
        "lifecycle.requireCanonicalDocument()",
        "lifecycle.registerObjectUrl(url)",
        'window.addEventListener("pagehide"',
    ):
        assert required in script
    for forbidden in (
        "localstorage",
        "sessionstorage",
        "indexeddb",
        "caches.open",
        "navigator.sendbeacon",
    ):
        assert forbidden not in executable_sources


def test_render_and_export_consume_data_without_live_dom_reassembly():
    controller = CALCULATE_JS.read_text(encoding="utf-8")
    view_model = VIEW_MODEL_JS.read_text(encoding="utf-8")

    assert "sectionSnapshots.set(section.id, section)" in controller
    assert "sectionSnapshots.get(sectionId)" in controller
    assert "ChartExport.renderSectionText(snapshot)" in controller
    assert "ChartExport.runDownloadAction" in controller
    assert "document.querySelectorAll(\".method-name, p\")" not in controller
    assert "buildViewTree" in view_model


def test_frontend_never_builds_markup_from_response_data():
    sources = (
        CALCULATE_JS.read_text(encoding="utf-8")
        + VIEW_MODEL_JS.read_text(encoding="utf-8")
    )
    for sink in (
        "insertAdjacentHTML",
        "outerHTML",
        "document.write",
        "eval(",
        "new Function(",
        "dangerouslySetInnerHTML",
    ):
        assert sink not in sources
    for match in re.finditer(r"innerHTML\s*=\s*([^;\n]+)", sources):
        assert match.group(1).strip() in {'""', "''"}
    assert ".textContent =" in sources


def test_download_and_api_failures_have_user_visible_paths():
    script = CALCULATE_JS.read_text(encoding="utf-8")
    client_context = CLIENT_CONTEXT_JS.read_text(encoding="utf-8")

    assert "ChartExport.runDownloadAction" in script
    assert "outcome.ok" in script
    assert "setStatus(" in script
    assert "showError(" in script
    assert "ClientContext.formatApiError" in script
    assert "ClientContext.networkErrorMessage" in script
    assert "formatApiError" in client_context


def test_calculate_controller_installs_submit_guard_on_real_form():
    html = CALCULATE_HTML.read_text(encoding="utf-8")
    script = CALCULATE_JS.read_text(encoding="utf-8")

    assert 'id="chart-form"' in html
    assert 'id="submit-button"' in html
    assert 'form.addEventListener("submit", (event) =>' in script
    assert "event.preventDefault();" in script
    assert 'fetch("/api/chart"' in script
