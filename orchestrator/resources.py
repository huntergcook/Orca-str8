"""Resource-aware parallelism: use the machine fully, but back off before it thrashes.

`parallel_cap` returns how many tickets may run at once right now: the configured `max_parallel_tickets`, reduced when the
CPU is saturated or free memory is low (and never below 1). Stdlib only; on a platform where a reading is unavailable that
check is skipped."""
from __future__ import annotations

import os
import sys
from typing import Any


def free_memory_mb() -> float | None:
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tp", ctypes.c_ulonglong),
                            ("ap", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                            ("tx", ctypes.c_ulonglong), ("ax", ctypes.c_ulonglong), ("ex", ctypes.c_ulonglong)]
            m = MS()
            m.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))   # type: ignore[attr-defined]
            return m.ap / 2**20
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return None


def cpu_load_fraction() -> float | None:
    """1-minute load average per core (POSIX). None where unavailable (Windows)."""
    try:
        return os.getloadavg()[0] / (os.cpu_count() or 1)
    except (AttributeError, OSError):
        return None


def parallel_cap(cfg: dict[str, Any], base: int | None = None, mem_mb: float | None = None, load: float | None = None) -> tuple[int, str]:
    pol = cfg["policies"]
    base = int(base if base is not None else pol.get("max_parallel_tickets", 8))
    mem = free_memory_mb() if mem_mb is None else mem_mb
    ld = cpu_load_fraction() if load is None else load
    cap, why = base, ""
    min_free = float(pol.get("min_free_memory_mb", 1500))
    if mem is not None and mem < min_free:
        cap, why = max(1, base // 2), f"free memory {mem:.0f} MB < {min_free:.0f} MB"
        if mem < min_free / 2:
            cap = 1
    max_load = float(pol.get("max_cpu_load", 1.5))
    if ld is not None and ld > max_load:
        cap, why = max(1, min(cap, base // 2)), f"cpu load {ld:.1f}/core > {max_load}"
    return max(1, cap), why
