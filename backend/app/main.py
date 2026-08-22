"""FastAPI 入口：組裝所有計算模組，並提供 /api/chart 端點與前端靜態頁面。

回應分三層，刻意不混在一起：
- astronomical_data：原始天文事實（星體位置/速度、ASC/MC 等角點、恆星位置），不含任何占星方法判斷
- derived_geometry：純幾何轉換，無需判斷「是否成立」（目前只有對蹠點）
- derived_methods：帶有技法假設/規則選擇的判斷結果（宮位劃分、Sect、Lots、VOC、赤緯相位），
  每一項都標註具名 method，方便日後替換不同流派規則而不影響天文資料層
"""

import hashlib
import hmac
import importlib.metadata
import os
from pathlib import Path
import re
import threading
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

import swisseph as swe
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from .config import (
    CLASSICAL_BODIES,
    NODE_BODIES,
    OUTER_BODIES,
    MODERN_MINOR_BODIES,
    LUNAR_APSIDES,
    FIXED_STARS,
    DECLINATION_ASPECT_ORB,
)
from .ephemeris import (
    FullEphemerisRequiredError,
    ensure_ephemeris_initialized_for_thread,
    release_ephemeris_for_thread,
    has_full_ephemeris_files,
    init_ephemeris,
)
from .schemas import (
    ChartRequest,
    HostedBoundaryErrorResponse,
    HostedValidationResponse,
    PlaceSearchRequest,
)
from .core.trace import Trace
from .core.tz_database import TZ_DATABASE
from .core.computation_mode import ComputationContext
from .core.time_utils import (
    AmbiguousLocalTimeChoiceRequiredError,
    NonexistentLocalTimeError,
    compute_time_conversion,
)
from .core.bodies import compute_bodies, derive_node_antipode, make_longitude_sampler
from .core.essential_dignities import compute_essential_dignities
from .core.aspects import compute_aspects
from .core.declination import compute_declination_aspects
from .core.houses import (
    EXTRA_ANGLE_PROVENANCE,
    HouseSystemUnavailableError,
    compute_houses,
)
from .core.house_placements import compute_planet_house_placements
from .core.birth_time_sensitivity import (
    build_approximate_hour_sensitivity,
    build_date_only_sensitivity,
)
from .core.fixed_stars import compute_fixed_stars, fixed_star_receipt
from .core.lots import determine_sect, compute_lots
from .core.antiscia import compute_antiscia
from .core.moon import find_voc_candidates, determine_void_of_course, VOC_METHOD_NAME
from .core.lunar_events import compute_primary_phases, compute_previous_eclipses
from .core.horizon_events import compute_horizon_events
from .core.root_finding import wrap_to_signed_180
from .core.dossier import DOSSIER_VERSION, build_calculation_dossier
from .core.place_catalog import (
    PlaceCatalog,
    PlaceCatalogUnavailableError,
)
from .privacy_logging import PrivacyBoundaryMiddleware, emit_security_event
from .request_limits import (
    ApiMethodBoundary,
    ChartRequestBoundary,
    RequestCapacityBoundary,
)
from .runtime_static import RuntimeStaticFiles
from .frontend_release import load_runtime_release
from .frontend_assets import discover_source_assets
from .settings import AppProfile, AppSettings, load_settings

SCHEMA_VERSION = "0.13.0"

# Swiss 天體 id 依 key 索引。求根器需要在時間軸上重新查詢星曆，而 astronomical_data
# 的星體紀錄刻意不外洩 Swiss 內部 id，故在此另建對照表，不改動既有輸出形狀。
_BODY_ID_BY_KEY = {
    body["key"]: body["id"]
    for body in CLASSICAL_BODIES + OUTER_BODIES + MODERN_MINOR_BODIES + NODE_BODIES
}
_CLASSICAL_KEYS = tuple(body["key"] for body in CLASSICAL_BODIES)
_OUTER_KEYS = tuple(body["key"] for body in OUTER_BODIES)
_NODE_KEYS = tuple(body["key"] for body in NODE_BODIES)

init_ephemeris()

# 是否有完整星曆檔只取決於 backend/ephe 目錄內容，執行期間不會變，啟動時算一次即可，
# 不需要每個請求都重新 listdir（尤其現在整個請求都在 _COMPUTE_LOCK 內序列執行）。
_HAS_FULL_EPHEMERIS = has_full_ephemeris_files()

# pyswisseph 這個 Python 套件的 distribution 版本號（如 2.10.3.2）跟它內建的
# Swiss Ephemeris C 函式庫版本號（swe.version，如 "2.10.03"）是兩個獨立數字，
# 只是長得很像；曾經誤把兩者視為同一個值回傳，這裡分開讀取、分開回報。
try:
    _PYSWISSEPH_DISTRIBUTION_VERSION: str | None = (
        importlib.metadata.version("pyswisseph")
    )
except importlib.metadata.PackageNotFoundError:
    _PYSWISSEPH_DISTRIBUTION_VERSION = None

def health_check(settings: AppSettings):
    """Report process liveness, not end-to-end calculation readiness."""
    if settings.profile is AppProfile.PRIVATE_ALPHA:
        return {
            "status": "ok",
            "ready": True,
            "readiness_scope": "process_liveness_only",
        }
    return {
        "status": "ok",
        "ready": True,
        "readiness_scope": "process_liveness_only",
        "service": "classical-astrology-app",
        "runtime_contract": "local-runtime-v2",
        "full_ephemeris_files": _HAS_FULL_EPHEMERIS,
        "swiss_ephemeris_library_version": swe.version,
    }


def client_configuration(
    settings: AppSettings,
    release_identity: dict[str, object] | None = None,
):
    """Expose the profile and verified deployment identity, when available."""
    response: dict[str, object] = {"profile": settings.profile.value}
    if release_identity is not None:
        response["release_identity"] = release_identity
    return response


# run-local.sh and diagnose_local_runtime.py both refuse a token record whose
# secret is shorter than this, so the three sides agree on one floor.
_MINIMUM_RUNTIME_TOKEN_LENGTH = 32


