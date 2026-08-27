"""Small, testable wrapper around Windows command execution.

Every child process is started without a shell, has an explicit timeout and
returns both output streams together with the exit code.  Keeping this policy
in one module makes it much harder for a future adapter to accidentally add an
unbounded ``subprocess`` call.
"""

from __future__ import annotations

from dataclasses import dataclass
import locale
import os
import subprocess
from typing import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0


class CommandError(RuntimeError):
    """Base error carrying the complete observable child-process result."""

    def __init__(self, message: str, result: CommandResult) -> None:
        super().__init__(message)
        self.result = result


class CommandExecutionError(CommandError):
    pass


class CommandTimeout(CommandError):
    pass


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data

    # vpncmd emits UTF-8 even on a Chinese/Japanese Windows installation.
    # PowerShell output can still follow the active ANSI code page, so retain
    # locale and common Windows code pages as conservative fallbacks.
    encodings = ["utf-8-sig", locale.getpreferredencoding(False), "cp932", "gb18030"]
    seen: set[str] = set()
    for encoding in encodings:
        normalized = (encoding or "").lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def run_command(
    argv: Sequence[str | os.PathLike[str]],
    *,
    timeout: float,
    check: bool = False,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
) -> CommandResult:
    """Execute *argv* without a shell and with a mandatory positive timeout."""

    normalized = tuple(os.fspath(part) for part in argv)
    if not normalized or not normalized[0]:
        raise ValueError("argv must contain an executable")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    try:
        completed = subprocess.run(
            normalized,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=os.fspath(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        result = CommandResult(
            argv=normalized,
            exit_code=None,
            stdout=_decode(exc.stdout),
            stderr=_decode(exc.stderr),
            timed_out=True,
        )
        raise CommandTimeout(
            f"command timed out after {timeout:g}s: {normalized[0]}", result
        ) from exc

    result = CommandResult(
        argv=normalized,
        exit_code=int(completed.returncode),
        stdout=_decode(completed.stdout),
        stderr=_decode(completed.stderr),
    )
    if check and not result.ok:
        raise CommandExecutionError(
            f"command exited with code {result.exit_code}: {normalized[0]}", result
        )
    return result
