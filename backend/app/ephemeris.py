"""swisseph 星曆檔路徑初始化與精度檢查。"""

import os
import threading

import swisseph as swe

EPHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ephe")


class FullEphemerisRequiredError(Exception):
    """A calculation requested Swiss files but Swiss silently fell back to Moshier."""

    code = "full_ephemeris_required"

    def __init__(self, *, operation: str, jd_ut: float, retflag: int):
        self.operation = operation
        self.jd_ut = jd_ut
        self.retflag = retflag
        super().__init__(
            f"{operation} 超出目前完整 Swiss Ephemeris 資料檔可計算的範圍；"
            "已拒絕改用 Moshier 近似模型。"
        )


def init_ephemeris():
    swe.set_ephe_path(EPHE_DIR)


# The Linux pyswisseph source build initialises its ephemeris path per worker
# thread, so a path set once on the import thread does not reach the threads
# FastAPI runs sync endpoints on, and the first calculation in each of those
# threads silently falls back to Moshier.  The old fix called init_ephemeris()
# on every request, which stated "once per thread" as "every time" and left the
# next reader unable to tell which one was intended.
#
# This says it once.  threading.local() gives each thread its own flag, so each
# threadpool thread pays exactly one set_ephe_path and no more.  Reused threads
# skip it; new threads do not.
_THREAD_STATE = threading.local()


def ensure_ephemeris_initialized_for_thread() -> bool:
    """Set the ephemeris path once per thread.  True if this call did the work."""

    if getattr(_THREAD_STATE, "initialized", False):
        return False
    swe.set_ephe_path(EPHE_DIR)
    _THREAD_STATE.initialized = True
    return True


def has_full_ephemeris_files() -> bool:
    """Check the exact 1800–2399 planet/Moon segment used by the API."""
    if not os.path.isdir(EPHE_DIR):
        return False
    names = set(os.listdir(EPHE_DIR))
    return {"sepl_18.se1", "semo_18.se1"} <= names


def used_swieph(retflag: int) -> bool:
    """由 swe.calc_ut 的回傳旗標判斷這次計算是否真的用了完整星曆檔（而非退回 Moshier）。"""
    return bool(retflag & swe.FLG_SWIEPH)


def require_full_ephemeris(retflag: int, *, operation: str, jd_ut: float) -> None:
    """Fail closed when a file-backed calculation silently changes ephemeris source."""
    if not used_swieph(retflag) or retflag & swe.FLG_MOSEPH:
        raise FullEphemerisRequiredError(
            operation=operation,
            jd_ut=jd_ut,
            retflag=retflag,
        )
