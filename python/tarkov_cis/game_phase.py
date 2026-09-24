"""Read-only Escape from Tarkov phase detection for failover protection.

The keeper must be able to distinguish the pre-raid authorization window from
an active Raid.  The game writes stable markers into its local Unity logs;
this module consumes those markers only and never promotes the literal Raid
IP into the authorization route set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Callable, Iterable

from .eft_logs import _read_tail, _recent_log_files


class GamePhase(str, Enum):
    UNKNOWN = "unknown"
    LOGIN = "login"
    CHARACTER_SELECT = "character_select"
    MATCHMAKING = "matchmaking"
    RAID = "raid"
    MENU = "menu"
    POST_RAID = "post_raid"


class PlayProtectionActivated(RuntimeError):
    """Unwind maintenance without running failure cleanup during a Raid."""


class LoginWindowClosed(PlayProtectionActivated):
    """Stop in-flight VPN work when the game has left the login window."""


@dataclass(frozen=True, slots=True)
class GamePhaseSnapshot:
    phase: GamePhase = GamePhase.UNKNOWN
    detail: str = "没有检测到近期游戏阶段标记"
    observed_at: datetime | None = None
    source: str = ""
    # None means the process query failed, not that the game exited.
    process_running: bool | None = False
    raid_ip: str = ""
    raid_port: int = 0
    session_id: str = ""


_TIMESTAMP_RE = re.compile(
    r"^\ufeff?(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\|"
)
_SESSION_RE = re.compile(r"^log_(\d{4}\.\d{2}\.\d{2}_\d{2}-\d{2}-\d{2})(?:_|$)")
_RAID_ENDPOINT_RE = re.compile(
    r"\bIp:\s*(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\s*,\s*Port:\s*(?P<port>\d+)"
    r"|\"ip\"\s*:\s*\"(?P<json_ip>\d{1,3}(?:\.\d{1,3}){3})\"\s*,\s*\"port\"\s*:\s*(?P<json_port>\d+)",
    re.IGNORECASE,
)
_PROTECTION_MARKER_MAX_AGE_SECONDS = 3 * 60 * 60


def is_eft_process_running(
    *,
    process_name: str = "EscapeFromTarkov.exe",
    runner: Callable[..., object] | None = None,
) -> bool | None:
    """Return whether the game process exists without requiring admin rights."""

    if runner is None and os.name != "nt":
        return False
    command = ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"]
    try:
        if runner is None:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            output = result.stdout or ""
        else:
            result = runner(command, capture_output=True, text=True, timeout=1.5, check=False)
            output = str(getattr(result, "stdout", "") or "")
    except (OSError, subprocess.SubprocessError, TypeError):
        return None
    if getattr(result, "returncode", 0) != 0:
        return None
    return any(
        line.strip().startswith('"') and process_name.lower() in line.lower()
        for line in output.splitlines()
    )


def _timestamp(line: str, fallback: float | None) -> float | None:
    match = _TIMESTAMP_RE.match(line)
    if not match:
        return fallback
    try:
        parsed = datetime.strptime(match.group("stamp"), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        try:
            parsed = datetime.strptime(match.group("stamp"), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return fallback
    # EFT timestamps are local Windows time.  Use the local offset when
    # available so a timestamp can be compared with filesystem mtime safely.
    local_zone = datetime.now().astimezone().tzinfo or timezone.utc
    return parsed.replace(tzinfo=local_zone).timestamp()


def _session_id(path: Path) -> str:
    for parent in path.parents:
        match = _SESSION_RE.match(parent.name)
        if match:
            return match.group(1)
    return ""


def _endpoint(line: str) -> tuple[str, int]:
    match = _RAID_ENDPOINT_RE.search(line)
    if not match:
        return "", 0
    ip = match.group("ip") or match.group("json_ip") or ""
    port_text = match.group("port") or match.group("json_port") or "0"
    try:
        port = int(port_text)
    except ValueError:
        port = 0
    return ip, port


def _event(line: str) -> tuple[GamePhase, str, str, int] | None:
    """Classify one log line; return phase, safe detail, optional endpoint."""

    lowered = line.lower()
    if "usermatchover" in lowered or "user match over" in lowered:
        return GamePhase.MENU, "UserMatchOver：Raid 已结束", "", 0
    if "showcharacterselectionscreen" in lowered:
        return GamePhase.CHARACTER_SELECT, "ShowCharacterSelectionScreen：等待选角色", "", 0
    if "showprofileloadingscreen" in lowered or "successful login" in lowered:
        return GamePhase.LOGIN, "登录/资料加载", "", 0
    if "trace-networkgamematching" in lowered or "tracenetworkgamematching" in lowered or "matchingcompleted" in lowered:
        return GamePhase.MATCHMAKING, "NetworkGameMatching：匹配中", "", 0
    if "userconfirmed" in lowered or "user confirmed" in lowered:
        ip, port = _endpoint(line)
        return GamePhase.RAID, "UserConfirmed：服务器已确认 Raid", ip, port
    if "trace-networkgamecreate" in lowered or "tracenetworkgamecreate" in lowered or "networkgamesession.gamestarted" in lowered:
        ip, port = _endpoint(line)
        return GamePhase.RAID, "NetworkGameCreate：Raid 会话已建立", ip, port
    if re.search(r"\bgame\s*started\s*:", lowered) or "gamestarted()" in lowered:
        ip, port = _endpoint(line)
        return GamePhase.RAID, "GameStarted：已进入 Raid", ip, port
    if "postraid." in lowered or "gameoversavestatusreceived" in lowered:
        return GamePhase.POST_RAID, "PostRaid：正在保存 Raid 结果", "", 0
    if "mainmenushowoperation" in lowered or "=== menu load profile ===" in lowered:
        return GamePhase.MENU, "MainMenu：游戏菜单", "", 0
    return None


def detect_game_phase(
    roots: Iterable[str | Path],
    *,
    max_age_days: int = 2,
    max_files: int = 24,
    max_bytes_per_file: int = 256 * 1024,
    process_checker: Callable[[], bool | None] = is_eft_process_running,
    _file_cache: dict | None = None,
) -> GamePhaseSnapshot:
    """Return the newest phase marker in the bounded local log sample.

    Raid protection is only considered active while EscapeFromTarkov.exe is
    still running.  This prevents a stale ``UserConfirmed`` from a crashed or
    closed game from permanently freezing relay maintenance.
    """

    try:
        process_running = process_checker()
    except Exception:
        process_running = None
    if max_files <= 0 or max_bytes_per_file <= 0:
        return GamePhaseSnapshot(process_running=process_running)

    files = tuple(dict.fromkeys(_recent_log_files(
        roots, max_age_days=max_age_days, max_files=max_files
    )))
    session_id = max((_session_id(path) for path in files), default="")
    if session_id:
        # A new EFT launch creates a new log_ directory before login markers.
        # Never let an unfinished Raid in the previous launch lock this one.
        files = tuple(path for path in files if _session_id(path) == session_id)
    cache = _file_cache if _file_cache is not None else {}
    for old_path in tuple(cache):
        if old_path not in files:
            del cache[old_path]
    events = []
    for file_order, path in enumerate(sorted(files, key=str)):
        try:
            stat = path.stat()
        except OSError:
            continue
        signature = (stat.st_mtime_ns, stat.st_size, max_bytes_per_file)
        cached = cache.get(path)
        if cached is not None and cached[0] == signature:
            file_events = cached[1]
        else:
            file_events = []
            # A tail may begin inside a stack trace.  Until its first header,
            # there is no timestamp and file mtime must not promote that old
            # stack frame into a new menu/character-selection event.
            logical_time = None
            for line_number, line in enumerate(_read_tail(path, max_bytes_per_file).splitlines()):
                logical_time = _timestamp(line, logical_time)
                classified = _event(line)
                if classified is not None and logical_time is not None:
                    file_events.append((logical_time, line_number, *classified))
            cache[path] = (signature, file_events)
        events.extend((stamp, file_order, line, *event)
                      for stamp, line, *event in file_events)

    if not events:
        return GamePhaseSnapshot(process_running=process_running, session_id=session_id)
    latest_raid_endpoint = ("", 0)
    for occurred, _, _, phase, detail, raid_ip, raid_port in sorted(events):
        if phase is GamePhase.RAID:
            if raid_ip:
                latest_raid_endpoint = (raid_ip, raid_port)
            else:
                raid_ip, raid_port = latest_raid_endpoint
        elif phase is not GamePhase.UNKNOWN:
            latest_raid_endpoint = ("", 0)
    if phase in {GamePhase.MATCHMAKING, GamePhase.RAID}:
        # A new game process can coexist with yesterday's log directory while
        # Unity is still opening a fresh file.  Do not let that stale marker
        # freeze relay maintenance indefinitely.
        if time.time() - occurred > _PROTECTION_MARKER_MAX_AGE_SECONDS:
            phase = GamePhase.UNKNOWN
            detail = "匹配/Raid 标记已过期，等待当前会话日志"
    observed = datetime.fromtimestamp(occurred, tz=datetime.now().astimezone().tzinfo)
    return GamePhaseSnapshot(
        phase=phase,
        detail=detail,
        observed_at=observed,
        source="local EFT log",
        process_running=process_running,
        raid_ip=raid_ip,
        raid_port=raid_port,
        session_id=session_id,
    )


def configured_game_phase_probe(config: object) -> Callable[[], GamePhaseSnapshot]:
    """Build a cheap, bounded probe for the keeper's 30-second loop."""

    configured = tuple(getattr(config, "game_log_roots", ()) or ())
    max_age_days = max(0, int(getattr(config, "game_phase_max_age_days", 2)))
    max_files = max(1, int(getattr(config, "game_phase_max_files", 24)))
    max_bytes = max(4096, int(getattr(config, "game_phase_max_bytes_per_file", 256 * 1024)))
    file_cache: dict = {}
    process_checked_at = 0.0
    process_running: bool | None = False

    def process_checker() -> bool | None:
        nonlocal process_checked_at, process_running
        now = time.monotonic()
        # Rechecks of a long maintenance cycle only need fresh log bytes.
        # Cache positive process presence briefly (exit then errs on the side
        # of retaining protection); never cache absence and miss a game start.
        if process_running is not True or now - process_checked_at >= 1.0:
            process_running = is_eft_process_running()
            process_checked_at = now
        return process_running

    def probe() -> GamePhaseSnapshot:
        from .eft_logs import discover_log_roots

        roots = discover_log_roots(configured)
        return detect_game_phase(
            roots,
            max_age_days=max_age_days,
            max_files=max_files,
            max_bytes_per_file=max_bytes,
            process_checker=process_checker,
            _file_cache=file_cache,
        )

    return probe
