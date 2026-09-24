from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from tarkov_cis.gui import (
    REFRESH_INTERVAL_MS,
    GuiCallbacks,
    GuiController,
    TarkovCisGui,
    _format_status,
)


def make_callbacks(**overrides):
    values = {
        "start": lambda: 0,
        "stop": lambda: 0,
        "uninstall": lambda: 0,
        "status": lambda: {"keeper_running": True},
        "tail_log": lambda: "log",
    }
    values.update(overrides)
    return GuiCallbacks(**values)


def wait_for_events(controller: GuiController, count: int, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    events = []
    while len(events) < count and time.monotonic() < deadline:
        events.extend(controller.drain_events())
        if len(events) < count:
            time.sleep(0.01)
    return events


class GuiControllerTests(unittest.TestCase):
    def test_actions_run_off_main_thread_and_are_serialized(self):
        entered = threading.Event()
        release = threading.Event()
        callback_thread = []

        def start():
            callback_thread.append(threading.get_ident())
            entered.set()
            release.wait(1)
            return 0

        controller = GuiController(make_callbacks(start=start))
        main_thread = threading.get_ident()
        self.assertTrue(controller.run_action("start"))
        self.assertTrue(entered.wait(1))
        self.assertEqual("start", controller.busy_action)
        self.assertFalse(controller.run_action("stop"))
        release.set()

        events = wait_for_events(controller, 1)
        self.assertEqual(1, len(events))
        self.assertTrue(events[0].ok)
        self.assertEqual("start", events[0].name)
        self.assertNotEqual(main_thread, callback_thread[0])
        self.assertIsNone(controller.busy_action)

    def test_nonzero_callback_result_is_reported_as_failure(self):
        controller = GuiController(make_callbacks(stop=lambda: 7))
        self.assertTrue(controller.run_action("stop"))
        event = wait_for_events(controller, 1)[0]
        self.assertFalse(event.ok)
        self.assertIn("退出码 7", event.error)

    def test_refresh_is_coalesced_and_reads_status_and_log_in_worker(self):
        entered = threading.Event()
        release = threading.Event()
        callback_threads = []

        def status():
            callback_threads.append(threading.get_ident())
            entered.set()
            release.wait(1)
            return {"keeper_running": True, "vpn_verified": True}

        def tail_log():
            callback_threads.append(threading.get_ident())
            return "latest"

        controller = GuiController(make_callbacks(status=status, tail_log=tail_log))
        main_thread = threading.get_ident()
        self.assertTrue(controller.refresh())
        self.assertTrue(entered.wait(1))
        self.assertFalse(controller.refresh())
        release.set()

        events = wait_for_events(controller, 2)
        self.assertEqual(["status", "log"], [event.name for event in events])
        self.assertEqual("latest", events[1].value)
        self.assertTrue(all(value != main_thread for value in callback_threads))


class GuiSurfaceContractTests(unittest.TestCase):
    def test_status_shows_current_connection_attempt(self):
        status = _format_status({
            "task_state": "Running",
            "connection_phase": "connecting",
            "detail": "正在连接候选 2/3: RU 192.0.2.10:443",
            "vpn_verified": False,
        })
        self.assertIn("正在连接候选 2/3", status)
        self.assertIn("VPN 未连接", status)

    def test_close_only_destroys_window_and_never_calls_task_callbacks(self):
        calls = []
        root = SimpleNamespace(destroy=lambda: calls.append("destroy"))
        gui = object.__new__(TarkovCisGui)
        gui.root = root
        gui._closed = False
        gui.controller = SimpleNamespace(
            run_action=lambda name: calls.append(name),
            refresh=lambda: calls.append("refresh"),
        )

        gui.close()
        self.assertEqual(["destroy"], calls)
        self.assertTrue(gui._closed)

    def test_uninstall_requires_confirmation(self):
        gui = object.__new__(TarkovCisGui)
        gui.root = object()
        gui._begin_action = Mock()
        with patch("tarkov_cis.gui.messagebox.askyesno", return_value=False):
            gui.uninstall()
        gui._begin_action.assert_not_called()

        with patch("tarkov_cis.gui.messagebox.askyesno", return_value=True):
            gui.uninstall()
        gui._begin_action.assert_called_once_with("uninstall")

    def test_refresh_tick_uses_exact_three_second_interval(self):
        scheduled = []
        refreshed = []
        gui = object.__new__(TarkovCisGui)
        gui.root = SimpleNamespace(after=lambda delay, callback: scheduled.append((delay, callback)))
        gui.controller = SimpleNamespace(refresh=lambda: refreshed.append(True))
        gui._closed = False

        gui._refresh_tick()
        self.assertEqual([True], refreshed)
        self.assertEqual(REFRESH_INTERVAL_MS, scheduled[0][0])
        self.assertEqual(3_000, scheduled[0][0])

    def test_all_operation_buttons_are_disabled_while_busy(self):
        buttons = [Mock(), Mock(), Mock()]
        gui = object.__new__(TarkovCisGui)
        gui.controller = SimpleNamespace(busy_action="start")
        gui._buttons = lambda: tuple(buttons)

        gui._update_buttons()
        for button in buttons:
            button.configure.assert_called_once_with(state="disabled")


if __name__ == "__main__":
    unittest.main()
