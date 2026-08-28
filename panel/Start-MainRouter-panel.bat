@echo off
rem AX3000E main-router panel one-click launcher (router mode: full port-forward/QoS/DHCP/firewall)
rem Keep this file ASCII-only (GBK console safe).
cd /d "%~dp0"
where python >nul 2>nul || (echo [!] Python not found. Install Python 3.9+ from python.org first. & pause & exit /b)
python -c "import paramiko" >nul 2>nul
if errorlevel 1 (
    echo [i] one-time setup: installing paramiko...
    pip install "paramiko<4,>=3" || (echo [!] pip install failed. Check network/proxy. & pause & exit /b)
)
set /p ROUTER_HOST=Router IP [default 192.168.31.1]:
if "%ROUTER_HOST%"=="" set ROUTER_HOST=192.168.31.1
for /f "usebackq delims=" %%p in (`powershell -NoProfile -Command "$s = Read-Host 'Router SSH password' -AsSecureString; [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))"`) do set "ROUTER_PASSWD=%%p"
if not defined ROUTER_PASSWD (echo [!] password required & pause & exit /b)
set ROUTER_USER=root
set ROUTER_SSH_PORT=22
echo [i] starting main-router panel on http://127.0.0.1:8788 ...
echo [i] security: bound to 127.0.0.1 only. For LAN access add --lan --token to the python line (Advanced).
start "" /b cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8788"
python router_monitor_ax3000e.py --web 8788
echo.
echo [!] panel exited. Common causes: wrong password, paramiko>=4 installed, wrong mode (this panel is for ROUTER mode).
pause
