"""Application/browser privacy claims attached to calculation receipts."""
from __future__ import annotations

def _privacy_evidence(
    evidence_type: str,
    reference: str,
) -> dict:
    return {
        "type": evidence_type,
        "reference": reference,
        "semantics": "repository_pointer_not_test_execution_result",
    }

def _privacy_claim(
    *,
    claim_id: str,
    status: str,
    statement: str,
    enforcement_layer: str,
    control_id: str,
    mechanism: str,
    evidence: list[dict],
    applies_to: list[str],
    excludes: list[str],
    limitations: list[str],
) -> dict:
    return {
        "id": claim_id,
        "status": status,
        "statement": statement,
        "enforcement_layer": enforcement_layer,
        "control": {
            "id": control_id,
            "mechanism": mechanism,
        },
        "evidence": evidence,
        "scope": {
            "surface": "current_local_product",
            "applies_to": applies_to,
            "excludes": excludes,
        },
        "limitations": limitations,
    }

_HOSTED_UNCOVERED_LAYERS = {
        "reverse_proxy_cdn_waf": (
            "本 profile **預期**應用程式前方有一層反向代理（規劃為 host NGINX）。"
            "這是本次執行所宣告的部署意圖，**不是本次執行確實具有該層的證據**——"
            "行程只知道自己被以哪個 profile 啟動，看不到自己前面有什麼。"
            "本產品亦未驗證該層的 log 關閉、retention 或轉發標頭處理，"
            "相關證據須由部署方以 deployment canary 提出。"
        ),
        "hosting_supervisor": (
            "本 profile **預期**託管於 Infomaniak VPS（identity verification 已通過）。"
            "同上：這是宣告的部署意圖，不是本次執行的實測結果。"
            "本產品未驗證 host 層的 log、backup、snapshot 或 retention，"
            "hypervisor 與機房人員的存取亦不在本產品控制範圍內。"
        ),
}
_UNCOVERED_LAYERS_BY_PROFILE = {
    profile: _HOSTED_UNCOVERED_LAYERS
    for profile in ("private_alpha", "public")
}

