@echo off
REM 构建桌面客户端 exe
cd /d "%~dp0.."
pyinstaller --onefile --windowed --name "路由器面板" ^
  --hidden-import http.server ^
  --add-data "panel;panel" ^
  --add-data "router;router" ^
  --add-data "tools;tools" ^
  desktop\panel_client.py
echo 构建完成: dist\路由器面板.exe
pause