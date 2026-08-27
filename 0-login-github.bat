@echo off
rem ===== STEP 0: GitHub browser login - double click me FIRST =====
cd /d "%USERPROFILE%\ax3000e-unlock-kit"

gh auth login --hostname github.com --git-protocol https --web

echo ============================================
gh auth status
echo ============================================
echo if you saw "Logged in to github.com" above - press any key and
echo then run 1-push-to-github.bat next.
pause
