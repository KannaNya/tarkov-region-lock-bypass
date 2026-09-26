"""The one place that starts child processes (vpncmd and schtasks only).

Every child gets an explicit timeout and CREATE_NO_WINDOW: the GUI and the
scheduled keeper have no console, so without the flag each console program
would open its own visible window.
"""

from __future__ import annotations

from dataclasses import dataclass
import locale
import os
import subprocess
from typing import Callable, Sequence


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def timed_out(self) -> bool:
        return self.exit_code is None

    def detail(self) -> str:
        return (self.stderr.strip() or self.stdout.strip())[-500:]


Runner = Callable[..., CommandResult]


def _decode(data: bytes | None) -> str:
    if not data:
        return ""
    # vpncmd writes UTF-8; schtasks follows the ANSI code page.
    for encoding in ("utf-8-sig", locale.getpreferredencoding(False), "cp932", "gb18030"):
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def run(argv: Sequence[str | os.PathLike[str]], *, timeout: float) -> CommandResult:
    """Run *argv* without a shell or window; a timeout yields exit_code None."""

    normalized = tuple(os.fspath(part) for part in argv)
    try:
        completed = subprocess.run(
            normalized,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(0.1, timeout),
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(normalized, None, _decode(exc.stdout), _decode(exc.stderr))
    return CommandResult(
        normalized, completed.returncode, _decode(completed.stdout), _decode(completed.stderr)
    )
