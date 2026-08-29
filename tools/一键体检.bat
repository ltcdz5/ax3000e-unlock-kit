@echo off
rem ===== 一键体检：发现路由器 IP + 状态体检 + 自动修复启动器 IP =====
cd /d "%~dp0.."
python tools\kit_doctor.py --fix %*
pause