def authenticated_runtime_health(
    x_local_runtime_nonce: str = Header(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
):
    """Prove that the listener was started with the launcher's per-run secret.

    The nonce is public and changes for every probe.  The secret stays in the
    launcher-owned token file and Uvicorn environment, so a process that merely
    pre-binds the port cannot copy a static health response.
    """
    runtime_token = os.environ.get("CLASSICAL_ASTROLOGY_RUNTIME_TOKEN")
    if not runtime_token:
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "runtime_auth_unavailable",
                    "message": "此伺服器不是由本機一鍵啟動器建立，無法證明執行身分。",
                }
            },
        )
    if len(runtime_token) < _MINIMUM_RUNTIME_TOKEN_LENGTH:
        # This endpoint answers with HMAC(token, caller-chosen nonce), which is
        # how a challenge-response attestation is supposed to work: the launcher
        # picks the nonce and compares the digest itself.  HMAC-SHA256 is a PRF,
        # so serving those pairs does not leak the key.  What it *does* do is
        # give anyone who can reach the endpoint unlimited known-plaintext pairs
        # to brute-force offline, which means the whole scheme is worth exactly
        # the token's entropy and nothing more.
        #
        # The endpoint is only registered outside Private Alpha and the local
        # launcher binds 127.0.0.1, so there is no reachable oracle in either
        # supported configuration.  This check is the depth behind that: refuse
        # to answer at all rather than attest with a weak secret, so a future
        # launcher that regressed to a short token fails loudly here instead of
        # quietly shrinking the search space.  run-local.sh issues
        # secrets.token_urlsafe(32) (256 bits, 43 characters).
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "runtime_auth_token_too_weak",
                    "message": "本機執行驗證 token 的長度不足，拒絕以其證明執行身分。",
                }
            },
        )

    nonce_hmac = hmac.new(
        runtime_token.encode(),
        x_local_runtime_nonce.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "service": "classical-astrology-app",
        "ready": True,
        "runtime_contract": "local-runtime-v2",
        "nonce_hmac": nonce_hmac,
    }


async def _handle_nonexistent_local_time(request: Request, exc: NonexistentLocalTimeError):
    if request.app.state.settings.is_private_alpha:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": exc.code}},
        )
    return JSONResponse(
        status_code=422,
        content={"detail": {"code": exc.code, "message": str(exc)}},
    )


async def _handle_ambiguous_local_time_choice(
    request: Request,
    exc: AmbiguousLocalTimeChoiceRequiredError,
):
    if request.app.state.settings.is_private_alpha:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": exc.code}},
        )
    return JSONResponse(
        status_code=422,
        content={"detail": {"code": exc.code, "message": str(exc)}},
    )


async def _handle_full_ephemeris_required(request: Request, exc: FullEphemerisRequiredError):
    if request.app.state.settings.is_private_alpha:
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": exc.code}},
        )
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "code": exc.code,
                "message": str(exc),
                "operation": exc.operation,
                "jd_ut": exc.jd_ut,
                "retflag": exc.retflag,
            }
        },
    )


async def _handle_house_system_unavailable(request: Request, exc: HouseSystemUnavailableError):
    if request.app.state.settings.is_private_alpha:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": exc.code}},
        )
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": exc.code,
                "message": str(exc),
                "house_system": exc.house_system,
                "latitude": exc.latitude,
            }
        },
    )


async def _handle_place_catalog_unavailable(
    request: Request,
    exc: PlaceCatalogUnavailableError,
):
    if request.app.state.settings.is_private_alpha:
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": exc.code}},
        )
    return JSONResponse(
        status_code=503,
        content={"detail": {"code": exc.code, "message": str(exc)}},
    )


async def _handle_compute_capacity_exhausted(
    request: Request,
    exc: "ComputeCapacityExhaustedError",
):
    headers = {"Retry-After": str(exc.retry_after_seconds)}
    if request.app.state.settings.is_private_alpha:
        return JSONResponse(
            status_code=503,
            content={"detail": {"code": exc.code}},
            headers=headers,
        )
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "code": exc.code,
                "message": "伺服器目前的計算佇列已滿，請稍後重試。",
            }
        },
        headers=headers,
    )


# 日期/時區的「使用者輸入是否合法」已經在 schemas.py 的 Pydantic model_validator 裡驗證過，
# 會統一以 422 回應。若程式跑到這裡才拋出 swe.Error，代表的是合法輸入下的伺服器端問題
# （例如星曆檔缺失/損毀），故回 500，不要跟輸入錯誤混在一起回 400。
async def _handle_swisseph_error(request: Request, exc: Exception):
    if request.app.state.settings.is_private_alpha:
        return JSONResponse(
            status_code=500,
            content={"detail": {"code": "swiss_ephemeris_error"}},
        )
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code": "swiss_ephemeris_error",
                "message": "天文計算失敗；伺服器端星曆資料可能無法使用。",
            }
        },
    )


_HOSTED_VALIDATION_LOCATIONS = frozenset(
    {
        "body",
        "datetime",
        "year",
        "month",
        "day",
        "hour",
        "minute",
        "second",
        "birth_time_precision",
        "fold",
        "timezone",
        "mode",
        "iana_name",
        "utc_offset_hours",
        "location",
        "latitude",
        "longitude",
        "altitude_m",
        "place_label",
        "location_source",
        "source_record_id",
        "location_precision",
        "atmosphere",
        "pressure_hpa",
        "temperature_c",
        "options",
        "include_houses",
        "house_system",
        "aspect_orb_profile",
        "aspect_set_profile",
        "aspect_orb_scale_percent",
        "aspect_fixed_orb_degrees",
        "aspect_include_angles",
        "aspect_angle_orb_degrees",
        "declination_aspect_orb_degrees",
        "partile_profile",
        "include_south_nodes",
        "include_anti_vertex",
        "include_chiron",
        "body_selection_preset",
        "computation_mode",
        "center",
        "zodiac",
        "ayanamsa",
        "position_mode",
        "ecliptic_frame",
        "nutation",
        "query",
        "country_code",
        "limit",
    }
)
_HOSTED_VALIDATION_TYPE = re.compile(r"^[a-z0-9_.]{1,64}$")


def _hosted_validation_detail(exc: RequestValidationError) -> list[dict]:
    """Return only the closed fields the bundled client actually consumes."""

    sanitized = []
    for issue in exc.errors():
        raw_type = issue.get("type")
        issue_type = (
            raw_type
            if isinstance(raw_type, str)
            and _HOSTED_VALIDATION_TYPE.fullmatch(raw_type)
            else "invalid"
        )
        location: list[str | int] = []
        for item in issue.get("loc", ()):
            if isinstance(item, int):
                location.append(item)
            elif item in _HOSTED_VALIDATION_LOCATIONS:
                location.append(item)
            else:
                location.append("input")
        sanitized.append({"type": issue_type, "loc": location})
    return sanitized


async def _handle_request_validation(
    request: Request,
    exc: RequestValidationError,
):
    return JSONResponse(
        status_code=422,
        content={"detail": _hosted_validation_detail(exc)},
    )


# swisseph 的 set_topo/set_sid_mode 是行程層級全域狀態、非執行緒安全；FastAPI 對 sync def 端點會用
# 執行緒池平行執行，若不加鎖，並發請求可能互相污染彼此的計算中心/ayanamsa 設定，安靜地產生錯誤結果。
# 每個 production worker 都有自己的鎖；同一 process 內犧牲 thread-level 計算平行度，
# 由多個 worker process 提供有界並行，避免用全域 Swiss state 換取表面吞吐量。
_COMPUTE_LOCK = threading.Lock()

