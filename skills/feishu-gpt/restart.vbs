Dim shell, dir
Set shell = CreateObject("WScript.Shell")
dir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
shell.Run "cmd.exe /k " & Chr(34) & "title 飞书 × ChatGPT 机器人 & call " & Chr(34) & dir & "restart.bat" & Chr(34) & Chr(34), 1, False
Set shell = Nothing
