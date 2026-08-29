@echo off
rem ===== 维护者自用脚本（普通用户无需理会）：GitHub 浏览器登录 =====
cd /d "%USERPROFILE%\ax3000e-unlock-kit"

gh auth login --hostname github.com --git-protocol https --web

echo ============================================
gh auth status
echo ============================================
echo if you saw "Logged in to github.com" above - press any key and
echo then run 1-push-to-github.bat next.
pause
