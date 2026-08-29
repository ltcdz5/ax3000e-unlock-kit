@echo off
rem ===== STEP 1: create public repo + push (run AFTER 0-login succeeded) =====
setlocal
cd /d "%USERPROFILE%\ax3000e-unlock-kit"

rem 自动跟随系统代理(梯子)设置
for /f "tokens=3" %%v in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer 2^>nul') do set HTTPS_PROXY=http://%%v
if defined HTTPS_PROXY echo [i] using proxy %HTTPS_PROXY%

gh auth status >nul 2>nul
if errorlevel 1 (
    echo [!] NOT logged in. Run 0-login-github.bat first.
    pause & exit /b
)

rem sync git identity to the logged-in account
for /f "delims=" %%u in ('gh api user --jq .login') do (
    git config user.name  "%%u"
    git config user.email "%%u@users.noreply.github.com"
    set GHUSER=%%u
)

if not exist .git (
    git init -b main
    git add -A
)
git commit -m "initial kit" --allow-empty-message >nul 2>&1

echo [i] creating repo and pushing...
gh repo view xiaomi-router-unlock-kit >nul 2>nul
if errorlevel 1 (
    gh repo create xiaomi-router-unlock-kit --public --source . --push
) else (
    git push -u origin main
)

echo ============================================
if defined GHUSER echo [ok] https://github.com/%GHUSER%/xiaomi-router-unlock-kit
pause
