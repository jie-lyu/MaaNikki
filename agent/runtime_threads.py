"""Thread-pool bounds and orphan-exit protection for embedded Python workers."""
from __future__ import annotations

import os
import sys

from host import platform_backend


def configure_env_thread_limits(max_threads: int = 2) -> int:
    """Cap OpenMP / BLAS thread pools before importing native scientific libraries."""
    cpus = os.cpu_count() or max_threads
    limit = max(1, min(int(max_threads), int(cpus)))
    value = str(limit)
    os.environ.setdefault("OMP_NUM_THREADS", value)
    os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", value)
    os.environ.setdefault("MKL_NUM_THREADS", value)
    os.environ.setdefault("NUMEXPR_NUM_THREADS", value)
    return limit


def cap_loaded_maafw_opencv(max_threads: int = 2) -> bool:
    """Limit MaaFramework's OpenCV worker pool where the host can (Windows); False elsewhere."""
    if platform_backend() != "win32":
        return False
    from host.win32 import process
    return process.cap_loaded_maafw_opencv(max_threads)


def configure_utf8_streams() -> None:
    """Write stdout/stderr as UTF-8, replacing what cannot be encoded instead of raising."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def configure_worker_threads(max_threads: int = 2) -> int:
    """Cap native OpenCV, OpenMP, and BLAS pools to avoid CONCRT140 wake storms."""
    limit = configure_env_thread_limits(max_threads)
    try:
        import cv2
        cv2.setNumThreads(limit)
    except Exception:
        pass
    cap_loaded_maafw_opencv(limit)
    return limit


def start_parent_exit_watchdog(on_parent_exit=None) -> bool:
    """Exit promptly if the parent host disappears (Windows); False where unavailable."""
    if platform_backend() != "win32":
        return False
    from host.win32 import process
    return process.start_parent_exit_watchdog(on_parent_exit)