def privacy_attestation(deployment_profile: str | None = None) -> dict:
    """Describe implemented controls without claiming per-request revalidation.

    Missing context uses the safe default profile; unknown values fail closed.
    """

    resolved = deployment_profile or "private_alpha"
    if resolved not in _UNCOVERED_LAYERS_BY_PROFILE:
        raise ValueError(f"unsupported privacy deployment profile: {resolved}")
    profile_status = (
        "declared_by_running_process"
        if deployment_profile is not None
        else "not_declared_defaulted_to_private_alpha"
    )
    uncovered_notes = _UNCOVERED_LAYERS_BY_PROFILE[resolved]

    return {
        "deployment_profile": resolved,
        "deployment_profile_status": profile_status,
        "uncovered_layer_semantics": (
            "these layers are named so the reader can see what this product "
            "does not control; presence of a layer is not a claim about its "
            "behaviour"
        ),
        # 1.3.0：新增 deployment_profile／deployment_profile_status 與
        # uncovered_layer_semantics，且未涵蓋層的敘述改為隨 profile 變動。
        # 純新增欄位，既有欄位語意未變。
        "privacy_attestation_version": "1.3.0",
        "attestation_status": "provisional_pending_external_review",
        "contains_sensitive_birth_data": True,
        "anonymous_share_ready": False,
        "evidence_semantics": (
            "repository_test_references_not_execution_attestation"
        ),
        "claims": [
            _privacy_claim(
                claim_id="application_chart_path_no_persistence",
                status="implemented_in_application_layer",
                statement=(
                    "目前 /api/chart application path 不使用出生資料資料庫、"
                    "session store、request cache或background queue，並以"
                    "Python write guard監看目前同步處理路徑。"
                ),
                enforcement_layer="application_request_path",
                control_id="application-no-persistence-current-chart-path-v1",
                mechanism=(
                    "No persistence dependency on the current chart path plus "
                    "Python file-write API regression guards."
                ),
                evidence=[
                    _privacy_evidence(
                        "python_test_reference",
                        "tests/backend/test_privacy_logging.py",
                    ),
                ],
                applies_to=[
                    "current synchronous FastAPI POST /api/chart path",
                ],
                excludes=[
                    "pyswisseph native or OS side effects",
                    "RAM, swap, crash dump and backup retention",
                    "user-initiated browser clipboard and downloads",
                ],
                limitations=[
                    "No secure memory erasure claim.",
                    (
                        "Python write guards do not intercept every native "
                        "library or operating-system side effect."
                    ),
                ],
            ),
            _privacy_claim(
                claim_id="application_telemetry_allowlist",
                status="implemented_in_application_layer",
                statement=(
                    "Application營運事件由封閉欄位與封閉 vocabulary重建，"
                    "不直接序列化request、response、header或exception。"
                ),
                enforcement_layer="application_event_sink",
                control_id="privacy-request-event-v1",
                mechanism=(
                    "Closed event builder plus sink-side schema validation and "
                    "non-propagating dedicated logger."
                ),
                evidence=[
                    _privacy_evidence(
                        "python_test_reference",
                        "tests/backend/test_privacy_logging.py",
                    ),
                ],
                applies_to=[
                    "classical_astrology.privacy application event sink",
                ],
                excludes=[
                    "ASGI server access log",
                    "reverse proxy, hosting supervisor and third-party telemetry",
                ],
                limitations=[
                    (
                        "Future logger or telemetry changes require a new "
                        "privacy review and canary test."
                    ),
                ],
            ),
            _privacy_claim(
                claim_id="asgi_exception_data_minimization",
                status="implemented_in_asgi_layer",
                statement=(
                    "目前ASGI application boundary以固定錯誤回應與完整"
                    "response-lifecycle containment避免原始exception進入"
                    "response或Uvicorn traceback。"
                ),
                enforcement_layer="asgi_application_boundary",
                control_id="privacy-asgi-boundary-v1",
                mechanism=(
                    "Low-level ASGI middleware contains pre-start and post-start "
                    "Exception paths and isolates event-sink failures."
                ),
                evidence=[
                    _privacy_evidence(
                        "python_test_reference",
                        "tests/backend/test_privacy_logging.py",
                    ),
                ],
                applies_to=[
                    "current FastAPI application wrapped by PrivacyBoundaryMiddleware",
                ],
                excludes=[
                    "process-control BaseException and cancellation semantics",
                    "transport failures outside the application boundary",
                    "future process supervisor error capture",
                ],
                limitations=[
                    (
                        "Post-response-start failures cannot rewrite an HTTP "
                        "status already sent on the wire."
                    ),
                ],
            ),
            _privacy_claim(
                claim_id="browser_transient_sensitive_state",
                status="conditional_on_bundled_frontend",
                statement=(
                    "目前browser application不使用persistent storage API，"
                    "並集中失效canonical document、section reference、"
                    "Blob URL與in-flight request。"
                ),
                enforcement_layer="browser_application",
                control_id="browser-sensitive-lifecycle-v1",
                mechanism=(
                    "Central lifecycle controller, request generation checks, "
                    "AbortController, result clear, panic clear and pagehide clear."
                ),
                evidence=[
                    _privacy_evidence(
                        "node_test_reference",
                        "frontend/tests/privacy_lifecycle.test.cjs",
                    ),
                ],
                applies_to=[
                    "current same-origin frontend calculation and export UI",
                ],
                excludes=[
                    "API clients that do not load the bundled frontend",
                    "browser extensions and compromised browser",
                    "browser or OS crash/session restoration",
                    "completed clipboard writes and downloaded files",
                ],
                limitations=[
                    "autocomplete=off is a browser hint, not an enforcement API.",
                    (
                        "JavaScript cannot securely erase runtime or "
                        "operating-system memory."
                    ),
                ],
            ),
        ],
        "uncovered_layers": [
            {
                "layer": "reverse_proxy_cdn_waf",
                "status": "outside_current_control_scope",
                "note": uncovered_notes["reverse_proxy_cdn_waf"],
            },
            {
                "layer": "hosting_supervisor",
                "status": "outside_current_control_scope",
                "note": uncovered_notes["hosting_supervisor"],
            },
            {
                "layer": "third_party_telemetry",
                "status": "outside_current_control_scope",
                "note": "目前未整合；未來新增前必須重新進行隱私審查。",
            },
            {
                "layer": "browser_os_native_memory",
                "status": "outside_current_control_scope",
                "note": "不宣稱RAM、swap、crash restore或原生函式庫資料可安全抹除。",
            },
        ],
    }
