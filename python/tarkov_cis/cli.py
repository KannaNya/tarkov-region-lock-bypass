"""Command-line entry points for the Python Windows application."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Iterable
from urllib.request import Request, urlopen

from .catalog import NativeCatalogReader, parse_vpngate_csv
from .config import AppConfig
from .keeper import KeeperService
from .health import probe_authorization
from .game_phase import PlayProtectionActivated, configured_game_phase_probe
from .log_sink import RotatingLogSink, tail_log
from .models import FailureRecord, Relay
from .process_runner import run_command
from .relay_selector import (
    merge_recent_known_good,
    plan_failover_cycle,
    select_relay_candidates,
)
from .routing import RouteManager
from .softether import DEFAULT_VPNCMD, SoftEtherClient


def _runtime_root() -> Path:
    """Return the writable bundle/repository root for source and frozen builds."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


REPOSITORY_ROOT = _runtime_root()
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "config.json"
EXAMPLE_CONFIG_PATH = REPOSITORY_ROOT / "config.example.json"
VPNGATE_API = "https://www.vpngate.net/api/iphone/"


def _local_state_directory() -> Path:
    return Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    ) / "TarkovCIS"


class LiveCandidateProvider:
    """Combine both live catalogs, endpoint cooldown and recent known-good state."""

    def __init__(self, config: AppConfig, *, state_dir: Path | None = None) -> None:
        self.config = config
        self.state_dir = state_dir or _local_state_directory()
        self.failure_path = self.state_dir / "failures.json"
        self.known_good_path = self.state_dir / "known-good.json"
        self._failures = self._load_failures()
        self._known_good = self._load_known_good()

    @staticmethod
    def _relay_from_mapping(item: Any) -> Relay:
        if not isinstance(item, dict):
            raise TypeError("relay entry must be an object")
        verified = item.get("verified_at")
        return Relay(
            host_name=str(item.get("host_name", "")),
            ip=str(item["ip"]),
            port=int(item["port"]),
            country_short=str(item["country_short"]),
            country_long=str(item.get("country_long", "")),
            score=int(item.get("score", 0)),
            ping=int(item.get("ping", 9999)),
            speed_mbps=float(item.get("speed_mbps", 0.0)),
            sessions=int(item.get("sessions", 0)),
            source="RecentKnownGood",
            source_priority=0,
            verified_at=datetime.fromisoformat(str(verified)) if verified else None,
        )

    @staticmethod
    def _relay_mapping(relay: Relay) -> dict[str, Any]:
        return {
            "host_name": relay.host_name,
            "ip": relay.ip,
            "port": relay.port,
            "country_short": relay.country_short,
            "country_long": relay.country_long,
            "score": relay.score,
            "ping": relay.ping,
            "speed_mbps": relay.speed_mbps,
            "sessions": relay.sessions,
            "verified_at": relay.verified_at.isoformat() if relay.verified_at else None,
        }

    def _load_failures(self) -> list[FailureRecord]:
        if not self.failure_path.is_file():
            return []
        try:
            raw = json.loads(self.failure_path.read_text(encoding="utf-8-sig"))
            failures = [
                FailureRecord(
                    endpoint=item["endpoint"],
                    failed_at=datetime.fromisoformat(item["failed_at"]),
                    reason=item.get("reason", ""),
                    country_short=item.get("country_short", ""),
                    host_name=item.get("host_name", ""),
                )
                for item in raw
            ]
            cutoff = datetime.now(timezone.utc) - timedelta(days=2)
            return [item for item in failures if item.failed_at >= cutoff]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return []

    def _load_known_good(self) -> list[Relay]:
        if not self.known_good_path.is_file():
            return []
        try:
            raw = json.loads(self.known_good_path.read_text(encoding="utf-8-sig"))
            relays = [self._relay_from_mapping(item) for item in raw]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self.config.known_good_lifetime_hours
        )
        return [
            relay
            for relay in relays
            if relay.verified_at is not None and relay.verified_at >= cutoff
        ]

    def _save_failures(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.now(timezone.utc) - timedelta(days=2)
        self._failures = [item for item in self._failures if item.failed_at >= cutoff]
        payload = [
            {
                "endpoint": item.endpoint,
                "failed_at": item.failed_at.isoformat(),
                "reason": item.reason,
                "country_short": item.country_short,
                "host_name": item.host_name,
            }
            for item in sorted(self._failures, key=lambda value: value.failed_at)[-200:]
        ]
        temporary = self.failure_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.failure_path)

    def _save_known_good(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=self.config.known_good_lifetime_hours
        )
        self._known_good = [
            relay
            for relay in self._known_good
            if relay.verified_at is not None and relay.verified_at >= cutoff
        ]
        payload = [
            self._relay_mapping(relay)
            for relay in sorted(
                self._known_good,
                key=lambda value: value.verified_at or datetime.min.replace(tzinfo=timezone.utc),
            )[-200:]
        ]
        temporary = self.known_good_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.known_good_path)

    def _https_relays(self) -> tuple[Relay, ...]:
        outcomes: queue.Queue[bytes | Exception] = queue.Queue(maxsize=1)

        def download() -> None:
            try:
                request = Request(
                    f"{VPNGATE_API}?t={secrets.token_hex(8)}",
                    headers={
                        "User-Agent": "Tarkov-CIS-Python/2.0",
                        "Cache-Control": "no-cache, no-store",
                        "Pragma": "no-cache",
                    },
                )
                with urlopen(
                    request, timeout=self.config.discovery_timeout_seconds
                ) as response:
                    # Refuse an unexpectedly unbounded payload from a
                    # compromised intermediary.
                    outcomes.put(response.read(32 * 1024 * 1024 + 1))
            except Exception as exc:
                outcomes.put(exc)

        threading.Thread(
            target=download,
            name="TarkovCisVpnGateHttps",
            daemon=True,
        ).start()
        try:
            outcome = outcomes.get(timeout=self.config.discovery_timeout_seconds)
        except queue.Empty as exc:
            raise RuntimeError("VPN Gate HTTPS 目录读取超过总时限") from exc
        if isinstance(outcome, Exception):
            raise outcome
        payload = outcome
        if len(payload) > 32 * 1024 * 1024:
            raise RuntimeError("VPN Gate catalog exceeded the 32 MiB safety limit")
        return parse_vpngate_csv(payload.decode("utf-8-sig"))

    def _native_relays(self) -> tuple[Relay, ...]:
        # The bundled SoftEther VPN Gate plug-in often has a richer live list.
        # Import lazily so the HTTPS path remains usable on installations that
        # do not ship the optional native reader.
        vpncmd = Path(self.config.vpncmd_path)
        native_path = (
            Path(self.config.native_catalog_path)
            if self.config.native_catalog_path
            else vpncmd.parent / "VPNGate.dat"
        )
        try:
            catalog = NativeCatalogReader().read(
                native_path,
                max_age_hours=self.config.native_catalog_max_age_hours,
            )
            return tuple(catalog.relays)
        except (OSError, ValueError, RuntimeError):
            return ()

    def _catalog_relays(self) -> tuple[Relay, ...]:
        live: dict[str, Relay] = {}
        errors: list[Exception] = []
        try:
            for relay in self._https_relays():
                live[relay.endpoint] = relay
        except Exception as exc:
            errors.append(exc)
        for relay in self._native_relays():
            previous = live.get(relay.endpoint)
            if previous is None or relay.source_priority > previous.source_priority:
                live[relay.endpoint] = relay
        merged = merge_recent_known_good(
            live.values(),
            self._known_good,
            lifetime=timedelta(hours=self.config.known_good_lifetime_hours),
        )
        if not merged:
            detail = f": {errors[0]}" if errors else ""
            raise RuntimeError(f"实时目录和近期成功记录都没有可用 CIS TCP 节点{detail}")
        return merged

    def candidates(self) -> tuple[Relay, ...]:
        """Return one connection batch, including bounded all-cooling fallback."""

        selection = plan_failover_cycle(
            self._catalog_relays(),
            self._failures,
            per_country_limit=self.config.max_candidates_per_country,
            total_limit=self.config.max_candidates_total,
            cooldown=timedelta(minutes=self.config.failure_cooldown_minutes),
            fallback_age=timedelta(minutes=self.config.cooling_fallback_minutes),
            fallback_limit=self.config.cooling_fallback_candidates,
        )
        return selection.candidates

    def list_candidates(self) -> tuple[Relay, ...]:
        """Read-only normal candidate view; never exposes cooling fallback."""

        return select_relay_candidates(
            self._catalog_relays(),
            self._failures,
            per_country_limit=self.config.max_candidates_per_country,
            total_limit=self.config.max_candidates_total,
            cooldown=timedelta(minutes=self.config.failure_cooldown_minutes),
        ).candidates

    def record_failure(self, relay: Relay, reason: str) -> None:
        failure = FailureRecord(
            endpoint=relay.endpoint,
            failed_at=datetime.now(timezone.utc),
            reason=reason,
            country_short=relay.country_short,
            host_name=relay.host_name,
        )
        self._failures = [
            item for item in self._failures if item.endpoint != failure.endpoint
        ] + [failure]
        self._save_failures()

    def record_success(self, relay: Relay) -> None:
        """Clear endpoint cooldown and retain a short-lived verified fallback."""

        now = datetime.now(timezone.utc)
        known = Relay(
            host_name=relay.host_name,
            ip=relay.ip,
            port=relay.port,
            country_short=relay.country_short,
            country_long=relay.country_long,
            score=relay.score,
            ping=relay.ping,
            speed_mbps=relay.speed_mbps,
            sessions=relay.sessions,
            source="RecentKnownGood",
            source_priority=0,
            verified_at=now,
        )
        self._failures = [item for item in self._failures if item.endpoint != relay.endpoint]
        self._known_good = [
            item for item in self._known_good if item.endpoint != relay.endpoint
        ] + [known]
        self._save_failures()
        self._save_known_good()


