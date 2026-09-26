"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Iterable

from . import control
from .config import DEFAULT_CONFIG_PATH, project_root
from .winapi import kernel

COMMANDS = ("gui", "start", "stop", "uninstall", "status", "candidates", "run")


def _tolerate_unencodable_output() -> None:
    """Keep non-Chinese code pages (e.g. Japanese cp932) from crashing on print."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):
                pass


def _gui(config_path: Path) -> int:
    if not kernel.is_admin():
        # Relaunch this same program elevated; the GUI manages routes and tasks.
        if getattr(sys, "frozen", False):
            arguments = ["gui", "--config", str(config_path.resolve())]
        else:
            arguments = [str(project_root() / "Tarkov-CIS-Python.py"), "gui", "--config", str(config_path.resolve())]
        if kernel.relaunch_elevated(sys.executable, subprocess.list2cmdline(arguments), str(project_root())):
            return 0
        raise PermissionError("需要管理员权限才能管理路由和后台任务；UAC 已取消。")
    kernel.detach_console()
    from .gui import GuiCallbacks, run_gui

    return run_gui(
        GuiCallbacks(
            start=lambda: control.start(config_path),
            stop=lambda: control.stop(config_path),
            uninstall=lambda: control.uninstall(config_path),
            status=lambda: control.status(config_path),
            tail_log=control.keeper_log,
        )
    )


def _candidates(config_path: Path) -> int:
    relays = control.candidates(config_path)
    for number, relay in enumerate(relays, start=1):
        print(
            f"{number:2d}. {relay.country} {relay.endpoint:<24} "
            f"ping={relay.ping}ms speed={relay.speed_mbps:g}Mbps source={relay.source}"
        )
    if not relays:
        print("当前没有 CIS 候选节点。")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    _tolerate_unencodable_output()
    parser = argparse.ArgumentParser(prog="tarkov-cis")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "gui":
            return _gui(args.config)
        if args.command == "run":
            return control.run_keeper(args.config)
        if args.command == "status":
            print(json.dumps(control.status(args.config), ensure_ascii=False, indent=2))
            return 0
        if args.command == "candidates":
            return _candidates(args.config)
        action = {"start": control.start, "stop": control.stop, "uninstall": control.uninstall}[args.command]
        print(action(args.config))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
