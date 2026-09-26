"""Process enumeration, named kernel objects and elevation through kernel32/shell32."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys

ERROR_ALREADY_EXISTS = 183
ERROR_ACCESS_DENIED = 5
SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


_kernel32 = None


def _k32():
    global _kernel32
    if _kernel32 is not None:
        return _kernel32
    if sys.platform != "win32":
        raise OSError("kernel32 is only available on Windows")
    k = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32NextW.restype = wintypes.BOOL
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateMutexW.restype = wintypes.HANDLE
    k.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.OpenMutexW.restype = wintypes.HANDLE
    k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.restype = wintypes.HANDLE
    k.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k.OpenEventW.restype = wintypes.HANDLE
    k.SetEvent.argtypes = [wintypes.HANDLE]
    k.SetEvent.restype = wintypes.BOOL
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    _kernel32 = k
    return k


def process_names() -> set[str]:
    """Lower-case executable names of all running processes."""

    k = _k32()
    snapshot = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, INVALID_HANDLE_VALUE):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        names: set[str] = set()
        more = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            names.add(entry.szExeFile.lower())
            more = k.Process32NextW(snapshot, ctypes.byref(entry))
        return names
    finally:
        k.CloseHandle(snapshot)


def is_process_running(image_name: str) -> bool | None:
    """True/False, or None if the process list could not be read."""

    try:
        return image_name.lower() in process_names()
    except OSError:
        return None


class NamedMutex:
    """Single-instance guard held for the lifetime of the keeper process."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle = None
        self.acquired = False

    def __enter__(self) -> "NamedMutex":
        k = _k32()
        handle = k.CreateMutexW(None, False, self.name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            k.CloseHandle(handle)
            return self
        self._handle = handle
        self.acquired = True
        return self

    def __exit__(self, *_exc) -> None:
        if self._handle:
            _k32().CloseHandle(self._handle)
            self._handle = None


def mutex_exists(name: str) -> bool:
    """Whether some process currently holds a handle to the named mutex.

    An elevated keeper's objects deny access to non-elevated callers; access
    denied still proves the object exists.
    """

    k = _k32()
    handle = k.OpenMutexW(SYNCHRONIZE, False, name)
    if handle:
        k.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == ERROR_ACCESS_DENIED


class NamedEvent:
    """Manual-reset event another process can signal to request a stop."""

    def __init__(self, name: str) -> None:
        k = _k32()
        self._handle = k.CreateEventW(None, True, False, name)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, timeout: float) -> bool:
        milliseconds = max(0, int(timeout * 1000))
        return _k32().WaitForSingleObject(self._handle, milliseconds) == WAIT_OBJECT_0

    def set(self) -> None:
        _k32().SetEvent(self._handle)

    def is_set(self) -> bool:
        return self.wait(0)

    def close(self) -> None:
        if self._handle:
            _k32().CloseHandle(self._handle)
            self._handle = None


def signal_event(name: str) -> bool:
    """Set an existing named event; False if nobody created it."""

    k = _k32()
    handle = k.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    try:
        return bool(k.SetEvent(handle))
    finally:
        k.CloseHandle(handle)


def is_admin() -> bool:
    if sys.platform != "win32":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def relaunch_elevated(executable: str, arguments: str, working_directory: str) -> bool:
    """Ask UAC to start *executable* elevated; False if the user declined."""

    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    result = shell32.ShellExecuteW(None, "runas", executable, arguments, working_directory, 1)
    return bool(result) and int(result) > 32


def owns_console() -> bool:
    """True if this process is the only one attached to its console window."""

    if sys.platform != "win32":
        return False
    processes = (wintypes.DWORD * 2)()
    count = ctypes.windll.kernel32.GetConsoleProcessList(processes, 2)
    return count == 1


def detach_console() -> None:
    try:
        ctypes.windll.kernel32.FreeConsole()
    except (AttributeError, OSError):
        pass
