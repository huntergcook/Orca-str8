"""Windows (and other OS) helpers that keep an unattended run alive. Everything is a safe no-op where it does not
apply, and nothing here may ever stop the harness from running.

  keep_awake()            ask Windows not to sleep while the daemon runs (SetThreadExecutionState)
  kill_children_on_exit() put this process in a Job Object so every child (claude, codex, jobs) dies with it,
                          instead of lingering as orphans after a crash
  service_command()       the command line that starts the supervised daemon for a project
  task_scheduler_args()   schtasks arguments that start it at logon and keep it running (Windows)
  systemd_unit()          the equivalent unit file (Linux)

The ctypes calls are only exercised on Windows.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)   # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def allow_sleep() -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)   # type: ignore[attr-defined]
        except Exception:
            pass


def kill_children_on_exit() -> bool:
    """Assign this process to a Job Object with KILL_ON_JOB_CLOSE (Windows). Children inherit the job, so when the
    daemon dies they are terminated with it and cannot pile up. Returns True if the job was set up."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        # private WinDLL with explicit types: default int marshalling mangles 64-bit handles (GetCurrentProcess() is -1)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)                    # type: ignore[attr-defined]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IOC(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("a", "b", "c", "d", "e", "f")]

        class EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        info = EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000                          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):   # 9 = ExtendedLimitInformation
            return False
        if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            return False
        globals()["_JOB"] = job                                                 # keep the handle alive for the process lifetime
        return True
    except Exception:
        return False


def service_command(project: str | Path) -> list[str]:
    """The supervised daemon for `project` (re-launches the daemon if it ever exits without finishing)."""
    return [sys.executable, "-P", "-m", "orchestrator.daemon", "--project", str(Path(project).resolve()), "supervise"]


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) else arg


def task_scheduler_args(project: str | Path, name: str = "OrcaDaemon") -> list[str]:
    """`schtasks` arguments that start the supervisor at every logon of the current user. Logon (not boot) because the
    claude/codex CLIs keep their sign-in in the user's profile. Restart-on-failure is added with the XML/settings the
    README describes; the supervisor itself already restarts the daemon, so the task only has to start it."""
    cmd = " ".join(_quote(a) for a in service_command(project))
    return ["schtasks", "/Create", "/F", "/TN", name, "/SC", "ONLOGON", "/RL", "LIMITED", "/TR", cmd]


def systemd_unit(project: str | Path) -> str:
    cmd = " ".join(_quote(a) for a in service_command(project))
    return (f"[Unit]\nDescription=Orca daemon for {project}\nAfter=network-online.target\n\n[Service]\n"
            f"WorkingDirectory={Path(project).resolve()}\nExecStart={cmd}\nRestart=always\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n")
