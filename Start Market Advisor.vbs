Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\machi\Desktop\Market Advisor v1\Src"
sh.Run """C:\Users\machi\AppData\Local\Programs\Python\Python312\pythonw.exe"" ""C:\Users\machi\Desktop\Market Advisor v1\Src\main.py""", 0, False
