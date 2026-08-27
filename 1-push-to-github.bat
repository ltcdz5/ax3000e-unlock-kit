@echo off
rem ===== double-click AFTER winget installs finish =====
setlocal
cd /d "%USERPROFILE%\ax3000e-unlock-kit"

where git >nul 2>nul || echo [!] need:  winget install --id Git.Git -e --source winget
where gh   >nul 2>nul || echo [!] need:  winget install --id GitHub.cli -e --source winget
where git >nul 2>nul && where gh >nul 2>nul || (pause & exit /b)

if not exist .git (
    git init -b main
    git add -A
    git commit -m "initial kit: panel + auto_ssh v4 + configs + self-rescue docs"
)

gh auth status >nul 2>nul
if errorlevel 1 (
    echo [i] browser will open for login...
    gh auth login --hostname github.com --git-protocol https --web
)

rem 同步本地 git 署名为 GitHub 账号(以后的新提交自动正确)
for /f "delims=" %%u in ('gh api user --jq .login') do (
    git config user.name  "%%u"
    git config user.email "%%u@users.noreply.github.com"
)

gh repo view ax3000e-unlock-kit >nul 2>nul
if errorlevel 1 (
    gh repo create ax3000e-unlock-kit --public --source . --push
) else (
    git push -u origin main
)

echo [ok] opening repo page...
gh repo view ax3000e-unlock-kit --web
echo [done] you can close this window
