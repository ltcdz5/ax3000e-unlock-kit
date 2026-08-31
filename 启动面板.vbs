' 小米路由器面板启动器
' 后台启动面板 + 自动打开浏览器
' 双击即可运行，无控制台窗口

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

' 获取脚本所在目录
ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)

' 读取配置（可选）
RouterIP = "192.168.2.106"
RouterPass = "admin"

' 后台启动面板（隐藏窗口）
WshShell.Run """C:\Users\xutengfa\AppData\Local\Programs\Python\Python312\python.exe"" """ & ScriptDir & "\panel\router_monitor_ap.py"" --host " & RouterIP & " --passwd " & RouterPass, 0, False

' 等待面板启动
WScript.Sleep 5000

' 打开浏览器
WshShell.Run "http://127.0.0.1:8787", 1, False