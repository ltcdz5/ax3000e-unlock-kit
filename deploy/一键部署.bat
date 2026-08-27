@echo off
chcp 65001 >nul
echo ============================================
echo  小米 BE3600 一键部署
echo ============================================
echo.
echo 使用方法:
echo   1. 先解锁 BE3600 的 SSH（方式见说明.txt）
echo   2. 运行本脚本（会自动检测/配置/启动面板）
echo.
echo 参数: 一键部署.bat [BE3600-IP]
echo   默认 192.168.31.1（主路由）
echo   AP 模式填实际地址，如: 一键部署.bat 192.168.2.100
echo.
python "%~dp0一键部署.py" %1
echo.
pause
