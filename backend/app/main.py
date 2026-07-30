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
import re
import threading
from collections.abc import Callable
from contextvars import ContextVar

import swisseph as swe
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import CLASSICAL_BODIES, NODE_BODIES, OUTER_BODIES, FIXED_STARS, DECLINATION_ASPECT_ORB
from .ephemeris import FullEphemerisRequiredError, init_ephemeris, has_full_ephemeris_files
from .schemas import (
    ChartRequest,
    HostedBoundaryErrorResponse,
    HostedValidationResponse,
    PlaceSearchRequest,
)
from .core.trace import Trace
from .core.computation_mode import ComputationContext
from .core.time_utils import (
    AmbiguousLocalTimeChoiceRequiredError,
    NonexistentLocalTimeError,
    compute_time_conversion,
)
from .core.bodies import compute_bodies
from .core.declination import compute_declination_aspects
from .core.houses import HouseSystemUnavailableError, compute_houses
from .core.house_placements import compute_planet_house_placements
from .core.birth_time_sensitivity import build_approximate_hour_sensitivity
from .core.fixed_stars import compute_fixed_stars
from .core.lots import determine_sect, compute_lots
from .core.antiscia import compute_antiscia
from .core.moon import find_voc_candidates, determine_void_of_course, VOC_METHOD_NAME
from .core.lunar_events import compute_primary_phases, compute_previous_eclipses
from .core.horizon_events import compute_horizon_events
from .core.dossier import build_calculation_dossier
from .core.place_catalog import (
    PlaceCatalog,
    PlaceCatalogUnavailableError,
)
from .privacy_logging import PrivacyBoundaryMiddleware, emit_security_event
from .request_limits import ChartRequestBoundary, RequestCapacityBoundary
from .runtime_static import RuntimeStaticFiles
from .settings import AppProfile, AppSettings, load_settings

SCHEMA_VERSION = "0.10.0"

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
    """供瀏覽器預覽與實際 HTTP 驗收確認後端確實載入完成。"""
    if settings.profile is AppProfile.PRIVATE_ALPHA:
        return {"status": "ok", "ready": True}
    return {
        "status": "ok",
        "ready": True,
        "service": "classical-astrology-app",
        "runtime_contract": "local-runtime-v2",
        "full_ephemeris_files": _HAS_FULL_EPHEMERIS,
        "swiss_ephemeris_library_version": swe.version,
    }


def client_configuration(settings: AppSettings):
    """Expose only the closed profile name needed by the bundled frontend."""
    return {"profile": settings.profile.value}


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
        "house_system",
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
_BUILD_IDENTITY_CONTEXT: ContextVar[dict | None] = ContextVar(
    "classical_astrology_build_identity",
    default=None,
)


