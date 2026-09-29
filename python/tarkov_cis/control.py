"""Wiring and lifecycle: build the keeper, run it, and control the scheduled task.

Cross-process coordination uses two named kernel objects:

* the keeper holds ``KEEPER_MUTEX`` for its whole life, so "is it running?"
  is a single OpenMutex call with no PID files to go stale;
* ``STOP_EVENT`` is created by the keeper and set by ``stop()``; the keeper
  then removes its routes, disconnects and exits on its own.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from . import catalog as catalogs
from .config import DEFAULT_TARGET_HOSTS, Config, load_or_create, load_readonly, project_root, state_dir
from .game import GameProbe
from .keeper import Keeper, Status
from .logfile import RotatingLog, tail
from .models import Relay
from .relays import KnownGood, order_candidates
from .routes import RouteManager
from .softether import SoftEther
from .targets import authorization_ips, log_roots
from .winapi import kernel
from .winapi.schtasks import TaskAction, TaskScheduler

KEEPER_MUTEX = r"Local\TarkovCIS-Keeper"
STOP_EVENT = r"Local\TarkovCIS-Stop"
# Files the pre-refactor keeper left behind; removed on install.
LEGACY_STATE_FILES = ("keeper.pid.json", "stop.request.json", "keeper-status.json", "failures.json")


def status_path() -> Path:
    return state_dir() / "status.json"


def log_path() -> Path:
    return state_dir() / "keeper.log"


def routes_path() -> Path:
    return state_dir() / "routes.json"


# --- wiring -------------------------------------------------------------------


def make_vpn(config: Config) -> SoftEther:
    return SoftEther(
        vpncmd_path=config.vpncmd_path,
        account_name=config.account_name,
        interface_alias=config.vpn_interface_alias,
        nic_name=config.nic_name,
    )


def native_cache_path() -> Path:
    return state_dir() / "VPNGate.dat"


def known_good_path() -> Path:
    return state_dir() / "known-good.json"


def _native_relays(config: Config) -> list[Relay]:
    """The full VPN Gate list: download it, else fall back to the newest local copy."""

    try:
        data = catalogs.download_native_catalog(config.discovery_timeout_seconds, native_cache_path())
        return list(catalogs.parse_native_catalog(data, max_age_hours=config.native_catalog_max_age_hours))
    except Exception as download_error:
        error: Exception = download_error
    for path in (native_cache_path(), *config.native_catalog_files):
        try:
            return list(catalogs.read_native_catalog(path, max_age_hours=config.native_catalog_max_age_hours))
        except FileNotFoundError:
            continue
        except Exception as exc:
            error = exc
    raise error


def load_catalog(config: Config, log=lambda _message: None) -> list[Relay]:
    """Both live catalogs; one failing is fine, both failing is an error."""

    relays: list[Relay] = []
    errors: list[str] = []
    for name, source in (
        ("VPNGate.dat", lambda: _native_relays(config)),
        ("HTTPS", lambda: catalogs.download_https_catalog(config.discovery_timeout_seconds)),
    ):
        try:
            relays.extend(source())
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    for error in errors:
        log(f"[warning] 节点目录 {error}")
    if not relays and errors:
        raise RuntimeError("; ".join(errors))
    return relays


def make_known_good(config: Config) -> KnownGood:
    return KnownGood(known_good_path(), lifetime_hours=config.known_good_lifetime_hours)


def build_keeper(config: Config, *, log, publish) -> Keeper:
    return Keeper(
        config,
        vpn=make_vpn(config),
        routes=RouteManager(routes_path()),
        catalog=lambda: load_catalog(config, log),
        targets=lambda: authorization_ips(config.target_hosts + DEFAULT_TARGET_HOSTS, config.game_log_roots),
        game=GameProbe(
            lambda: log_roots(config.game_log_roots),
            max_age_days=config.game_phase_max_age_days,
            max_files=config.game_phase_max_files,
            max_bytes_per_file=config.game_phase_max_bytes_per_file,
        ),
        known_good=make_known_good(config),
        log=log,
        publish=publish,
    )


def _write_status(status: Status) -> None:
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


# --- the keeper process ---------------------------------------------------------


def run_keeper(config_path: Path) -> int:
    """Foreground keeper; this is what the scheduled task executes."""

    config = load_or_create(config_path)
    if kernel.owns_console():
        # The frozen EXE is a console program; started by Task Scheduler it
        # gets a console of its own that would stay open for its whole life.
        # A terminal the user runs `run` from is shared and is left alone.
        kernel.detach_console()
    with kernel.NamedMutex(KEEPER_MUTEX) as mutex:
        if not mutex.acquired:
            print("后台任务已经在运行。")
            return 0
        stop = kernel.NamedEvent(STOP_EVENT)
        try:
            keeper = build_keeper(config, log=RotatingLog(log_path()), publish=_write_status)
            keeper.run(stop)
        finally:
            stop.close()
    return 0


def keeper_running() -> bool:
    return kernel.mutex_exists(KEEPER_MUTEX)


# --- scheduled task control -------------------------------------------------------


def task_action(config_path: Path) -> TaskAction:
    root = project_root()
    arguments = ["run", "--config", str(config_path.resolve())]
    if getattr(sys, "frozen", False):
        executable = sys.executable
    else:
        # pythonw.exe: the keeper must not own a console window for its lifetime.
        interpreter = Path(sys.executable)
        windowless = interpreter.with_name("pythonw.exe")
        executable = str(windowless if windowless.is_file() else interpreter)
        arguments.insert(0, str(root / "Tarkov-CIS-Python.py"))
    return TaskAction(executable, subprocess.list2cmdline(arguments), str(root))


def _require_admin() -> None:
    if not kernel.is_admin():
        raise PermissionError("需要管理员权限：请用管理员身份运行，或使用图形界面。")


def _wait(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return predicate()


def start(config_path: Path) -> str:
    _require_admin()
    config = load_or_create(config_path)
    scheduler = TaskScheduler(config.task_name)
    if keeper_running():
        return f"后台任务 {config.task_name} 已经在运行。"
    # A pre-refactor keeper uses different kernel object names; end it the
    # blunt way and clean up its routes before installing the new task.
    scheduler.end()
    offline_cleanup(config)
    for name in LEGACY_STATE_FILES:
        (state_dir() / name).unlink(missing_ok=True)
    scheduler.install(task_action(config_path), description="Tarkov CIS 鉴权分流后台任务")
    scheduler.start()
    if not _wait(keeper_running, 20):
        raise RuntimeError("计划任务已启动，但 20 秒内没有检测到后台进程；请查看日志。")
    return f"后台任务 {config.task_name} 已启动，节点连接在后台进行。"


def offline_cleanup(config: Config) -> list[str]:
    """Remove recorded routes and disconnect when no keeper is running."""

    errors: list[str] = []
    try:
        RouteManager(routes_path()).cleanup()
    except Exception as exc:
        errors.append(str(exc))
    try:
        make_vpn(config).disconnect(timeout=config.disconnect_wait_seconds)
    except Exception as exc:
        errors.append(str(exc))
    return errors


def stop(config_path: Path) -> str:
    _require_admin()
    config = load_readonly(config_path)
    if keeper_running():
        kernel.signal_event(STOP_EVENT)
        # A connect attempt in progress finishes first (bounded by its timeout).
        grace = config.connect_timeout_seconds + config.disconnect_wait_seconds + 15
        if not _wait(lambda: not keeper_running(), grace):
            TaskScheduler(config.task_name).end()
            _wait(lambda: not keeper_running(), 10)
    errors = offline_cleanup(config)
    if errors:
        raise RuntimeError("停止后清理不完整: " + "; ".join(errors))
    return "后台任务已停止，临时路由已撤销，VPN 已断开。"


def uninstall(config_path: Path) -> str:
    message = stop(config_path)
    TaskScheduler(load_readonly(config_path).task_name).delete()
    return message + " 计划任务已删除。"


# --- read-only views ----------------------------------------------------------------


def status(config_path: Path) -> dict[str, Any]:
    config = load_readonly(config_path)
    running = keeper_running()
    snapshot: dict[str, Any] = {}
    if running:
        try:
            snapshot = json.loads(status_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            snapshot = {}
    try:
        # iphlpapi only: safe to read even mid-match, never touches SoftEther.
        lease = make_vpn(config).adapter_lease()
    except OSError:
        lease = None
    return {
        "task_installed": TaskScheduler(config.task_name).exists(),
        "keeper_running": running,
        "phase": snapshot.get("phase", "stopped" if not running else "starting"),
        "detail": snapshot.get("detail", "后台未运行" if not running else "正在启动"),
        "relay": snapshot.get("relay", ""),
        "country": snapshot.get("country", ""),
        "game_phase": snapshot.get("game_phase", ""),
        "game_detail": snapshot.get("game_detail", ""),
        "vpn_ipv4": lease.ipv4 if lease else "",
        "route_count": len(RouteManager(routes_path()).owned),
        "updated_at": snapshot.get("updated_at", ""),
    }


def candidates(config_path: Path) -> list[Relay]:
    config = load_readonly(config_path)
    return list(
        order_candidates(
            make_known_good(config).merge(load_catalog(config)),
            per_country=config.max_candidates_per_country,
            total=config.max_candidates_total,
        )
    )


def keeper_log() -> str:
    return tail(log_path())
