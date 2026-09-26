"""Size-bounded keeper log with one rotated backup."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import threading


class RotatingLog:
    def __init__(self, path: Path, *, max_bytes: int = 2 * 1024 * 1024) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def __call__(self, message: str) -> None:
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        data = "".join(f"{stamp} {line}\n" for line in (str(message).splitlines() or [""])).encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if self.path.stat().st_size + len(data) > self.max_bytes:
                    self.path.replace(self.path.with_name(self.path.name + ".1"))
            except FileNotFoundError:
                pass
            with self.path.open("ab") as handle:
                handle.write(data)


def tail(path: Path, *, max_bytes: int = 64 * 1024) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    # Drop a partial first line when the tail cut into the file.
    return text.split("\n", 1)[-1] if size > max_bytes else text