def compute_chart(req: ChartRequest):
    with _COMPUTE_LOCK:
        return _compute_chart_locked(req)


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

    build_identity = _BUILD_IDENTITY_CONTEXT.get() or {
        "status": "unavailable",
        "source_revision": None,
        "revision_source": None,
    }
    # Linux 的 pyswisseph source build 會讓 ephemeris path 隨目前 worker
    # thread 初始化；只在 module import 的主執行緒設定一次，FastAPI threadpool
    # 內的第一次計算會靜默退回 Moshier。此呼叫必須留在 _COMPUTE_LOCK 內，
    # 並指向 entrypoint 已逐檔驗 hash 的同一個絕對路徑。
    init_ephemeris()
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
    )
    jd_ut = time_conv["jd_ut"]

    ctx = ComputationContext(req.computation_mode, req.location)
    ctx.describe(trace, jd_ut)
    time_conv["ayanamsa"] = ctx.ayanamsa_value(jd_ut)

    # --- astronomical_data：原始天文事實 ---
    body_defs = (
        [{"id": b["id"], "key": b["key"], "zh": b["zh"]} for b in CLASSICAL_BODIES]
        + ([{"id": b["id"], "key": b["key"], "zh": b["zh"]} for b in OUTER_BODIES] if req.options.include_outer_planets else [])
    )
    bodies = compute_bodies(body_defs, jd_ut, ctx, req.atmosphere, trace, physical=True)

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
    bodies_all = bodies + nodes
    bodies_by_key = {b["key"]: b for b in bodies_all}

    houses = compute_houses(req.options.house_system, jd_ut, req.location, ctx, trace)
    asc = houses["asc"]
    angles = {
        "asc": houses["asc"],
        "mc": houses["mc"],
        "desc": houses["desc"],
        "ic": houses["ic"],
        "armc": houses["armc"],
    }
    house_division = {
        "method": "swiss_ephemeris_house_division_v1",
        "method_status": "provisional_pending_method_audit",
        "method_authority": None,
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
        antiscia_input = bodies_all + [
            {"key": s["key"], "name": s["name"], "longitude": s["longitude"]} for s in fixed_stars
        ]
        antiscia = compute_antiscia(antiscia_input, trace, ayanamsa=time_conv["ayanamsa"] or 0.0)

    # --- derived_methods：帶技法假設的判斷結果，每項皆標註具名 method ---
    sect = None
    if req.options.include_lots:
        sect = determine_sect(bodies_by_key["sun"]["altitude_true"], trace)

    lots = {}
    if req.options.include_lots:
        if sect is None:
            raise RuntimeError("sect must exist when lots are requested")
        lots = compute_lots(asc, bodies_by_key["sun"]["longitude"], bodies_by_key["moon"]["longitude"], sect, trace)

    void_of_course = {}
    if req.options.include_void_of_course:
        if ctx.horizon_meaningful:
            classical_keys_no_moon = {b["key"] for b in CLASSICAL_BODIES if b["key"] != "moon"}
            other_bodies_for_voc = [b for b in bodies_all if b["key"] in classical_keys_no_moon]
            voc_candidates = find_voc_candidates(bodies_by_key["moon"], other_bodies_for_voc, trace)
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
        declination_aspects = compute_declination_aspects(bodies_all, DECLINATION_ASPECT_ORB, trace)

    library_info = {
        "pyswisseph_distribution_version": _PYSWISSEPH_DISTRIBUTION_VERSION,
        "swiss_ephemeris_library_version": swe.version,
        "note": "兩者是不同的版本號：前者是 pip 安裝的 pyswisseph 套件版本，後者是其內建 "
                "Swiss Ephemeris C 函式庫自報的版本，數字格式相近但並非同一個值",
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
        "angles": angles,
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
        birth_time_sensitivity=birth_time_sensitivity,
        event_module_availability=event_module_availability,
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
    place_catalog = PlaceCatalog(resolved_settings.place_catalog_path)
    application = FastAPI(
        title="古典西洋占星天文計算 API",
        docs_url=None,
        redoc_url=None,
        openapi_url=resolved_settings.live_openapi_url,
    )
    application.state.settings = resolved_settings

    def profile_health_check():
        return health_check(resolved_settings)

    def profile_compute_chart(req: ChartRequest):
        token = _BUILD_IDENTITY_CONTEXT.set(resolved_settings.build_identity)
        try:
            return compute_chart(req)
        finally:
            _BUILD_IDENTITY_CONTEXT.reset(token)

    def search_places(request: PlaceSearchRequest):
        return place_catalog.search(
            query=request.query,
            country_code=request.country_code,
            limit=request.limit,
        )

    hosted_json_responses = (
        {
            400: {"model": HostedBoundaryErrorResponse},
            413: {"model": HostedBoundaryErrorResponse},
            415: {"model": HostedBoundaryErrorResponse},
            422: {
                "model": (
                    HostedValidationResponse
                    | HostedBoundaryErrorResponse
                )
            },
        }
        if resolved_settings.is_private_alpha
        else None
    )

    application.add_api_route(
        "/api/health",
        profile_health_check,
        methods=["GET"],
        name="health_check",
    )
    application.add_api_route(
        "/api/client-config",
        lambda: client_configuration(resolved_settings),
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
        )
    application.add_api_route(
        "/api/chart",
        profile_compute_chart,
        methods=["POST"],
        name="compute_chart",
        responses=hosted_json_responses,
    )
    application.add_api_route(
        "/api/places/search",
        search_places,
        methods=["POST"],
        name="search_places",
        responses=hosted_json_responses,
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
    if resolved_settings.is_private_alpha:
        application.add_exception_handler(
            RequestValidationError,
            _handle_request_validation,  # type: ignore[arg-type]
        )
    application.add_exception_handler(swe.Error, _handle_swisseph_error)

    frontend_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..",
        "frontend",
    )
    frontend_dir = os.path.abspath(frontend_dir)
    if os.path.isdir(frontend_dir):
        application.mount(
            "/",
            RuntimeStaticFiles(directory=frontend_dir, html=True),
            name="frontend",
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
