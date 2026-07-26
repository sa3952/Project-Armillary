"""Static acceptance contracts for failure-safe frontend startup."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = PROJECT_ROOT / "frontend" / "index.html"
APP_JS = PROJECT_ROOT / "frontend" / "app.js"
EXPORTERS_JS = PROJECT_ROOT / "frontend" / "exporters.js"
PRIVACY_LIFECYCLE_JS = PROJECT_ROOT / "frontend" / "privacy-lifecycle.js"
CLIENT_CONTEXT_JS = PROJECT_ROOT / "frontend" / "client-context.js"


class _CalculateButtonParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attributes: dict[str, str | None] | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "button" and attributes.get("id") == "calculate-button":
            self.attributes = attributes


class _PrivacyControlsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.form_attributes: dict[str, str | None] | None = None
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
            self.form_attributes = attributes
        elif tag == "input" and element_id:
            self.inputs[element_id] = attributes
        elif tag == "button" and element_id:
            self.buttons[element_id] = attributes


def test_calculate_button_cannot_submit_before_javascript_is_ready():
    """A slow first script load must not fall back to a native GET and reset input."""

    parser = _CalculateButtonParser()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))

    assert parser.attributes is not None, "calculate button needs a stable identifier"
    assert parser.attributes.get("type") == "submit"
    assert "disabled" in parser.attributes, (
        "the native submit control must remain inert until app.js installs preventDefault"
    )

    javascript = APP_JS.read_text(encoding="utf-8")
    listener_offset = javascript.index('form.addEventListener("submit"')
    ready_offset = javascript.index("calculateButton.disabled = false")
    assert listener_offset < ready_offset, (
        "JavaScript may enable native submission only after the submit guard is installed"
    )


def test_unified_export_pipeline_is_loaded_before_the_ui_controller():
    html = INDEX_HTML.read_text(encoding="utf-8")
    script = APP_JS.read_text(encoding="utf-8")
    exporters = EXPORTERS_JS.read_text(encoding="utf-8")

    assert html.index("privacy-lifecycle.js") < html.index("exporters.js")
    assert html.index("exporters.js") < html.index("app.js")
    assert html.index("client-context.js") < html.index("app.js")
    assert "style.css?v=0.8.0-alpha-ux0" in html
    assert "privacy-lifecycle.js?v=0.8.0-alpha-ux0" in html
    assert "exporters.js?v=0.8.0-alpha-ux0" in html
    assert "client-context.js?v=0.8.0-alpha-ux0" in html
    assert "app.js?v=0.8.0-alpha-ux0" in html
    for required_call in (
        "ChartExport.createDocument",
        "ChartExport.renderSectionText",
        "ChartExport.renderPlainText",
        "ChartExport.runDownloadAction",
    ):
        assert required_call in script
    for required_renderer in (
        "function renderCsv",
        "function renderJson",
        "function renderPlainText",
        "function renderMarkdown",
        "function buildDownloadArtifact",
    ):
        assert required_renderer in exporters


def test_sensitive_form_and_clear_controls_have_a_transient_browser_contract():
    parser = _PrivacyControlsParser()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))

    assert parser.form_attributes is not None
    assert parser.form_attributes.get("autocomplete") == "off"
    sensitive_input_ids = {
        "year",
        "month",
        "day",
        "hour",
        "minute",
        "second",
        "iana-name",
        "fixed-offset",
        "latitude",
        "longitude",
        "altitude",
        "pressure-hpa",
        "temperature-c",
    }
    for element_id in sensitive_input_ids:
        attributes = parser.inputs[element_id]
        assert attributes.get("autocomplete") == "off", element_id
        assert "data-sensitive-input" in attributes, element_id

    assert parser.buttons["clear-results-button"].get("type") == "button"
    assert parser.buttons["panic-clear-button"].get("type") == "button"


def test_sensitive_inputs_start_empty_and_example_loading_is_explicit():
    parser = _PrivacyControlsParser()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))

    sensitive_inputs = {
        element_id: attributes
        for element_id, attributes in parser.inputs.items()
        if "data-sensitive-input" in attributes
    }
    assert sensitive_inputs
    assert all(
        attributes.get("value") in {None, ""}
        for attributes in sensitive_inputs.values()
    )
    assert parser.buttons["load-example-button"].get("type") == "button"

    html = INDEX_HTML.read_text(encoding="utf-8")
    script = APP_JS.read_text(encoding="utf-8")
    for label in ("必填", "選填", "符合條件時必填"):
        assert label in html
    assert "載入範例資料" in html
    assert "loadExampleData" in script


def test_hosted_identity_trust_and_discovery_controls_are_profile_scoped():
    html = INDEX_HTML.read_text(encoding="utf-8")
    script = APP_JS.read_text(encoding="utf-8")

    assert (
        '<meta name="robots" content="noindex, nofollow, noarchive">'
        in html
    )
    assert 'id="profile-badge"' in html
    assert 'id="private-alpha-banner"' in html
    assert 'id="hosted-trust-notice"' in html
    assert 'data-profile-only="private_alpha"' in html
    assert "不建立 account database" in html
    assert "/api/chart" in html
    assert "application access event" in html
    assert "analytics" in html
    assert "proxy" in html
    assert "RAM" in html
    assert "信任說明仍在發布前定稿" in html
    assert "Sebastian" not in html

    assert CLIENT_CONTEXT_JS.is_file()
    client_context = CLIENT_CONTEXT_JS.read_text(encoding="utf-8")
    assert "validateClientConfiguration" in client_context
    assert "formatApiError" in client_context
    assert "networkErrorMessage" in client_context
    assert "127.0.0.1:8123" not in client_context
    assert "JSON.stringify(detail" not in script
    assert 'fetch("/api/client-config"' in script
    assert "applyApplicationProfile" in script


def test_privacy_lifecycle_precedes_handlers_and_blocks_browser_persistence():
    html = INDEX_HTML.read_text(encoding="utf-8")
    script = APP_JS.read_text(encoding="utf-8")
    lifecycle = PRIVACY_LIFECYCLE_JS.read_text(encoding="utf-8")
    exporters = EXPORTERS_JS.read_text(encoding="utf-8")
    executable_sources = (script + lifecycle + exporters).lower()

    assert html.index("privacy-lifecycle.js") < html.index("app.js")
    assert "PrivacyLifecycle.createSensitiveDataLifecycle" in script
    assert "sensitiveLifecycle.requireCanonicalDocument()" in script
    assert "sensitiveLifecycle.registerObjectUrl(url)" in script
    assert "sensitiveLifecycle.clear()" in script
    assert 'window.addEventListener("pagehide"' in script
    assert "new AbortController()" in script
    assert "sensitiveLifecycle.isCurrentRequest(requestToken)" in script
    assert 'querySelectorAll("[data-sensitive-input]")' in script
    for forbidden in (
        "localstorage",
        "sessionstorage",
        "indexeddb",
        "caches.open",
        "navigator.sendbeacon",
    ):
        assert forbidden not in executable_sources


def test_export_controls_disclose_local_download_privacy_boundary():
    script = APP_JS.read_text(encoding="utf-8")

    for visible_label in (
        "複製本節",
        "複製全部",
        "CSV",
        "JSON",
        "純文字 .txt",
        "AI-friendly .md",
    ):
        assert visible_label in script
    assert "出生時間與精確座標" in script
    assert "不會由 App 保存" in script
    assert "Calculation Dossier 計算收據" in script
    assert "已驗證出生資料" in script
    assert '"calculation_dossier"' in script
    assert '"time_conversion"' in script
    assert '"core_bodies"' in script
    assert "ChartExport.runDownloadAction" in script


def test_dossier_ui_renders_the_backend_privacy_attestation_without_aliases():
    script = APP_JS.read_text(encoding="utf-8")

    assert "privacy.privacy_attestation_version" in script
    assert "privacy.attestation_status" in script
    assert "privacy.claims" in script
    assert "claim.enforcement_layer" in script
    assert "claim.control?.id" in script
    assert "claim.scope?.applies_to" in script
    assert "claim.limitations" in script
    assert "privacy.application_persistence" not in script


def test_section_copy_reads_the_final_canonical_section_without_rescanning_dom():
    script = APP_JS.read_text(encoding="utf-8")

    assert "collectSectionModel(s)" not in script
    assert "s._canonicalExportSection" in script
    assert "node._canonicalExportSection = canonicalSection" in script


def test_dom_headings_have_one_export_role_and_download_failures_are_visible():
    script = APP_JS.read_text(encoding="utf-8")
    stylesheet = (
        PROJECT_ROOT / "frontend" / "style.css"
    ).read_text(encoding="utf-8")

    assert 'querySelectorAll(".method-name, p")' in script
    assert 'querySelectorAll("h3, .method-name, p")' not in script
    assert 'class: "export-error hidden"' in script
    assert 'role: "alert"' in script
    assert "無法建立" in script
    assert "ChartExport.runDownloadAction" in script
    assert ".export-error" in stylesheet