# How long a request may wait for the compute lock before it is refused.  The
# ceiling is generous relative to a served calculation (~0.3 s for the widest
# option set measured so far), so it never rejects a request that a merely busy
# service would have answered; it exists to put a bound on the queue.
_COMPUTE_LOCK_WAIT_TIMEOUT_SECONDS = 20.0
_COMPUTE_LOCK_RETRY_AFTER_SECONDS = 5
_BUILD_IDENTITY_CONTEXT: ContextVar[dict | None] = ContextVar(
    "classical_astrology_build_identity",
    default=None,
)
# `FPI-2026-08-06-E-011`. The privacy attestation used to be a constant, so a
# hosted deployment kept telling every user that no reverse-proxy layer exists
# and that the VPS is not deployed — while build_identity in the same receipt
# carried a controlled build revision. The receipt could not state its own
# deployment shape because nothing gave it one.
#
# Carried the same way as build_identity rather than threaded through
# compute_chart: both are properties of the running process, not of the
# request, and the existing seam already works.
_DEPLOYMENT_PROFILE_CONTEXT: ContextVar[str | None] = ContextVar(
    "classical_astrology_deployment_profile",
    default=None,
)


def _aspect_participants(
    options,
    bodies_all: list,
    lots: dict,
    angles: dict,
    *,
    angle_aspects_applicable: bool,
) -> list[dict]:
    """組出相位模組的參與者清單。

    七政恆為必要成員。三王星與阿拉伯點不另設開關，直接跟隨其本身的計算開關
    （`include_outer_planets` / `include_lots`），避免出現「要求三王星入相位但
    根本沒算三王星」這種自相矛盾的請求。交點永遠已算出，故由
    `aspect_include_nodes` 單獨控制。Chiron 雖可作為現代天文物件 opt-in 計算，
    本批沒有任何具名相位參與／orb 裁決，故不自動加入。ASC／MC 則依
    `aspect_include_angles` 加入逐度層；其不互配、整宮與 applying 邊界由 aspects 模組執行。

    阿拉伯點沒有 `body_id`（它不是星曆天體，而是 ASC/日/月的組合），也沒有黃經速度：
    Fortune 的速度需要 ASC 的角速度，而 ASC 每日轉一整圈，語意與行星速度不同類。
    因此兩者皆留空，讓相位模組回傳 null 而非給出一個不可比較的數字。
    """

    by_key = {body["key"]: body for body in bodies_all}
    participants = []

    def add_body(key: str, category: str):
        body = by_key.get(key)
        if body is None:
            return
        participants.append(
            {
                "key": key,
                "name": body["name"],
                "longitude": body["longitude"],
                "speed_longitude": body["speed_longitude"],
                "body_id": _BODY_ID_BY_KEY[key],
                "category": category,
            }
        )

    for key in _CLASSICAL_KEYS:
        add_body(key, "classical_planet")
    if options.aspect_include_nodes:
        for key in _NODE_KEYS:
            add_body(key, "lunar_node")
    if options.include_outer_planets:
        for key in _OUTER_KEYS:
            add_body(key, "outer_planet")
    if options.include_lots:
        for key, name in (("fortune", "福點"), ("spirit", "精神點")):
            longitude = lots.get(key) if lots else None
            if longitude is None:
                continue
            participants.append(
                {
                    "key": key,
                    "name": name,
                    "longitude": longitude,
                    "speed_longitude": None,
                    "body_id": None,
                    "category": "lot",
                }
            )
    if options.aspect_include_angles and angle_aspects_applicable:
        for key, name in (("asc", "上升點"), ("mc", "天頂")):
            longitude = angles.get(key)
            if longitude is None:
                continue
            participants.append(
                {
                    "key": key,
                    "name": name,
                    "longitude": longitude,
                    "speed_longitude": None,
                    "body_id": None,
                    "category": "angle",
                }
            )
    return participants


class ComputeCapacityExhaustedError(Exception):
    """Waiting for the Swiss compute lock exceeded the bounded queue budget."""

    code = "compute_capacity_exhausted"
    retry_after_seconds = _COMPUTE_LOCK_RETRY_AFTER_SECONDS


def compute_chart(req: ChartRequest):
    # _COMPUTE_LOCK serialises the whole calculation, which is required for
    # correctness (see its definition) but makes the wait queue the scarcest
    # resource in the process.  An unbounded `with _COMPUTE_LOCK:` lets a small
    # number of expensive requests hold every other caller indefinitely: the
    # connections stay open, nothing times out at this layer, and the service
    # stops answering rather than answering "not now".
    #
    # Bounding the wait does not make the service faster.  It converts an
    # open-ended stall into a fast, explicit refusal with Retry-After, so
    # overload degrades as rejected requests instead of as a wedged process.
    # Requests already holding the lock are never interrupted — cancelling a
    # calculation mid-way would leave the Swiss global state half-configured,
    # which is the exact failure the lock exists to prevent.
    if not _COMPUTE_LOCK.acquire(timeout=_COMPUTE_LOCK_WAIT_TIMEOUT_SECONDS):
        raise ComputeCapacityExhaustedError()
    try:
        return _compute_chart_locked(req)
    finally:
        try:
            release_ephemeris_for_thread()
        finally:
            _COMPUTE_LOCK.release()