def _ensure_config(path: Path) -> AppConfig:
    if not path.exists():
        if not EXAMPLE_CONFIG_PATH.is_file():
            raise FileNotFoundError(f"找不到配置和示例配置: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(EXAMPLE_CONFIG_PATH.read_bytes())
    return AppConfig.load(path)


def _read_config(path: Path) -> AppConfig:
    """Load configuration without creating files for read-only commands."""

    if path.is_file():
        return AppConfig.load(path)
    if EXAMPLE_CONFIG_PATH.is_file():
        return AppConfig.load(EXAMPLE_CONFIG_PATH)
    raise FileNotFoundError(f"找不到配置和示例配置: {path}")


def _write_keeper_status(
    status: Any,
    *,
    pid: int,
    generation: str,
    process_created: str,
) -> None:
    state_dir = _local_state_directory()
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "keeper-status.json"
    payload = {
        "pid": int(pid),
        "generation": generation,
        "process_created": process_created,
        "phase": status.phase.value,
        "connection_phase": status.connection_phase.value,
        "detail": status.detail,
        "relay": status.relay,
        "country": status.country,
        "vpn_ipv4": status.vpn_ipv4,
        "target_count": status.target_count,
        "failed_candidates": status.failed_candidates,
        "game_phase": status.game_phase,
        "game_evidence": status.game_evidence,
        "play_protected": status.play_protected,
        "updated_at": status.updated_at.isoformat(),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def build_runtime(config: AppConfig, logger=print, status_sink=None) -> KeeperService:
    vpncmd = Path(config.vpncmd_path or DEFAULT_VPNCMD)
    state_dir = _local_state_directory()
    softether = SoftEtherClient(
        vpncmd_path=vpncmd,
        account_name=config.account_name,
        interface_alias=config.vpn_interface_alias,
        nic_name=config.nic_name,
    )
    routes = RouteManager(state_path=state_dir / "routes.json")
    provider = LiveCandidateProvider(config, state_dir=state_dir)
    return KeeperService(
        config=config,
        candidate_provider=provider,
        softether=softether,
        routes=routes,
        logger=logger,
        status_sink=status_sink,
        health_probe=probe_authorization,
        game_phase_probe=configured_game_phase_probe(config),
    )


def _powershell_executable() -> str:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    built_in = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(built_in if built_in.is_file() else "powershell.exe")


def _task_script_path() -> Path:
    return REPOSITORY_ROOT / "scripts" / "python-task.ps1"


def _invoke_task_action(action: str, config_path: Path, *, timeout: float = 90.0) -> None:
    script = _task_script_path()
    if not script.is_file():
        raise FileNotFoundError(f"找不到后台任务控制脚本: {script}")
    result = run_command(
        [
            _powershell_executable(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Action",
            action,
            "-ConfigPath",
            str(config_path.resolve()),
        ],
        timeout=timeout,
        cwd=REPOSITORY_ROOT,
    )
    if not result.ok:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"后台任务 {action} 失败（退出码 {result.exit_code}）"
            + (f": {detail}" if detail else "")
        )


def _scheduled_task_info(task_name: str) -> dict[str, str]:
    if os.name != "nt":
        return {"state": "Unsupported", "implementation": "unknown"}
    script = (
        "$ErrorActionPreference='Stop';"
        "$t=Get-ScheduledTask -TaskName $env:TARKOV_CIS_TASK_NAME "
        "-ErrorAction SilentlyContinue;"
        "if($null -eq $t){"
        "[pscustomobject]@{State='NotInstalled';Arguments='';Execute=''}|ConvertTo-Json -Compress"
        "}else{"
        "$a=@($t.Actions|ForEach-Object{[string]$_.Arguments}) -join ' ';"
        "$e=@($t.Actions|ForEach-Object{[string]$_.Execute}) -join ' ';"
        "[pscustomobject]@{State=[string]$t.State;Arguments=$a;Execute=$e}|ConvertTo-Json -Compress}"
    )
    environment = os.environ.copy()
    environment["TARKOV_CIS_TASK_NAME"] = task_name
    try:
        result = run_command(
            [_powershell_executable(), "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=10.0,
            env=environment,
        )
        if not result.ok or not result.stdout.strip():
            raise RuntimeError("scheduled task query returned no data")
        payload = json.loads(result.stdout)
        arguments = str(payload.get("Arguments", "")).lower()
        executables = str(payload.get("Execute", "")).lower()
        if (
            "python-task.ps1" in arguments
            or "tarkov-cis-python.py" in arguments
            or "tarkovcis.exe" in arguments
            or "tarkovcis.exe" in executables
        ):
            implementation = "python"
        elif "tarkov-cisroutekeeper.ps1" in arguments:
            implementation = "legacy-powershell"
        else:
            implementation = "unknown"
        return {
            "state": str(payload.get("State", "Unknown")),
            "implementation": implementation,
        }
    except Exception:
        return {"state": "Unknown", "implementation": "unknown"}


def _is_administrator() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _request_elevated_gui(config_path: Path) -> bool:
    if os.name != "nt":
        return False
    import ctypes

    if getattr(sys, "frozen", False):
        executable = str(Path(sys.executable).resolve())
        arguments = ["gui", "--config", str(config_path.resolve())]
    else:
        executable = str(Path(sys.executable).resolve())
        arguments = [
            str(REPOSITORY_ROOT / "Tarkov-CIS-Python.py"),
            "gui",
            "--config",
            str(config_path.resolve()),
        ]
    ctypes.windll.shell32.ShellExecuteW.restype = ctypes.c_void_p
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        executable,
        subprocess.list2cmdline(arguments),
        str(REPOSITORY_ROOT),
        1,
    )
    return bool(result) and int(result) > 32


def _detach_console_for_gui() -> None:
    """Detach the GUI process without hiding a terminal shared with its parent."""

    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.FreeConsole()
    except (AttributeError, OSError):
        pass


def _control_paths() -> tuple[Path, Path]:
    directory = _local_state_directory()
    return directory / "keeper.pid.json", directory / "stop.request.json"


class _NamedMutex:
    """Windows cross-process single-instance guard with a no-op test fallback."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.handle: int | None = None
        self.acquired = False

    def __enter__(self) -> "_NamedMutex":
        if os.name != "nt":
            self.acquired = True
            return self
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, True, self.name)
        if not handle:
            raise OSError("无法创建后台任务单实例锁")
        self.handle = handle
        self.acquired = kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
        if not self.acquired:
            kernel32.CloseHandle(handle)
            self.handle = None
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self.handle is None or os.name != "nt":
            return
        import ctypes

        try:
            if self.acquired:
                ctypes.windll.kernel32.ReleaseMutex(self.handle)
        finally:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process = kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        exit_code = wintypes.DWORD()
        try:
            return bool(kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code))) and (
                exit_code.value == 259  # STILL_ACTIVE
            )
        finally:
            kernel32.CloseHandle(process)
    except (AttributeError, OSError):
        return False


def _process_creation_marker(pid: int) -> str | None:
    """Return an OS-backed marker that changes when a PID is reused."""

    if pid <= 0:
        return None
    if os.name != "nt":
        try:
            stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            after_name = stat_text[stat_text.rfind(")") + 2 :].split()
            # /proc stat field 22 is process start time; after_name starts at
            # field 3, so its zero-based index is 19.
            return f"proc-start:{after_name[19]}"
        except (OSError, IndexError, ValueError):
            return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process = kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return None
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        try:
            if not kernel32.GetProcessTimes(
                process,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            value = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
            return f"win-filetime:{value}"
        finally:
            kernel32.CloseHandle(process)
    except (AttributeError, OSError):
        return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _state_pid(state: dict[str, Any] | None) -> int:
    try:
        return int(state.get("pid", 0)) if state else 0
    except (TypeError, ValueError):
        return 0


def _pid_state_matches_live_process(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    pid = _state_pid(state)
    expected = str(state.get("process_created", ""))
    generation = str(state.get("generation", ""))
    token = str(state.get("token", ""))
    if pid <= 0 or not expected or not generation or not token:
        return False
    if not _process_is_running(pid):
        return False
    observed = _process_creation_marker(pid)
    return bool(observed) and secrets.compare_digest(expected, observed)


def _status_matches_pid_state(
    status: dict[str, Any] | None, pid_state: dict[str, Any] | None
) -> bool:
    """Bind a status heartbeat to one live keeper generation."""

    if not status or not pid_state or not status.get("updated_at"):
        return False
    return bool(
        _state_pid(status) == _state_pid(pid_state)
        and secrets.compare_digest(
            str(status.get("generation", "")),
            str(pid_state.get("generation", "")),
        )
        and secrets.compare_digest(
            str(status.get("process_created", "")),
            str(pid_state.get("process_created", "")),
        )
    )


def _stop_requested(path: Path, token: str) -> bool:
    request = _read_json(path)
    return bool(request and secrets.compare_digest(str(request.get("token", "")), token))


def _command_run_locked(config_path: Path) -> int:
    config = _ensure_config(config_path)
    pid_path, stop_path = _control_paths()
    existing = _read_json(pid_path)
    if existing and _process_is_running(_state_pid(existing)):
        # The named mutex is authoritative.  If it was acquired while this
        # PID still exists, the control file is stale (possibly PID reuse).
        pid_path.unlink(missing_ok=True)

    token = secrets.token_urlsafe(24)
    generation = secrets.token_hex(16)
    pid = os.getpid()
    process_created = _process_creation_marker(pid)
    if not process_created:
        raise RuntimeError("无法读取 Keeper 进程创建时间；拒绝发布不可验证的 PID 状态。")
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_temporary = pid_path.with_suffix(pid_path.suffix + ".tmp")
    pid_temporary.write_text(
        json.dumps(
            {
                "pid": pid,
                "token": token,
                "generation": generation,
                "process_created": process_created,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    pid_temporary.replace(pid_path)
    stale = _read_json(stop_path)
    if stale and stale.get("token") != token:
        stop_path.unlink(missing_ok=True)

    logger = RotatingLogSink(_local_state_directory() / "keeper.log")
    logger(f"keeper starting as PID {pid}, generation {generation}")
    service = build_runtime(
        config,
        logger=logger,
        status_sink=lambda status: _write_keeper_status(
            status,
            pid=pid,
            generation=generation,
            process_created=process_created,
        ),
    )

    def request_stop(_signum=None, _frame=None) -> None:
        service.request_stop()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)
    service.start()
    stop_succeeded = False
    stop_timeout = float(
        config.failover_timeout_seconds + config.disconnect_wait_seconds + 30
    )
    try:
        while service.running:
            if _stop_requested(stop_path, token):
                service.request_stop()
                break
            time.sleep(0.25)
    finally:
        try:
            service.stop(
                disconnect=True,
                timeout=stop_timeout,
                disconnect_timeout=float(config.disconnect_wait_seconds),
            )
            stop_succeeded = True
        except Exception as exc:
            logger(f"[error] 停止时清理不完整: {exc}")
            print(f"停止时清理不完整: {exc}", file=sys.stderr)
            service.request_stop()
            if service.running:
                # Do not return from command_run (and therefore release the
                # named mutex) while the keeper thread can still mutate the
                # VPN session or routes.  Every normal connection operation is
                # bounded by the shared failover deadline; if an implementation
                # bug violates that contract, keeping this process and mutex
                # alive is safer than starting a competing generation.
                logger("keeper thread is still stopping; retaining process ownership")
                while service.running:
                    time.sleep(0.25)
                try:
                    service.stop(
                        disconnect=True,
                        timeout=float(config.disconnect_wait_seconds) + 30.0,
                        disconnect_timeout=float(config.disconnect_wait_seconds),
                    )
                    stop_succeeded = True
                    logger("keeper cleanup succeeded after the worker finished")
                except Exception as retry_exc:
                    logger(f"[error] 工作线程结束后仍无法完成清理: {retry_exc}")
        if stop_succeeded:
            current = _read_json(pid_path)
            if current and secrets.compare_digest(str(current.get("token", "")), token):
                pid_path.unlink(missing_ok=True)
            if _stop_requested(stop_path, token):
                stop_path.unlink(missing_ok=True)
            logger("keeper process stopped cleanly")
        else:
            logger("keeper cleanup failed; PID/token retained for a later safe cleanup")
    return 0 if stop_succeeded else 1


def command_run(config_path: Path) -> int:
    with _NamedMutex(r"Global\Tarkov-CisRouteKeeper-Python") as mutex:
        if not mutex.acquired:
            print("后台任务已经运行。")
            return 0
        return _command_run_locked(config_path)


def status_snapshot(config_path: Path) -> dict[str, Any]:
    config = _read_config(config_path)
    task_info = _scheduled_task_info(config.task_name)
    pid_path, _ = _control_paths()
    pid_state = _read_json(pid_path)
    process_running = _pid_state_matches_live_process(pid_state)
    runtime = build_runtime(config, logger=lambda _message: None)
    persisted_raw = _read_json(_local_state_directory() / "keeper-status.json") or {}
    persisted_matches = bool(
        process_running and _status_matches_pid_state(persisted_raw, pid_state)
    )
    persisted = persisted_raw if persisted_matches else {}
    probe_paused = bool(persisted.get("play_protected", False))
    lease = None
    if not probe_paused:
        try:
            lease = runtime.softether.verified_connection()
        except PlayProtectionActivated:
            # Status/UI refreshes must not bypass the keeper's play guard.
            probe_paused = True
        except Exception:
            pass
    output: dict[str, Any] = {
        "task_name": config.task_name,
        "task_state": task_info["state"],
        "task_implementation": task_info["implementation"],
        "keeper_running": process_running,
        "pid": pid_state.get("pid") if process_running and pid_state else None,
        "generation": pid_state.get("generation") if process_running and pid_state else None,
        "status_stale": bool(persisted_raw) and not persisted_matches,
        "phase": persisted.get("phase"),
        "connection_phase": persisted.get("connection_phase"),
        "detail": persisted.get("detail"),
        "relay": persisted.get("relay"),
        "country": persisted.get("country"),
        "vpn_verified": None if probe_paused else lease is not None,
        "vpn_probe_paused": probe_paused,
        "vpn_interface": lease.interface_alias if lease else config.vpn_interface_alias,
        "vpn_ipv4": lease.ipv4 if lease else persisted.get("vpn_ipv4") if probe_paused else None,
        "managed_authorization_routes": len(runtime.routes.managed),
        "game_phase": persisted.get("game_phase"),
        "game_evidence": persisted.get("game_evidence"),
        "play_protected": bool(persisted.get("play_protected", False)),
    }
    return output


def command_status(config_path: Path) -> int:
    print(json.dumps(status_snapshot(config_path), ensure_ascii=False, indent=2))
    return 0


def command_cleanup(config_path: Path) -> int:
    """Internal graceful cleanup used by the task wrapper before task stop."""

    config = _ensure_config(config_path)
    pid_path, stop_path = _control_paths()
    pid_state = _read_json(pid_path)
    task_info = _scheduled_task_info(config.task_name)
    if (
        pid_state is None
        and task_info["state"].lower() == "running"
        and task_info["implementation"] == "python"
    ):
        # Close the narrow launch window in which Task Scheduler reports
        # Running but the keeper has not published its token yet.
        publish_deadline = time.monotonic() + 3.0
        while time.monotonic() < publish_deadline and pid_state is None:
            time.sleep(0.1)
            pid_state = _read_json(pid_path)
        task_info = _scheduled_task_info(config.task_name)
        if (
            pid_state is None
            and task_info["state"].lower() == "running"
            and task_info["implementation"] == "python"
        ):
            print(
                "后台任务仍在运行，但 PID/token 控制文件无效；为避免与 Keeper 竞态，已拒绝清理。",
                file=sys.stderr,
            )
            return 1
    if pid_state and _pid_state_matches_live_process(pid_state):
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_temporary = stop_path.with_suffix(stop_path.suffix + ".tmp")
        stop_temporary.write_text(
            json.dumps({"token": pid_state.get("token", "")}), encoding="utf-8"
        )
        stop_temporary.replace(stop_path)
        deadline = time.monotonic() + float(
            config.failover_timeout_seconds + config.disconnect_wait_seconds + 45
        )
        while time.monotonic() < deadline and pid_path.exists():
            time.sleep(0.25)
        if pid_path.exists():
            print("已请求安全停止，但后台仍在完成当前有界操作；未强制终止。", file=sys.stderr)
            return 1
    elif pid_state and _process_is_running(_state_pid(pid_state)):
        print(
            "PID 当前属于活进程，但创建时间/generation 与 Keeper 状态不匹配；拒绝竞态清理。",
            file=sys.stderr,
        )
        return 1
    elif pid_state:
        # The exact control file is stale; no process owns its token anymore.
        pid_path.unlink(missing_ok=True)

    # No process is racing us now: load the persisted exact ownership record
    # and clean only those routes/default metrics.
    runtime = build_runtime(config, logger=lambda _message: None)
    cleanup_errors: list[str] = []
    try:
        runtime.routes.cleanup()
    except Exception as exc:
        cleanup_errors.append(str(exc))
    try:
        runtime.softether.disconnect(timeout=float(config.disconnect_wait_seconds))
    except Exception as exc:
        cleanup_errors.append(str(exc))
    if cleanup_errors:
        print("停止后的清理不完整: " + "; ".join(cleanup_errors), file=sys.stderr)
        return 1
    print("已停止；本工具拥有的临时路由已撤销。")
    return 0


def command_start(config_path: Path) -> int:
    config = _ensure_config(config_path)
    _invoke_task_action("Install", config_path)
    startup_deadline = time.monotonic() + float(
        max(10, min(60, config.discovery_timeout_seconds + 10))
    )
    last_task_info: dict[str, str] = {"state": "Unknown", "implementation": "unknown"}
    while time.monotonic() < startup_deadline:
        last_task_info = _scheduled_task_info(config.task_name)
        pid_state = _read_json(_control_paths()[0])
        status_state = _read_json(_local_state_directory() / "keeper-status.json")
        if (
            last_task_info["implementation"] == "python"
            and last_task_info["state"].lower() == "running"
            and _pid_state_matches_live_process(pid_state)
            and _status_matches_pid_state(status_state, pid_state)
        ):
            print(
                f"后台任务 {config.task_name} 已确认启动；"
                f"PID {_state_pid(pid_state)}，节点切换在后台继续运行。"
            )
            return 0
        time.sleep(0.25)
    raise RuntimeError(
        f"计划任务安装命令已返回，但未在时限内确认 Python Keeper 启动"
        f"（任务状态 {last_task_info['state']}，实现 {last_task_info['implementation']}）。"
    )


def command_stop(config_path: Path) -> int:
    config = _ensure_config(config_path)
    timeout = float(
        config.failover_timeout_seconds + (2 * config.disconnect_wait_seconds) + 75
    )
    _invoke_task_action("Stop", config_path, timeout=timeout)
    print("后台任务已停止；本工具拥有的临时路由已撤销。")
    return 0


def command_uninstall(config_path: Path) -> int:
    config = _ensure_config(config_path)
    timeout = float(
        config.failover_timeout_seconds + (2 * config.disconnect_wait_seconds) + 75
    )
    _invoke_task_action("Uninstall", config_path, timeout=timeout)
    print("后台任务已卸载；配置和日志予以保留。")
    return 0


def command_candidates(config_path: Path) -> int:
    provider = LiveCandidateProvider(_read_config(config_path))
    candidates = provider.list_candidates()
    for index, relay in enumerate(candidates, start=1):
        print(
            f"{index:>2}. {relay.country_short:<2} {relay.endpoint:<22} "
            f"ping={relay.ping}ms speed={relay.speed_mbps:g}Mbps source={relay.source}"
        )
    return 0 if candidates else 1


def command_gui(config_path: Path) -> int:
    _ensure_config(config_path)
    if not _is_administrator():
        if _request_elevated_gui(config_path):
            return 0
        raise PermissionError("需要管理员权限才能管理临时路由和后台任务；UAC 已取消。")
    _detach_console_for_gui()
    from .gui import GuiCallbacks, run_gui

    callbacks = GuiCallbacks(
        start=lambda: command_start(config_path),
        stop=lambda: command_stop(config_path),
        uninstall=lambda: command_uninstall(config_path),
        status=lambda: status_snapshot(config_path),
        tail_log=lambda: tail_log(_local_state_directory() / "keeper.log"),
    )
    return run_gui(callbacks)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tarkov_cis")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in (
        "gui",
        "start",
        "run",
        "status",
        "stop",
        "uninstall",
        "candidates",
        "cleanup",
    ):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "gui":
            return command_gui(args.config)
        if args.command == "start":
            return command_start(args.config)
        if args.command == "run":
            return command_run(args.config)
        if args.command == "status":
            return command_status(args.config)
        if args.command == "stop":
            return command_stop(args.config)
        if args.command == "uninstall":
            return command_uninstall(args.config)
        if args.command == "candidates":
            return command_candidates(args.config)
        if args.command == "cleanup":
            return command_cleanup(args.config)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 2
