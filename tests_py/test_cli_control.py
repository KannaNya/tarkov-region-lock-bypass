from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from tarkov_cis import cli
from tarkov_cis.config import AppConfig
from tarkov_cis.keeper import KeeperService
from tarkov_cis.models import Relay


class CliControlTests(unittest.TestCase):
    @patch("tarkov_cis.cli._invoke_task_action")
    @patch("tarkov_cis.cli._ensure_config", return_value=AppConfig())
    def test_start_is_a_task_install_not_a_foreground_run(self, _config, invoke):
        pid_state = {
            "pid": 1234,
            "token": "control-token",
            "generation": "generation-a",
            "process_created": "created-a",
        }
        status_state = {
            "pid": 1234,
            "generation": "generation-a",
            "process_created": "created-a",
            "updated_at": "2026-08-28T00:00:00+00:00",
        }
        with (
            patch(
                "tarkov_cis.cli._scheduled_task_info",
                return_value={"state": "Running", "implementation": "python"},
            ),
            patch("tarkov_cis.cli._read_json", side_effect=[pid_state, status_state]),
            patch("tarkov_cis.cli._pid_state_matches_live_process", return_value=True),
        ):
            self.assertEqual(0, cli.command_start(Path("config.json")))
        invoke.assert_called_once_with("Install", Path("config.json"))

    @patch("tarkov_cis.cli._invoke_task_action")
    @patch(
        "tarkov_cis.cli._ensure_config",
        return_value=AppConfig(discovery_timeout_seconds=1),
    )
    def test_start_fails_when_python_task_never_publishes_current_generation(
        self, _config, invoke
    ):
        with (
            patch(
                "tarkov_cis.cli._scheduled_task_info",
                return_value={"state": "Ready", "implementation": "python"},
            ),
            patch("tarkov_cis.cli._read_json", return_value=None),
            patch("tarkov_cis.cli.time.monotonic", side_effect=[0.0, 100.0]),
        ):
            with self.assertRaisesRegex(RuntimeError, "未在时限内确认 Python Keeper"):
                cli.command_start(Path("config.json"))
        invoke.assert_called_once_with("Install", Path("config.json"))

    @patch("tarkov_cis.cli._invoke_task_action")
    @patch(
        "tarkov_cis.cli._ensure_config",
        return_value=AppConfig(discovery_timeout_seconds=1),
    )
    def test_start_requires_a_current_status_heartbeat(self, _config, invoke):
        pid_state = {
            "pid": 1234,
            "token": "control-token",
            "generation": "generation-a",
            "process_created": "created-a",
        }
        with (
            patch(
                "tarkov_cis.cli._scheduled_task_info",
                return_value={"state": "Running", "implementation": "python"},
            ),
            patch("tarkov_cis.cli._read_json", side_effect=[pid_state, None]),
            patch("tarkov_cis.cli._pid_state_matches_live_process", return_value=True),
            patch("tarkov_cis.cli.time.monotonic", side_effect=[0.0, 1.0, 100.0]),
            patch("tarkov_cis.cli.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "未在时限内确认 Python Keeper"):
                cli.command_start(Path("config.json"))
        invoke.assert_called_once_with("Install", Path("config.json"))

    @patch("tarkov_cis.cli._invoke_task_action")
    @patch("tarkov_cis.cli._ensure_config", return_value=AppConfig())
    def test_public_stop_delegates_to_task_wrapper(self, _config, invoke):
        self.assertEqual(0, cli.command_stop(Path("config.json")))
        self.assertEqual("Stop", invoke.call_args.args[0])
        expected_floor = (
            AppConfig().failover_timeout_seconds
            + (2 * AppConfig().disconnect_wait_seconds)
            + 60
        )
        self.assertGreaterEqual(invoke.call_args.kwargs["timeout"], expected_floor)

    def test_run_stop_failure_retains_owned_pid_and_generation(self):
        class FailingService:
            running = False

            def __init__(self):
                self.stop_calls = []
                self.stop_requested = False

            def start(self):
                return True

            def request_stop(self):
                self.stop_requested = True

            def stop(self, **kwargs):
                self.stop_calls.append(kwargs)
                raise RuntimeError("route cleanup failed")

        service = FailingService()
        config = AppConfig(
            failover_timeout_seconds=2,
            disconnect_wait_seconds=3,
        )
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            pid_path = state_dir / "keeper.pid.json"
            stop_path = state_dir / "stop.request.json"
            with (
                patch("tarkov_cis.cli._ensure_config", return_value=config),
                patch(
                    "tarkov_cis.cli._control_paths",
                    return_value=(pid_path, stop_path),
                ),
                patch("tarkov_cis.cli._local_state_directory", return_value=state_dir),
                patch(
                    "tarkov_cis.cli._process_creation_marker",
                    return_value="created-current",
                ),
                patch("tarkov_cis.cli.build_runtime", return_value=service),
                patch("tarkov_cis.cli.signal.signal"),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(1, cli._command_run_locked(Path("config.json")))

            self.assertTrue(pid_path.is_file())
            state = json.loads(pid_path.read_text(encoding="utf-8"))
            self.assertEqual(os.getpid(), state["pid"])
            self.assertEqual("created-current", state["process_created"])
            self.assertTrue(state["token"])
            self.assertTrue(state["generation"])
            self.assertFalse(stop_path.exists())
            self.assertEqual(1, len(service.stop_calls))
            self.assertEqual(35.0, service.stop_calls[0]["timeout"])
            self.assertEqual(3.0, service.stop_calls[0]["disconnect_timeout"])

    def test_status_uses_only_live_matching_keeper_generation(self):
        class SoftEther:
            @staticmethod
            def verified_connection():
                return None

        runtime = SimpleNamespace(
            softether=SoftEther(),
            routes=SimpleNamespace(managed=()),
        )
        pid_state = {
            "pid": 4321,
            "token": "control-token",
            "generation": "generation-a",
            "process_created": "created-a",
        }
        persisted = {
            "pid": 4321,
            "generation": "generation-a",
            "process_created": "created-a",
            "phase": "ready",
            "connection_phase": "ready",
            "detail": "current generation",
            "relay": "192.0.2.1:443",
            "country": "RU",
            "updated_at": "2026-08-28T00:00:00+00:00",
        }
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            pid_path = state_dir / "keeper.pid.json"
            stop_path = state_dir / "stop.request.json"
            status_path = state_dir / "keeper-status.json"
            pid_path.write_text(json.dumps(pid_state), encoding="utf-8")
            status_path.write_text(json.dumps(persisted), encoding="utf-8")
            with (
                patch("tarkov_cis.cli._read_config", return_value=AppConfig()),
                patch(
                    "tarkov_cis.cli._scheduled_task_info",
                    return_value={"state": "Running", "implementation": "python"},
                ),
                patch(
                    "tarkov_cis.cli._control_paths",
                    return_value=(pid_path, stop_path),
                ),
                patch("tarkov_cis.cli._local_state_directory", return_value=state_dir),
                patch("tarkov_cis.cli._process_is_running", return_value=True),
                patch(
                    "tarkov_cis.cli._process_creation_marker",
                    return_value="created-a",
                ),
                patch("tarkov_cis.cli.build_runtime", return_value=runtime),
            ):
                current = cli.status_snapshot(Path("config.json"))
                self.assertTrue(current["keeper_running"])
                self.assertFalse(current["status_stale"])
                self.assertEqual("ready", current["connection_phase"])
                self.assertEqual("current generation", current["detail"])

                persisted["generation"] = "generation-old"
                status_path.write_text(json.dumps(persisted), encoding="utf-8")
                stale = cli.status_snapshot(Path("config.json"))

            self.assertTrue(stale["keeper_running"])
            self.assertTrue(stale["status_stale"])
            self.assertIsNone(stale["phase"])
            self.assertIsNone(stale["connection_phase"])
            self.assertIsNone(stale["detail"])
            self.assertIsNone(stale["relay"])

    def test_pid_reuse_does_not_match_keeper_generation(self):
        state = {
            "pid": 9876,
            "token": "control-token",
            "generation": "generation-a",
            "process_created": "created-old",
        }
        with (
            patch("tarkov_cis.cli._process_is_running", return_value=True),
            patch(
                "tarkov_cis.cli._process_creation_marker",
                return_value="created-new",
            ),
        ):
            self.assertFalse(cli._pid_state_matches_live_process(state))

    @patch("tarkov_cis.keeper.threading.Thread")
    def test_keeper_worker_is_non_daemon_until_owned_work_finishes(self, thread_type):
        thread_type.return_value.is_alive.return_value = False
        keeper = KeeperService(
            config=SimpleNamespace(),
            candidate_provider=lambda: (),
            softether=SimpleNamespace(),
            routes=SimpleNamespace(),
        )

        self.assertTrue(keeper.start())

        self.assertFalse(thread_type.call_args.kwargs["daemon"])
        thread_type.return_value.start.assert_called_once_with()

    @patch("tarkov_cis.cli.LiveCandidateProvider")
    @patch("tarkov_cis.cli._read_config", return_value=AppConfig())
    def test_candidates_uses_read_only_normal_selection(self, _config, provider_type):
        candidate = Relay("vpn.example", "192.0.2.10", 443, "RU")
        provider = provider_type.return_value
        provider.list_candidates.return_value = (candidate,)
        provider.candidates = Mock(side_effect=AssertionError("fallback must stay inactive"))
        self.assertEqual(0, cli.command_candidates(Path("missing.json")))
        provider.list_candidates.assert_called_once_with()

    def test_task_wrapper_has_non_recursive_cleanup_and_legacy_migration_guard(self):
        script = (cli.REPOSITORY_ROOT / "scripts" / "python-task.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("-Command cleanup", script)
        self.assertNotIn("-Command stop", script)
        self.assertIn("Test-TaskUsesThisWrapper", script)
        self.assertIn("Get-PythonWrapperPath", script)
        self.assertIn("System32\\WindowsPowerShell\\v1.0\\powershell.exe", script)
        self.assertIn("Migrating the Python keeper from", script)
        self.assertIn("shared PID/token cleanup", script)
        self.assertIn("Get-LegacyKeeperPath", script)
        self.assertIn("refusing to overwrite it", script)
        self.assertIn("refusing to stop it", script)

    def test_task_wrapper_recognizes_current_moved_and_unknown_actions(self):
        shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
        if shell is None:
            self.skipTest("PowerShell is required for the wrapper behavior probe")
        wrapper = cli.REPOSITORY_ROOT / "scripts" / "python-task.ps1"
        probe = r"""
$source = Get-Content -Raw -LiteralPath $env:TARKOV_CIS_WRAPPER_TEST_PATH
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput(
    $source, [ref]$tokens, [ref]$errors
)
if ($errors.Count) { throw $errors[0].Message }
foreach ($name in @(
    'ConvertTo-SafeWindowsArgument',
    'Join-SafeWindowsArguments',
    'Get-PythonWrapperPath',
    'Test-TaskUsesThisWrapper'
)) {
    $node = $ast.Find({
        param($candidate)
        $candidate -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $candidate.Name -eq $name
    }, $true)
    if (-not $node) { throw "Missing function: $name" }
    Invoke-Expression $node.Extent.Text
}
$currentWrapperPath = 'C:\Release-B\scripts\python-task.ps1'
$sameArguments = Join-SafeWindowsArguments -Arguments @(
    '-NoProfile', '-File', $currentWrapperPath, '-Action', 'Run'
)
$same = [pscustomobject]@{
    Actions = @([pscustomobject]@{ Arguments = $sameArguments })
}
$moved = [pscustomobject]@{
    Actions = @([pscustomobject]@{
        Arguments = '-File "C:\Release-A\scripts\python-task.ps1" -Action Run'
    })
}
$unknown = [pscustomobject]@{
    Actions = @([pscustomobject]@{
        Arguments = '-File "C:\Other\maintenance.ps1" -Action Run'
    })
}
[pscustomobject]@{
    SameIsCurrent = Test-TaskUsesThisWrapper -Task $same
    MovedPath = Get-PythonWrapperPath -Task $moved
    MovedIsCurrent = Test-TaskUsesThisWrapper -Task $moved
    UnknownPath = Get-PythonWrapperPath -Task $unknown
} | ConvertTo-Json -Compress
"""
        environment = os.environ.copy()
        environment["TARKOV_CIS_WRAPPER_TEST_PATH"] = str(wrapper)
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", probe],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=environment,
        )
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["SameIsCurrent"])
        self.assertEqual(
            r"C:\Release-A\scripts\python-task.ps1", payload["MovedPath"]
        )
        self.assertFalse(payload["MovedIsCurrent"])
        self.assertIsNone(payload["UnknownPath"])

    @patch("tarkov_cis.cli.build_runtime")
    @patch(
        "tarkov_cis.cli._scheduled_task_info",
        return_value={"state": "Running", "implementation": "python"},
    )
    @patch("tarkov_cis.cli._ensure_config", return_value=AppConfig())
    def test_corrupt_missing_control_state_fails_closed_while_task_runs(
        self, _config, _task, build_runtime
    ):
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory, "keeper.pid.json")
            stop_path = Path(directory, "stop.request.json")
            pid_path.write_text("{truncated", encoding="utf-8")
            with (
                patch("tarkov_cis.cli._control_paths", return_value=(pid_path, stop_path)),
                patch("tarkov_cis.cli.time.monotonic", side_effect=[0.0, 4.0]),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(1, cli.command_cleanup(Path("config.json")))
        build_runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
