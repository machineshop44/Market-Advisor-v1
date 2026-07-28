' Launches Market Advisor with no CMD window (tray-friendly, like Sonarr/Radarr).
Option Explicit
Dim sh, fso, root, src
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
src = root & "\Src"
sh.CurrentDirectory = src
' 0 = hidden window; False = don't wait
sh.Run "pyw -3.12 main.py", 0, False
