' run_silent.vbs
' Runs sync_instagram.py with no visible console window.
' Put a shortcut to this file in shell:startup.
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set shell = CreateObject("WScript.Shell")
cmd = "cmd /c cd /d """ & scriptDir & """ && python sync_instagram.py"
shell.Run cmd, 0, False
