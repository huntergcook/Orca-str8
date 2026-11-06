"""`orca`: one short command for everything day to day.

    orca init [FOLDER] [--goal ... --done-when ... --start]   set a project up (see `orca init -h`)
    orca start | stop | status | install                       the unattended daemon
    orca report | board | intake | ...                          anything `python -m orchestrator.ctl` offers
Run it from the project folder (or pass --project DIR before the command)."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    project: list[str] = []
    if len(args) >= 2 and args[0] == "--project":
        project, args = ["--project", args[1]], args[2:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = args[0], args[1:]
    if cmd == "init":
        from .init import main as init_main
        return init_main(rest)
    if cmd in ("start", "stop", "status", "install", "run", "supervise"):
        from .daemon import main as daemon_main
        return daemon_main([*project, cmd, *rest])
    from .ctl import main as ctl_main
    return ctl_main([*project, cmd, *rest])


if __name__ == "__main__":
    raise SystemExit(main())
