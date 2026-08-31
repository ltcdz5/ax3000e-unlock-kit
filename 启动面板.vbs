Set WshShell = CreateObject("WScript.Shell")
RouterIP = "192.168.2.106"
RouterPass = "admin"
RepoPath = "C:\Users\xutengfa\ax3000e-unlock-kit"
PythonExe = "C:\Users\xutengfa\AppData\Local\Programs\Python\Python312\python.exe"
cmd = """" & PythonExe & """ """ & RepoPath & "\panel\router_monitor_ap.py"" --host " & RouterIP & " --passwd " & RouterPass
WshShell.Run cmd, 0, False
WScript.Sleep 5000
WshShell.Run "http://127.0.0.1:8787", 1, False
