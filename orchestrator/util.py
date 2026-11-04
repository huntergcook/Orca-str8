"""Crash/power-loss safe file helpers and process identity checks."""
from __future__ import annotations

import os
from pathlib import Path

import psutil


def atomic_write(path: str | Path, text: str) -> None:
    """Write a file so a power cut leaves either the old or the new content, never a torn mix:
    write a temp file, flush it to disk, then atomically replace the target."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def append_durable(path: str | Path, text: str) -> None:
    """Append and force the data to disk, so an acknowledged journal entry survives a power cut."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


def process_stamp(pid: int | None = None) -> str:
    """'pid:start_time' for a process (default: this one). The start time makes the identity unique
    even after the OS reuses the pid (which happens after a reboot)."""
    p = psutil.Process(pid) if pid else psutil.Process()
    return f"{p.pid}:{p.create_time():.2f}"


def process_alive(pid: int | None, started: float | None = None) -> bool:
    """True if `pid` is a live process and, when `started` is given, is the SAME process that started
    then (not an unrelated one that was assigned the same pid after a reboot)."""
    if not pid:
        return False
    try:
        p = psutil.Process(pid)
        if p.status() == psutil.STATUS_ZOMBIE:
            return False
        return started is None or abs(p.create_time() - started) <= 2.0
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return psutil.pid_exists(pid)


def parse_stamp(text: str) -> tuple[int | None, float | None]:
    """Inverse of process_stamp; also accepts a bare pid from older lock files."""
    pid_s, _, started_s = text.strip().partition(":")
    try:
        return int(pid_s), (float(started_s) if started_s else None)
    except ValueError:
        return None, None
