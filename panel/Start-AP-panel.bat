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
set "ROUTER_HOST="
set /p ROUTER_HOST=Router IP (the AX3000E itself, e.g. 192.168.2.x):
if not defined ROUTER_HOST (echo [!] IP required. Tip: ping devices in your subnet then run "arp -a" and match your router MAC. & pause & exit /b)
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "$s = Read-Host 'Router SSH password' -AsSecureString; [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))"`) do set "ROUTER_PASSWD=%%p"
if not defined ROUTER_PASSWD (echo [!] password required & pause & exit /b)
set ROUTER_USER=root
set ROUTER_SSH_PORT=22
echo [i] starting AP panel on http://127.0.0.1:8787 ...
start "" /b cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8787"
python router_monitor_ap.py
echo.
echo [!] panel exited. Common causes: wrong password, paramiko>=4 installed, router IP moved.
echo     If IP moved: ping devices in your subnet, run "arp -a", match your router MAC, retry.
pause
