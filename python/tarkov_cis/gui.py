"""Minimal Tkinter control surface for an independent background task.

The GUI deliberately owns no ``KeeperService``. Windows/task integration is
provided through injected callbacks, keeping this module testable without
SoftEther, administrator rights or even a Windows host.
"""

from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable, Mapping


REFRESH_INTERVAL_MS = 3_000
EVENT_POLL_INTERVAL_MS = 100

ActionCallback = Callable[[], Any]
ReadCallback = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class GuiCallbacks:
    start: ActionCallback
    stop: ActionCallback
    uninstall: ActionCallback
    status: ReadCallback
    tail_log: ReadCallback


@dataclass(frozen=True, slots=True)
class ControllerEvent:
    kind: str
    name: str
    ok: bool
    value: Any = None
    error: str = ""


class GuiController:
    """Run injected operations away from Tk's main thread."""

    def __init__(self, callbacks: GuiCallbacks) -> None:
        self.callbacks = callbacks
        self._events: queue.Queue[ControllerEvent] = queue.Queue()
        self._lock = threading.Lock()
        self._busy_action: str | None = None
        self._refreshing = False

    @property
    def busy_action(self) -> str | None:
        with self._lock:
            return self._busy_action

    @property
    def refreshing(self) -> bool:
        with self._lock:
            return self._refreshing

    def run_action(self, name: str) -> bool:
        callback = {
            "start": self.callbacks.start,
            "stop": self.callbacks.stop,
            "uninstall": self.callbacks.uninstall,
        }.get(name)
        if callback is None:
            raise ValueError(f"unknown GUI action: {name}")
        with self._lock:
            if self._busy_action is not None:
                return False
            self._busy_action = name

        def worker() -> None:
            try:
                value = callback()
                if isinstance(value, int) and value != 0:
                    raise RuntimeError(f"操作返回退出码 {value}")
                event = ControllerEvent("action", name, True, value=value)
            except Exception as exc:
                event = ControllerEvent("action", name, False, error=str(exc))
            finally:
                with self._lock:
                    self._busy_action = None
            self._events.put(event)

        threading.Thread(
            target=worker,
            name=f"TarkovCisGui-{name}",
            daemon=True,
        ).start()
        return True

    def refresh(self) -> bool:
        """Start one coalesced status/log refresh in a worker thread."""

        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True

        def worker() -> None:
            for name, callback in (
                ("status", self.callbacks.status),
                ("log", self.callbacks.tail_log),
            ):
                try:
                    self._events.put(
                        ControllerEvent("refresh", name, True, value=callback())
                    )
                except Exception as exc:
                    self._events.put(
                        ControllerEvent("refresh", name, False, error=str(exc))
                    )
            with self._lock:
                self._refreshing = False

        threading.Thread(
            target=worker,
            name="TarkovCisGui-refresh",
            daemon=True,
        ).start()
        return True

    def drain_events(self) -> tuple[ControllerEvent, ...]:
        events: list[ControllerEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return tuple(events)


def _format_status(value: Any) -> str:
    if value is None:
        return "状态：未获取到后台状态"
    if isinstance(value, str):
        return "状态：" + value.strip()
    if isinstance(value, Mapping):
        parts: list[str] = []
        if value.get("task_state"):
            task_state = str(value["task_state"])
            parts.append(f"任务 {task_state}")
        if value.get("task_implementation") == "legacy-powershell":
            parts.append("旧版 PowerShell（点击启动可迁移）")
        if "keeper_running" in value:
            parts.append("后台运行中" if value["keeper_running"] else "后台未运行")
        if value.get("status_stale"):
            parts.append("旧状态已忽略")
        if value.get("connection_phase"):
            parts.append(str(value["connection_phase"]))
        if "vpn_verified" in value:
            parts.append("VPN 已验证" if value["vpn_verified"] else "VPN 未连接")
        if value.get("vpn_ipv4"):
            parts.append(f"VPN {value['vpn_ipv4']}")
        if "managed_authorization_routes" in value:
            parts.append(f"已记录鉴权路由 {value['managed_authorization_routes']} 条")
        if parts:
            return "状态：" + "｜".join(parts)
        return "状态：" + "｜".join(f"{key}={item}" for key, item in value.items())
    return "状态：" + str(value)


class TarkovCisGui:
    def __init__(self, root: tk.Tk, callbacks: GuiCallbacks) -> None:
        self.root = root
        self.controller = GuiController(callbacks)
        self._closed = False
        self.root.title("Tarkov CIS 区域鉴权分流")
        self.root.geometry("680x430")
        self.root.minsize(560, 340)

        container = ttk.Frame(root, padding=12)
        container.pack(fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar(value="状态：正在读取后台状态")
        ttk.Label(container, textvariable=self.status_var).pack(
            anchor=tk.W, pady=(0, 10)
        )

        controls = ttk.Frame(container)
        controls.pack(fill=tk.X, pady=(0, 10))
        self.start_button = ttk.Button(
            controls, text="启动", command=lambda: self._begin_action("start")
        )
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(
            controls, text="停止", command=lambda: self._begin_action("stop")
        )
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))
        self.uninstall_button = ttk.Button(
            controls, text="卸载后台任务", command=self.uninstall
        )
        self.uninstall_button.pack(side=tk.LEFT, padx=(8, 0))

        self.log_text = tk.Text(container, wrap=tk.WORD, state=tk.DISABLED, height=16)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.controller.refresh()
        self.root.after(EVENT_POLL_INTERVAL_MS, self._poll_events)
        self.root.after(REFRESH_INTERVAL_MS, self._refresh_tick)

    def _buttons(self) -> tuple[ttk.Button, ...]:
        return self.start_button, self.stop_button, self.uninstall_button

    def _update_buttons(self) -> None:
        state = tk.DISABLED if self.controller.busy_action else tk.NORMAL
        for button in self._buttons():
            button.configure(state=state)

    def _begin_action(self, name: str) -> None:
        if self.controller.run_action(name):
            labels = {"start": "启动", "stop": "停止", "uninstall": "卸载"}
            self.status_var.set(f"状态：正在执行{labels[name]}操作…")
            self._update_buttons()

    def uninstall(self) -> None:
        confirmed = messagebox.askyesno(
            "确认卸载后台任务",
            "这会停止并删除后台任务，同时撤销本工具管理的临时路由。\n\n"
            "配置文件和日志不会删除。确定继续吗？",
            parent=self.root,
        )
        if confirmed:
            self._begin_action("uninstall")

    def _replace_log(self, value: Any) -> None:
        text = "" if value is None else str(value)
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _poll_events(self) -> None:
        if self._closed:
            return
        for event in self.controller.drain_events():
            if event.kind == "action":
                if event.ok:
                    label = {"start": "启动", "stop": "停止", "uninstall": "卸载"}[
                        event.name
                    ]
                    self.status_var.set(f"状态：{label}操作已完成")
                    self.controller.refresh()
                else:
                    self.status_var.set(f"状态：{event.name} 操作失败")
                    messagebox.showerror("操作失败", event.error, parent=self.root)
            elif event.name == "status":
                if event.ok:
                    self.status_var.set(_format_status(event.value))
                else:
                    self.status_var.set(f"状态：读取后台状态失败：{event.error}")
            elif event.name == "log" and event.ok:
                self._replace_log(event.value)
        self._update_buttons()
        self.root.after(EVENT_POLL_INTERVAL_MS, self._poll_events)

    def _refresh_tick(self) -> None:
        if self._closed:
            return
        self.controller.refresh()
        self.root.after(REFRESH_INTERVAL_MS, self._refresh_tick)

    def close(self) -> None:
        # Closing the control panel must never stop, uninstall or otherwise
        # alter the independently running background task.
        self._closed = True
        self.root.destroy()


def run_gui(callbacks: GuiCallbacks) -> int:
    root = tk.Tk()
    TarkovCisGui(root, callbacks)
    root.mainloop()
    return 0
