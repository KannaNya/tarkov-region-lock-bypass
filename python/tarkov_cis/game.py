"""Read-only Escape from Tarkov phase detection from local Unity logs.

The keeper uses this to freeze all VPN maintenance while a match is running.
Only phase markers are consumed; Raid server addresses in the logs are never
turned into routes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
import re
import time
from typing import Callable, Iterable

from .targets import read_tail, recent_log_files

EFT_PROCESS = "EscapeFromTarkov.exe"
# A match marker older than this cannot describe the current game process.
MARKER_MAX_AGE_SECONDS = 3 * 60 * 60


class Phase(str, Enum):
    UNKNOWN = "unknown"
    LOGIN = "login"
    CHARACTER_SELECT = "character_select"
    MATCHMAKING = "matchmaking"
    RAID = "raid"
    POST_RAID = "post_raid"


# From the start of matchmaking until settlement finishes the relay must not change.
PLAY_PHASES = frozenset({Phase.MATCHMAKING, Phase.RAID, Phase.POST_RAID})


@dataclass(frozen=True, slots=True)
class GameSnapshot:
    phase: Phase = Phase.UNKNOWN
    detail: str = "没有检测到近期游戏阶段标记"
    observed_at: float | None = None
    # None means the process list could not be read, not that the game exited.
    process_running: bool | None = False
    session_id: str = ""

    @property
    def in_match(self) -> bool:
        return self.phase in PLAY_PHASES and self.process_running is not False


_TIMESTAMP_RE = re.compile(r"^﻿?(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\|")
# log_2026.09.25_0-23-51_1.0 — the hour is not zero-padded.
_SESSION_RE = re.compile(r"^log_(\d{4}\.\d{2}\.\d{2})_(\d{1,2})-(\d{2})-(\d{2})(?:_|$)")
_GAME_STARTED_RE = re.compile(r"\bgame\s*started\s*:")

# (substring or regex, phase, detail) checked in order against lower-cased lines.
_MARKERS: tuple[tuple[str | re.Pattern, Phase, str], ...] = (
    ("usermatchover", Phase.POST_RAID, "UserMatchOver：正在结算战局"),
    ("user match over", Phase.POST_RAID, "UserMatchOver：正在结算战局"),
    ("showcharacterselectionscreen", Phase.CHARACTER_SELECT, "ShowCharacterSelectionScreen：等待选角色"),
    ("showprofileloadingscreen", Phase.LOGIN, "登录/资料加载"),
    ("successful login", Phase.LOGIN, "登录/资料加载"),
    ("trace-networkgamematching", Phase.MATCHMAKING, "NetworkGameMatching：匹配中"),
    ("tracenetworkgamematching", Phase.MATCHMAKING, "NetworkGameMatching：匹配中"),
    ("matchingcompleted", Phase.MATCHMAKING, "NetworkGameMatching：匹配中"),
    ("userconfirmed", Phase.RAID, "UserConfirmed：服务器已确认 Raid"),
    ("user confirmed", Phase.RAID, "UserConfirmed：服务器已确认 Raid"),
    ("trace-networkgamecreate", Phase.RAID, "NetworkGameCreate：Raid 会话已建立"),
    ("tracenetworkgamecreate", Phase.RAID, "NetworkGameCreate：Raid 会话已建立"),
    (_GAME_STARTED_RE, Phase.RAID, "GameStarted：已进入 Raid"),
    ("postraid.", Phase.POST_RAID, "PostRaid：正在保存 Raid 结果"),
    ("gameoversavestatusreceived", Phase.POST_RAID, "PostRaid：正在保存 Raid 结果"),
)


def classify(line: str) -> tuple[Phase, str] | None:
    """Phase marker on one timestamped log line.

    Untimestamped lines are Unity stack frames; they repeat old method names
    (MainMenuShowOperation, NetworkGameCreate) and never mark a transition.
    """

    if not _TIMESTAMP_RE.match(line):
        return None
    lowered = line.lower()
    for marker, phase, detail in _MARKERS:
        hit = marker.search(lowered) if isinstance(marker, re.Pattern) else marker in lowered
        if hit:
            return phase, detail
    return None


def _timestamp(line: str) -> float | None:
    match = _TIMESTAMP_RE.match(line)
    if not match:
        return None
    stamp = match.group(1)
    parsed = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f" if "." in stamp else "%Y-%m-%d %H:%M:%S")
    # EFT logs local wall-clock time.
    return parsed.astimezone().timestamp()


def session_id(path: Path) -> str:
    for parent in path.parents:
        match = _SESSION_RE.match(parent.name)
        if match:
            date, hour, minute, second = match.groups()
            return f"{date}_{int(hour):02d}-{minute}-{second}"
    return ""


def _file_events(path: Path, max_bytes: int) -> list[tuple[float, int, Phase, str]]:
    events = []
    # A tail can start inside a stack trace: until the first header line there
    # is no trustworthy time, so nothing before it counts.
    current: float | None = None
    for number, line in enumerate(read_tail(path, max_bytes).splitlines()):
        current = _timestamp(line) or current
        marker = classify(line)
        if marker is not None and current is not None:
            events.append((current, number, *marker))
    return events


class GameProbe:
    """Cheap repeated phase checks with a per-file parse cache."""

    def __init__(
        self,
        roots: Callable[[], Iterable[Path]],
        *,
        max_age_days: int = 2,
        max_files: int = 24,
        max_bytes_per_file: int = 256 * 1024,
        process_running: Callable[[], bool | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._roots = roots
        self.max_age_days = max_age_days
        self.max_files = max_files
        self.max_bytes = max_bytes_per_file
        self._process_running = process_running or _eft_running
        self._clock = clock
        self._cache: dict[Path, tuple[tuple[int, int], list]] = {}
        self._last = GameSnapshot()

    def __call__(self) -> GameSnapshot:
        snapshot = self._detect()
        previous = self._last
        new_launch = bool(
            previous.session_id and snapshot.session_id and previous.session_id != snapshot.session_id
        )
        if (
            previous.in_match
            and snapshot.process_running is not False
            and not new_launch
            and (
                snapshot.phase is Phase.UNKNOWN
                or (snapshot.observed_at or 0) < (previous.observed_at or 0)
            )
        ):
            # A rolled log tail or unreadable file must not unlock a known match.
            snapshot = replace(previous, process_running=snapshot.process_running)
        self._last = snapshot
        return snapshot

    def _detect(self) -> GameSnapshot:
        try:
            running = self._process_running()
        except Exception:
            running = None
        files = recent_log_files(self._roots(), max_age_days=self.max_age_days, max_files=self.max_files)
        newest_session = max((session_id(path) for path in files), default="")
        if newest_session:
            # A new launch opens a new log_ directory; an unfinished Raid in the
            # previous launch must not lock this one.
            files = [path for path in files if session_id(path) == newest_session]
        for stale in set(self._cache) - set(files):
            del self._cache[stale]

        events = []
        for order, path in enumerate(sorted(files, key=str)):
            try:
                stat = path.stat()
            except OSError:
                continue
            signature = (stat.st_mtime_ns, stat.st_size)
            cached = self._cache.get(path)
            if cached is None or cached[0] != signature:
                cached = (signature, _file_events(path, self.max_bytes))
                self._cache[path] = cached
            events.extend((stamp, order, number, phase, detail) for stamp, number, phase, detail in cached[1])

        if not events:
            return GameSnapshot(process_running=running, session_id=newest_session)
        stamp, _, _, phase, detail = max(events, key=lambda event: event[:3])
        if phase in PLAY_PHASES - {Phase.POST_RAID} and self._clock() - stamp > MARKER_MAX_AGE_SECONDS:
            phase, detail = Phase.UNKNOWN, "匹配/Raid 标记已过期，等待当前会话日志"
        return GameSnapshot(
            phase=phase,
            detail=detail,
            observed_at=stamp,
            process_running=running,
            session_id=newest_session,
        )


def _eft_running() -> bool | None:
    from .winapi.kernel import is_process_running

    try:
        return is_process_running(EFT_PROCESS)
    except OSError:
        return None