def _compute_chart_locked(req: ChartRequest):
    input_request = req
    if input_request.birth_time_precision == "approximate_hour":
        req = input_request.model_copy(
            update={
                "datetime": input_request.datetime.model_copy(
                    update={"minute": 30, "second": 0.0}
                )
            }
        )
    elif input_request.birth_time_precision == "date_only":
        # 12:00 只是重現性良好的 representative anchor；input receipt 仍保留
        # 00:00:00 日期容器，且 Dossier 明示這不是出生時刻。
        req = input_request.model_copy(
            update={
                "datetime": input_request.datetime.model_copy(
                    update={"hour": 12, "minute": 0, "second": 0.0}
                )
            }
        )

    build_identity = _BUILD_IDENTITY_CONTEXT.get() or {
        "status": "unavailable",
        "source_revision": None,
        "revision_source": None,
    }
    deployment_profile = _DEPLOYMENT_PROFILE_CONTEXT.get()
    # Linux 的 pyswisseph source build 會讓 ephemeris path 隨目前 worker
    # thread 初始化；只在 module import 的主執行緒設定一次，FastAPI threadpool
    # 內的第一次計算會靜默退回 Moshier。此呼叫必須留在 _COMPUTE_LOCK 內，
    # 並指向 entrypoint 已逐檔驗 hash 的同一個絕對路徑。
    # 每條執行緒只實際設定一次，之後為 no-op；語意就是註解說的「每執行緒首次」，
    # 而不是「每個請求」。
    ensure_ephemeris_initialized_for_thread()
    trace = Trace()

    if not _HAS_FULL_EPHEMERIS:
        trace.add(
            "⚠ 星曆檔檢查",
            note="backend/ephe 目錄未偵測到完整星曆檔，計算將退回 Moshier 半分析模型（精度較低）。請參閱 README 下載星曆檔。",
        )

    time_conv = compute_time_conversion(
        req.datetime,
        req.timezone,
        req.location,
        trace,
        require_explicit_fold=(
            input_request.birth_time_precision == "approximate_hour"
        ),
        input_semantics=(
            "representative_computational_anchor_not_birth_time"
            if input_request.birth_time_precision == "date_only"
            else "representative_midpoint_not_exact_birth_time"
            if input_request.birth_time_precision == "approximate_hour"
            else "exact_birth_time"
        ),
    )
    jd_ut = time_conv["jd_ut"]

    ctx = ComputationContext(req.computation_mode, req.location)
    ctx.describe(trace, jd_ut)
    time_conv["ayanamsa"] = ctx.ayanamsa_value(jd_ut)

    # --- astronomical_data：原始天文事實 ---
    body_defs = (
        [{"id": b["id"], "key": b["key"], "zh": b["zh"]} for b in CLASSICAL_BODIES]
        + ([{"id": b["id"], "key": b["key"], "zh": b["zh"]} for b in OUTER_BODIES] if req.options.include_outer_planets else [])
        + ([dict(b) for b in MODERN_MINOR_BODIES] if req.options.include_chiron else [])
    )
    bodies = compute_bodies(body_defs, jd_ut, ctx, req.atmosphere, trace, physical=True)

    # CMP-A10（Sebastian 2026-08-04）：全域仍是 geocentric，只有 Moon 可明示
    # 改採同一地點的 topocentric observer origin。兩個結果都保留；下游只讀
    # effective Moon，避免同一模組內暗中混用兩個月亮。這是座標原點選擇，
    # 不宣稱 topocentric 比 geocentric「更準」。
    parallax_moon = None
    moon_ctx = None
    if req.options.moon_position_profile == "moon_only_topocentric_v1":
        geocentric_moon = next(body for body in bodies if body["key"] == "moon")
        moon_mode = req.computation_mode.model_copy(update={"center": "topocentric"})
        moon_ctx = ComputationContext(moon_mode, req.location)
        topocentric_moon = compute_bodies(
            [next(body for body in body_defs if body["key"] == "moon")],
            jd_ut,
            moon_ctx,
            req.atmosphere,
            trace,
            physical=True,
        )[0]
        geocentric_reference = {
            **geocentric_moon,
            "coordinate_origin": "earth_center",
            "role": "retained_reference_not_used_by_effective_downstream",
        }
        effective_topocentric = {
            **topocentric_moon,
            "coordinate_origin": "topocentric_observer",
            "effective_coordinate_origin": "topocentric_observer",
            "role": "effective_moon_used_by_downstream",
        }
        bodies = [
            effective_topocentric if body["key"] == "moon" else body
            for body in bodies
        ]
        moon_longitude_delta = wrap_to_signed_180(
            topocentric_moon["longitude"] - geocentric_moon["longitude"]
        )
        parallax_moon = {
            "method": "moon_only_topocentric_comparison_v1",
            "method_status": "sebastian_authorized_2026_08_04",
            "method_authority": "Sebastian ruling 2026-08-04 CMP-A10",
            "requested": True,
            "executed": True,
            "applicable": True,
            "available": True,
            "source": "Swiss Ephemeris SEFLG_TOPOCTR with observer geoposition",
            "coordinate_frame": (
                "mixed_origin_geocentric_chart_with_topocentric_effective_moon"
            ),
            "global_center": "geocentric",
            "effective_moon_center": "topocentric",
            "geocentric_reference": geocentric_reference,
            "topocentric_effective": effective_topocentric,
            "longitude_delta_degrees": moon_longitude_delta,
            "effective_downstream_paths": [
                "astronomical_data.bodies[moon]",
                "derived_methods.planet_in_house",
                "derived_geometry.antiscia",
                "derived_methods.lots",
                "derived_methods.void_of_course",
                "derived_methods.declination_aspects",
                "derived_methods.aspects",
                "derived_methods.essential_dignities",
                "birth_time_sensitivity",
            ],
            "claim": "different_observer_origin_not_more_accurate",
            "difference": {
                "longitude_signed_degrees": moon_longitude_delta,
                "latitude_degrees": (
                    topocentric_moon["latitude"] - geocentric_moon["latitude"]
                ),
                "right_ascension_signed_degrees": wrap_to_signed_180(
                    topocentric_moon["right_ascension"]
                    - geocentric_moon["right_ascension"]
                ),
                "declination_degrees": (
                    topocentric_moon["declination"] - geocentric_moon["declination"]
                ),
            },
            "downstream_policy": {
                "effective_moon": "topocentric",
                "other_bodies": "geocentric",
                "frame_semantics": "intentional_mixed_origin_profile",
                "interpretive_claim": None,
            },
        }

    lunar_apsides = None
    if req.options.include_lilith_priapus:
        apsis_points = compute_bodies(
            [dict(body) for body in LUNAR_APSIDES],
            jd_ut,
            ctx,
            req.atmosphere,
            trace,
            physical=False,
            moon_relative=True,
        )
        lunar_apsides = {
            "method": "swiss_lunar_apsides_named_points_v1",
            "method_status": "sebastian_authorized_2026_08_04",
            "method_authority": "Sebastian ruling 2026-08-04 CMP-A6 option 3",
            "requested": True,
            "executed": True,
            "applicable": ctx.mode.center in {"geocentric", "topocentric"},
            "available": all(
                point.get("longitude") is not None for point in apsis_points
            ),
            "source": "Swiss Ephemeris MEAN_APOG, INTP_APOG and INTP_PERG",
            "coordinate_policy": {
                "orbital_reference": "geocentric_lunar_orbit_model",
                "topocentric_parallax_applied": False,
                "reason": (
                    "Swiss Ephemeris does not distinguish topocentric from "
                    "geocentric lunar nodes/apogees for these orbital points"
                ),
            },
            "aspect_participation": "not_included_by_this_option",
            "points": apsis_points,
            "scope": {
                "classification": "modern_research_additional_points",
                "automatic_aspects": False,
                "automatic_classical_methods": False,
                "natural_priapus_is_independently_calculated": True,
                "natural_priapus_is_not_forced_opposite_natural_lilith": True,
            },
        }

    node_defs = [{"id": b["id"], "key": b["key"], "zh": b["zh"]} for b in NODE_BODIES]
    nodes = compute_bodies(
        node_defs,
        jd_ut,
        ctx,
        req.atmosphere,
        trace,
        physical=False,
        moon_relative=True,
    )
    if req.options.include_south_nodes:
        nodes_by_key = {node["key"]: node for node in nodes}
        nodes.extend(
            [
                derive_node_antipode(
                    nodes_by_key["true_node"],
                    key="true_south_node",
                    name="南交點(密切)",
                    body_id=_BODY_ID_BY_KEY["true_node"],
                    jd_ut=jd_ut,
                    ctx=ctx,
                    atmosphere=req.atmosphere,
                    trace=trace,
                ),
                derive_node_antipode(
                    nodes_by_key["mean_node"],
                    key="mean_south_node",
                    name="南交點(平均)",
                    body_id=_BODY_ID_BY_KEY["mean_node"],
                    jd_ut=jd_ut,
                    ctx=ctx,
                    atmosphere=req.atmosphere,
                    trace=trace,
                ),
            ]
        )
    bodies_all = bodies + nodes
    bodies_by_key = {b["key"]: b for b in bodies_all}
    longitude_at = make_longitude_sampler(ctx, moon_ctx=moon_ctx)

    houses = None
    houses_for_placement = None
    latitude_regime = None
    if req.options.include_houses:
        houses = compute_houses(
            req.options.house_system, jd_ut, req.location, ctx, trace
        )
        latitude_regime = houses["latitude_regime"]
        asc = houses["asc"]
        angles = {
            "asc": houses["asc"],
            "mc": houses["mc"],
            "desc": houses["desc"],
            "ic": houses["ic"],
            "armc": houses["armc"],
        }
        # Either None, or one of two differently shaped receipts.
        extra_angles: dict | None = None
        if req.options.include_extra_angles or req.options.include_anti_vertex:
            selected_extra_angles = {}
            if req.options.include_extra_angles:
                selected_extra_angles.update(
                    {
                        key: {
                            "longitude": value,
                            **EXTRA_ANGLE_PROVENANCE[key],
                        }
                        for key, value in houses["extra_angles"].items()
                    }
                )
            if req.options.include_anti_vertex:
                vertex = houses["extra_angles"]["vertex"]
                selected_extra_angles["anti_vertex"] = {
                    "longitude": (vertex + 180.0) % 360.0,
                    "source_vertex_longitude_degrees": vertex,
                    **EXTRA_ANGLE_PROVENANCE["anti_vertex"],
                }
            extra_angles = {
                "semantics": "non_classical_technical_angles_opt_in",
                "note": (
                    "以下五個角點由 swe.houses_ex 附帶回傳，皆非古典占星的四角。"
                    "本產品輸出其數值，不對其占星用途表示任何立場。"
                ),
                "angles": selected_extra_angles,
            }

        house_division = {
            "method": "swiss_ephemeris_house_division_v1",
            "method_status": "provisional_pending_method_audit",
            "method_authority": None,
            "execution_status": "computed",
            "system_code": req.options.house_system,
            "system_name": houses["system_name"],
            "cusps": houses["cusps"],
            "angles_source": "astronomical_data.angles",
        }
        houses_for_placement = {
            **houses,
            "system_code": req.options.house_system,
            "system_name": houses["system_name"],
        }
        planet_in_house = compute_planet_house_placements(
            bodies,
            houses_for_placement,
            ctx,
            trace,
        )
    else:
        asc = None
        angles = {
            "status": "not_requested",
            "requested": False,
            "executed": False,
            "applicable": False,
            "available": False,
            "source": None,
            "reason_code": "house_calculation_disabled",
            "asc": None,
            "mc": None,
            "desc": None,
            "ic": None,
            "armc": None,
        }
        extra_angles = (
            {
                "status": "not_applicable",
                "requested": True,
                "executed": False,
                "applicable": False,
                "available": False,
                "source": None,
                "reason_code": "house_calculation_not_executed",
                "semantics": "non_classical_technical_angles_opt_in",
                "angles": {},
            }
            if req.options.include_extra_angles or req.options.include_anti_vertex
            else None
        )
        house_division = {
            "method": None,
            "method_status": None,
            "method_authority": None,
            "execution_status": "not_requested",
            "requested": False,
            "executed": False,
            "applicable": False,
            "available": False,
            "reason_code": "house_calculation_disabled",
            "requested_system_code": req.options.house_system,
            "system_code": None,
            "system_name": None,
            "cusps": [],
            "angles_source": None,
        }
        planet_in_house = {
            "method": None,
            "method_status": None,
            "method_authority": None,
            "execution_status": "not_requested",
            "reason_codes": ["house_calculation_not_executed"],
            "house_system_code": None,
            "house_system_name": None,
            "interval_semantics": None,
            "placements": [],
        }
        trace.add(
            "宮位與角點",
            inputs={"include_houses": False},
            result={"execution_status": "not_requested"},
            note=(
                "依 request 真正略過 swe.houses_ex；未產生宮頭、ASC、MC、"
                "額外角點或行星落宮。"
            ),
        )

    fixed_stars = []
    if req.options.include_fixed_stars:
        fixed_stars = compute_fixed_stars(FIXED_STARS, jd_ut, ctx, req.atmosphere, trace)

    lunar_events = {}
    event_module_availability = {
        "lunar_phases": {
            "requested": req.options.include_lunar_phases,
            "status": (
                "not_requested"
                if not req.options.include_lunar_phases
                else "computed"
            ),
            "reason_code": None,
        }
    }
    if req.options.include_lunar_phases:
        try:
            lunar_events.update(compute_primary_phases(jd_ut, trace))
        except FullEphemerisRequiredError:
            event_module_availability["lunar_phases"] = {
                "requested": True,
                "status": "not_applicable",
                "reason_code": (
                    "full_ephemeris_unavailable_for_search_window"
                ),
            }
            trace.add(
                "主要月相事件",
                note=(
                    "請求日期的核心命盤可計算，但月相搜尋會離開完整 "
                    "Swiss Ephemeris files 覆蓋範圍；此模組標為不可用。"
                ),
            )
    if req.options.include_eclipses:
        lunar_events["eclipses"] = compute_previous_eclipses(jd_ut, trace)

    horizon_events = {}
    if req.options.include_rise_set_transits:
        horizon_events = compute_horizon_events(
            body_defs,
            jd_ut,
            req.location,
            req.atmosphere,
            trace,
        )

    # --- derived_geometry：純幾何轉換，不做「是否成立」的判斷 ---
    antiscia = {}
    if req.options.include_antiscia:
        # MTH-Q-008 乙裁決（2026-08-03）：反照點只算七政（必要）與交點（可選），
        # **不算恆星，不算軸點**。恆星在黃道上幾乎不動，其「反照點」缺乏傳統來源
        # 支持；恆星也是舊行為下 46 行輸出的主要來源。裁決表把交點列為「可選」，
        # 故另給 antiscia_include_nodes 開關，預設關閉。三王星不在裁決表內，
        # 一律不算——把未經裁決的對象加進來會是本程式自行擴張方法範圍。
        antiscia_keys = set(_CLASSICAL_KEYS)
        if req.options.antiscia_include_nodes:
            antiscia_keys |= set(_NODE_KEYS)
        antiscia_input = [
            body for body in bodies_all if body["key"] in antiscia_keys
        ]
        antiscia = compute_antiscia(
            antiscia_input,
            trace,
            ayanamsa=time_conv["ayanamsa"] or 0.0,
        )
        antiscia["scope"] = {
            "ruling": "MTH-Q-008 乙 (2026-08-03)",
            "included": (
                ["classical_planets", "lunar_nodes"]
                if req.options.antiscia_include_nodes
                else ["classical_planets"]
            ),
            "excluded": (
                ["fixed_stars", "angles", "outer_planets", "lots"]
                + ([] if req.options.antiscia_include_nodes else ["lunar_nodes"])
            ),
            "included_keys": [body["key"] for body in antiscia_input],
            "semantics": (
                "geometric_mirror_coordinates_only_no_relationship_verdict_no_orb"
            ),
        }

    # --- derived_methods：帶技法假設的判斷結果，每項皆標註具名 method ---
    sect = None
    if req.options.include_lots and req.options.include_houses:
        sect = determine_sect(bodies_by_key["sun"]["altitude_true"], trace)

    lots = {}
    if req.options.include_lots and req.options.include_houses:
        if sect is None:
            raise RuntimeError("sect must exist when lots are requested")
        lots = compute_lots(asc, bodies_by_key["sun"]["longitude"], bodies_by_key["moon"]["longitude"], sect, trace)
    elif req.options.include_lots:
        sect = {
            "method": None,
            "method_status": None,
            "method_authority": None,
            "execution_status": "not_applicable",
            "requested": True,
            "executed": False,
            "applicable": False,
            "available": False,
            "source": None,
            "reason_code": "house_calculation_not_executed",
            "is_day": None,
        }
        lots = {
            "method": None,
            "method_status": None,
            "method_authority": None,
            "execution_status": "not_applicable",
            "requested": True,
            "executed": False,
            "applicable": False,
            "available": False,
            "source": None,
            "reason_code": "house_calculation_not_executed",
            "fortune": None,
            "spirit": None,
            "depends_on_sect": None,
        }
        trace.add(
            "Sect 與 Lots",
            inputs={"include_lots": True, "include_houses": False},
            result={"execution_status": "not_applicable"},
            note="Lots 依賴 ASC；無宮位模式不以代表時刻產生 ASC 或 Sect 下游判定。",
        )

    void_of_course = {}
    if req.options.include_void_of_course:
        if ctx.horizon_meaningful:
            classical_keys_no_moon = {b["key"] for b in CLASSICAL_BODIES if b["key"] != "moon"}
            other_bodies_for_voc = [
                {**body, "body_id": _BODY_ID_BY_KEY[body["key"]]}
                for body in bodies_all
                if body["key"] in classical_keys_no_moon
            ]
            voc_candidates = find_voc_candidates(
                bodies_by_key["moon"],
                other_bodies_for_voc,
                trace,
                jd_ut=jd_ut,
                longitude_at=longitude_at,
                moon_id=_BODY_ID_BY_KEY["moon"],
            )
            void_of_course = determine_void_of_course(voc_candidates, trace)
        else:
            trace.add("月空亡(VOC)", note="⚠ heliocentric/barycentric 模式下無地平座標基礎，VOC 無法定義，回傳 null。")
            void_of_course = {
                "method": VOC_METHOD_NAME,
                "method_status": "provisional_pending_method_audit",
                "method_authority": None,
                "is_void_of_course": None,
                "time_to_sign_exit_hours": None, "next_completing_aspect": None, "all_candidates": [],
            }

    declination_aspects = {}
    if req.options.include_declination_aspects:
        declination_aspects = compute_declination_aspects(
            bodies_all,
            req.options.declination_aspect_orb_degrees,
            trace,
            default_orb=DECLINATION_ASPECT_ORB,
        )

    aspects = {}
    if req.options.include_aspects:
        angle_aspects_applicable = (
            req.options.include_houses
            and req.computation_mode.center in {"geocentric", "topocentric"}
            and req.computation_mode.ecliptic_frame == "of_date"
            and req.computation_mode.nutation
        )
        aspects = compute_aspects(
            _aspect_participants(
                req.options,
                bodies_all,
                lots,
                angles,
                angle_aspects_applicable=angle_aspects_applicable,
            ),
            trace,
            orb_profile_key=req.options.aspect_orb_profile,
            aspect_set_profile_key=req.options.aspect_set_profile,
            orb_scale_percent=req.options.aspect_orb_scale_percent,
            fixed_orb_degrees=req.options.aspect_fixed_orb_degrees,
            angle_orb_degrees=req.options.aspect_angle_orb_degrees,
            angles_requested=req.options.aspect_include_angles,
            angles_applicable=angle_aspects_applicable,
            angle_inapplicable_reason_code=(
                "house_calculation_not_executed"
                if not req.options.include_houses
                else "angle_frame_incompatible_with_body_longitudes"
            ),
            partile_profile_key=req.options.partile_profile,
            include_perfection=req.options.include_aspect_perfection,
            jd_ut=jd_ut,
            longitude_at=longitude_at,
        )

    dignity_sect_is_day = None
    dignity_sect_basis = "unavailable_date_only"
    if input_request.birth_time_precision != "date_only" and ctx.horizon_meaningful:
        dignity_sect_is_day = bodies_by_key["sun"]["altitude_true"] > 0.0
        dignity_sect_basis = (
            "sun_center_true_altitude_gt_zero_v1_exact_birth_time"
            if input_request.birth_time_precision == "exact"
            else (
                "sun_center_true_altitude_gt_zero_v1_"
                "representative_midpoint_not_exact_birth_time"
            )
        )
    elif not ctx.horizon_meaningful:
        dignity_sect_basis = "unavailable_for_display_center"
    essential_dignities = compute_essential_dignities(
        bodies=[body for body in bodies if body["key"] in _CLASSICAL_KEYS],
        include_domicile_exaltation=(
            req.options.include_domicile_exaltation
        ),
        requested_explicitly=bool(
            (
                "include_domicile_exaltation"
                in req.options.model_fields_set
                and req.options.include_domicile_exaltation
            )
            or (
                "bounds_profile" in req.options.model_fields_set
                and req.options.bounds_profile is not None
            )
            or (
                "decan_profile" in req.options.model_fields_set
                and req.options.decan_profile is not None
            )
            or (
                "triplicity_profile" in req.options.model_fields_set
                and req.options.triplicity_profile is not None
            )
            or (
                "triplicity_include_research_comparison"
                in req.options.model_fields_set
                and req.options.triplicity_include_research_comparison
            )
        ),
        domicile_exaltation_defaulted=(
            req.options.include_domicile_exaltation
            and "include_domicile_exaltation"
            not in req.options.model_fields_set
        ),
        bounds_profile=req.options.bounds_profile,
        decan_profile=req.options.decan_profile,
        triplicity_profile=req.options.triplicity_profile,
        include_triplicity_comparison=(
            req.options.triplicity_include_research_comparison
        ),
        center=req.computation_mode.center,
        zodiac=req.computation_mode.zodiac,
        ecliptic_frame=req.computation_mode.ecliptic_frame,
        nutation=req.computation_mode.nutation,
        sect_is_day=dignity_sect_is_day,
        sect_basis=dignity_sect_basis,
        trace=trace,
    )

    if input_request.birth_time_precision == "date_only":
        for requested, result in (
            (req.options.include_void_of_course, void_of_course),
            (req.options.include_aspects, aspects),
            (req.options.include_declination_aspects, declination_aspects),
            (essential_dignities is not None, essential_dignities),
        ):
            if requested and result:
                result["time_basis"] = (
                    "representative_computational_anchor_not_birth_time"
                )
                result["date_only_sensitivity_status"] = (
                    "not_evaluated_across_civil_day"
                )
        if lunar_apsides is not None:
            lunar_apsides["time_basis"] = (
                "representative_computational_anchor_not_birth_time"
            )
            lunar_apsides["date_only_sensitivity_status"] = (
                "not_evaluated_across_civil_day"
            )

    library_info = {
        "pyswisseph_distribution_version": _PYSWISSEPH_DISTRIBUTION_VERSION,
        "swiss_ephemeris_library_version": swe.version,
        # 時區資料庫和星曆檔一樣是這次計算的**輸入**：本地時間→UTC 完全由它決定。
        # IANA 每年釋出數次且經常修正歷史偏移，因此同一組輸入在不同版本下會得到
        # 不同的 UTC、不同的上升點、不同的宮位。先前收據只記 Swiss 版本，
        # 這個會靜默改變結果的輸入沒有留下任何痕跡。
        "tz_database": dict(TZ_DATABASE),
        "note": "兩者是不同的版本號：前者是 pip 安裝的 pyswisseph 套件版本，後者是其內建 "
                "Swiss Ephemeris C 函式庫自報的版本，數字格式相近但並非同一個值。"
                "tz_database 是本地時間換算所用的 IANA 時區資料庫版本，"
                "它不由本產品固定：macOS 由作業系統提供、容器由 image 提供，"
                "更新後同一組輸入可能換算出不同的 UTC。",
    }
    astronomical_data = {
        "time": time_conv,
        "atmosphere": {
            "pressure_hpa": req.atmosphere.pressure_hpa,
            "pressure_mode": (
                "user_supplied" if req.atmosphere.pressure_hpa is not None
                else "swiss_estimate_from_altitude"
            ),
            "temperature_c": req.atmosphere.temperature_c,
            "refraction": "swiss_ephemeris_standard_model",
            "applies_to": "altitude_apparent",
        },
        "bodies": bodies,
        "nodes": nodes,
        "fixed_stars": fixed_stars,
        "fixed_star_policy": fixed_star_receipt(
            FIXED_STARS,
            req.options.include_fixed_stars,
        ),
        "lunar_apsides": lunar_apsides,
        "parallax_moon": parallax_moon,
        "angles": angles,
        "extra_angles": extra_angles,
        "lunar_events": lunar_events,
        "horizon_events": horizon_events,
    }
    derived_methods = {
        "house_division": house_division,
        "planet_in_house": planet_in_house,
        "sect": sect,
        "lots": lots,
        "void_of_course": void_of_course,
        "declination_aspects": declination_aspects,
        "aspects": aspects,
        "essential_dignities": essential_dignities,
    }
    if input_request.birth_time_precision == "approximate_hour":
        birth_time_sensitivity = build_approximate_hour_sensitivity(
            request=input_request,
            body_defs=body_defs,
            representative_bodies=bodies,
            representative_houses=houses_for_placement,
            representative_placements=planet_in_house,
            representative_sect=sect,
            representative_lots=lots,
            representative_void_of_course=void_of_course,
        )
    elif input_request.birth_time_precision == "date_only":
        birth_time_sensitivity = build_date_only_sensitivity(
            request=input_request,
            body_defs=body_defs,
            node_defs=node_defs,
            representative_bodies=bodies,
            representative_nodes=nodes,
        )
        if req.options.include_lilith_priapus:
            birth_time_sensitivity["not_evaluated_paths"].append(
                "astronomical_data.lunar_apsides"
            )
    else:
        birth_time_sensitivity = {
            "precision": "exact",
            "status": "not_applicable",
        }
    trace_steps = trace.as_list()
    calculation_dossier = build_calculation_dossier(
        request=input_request,
        effective_request=req,
        time_conversion=time_conv,
        context=ctx,
        library_info=library_info,
        astronomical_data=astronomical_data,
        derived_methods=derived_methods,
        trace_steps=trace_steps,
        full_ephemeris_files_available=_HAS_FULL_EPHEMERIS,
        build_identity=build_identity,
        deployment_profile=deployment_profile,
        birth_time_sensitivity=birth_time_sensitivity,
        event_module_availability=event_module_availability,
        latitude_regime=latitude_regime,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "output_contract": {
            "status": "provisional",
            "compatibility": "additive_during_0.x_unless_a_documented_correctness_fix_requires_change",
            "layers": {
                "astronomical_data": "直接天文計算與座標／時間轉換",
                "derived_geometry": "只需明示公式的幾何轉換",
                "derived_methods": "需要占星方法或門檻選擇的結果；預設不計算",
            },
        },
        "requested_options": req.options.model_dump(),
        "library_info": library_info,
        "computation_mode": req.computation_mode.model_dump(),
        "calculation_dossier": calculation_dossier,
        "astronomical_data": astronomical_data,
        "derived_geometry": {
            "antiscia": antiscia,
        },
        "derived_methods": derived_methods,
        "birth_time_sensitivity": birth_time_sensitivity,
        "calculation_trace": trace_steps,
    }


