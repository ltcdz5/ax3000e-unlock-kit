@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: 默认路由器密码（改成你的）
set ROUTER_HOST=192.168.2.106
set ROUTER_PASSWD=admin

:: 后台启动面板，5秒后开浏览器
start "" /min python panel\router_monitor_ap.py --host %ROUTER_HOST% --passwd %ROUTER_PASSWD%
timeout /t 5 /nobreak >nul
start http://127.0.0.1:8787

echo 面板: http://127.0.0.1:8787
echo 关闭此窗口即停止面板
pause