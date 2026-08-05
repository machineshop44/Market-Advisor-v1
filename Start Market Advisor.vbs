' Market Advisor silent launcher (no CMD). For the branded icon, use Start Market Advisor.lnk
' Process name in Task Manager Details: MarketAdvisor.exe
Option Explicit
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\machi\Desktop\Market Advisor v1\Src"
sh.Run """C:\Users\machi\AppData\Local\Programs\Python\Python312\MarketAdvisor.exe"" ""C:\Users\machi\Desktop\Market Advisor v1\Src\main.py""", 0, False
