@echo off
rem AX3000E AP panel one-click launcher (relay/AP mode, recommended)
rem Keep this file ASCII-only (GBK console safe).
cd /d "%~dp0"
where python >nul 2>nul || (echo [!] Python not found. Install Python 3.9+ from python.org first. & pause & exit /b)
python -c "import paramiko" >nul 2>nul
if errorlevel 1 (
    echo [i] one-time setup: installing paramiko...
    pip install "paramiko<4,>=3" || (echo [!] pip install failed. Check network/proxy. & pause & exit /b)
)
set /p ROUTER_HOST=Router IP [default 192.168.2.106]:
if "%ROUTER_HOST%"=="" set ROUTER_HOST=192.168.2.106
set /p ROUTER_PASSWD=Router SSH password:
if "%ROUTER_PASSWD%"=="" (echo [!] password required & pause & exit /b)
set ROUTER_USER=root
set ROUTER_SSH_PORT=22
echo [i] starting AP panel on http://127.0.0.1:8787 ...
start "" /b cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8787"
python router_monitor_ap.py
echo.
echo [!] panel exited. Common causes: wrong password, paramiko>=4 installed, router IP moved.
echo     If IP moved: run arp -a and find MAC 58-ea-1f-ca-0c-b4, then retry with new IP.
pause
