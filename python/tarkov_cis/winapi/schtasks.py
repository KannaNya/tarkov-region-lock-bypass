"""Scheduled task registration through schtasks.exe and a task XML definition.

Only exit codes are interpreted, so the localized schtasks output never
matters.  The XML form avoids every command-line quoting pitfall: the action
path and arguments are separate elements.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from xml.sax.saxutils import escape

from ..process import CommandResult, Runner, run


@dataclass(frozen=True, slots=True)
class TaskAction:
    executable: str
    arguments: str
    working_directory: str


class TaskError(RuntimeError):
    def __init__(self, message: str, result: CommandResult) -> None:
        detail = result.detail()
        super().__init__(f"{message}: {detail}" if detail else message)
        self.result = result


def task_xml(action: TaskAction, *, user: str, description: str) -> str:
    """Logon-triggered, highest-privilege task that never times out."""

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(action.executable)}</Command>
      <Arguments>{escape(action.arguments)}</Arguments>
      <WorkingDirectory>{escape(action.working_directory)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


class TaskScheduler:
    def __init__(self, name: str, *, runner: Runner = run, timeout: float = 20.0) -> None:
        self.name = name
        self._run = runner
        self.timeout = timeout

    def _schtasks(self, *arguments: str) -> CommandResult:
        return self._run(["schtasks.exe", *arguments], timeout=self.timeout)

    def exists(self) -> bool:
        return self._schtasks("/Query", "/TN", self.name).ok

    def install(self, action: TaskAction, *, description: str) -> None:
        user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
        xml = task_xml(action, user=user.lstrip("\\"), description=description)
        with tempfile.TemporaryDirectory(prefix="tarkov-cis-task-") as directory:
            path = Path(directory) / "task.xml"
            path.write_text(xml, encoding="utf-16")
            result = self._schtasks("/Create", "/TN", self.name, "/XML", str(path), "/F")
        if not result.ok:
            raise TaskError(f"无法注册计划任务 {self.name}", result)

    def start(self) -> None:
        result = self._schtasks("/Run", "/TN", self.name)
        if not result.ok:
            raise TaskError(f"无法启动计划任务 {self.name}", result)

    def end(self) -> None:
        # Ending a task that is not running is not an error worth reporting.
        self._schtasks("/End", "/TN", self.name)

    def delete(self) -> None:
        if not self.exists():
            return
        result = self._schtasks("/Delete", "/TN", self.name, "/F")
        if not result.ok:
            raise TaskError(f"无法删除计划任务 {self.name}", result)