def create_app(
    settings: AppSettings | None = None,
    *,
    event_emitter: Callable[[dict], bool] | None = None,
) -> FastAPI:
    resolved_settings = settings or load_settings()
    authority_root = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    )
    release_frontend_root, release_identity, release_assets = load_runtime_release(
        environment=os.environ,
        authority_root=Path(authority_root),
        backend_public_source_revision=resolved_settings.source_revision,
        api_schema_version=SCHEMA_VERSION,
        dossier_version=DOSSIER_VERSION,
    )
    runtime_build_identity = dict(resolved_settings.build_identity)
    if release_identity is not None:
        runtime_build_identity["release_identity"] = release_identity
    place_catalog = PlaceCatalog(resolved_settings.place_catalog_path)
    application = FastAPI(
        title="古典西洋占星天文計算 API",
        docs_url=None,
        redoc_url=None,
        openapi_url=resolved_settings.live_openapi_url,
    )
    application.state.settings = resolved_settings
    application.state.release_identity = release_identity

    def profile_health_check():
        return health_check(resolved_settings)

    def profile_compute_chart(req: ChartRequest):
        token = _BUILD_IDENTITY_CONTEXT.set(runtime_build_identity)
        profile_token = _DEPLOYMENT_PROFILE_CONTEXT.set(
            resolved_settings.profile.value
        )
        try:
            return compute_chart(req)
        finally:
            _DEPLOYMENT_PROFILE_CONTEXT.reset(profile_token)
            _BUILD_IDENTITY_CONTEXT.reset(token)

    def search_places(request: PlaceSearchRequest):
        return place_catalog.search(
            query=request.query,
            country_code=request.country_code,
            limit=request.limit,
        )

    # FastAPI response specs: the values are heterogeneous by design —
    # a single model, a union, extra keys. Typed from the first entry they
    # all looked wrong.
    json_responses: dict[int | str, dict[str, Any]] = {
        400: {"model": HostedBoundaryErrorResponse},
        503: {"model": HostedBoundaryErrorResponse},
    }
    if resolved_settings.is_private_alpha:
        json_responses.update(
            {
                431: {"model": HostedBoundaryErrorResponse},
                413: {"model": HostedBoundaryErrorResponse},
                415: {"model": HostedBoundaryErrorResponse},
                422: {
                    "model": (
                        HostedValidationResponse
                        | HostedBoundaryErrorResponse
                    )
                },
            }
        )

    api_method_routes = {
        "/api/health": frozenset({"GET"}),
        "/api/client-config": frozenset({"GET"}),
        "/api/chart": frozenset({"POST"}),
        "/api/places/search": frozenset({"POST"}),
    }
    if resolved_settings.expose_runtime_health:
        api_method_routes["/api/runtime-health"] = frozenset({"GET"})

    application.add_api_route(
        "/api/health",
        profile_health_check,
        methods=["GET"],
        name="health_check",
    )
    application.add_api_route(
        "/api/client-config",
        lambda: client_configuration(resolved_settings, release_identity),
        methods=["GET"],
        name="client_configuration",
        include_in_schema=False,
    )
    if resolved_settings.expose_runtime_health:
        application.add_api_route(
            "/api/runtime-health",
            authenticated_runtime_health,
            methods=["GET"],
            name="authenticated_runtime_health",
            responses={503: {"model": HostedBoundaryErrorResponse}},
        )
    application.add_api_route(
        "/api/chart",
        profile_compute_chart,
        methods=["POST"],
        name="compute_chart",
        responses=json_responses,
    )
    application.add_api_route(
        "/api/places/search",
        search_places,
        methods=["POST"],
        name="search_places",
        responses=json_responses,
    )

    application.add_exception_handler(
        NonexistentLocalTimeError,
        _handle_nonexistent_local_time,  # type: ignore[arg-type]
    )
    application.add_exception_handler(
        AmbiguousLocalTimeChoiceRequiredError,
        _handle_ambiguous_local_time_choice,  # type: ignore[arg-type]
    )
    application.add_exception_handler(
        FullEphemerisRequiredError,
        _handle_full_ephemeris_required,  # type: ignore[arg-type]
    )
    application.add_exception_handler(
        HouseSystemUnavailableError,
        _handle_house_system_unavailable,  # type: ignore[arg-type]
    )
    application.add_exception_handler(
        PlaceCatalogUnavailableError,
        _handle_place_catalog_unavailable,  # type: ignore[arg-type]
    )
    # Validation errors can contain the rejected value, including exact birth
    # coordinates.  The privacy boundary applies to every profile: local mode
    # is still routinely observed by browser tooling, proxies and test logs.
    # Sanitising globally also prevents non-standard JSON NaN/Infinity values
    # from making FastAPI's default error serializer fail with a secondary 500.
    application.add_exception_handler(
        RequestValidationError,
        _handle_request_validation,  # type: ignore[arg-type]
    )
    application.add_exception_handler(
        ComputeCapacityExhaustedError,
        _handle_compute_capacity_exhausted,  # type: ignore[arg-type]
    )
    application.add_exception_handler(swe.Error, _handle_swisseph_error)

    frontend_dir = str(
        release_frontend_root
        if release_frontend_root is not None
        else Path(authority_root) / "frontend"
    )
    if os.path.isdir(frontend_dir):
        allowed_assets = (
            release_assets
            if release_assets is not None
            else discover_source_assets(Path(frontend_dir))
        )
        @application.get("/", include_in_schema=False)
        async def redirect_default_locale() -> RedirectResponse:
            """Keep the public entry deterministic; never infer browser language."""
            return RedirectResponse(url="/zh-TW/", status_code=308)

        application.mount(
            "/",
            RuntimeStaticFiles(
                directory=frontend_dir,
                html=True,
                allowed_assets=allowed_assets,
            ),
            name="frontend",
        )

    application.add_middleware(
        ApiMethodBoundary,
        route_methods=api_method_routes,
    )

    if resolved_settings.is_private_alpha:
        application.add_middleware(ChartRequestBoundary)
        application.add_middleware(RequestCapacityBoundary)

    # Privacy remains the outer user middleware so it can contain downstream
    # response-lifecycle failures and apply headers to request-boundary errors.
    # The lambda preserves existing tests that replace the module-level sink.
    emitter = event_emitter or (lambda event: emit_security_event(event))
    application.add_middleware(
        PrivacyBoundaryMiddleware,
        event_emitter=emitter,
        additional_response_headers=(
            {"X-Robots-Tag": "noindex, nofollow, noarchive"}
            if resolved_settings.is_private_alpha
            else None
        ),
    )
    return application


app = create_app()
