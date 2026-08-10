Option Explicit

Dim shell, fileSystem, projectRoot, scriptPath, command
Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

projectRoot = fileSystem.GetParentFolderName(WScript.ScriptFullName)
scriptPath = fileSystem.BuildPath(projectRoot, "src\Tarkov-CisGui.ps1")
command = "powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File " & Chr(34) & scriptPath & Chr(34)

shell.Run command, 0, False
