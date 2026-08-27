"""Small rotating text log used by the detached Windows keeper."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading


class RotatingLogSink:
    """Append timestamped lines and keep exactly one bounded backup file."""

    def __init__(self, path: Path, *, maximum_bytes: int = 2 * 1024 * 1024) -> None:
        if maximum_bytes < 1024:
            raise ValueError("maximum_bytes must be at least 1024")
        self.path = path
        self.backup_path = path.with_suffix(path.suffix + ".1")
        self.maximum_bytes = maximum_bytes
        self._lock = threading.Lock()

    def __call__(self, message: str) -> None:
        lines = str(message).splitlines() or [""]
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        encoded = "".join(f"{timestamp} {line}\n" for line in lines).encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                current_size = self.path.stat().st_size
            except OSError:
                current_size = 0
            if current_size and current_size + len(encoded) > self.maximum_bytes:
                self.backup_path.unlink(missing_ok=True)
                self.path.replace(self.backup_path)
            with self.path.open("ab") as handle:
                handle.write(encoded)


def tail_log(path: Path, *, maximum_bytes: int = 64 * 1024) -> str:
    """Read only the bounded tail of a UTF-8 keeper log."""

    if maximum_bytes <= 0 or not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - maximum_bytes))
        payload = handle.read(maximum_bytes)
    text = payload.decode("utf-8", errors="replace")
    if size > maximum_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return text
