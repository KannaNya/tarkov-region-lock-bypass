from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import support  # noqa: F401

from tarkov_cis import cli, control
from tarkov_cis.process import CommandResult
from tarkov_cis.winapi.schtasks import TaskAction, TaskError, TaskScheduler, task_xml

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class TaskXmlTests(unittest.TestCase):
    def test_action_is_escaped_and_task_runs_elevated_at_logon(self):
        action = TaskAction(r"C:\Py 3\pythonw.exe", r'"E:\塔可夫\a&b\Tarkov-CIS-Python.py" run', r"E:\塔可夫")
        root = ET.fromstring(task_xml(action, user=r"PC\user", description="d").split("?>", 1)[1])
        self.assertEqual(root.find(".//t:Exec/t:Command", NS).text, action.executable)
        self.assertEqual(root.find(".//t:Exec/t:Arguments", NS).text, action.arguments)
        self.assertEqual(root.find(".//t:RunLevel", NS).text, "HighestAvailable")
        self.assertEqual(root.find(".//t:LogonTrigger/t:UserId", NS).text, r"PC\user")
        self.assertEqual(root.find(".//t:ExecutionTimeLimit", NS).text, "PT0S")

    def test_source_task_uses_windowless_interpreter_without_py_launcher_flags(self):
        action = control.task_action(Path("config.json"))
        self.assertNotIn("-3", action.arguments.split())
        self.assertIn("Tarkov-CIS-Python.py", action.arguments)
        self.assertTrue(action.arguments.endswith(f'--config {Path("config.json").resolve()}')
                        or str(Path("config.json").resolve()) in action.arguments)
        if Path(sys.executable).with_name("pythonw.exe").is_file():
            self.assertTrue(action.executable.lower().endswith("pythonw.exe"))


class SchedulerTests(unittest.TestCase):
    def test_only_exit_codes_are_interpreted(self):
        calls = []

        def runner(argv, *, timeout):
            calls.append(argv[1])
            code = 1 if argv[1] == "/Run" else 0
            return CommandResult(tuple(argv), code, "", "エラー: アクセスが拒否されました。")

        scheduler = TaskScheduler("Task", runner=runner)
        self.assertTrue(scheduler.exists())
        with self.assertRaisesRegex(TaskError, "アクセス"):
            scheduler.start()

    def test_install_writes_utf16_xml(self):
        seen = {}

        def runner(argv, *, timeout):
            path = Path(argv[argv.index("/XML") + 1])
            seen["xml"] = path.read_bytes()
            return CommandResult(tuple(argv), 0, "", "")

        TaskScheduler("Task", runner=runner).install(TaskAction("a.exe", "run", "C:\\"), description="x")
        self.assertTrue(seen["xml"].startswith(b"\xff\xfe"))


class ControlTests(unittest.TestCase):
    def test_stop_signals_the_keeper_then_cleans_up(self):
        running = [True, True, False]
        with patch.object(control.kernel, "is_admin", return_value=True), \
             patch.object(control, "keeper_running", side_effect=lambda: running.pop(0) if running else False), \
             patch.object(control.kernel, "signal_event", return_value=True) as signal, \
             patch.object(control, "offline_cleanup", return_value=[]) as cleanup, \
             patch.object(control.time, "sleep"):
            message = control.stop(Path("missing-config.json"))
        signal.assert_called_once_with(control.STOP_EVENT)
        cleanup.assert_called_once()
        self.assertIn("已停止", message)

    def test_stop_and_start_require_admin(self):
        with patch.object(control.kernel, "is_admin", return_value=False):
            for action in (control.start, control.stop, control.uninstall):
                with self.subTest(action=action.__name__), self.assertRaises(PermissionError):
                    action(Path("missing-config.json"))

    def test_cli_reports_errors_without_traceback(self):
        with patch.object(control, "stop", side_effect=RuntimeError("清理失败 拥")), \
             patch("sys.stderr") as stderr:
            self.assertEqual(cli.main(["stop"]), 1)
        self.assertIn("清理失败", "".join(call.args[0] for call in stderr.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
