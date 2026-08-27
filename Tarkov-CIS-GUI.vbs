Option Explicit

Dim shell, shellApplication, fileSystem, projectRoot, configPath
Dim candidate, executablePath, sourceEntry, pythonPath, legacyScript, probeMode

Set shell = CreateObject("WScript.Shell")
Set shellApplication = CreateObject("Shell.Application")
Set fileSystem = CreateObject("Scripting.FileSystemObject")

projectRoot = fileSystem.GetParentFolderName(WScript.ScriptFullName)
configPath = fileSystem.BuildPath(projectRoot, "config.json")
probeMode = WScript.Arguments.Named.Exists("probe")

Function QuoteArgument(ByVal value)
    If InStr(value, Chr(34)) > 0 Then
        Err.Raise vbObjectError + 1000, "Tarkov-CIS-GUI", "A launch path contains an unsupported quote character."
    End If
    QuoteArgument = Chr(34) & value & Chr(34)
End Function

Function FindOnPath(ByVal fileName)
    Dim systemCandidate, pathValue, directory, pathCandidate

    systemCandidate = shell.ExpandEnvironmentStrings("%SystemRoot%\" & fileName)
    If fileSystem.FileExists(systemCandidate) Then
        FindOnPath = systemCandidate
        Exit Function
    End If

    pathValue = shell.ExpandEnvironmentStrings("%PATH%")
    For Each directory In Split(pathValue, ";")
        directory = Trim(Replace(directory, Chr(34), ""))
        If Len(directory) > 0 Then
            pathCandidate = fileSystem.BuildPath(directory, fileName)
            If fileSystem.FileExists(pathCandidate) Then
                FindOnPath = pathCandidate
                Exit Function
            End If
        End If
    Next
    FindOnPath = ""
End Function

Sub LaunchElevated(ByVal filePath, ByVal arguments)
    If probeMode Then
        WScript.Echo filePath & " " & arguments
        Exit Sub
    End If
    On Error Resume Next
    ' Launch normally so Tk can create a visible top-level window.  The Python
    ' GUI command detaches its inherited console after elevation.
    shellApplication.ShellExecute filePath, arguments, projectRoot, "runas", 1
    If Err.Number <> 0 Then
        Call MsgBox("Launch failed or administrator approval was cancelled." & vbCrLf & Err.Description, vbExclamation, "Tarkov CIS Split Route")
        Err.Clear
    End If
    On Error GoTo 0
End Sub

' A packaged executable is the preferred path for users who do not have
' Python installed.  The second location is the development build output.
For Each candidate In Array( _
    fileSystem.BuildPath(projectRoot, "TarkovCIS.exe"), _
    fileSystem.BuildPath(projectRoot, "dist\TarkovCIS\TarkovCIS.exe"), _
    fileSystem.BuildPath(projectRoot, "dist\TarkovCIS.exe") _
)
    If fileSystem.FileExists(candidate) Then
        executablePath = candidate
        Exit For
    End If
Next

If Len(executablePath) > 0 Then
    LaunchElevated executablePath, "gui --config " & QuoteArgument(configPath)
    WScript.Quit 0
End If

' In a source checkout, prefer a windowless Python launcher.  Tarkov-CIS-
' Python.py adds the repository's python package directory to sys.path, so no
' global pip installation is required.
sourceEntry = fileSystem.BuildPath(projectRoot, "Tarkov-CIS-Python.py")
If fileSystem.FileExists(sourceEntry) Then
    pythonPath = FindOnPath("pyw.exe")
    If Len(pythonPath) > 0 Then
        LaunchElevated pythonPath, "-3 " & QuoteArgument(sourceEntry) & " gui --config " & QuoteArgument(configPath)
        WScript.Quit 0
    End If

    pythonPath = FindOnPath("pythonw.exe")
    If Len(pythonPath) > 0 Then
        LaunchElevated pythonPath, QuoteArgument(sourceEntry) & " gui --config " & QuoteArgument(configPath)
        WScript.Quit 0
    End If

    pythonPath = FindOnPath("py.exe")
    If Len(pythonPath) > 0 Then
        LaunchElevated pythonPath, "-3 " & QuoteArgument(sourceEntry) & " gui --config " & QuoteArgument(configPath)
        WScript.Quit 0
    End If

    pythonPath = FindOnPath("python.exe")
    If Len(pythonPath) > 0 Then
        LaunchElevated pythonPath, QuoteArgument(sourceEntry) & " gui --config " & QuoteArgument(configPath)
        WScript.Quit 0
    End If
End If

' Compatibility fallback for an older checkout without a packaged executable
' or Python runtime.
legacyScript = fileSystem.BuildPath(projectRoot, "src\Tarkov-CisGui.ps1")
If fileSystem.FileExists(legacyScript) Then
    executablePath = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
    LaunchElevated executablePath, "-NoProfile -STA -WindowStyle Hidden -ExecutionPolicy Bypass -File " & QuoteArgument(legacyScript) & " -ConfigPath " & QuoteArgument(configPath)
    WScript.Quit 0
End If

Call MsgBox("TarkovCIS.exe, Python 3, and the legacy PowerShell GUI were not found. Download the complete Windows release bundle.", vbCritical, "Tarkov CIS Split Route")
WScript.Quit 1
