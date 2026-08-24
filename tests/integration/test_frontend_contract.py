"""Static acceptance contracts for the maintained /zh-TW/calculate frontend."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CALCULATE_HTML = PROJECT_ROOT / "frontend" / "zh-TW" / "calculate.html"
CALCULATE_JS = PROJECT_ROOT / "frontend" / "zh-TW" / "calculate.js"
CALCULATE_CSS = PROJECT_ROOT / "frontend" / "zh-TW" / "calculate.css"


def test_public_frontend_source_rebuilds_adopted_methods_and_sensitivity():
    assert (PROJECT_ROOT / "frontend" / "zh-TW" / "methods.html").is_file()
    assert (PROJECT_ROOT / "frontend" / "zh-TW" / "sensitivity.js").is_file()

    completed = subprocess.run(
        [sys.executable, "-m", "scripts.frontend.build_pages", "--check"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_calculation_page_does_not_export_a_sensitive_debug_accessor():
    script = CALCULATE_JS.read_text(encoding="utf-8")
    assert "__calculatePageInspect" not in script


def test_calculate_page_exposes_bootstrap_failure_until_controller_is_ready():
    """A missing required script must not leave a silent disabled shell."""

    html = CALCULATE_HTML.read_text(encoding="utf-8")
    script = CALCULATE_JS.read_text(encoding="utf-8")

    match = re.search(
        r'<p[^>]*id="bootstrap-status"[^>]*>(.*?)</p>',
        html,
        re.DOTALL,
    )
    assert match is not None
    opening_tag = match.group(0).split(">", 1)[0]
    visible_text = re.sub(r"<[^>]+>", "", match.group(1))
    assert "hidden" not in opening_tag
    assert "頁面未完整載入" in visible_text
    assert "重新整理" in visible_text
    assert 'el("bootstrap-status")' in script
    assert "bootstrapStatus.hidden = true" in script
    assert script.index("buildOptionUi();") < script.index(
        "bootstrapStatus.hidden = true"
    )


def test_calculate_page_enables_submit_only_after_complete_initialization():
    """A half-built options UI must remain visibly unusable, not submittable."""

    script = CALCULATE_JS.read_text(encoding="utf-8")
    initialization = script.split("// ══ 起始狀態", 1)[1]
    order = [
        initialization.index("buildOptionUi();"),
        initialization.index("applyPrecisionConsequences();"),
        initialization.index("applyZodiacConsequences();"),
        initialization.index("bootstrapStatus.hidden = true"),
        initialization.index("submitButton.disabled = false"),
    ]
    assert order == sorted(order), (
        "bootstrap may hide and submit may enable only after every required "
        "initialization guard succeeds"
    )


def test_result_notes_wrap_long_evidence_identifiers_on_mobile():
    """Hashes and policy identifiers must not widen the whole mobile page."""

    stylesheet = CALCULATE_CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.section-note\s*\{([^}]*)\}", stylesheet)

    assert rule is not None
    declarations = {
        name.strip(): value.strip()
        for declaration in rule.group(1).split(";")
        if ":" in declaration
        for name, value in [declaration.split(":", 1)]
    }
    assert declarations.get("overflow-wrap") == "anywhere"


@pytest.mark.parametrize(
    "selector",
    (
        ".warnings li",
        ".status-line",
        ".inline-status",
        ".error-panel p",
        ".versions",
        ".reason-code",
        ".place-option b",
        ".place-meta",
    ),
)
def test_dynamic_status_and_warning_consumers_wrap_long_tokens(selector):
    stylesheet = CALCULATE_CSS.read_text(encoding="utf-8")
    rule = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", stylesheet)

    assert rule is not None
    assert "overflow-wrap:anywhere" in rule.group(1).replace(" ", "")


def test_response_compatibility_failure_uses_closed_user_facing_copy():
    """Untrusted response metadata must not become official-looking UI copy."""

    script = CALCULATE_JS.read_text(encoding="utf-8")
    render_response = script[script.index("function renderResponse(response)") :]
    catch_block = render_response[
        render_response.index("} catch (_error) {") :
        render_response.index("lifecycle.setCanonicalDocument(canonical)")
    ]

    assert "error.message" not in catch_block
    assert "這個版本的頁面無法安全呈現本次回應。" in catch_block
VIEW_MODEL_JS = PROJECT_ROOT / "frontend" / "zh-TW" / "view-model.js"
EXPORTERS_JS = PROJECT_ROOT / "frontend" / "zh-TW" / "exporters.js"
PRIVACY_LIFECYCLE_JS = PROJECT_ROOT / "frontend" / "zh-TW" / "privacy-lifecycle.js"
CLIENT_CONTEXT_JS = PROJECT_ROOT / "frontend" / "zh-TW" / "client-context.js"
PUBLIC_COPY_PRODUCER = PROJECT_ROOT / "scripts" / "frontend" / "pages"
PUBLIC_COPY_CONSUMER = PROJECT_ROOT / "frontend" / "zh-TW"
SECURITY_SOURCE = (
    PUBLIC_COPY_PRODUCER / "security.html"
    if (PUBLIC_COPY_PRODUCER / "security.html").is_file()
    else PUBLIC_COPY_CONSUMER / "security.html"
)
TRUST_SOURCE = (
    PUBLIC_COPY_PRODUCER / "trust.html"
    if (PUBLIC_COPY_PRODUCER / "trust.html").is_file()
    else PUBLIC_COPY_CONSUMER / "trust.html"
)


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


def test_new_valid_submission_invalidates_prior_result_before_fetch():
    script = CALCULATE_JS.read_text(encoding="utf-8")
    function = script[script.index("function submitPayload(payload)") :]
    before_fetch = function[: function.index('fetch("/api/chart"')]

    assert "dropResultsForNewAttempt()" in before_fetch


def test_place_search_supersedes_and_aborts_its_previous_request():
    script = CALCULATE_JS.read_text(encoding="utf-8")
    search_block = script[
        script.index("function searchPlaces()") :
        script.index("function buildPlaceRow(place)")
    ]

    assert "activePlaceSearchController" in search_block
    assert "activePlaceSearchGeneration" in search_block
    assert "activePlaceSearchController.abort()" in search_block
    assert "generation !== activePlaceSearchGeneration" in search_block


def test_rapid_submit_is_coalesced_before_profile_promise_resolves():
    script = CALCULATE_JS.read_text(encoding="utf-8")
    submit_handler = script[
        script.index('form.addEventListener("submit", (event) =>') :
        script.index("const REQUEST_TIMEOUT_MS")
    ]

    assert "submissionQueued" in submit_handler
    assert "if (submissionQueued) return" in submit_handler
    assert "submissionQueued = true" in submit_handler


def test_chart_consumer_rejects_non_json_success_media_types():
    script = CALCULATE_JS.read_text(encoding="utf-8")

    assert "isJsonResponse(response)" in script
    assert "application/json" in script
    assert "+json" in script
    assert "回應格式不是 JSON" in script


def test_place_search_has_a_client_timeout_and_recovers_after_abort():
    script = CALCULATE_JS.read_text(encoding="utf-8")
    search = script[script.index("function searchPlaces()") : script.index("function buildPlaceRow")]

    assert "PLACE_SEARCH_TIMEOUT_MS" in script
    assert "const generation = ++activePlaceSearchGeneration;" in script
    assert "}, PLACE_SEARCH_TIMEOUT_MS);" in script
    assert "signal: controller.signal" in script
    assert "new AbortController()" in search
    assert "signal: controller.signal" in search
    assert "window.clearTimeout(timer)" in search
    assert "地名查詢逾時" in search


def test_public_copy_does_not_promote_historical_host_evidence_to_current_state():
    security = SECURITY_SOURCE.read_text(encoding="utf-8")
    trust = TRUST_SOURCE.read_text(encoding="utf-8")

    for source in (security, trust):
        assert "目前主機不可達" not in source
        assert "目前主機可達" not in source
        assert "不自動代表" in source
    assert "具名時間點" in security
    assert "具名實測" in trust
    assert "實際運行的主機上以驗證器與隱私探針逐項確認" not in security
    assert "<td>已驗證</td>" not in trust


def test_public_copy_distinguishes_ai_review_from_professional_pentest():
    security = SECURITY_SOURCE.read_text(encoding="utf-8")
    trust = TRUST_SOURCE.read_text(encoding="utf-8")

    for source in (security, trust):
        assert "獨立 AI 紅隊" in source
        assert "專業滲透測試" in source

    public_copy_root = (
        PUBLIC_COPY_PRODUCER if PUBLIC_COPY_PRODUCER.is_dir() else PUBLIC_COPY_CONSUMER
    )
    for source_path in sorted(public_copy_root.glob("*.html")):
        source = source_path.read_text(encoding="utf-8")
        assert "沒有通過外部紅隊" not in source, source_path.name
