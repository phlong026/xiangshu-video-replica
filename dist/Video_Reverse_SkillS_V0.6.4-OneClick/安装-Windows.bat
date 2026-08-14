@echo off
chcp 65001 >nul
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0安装-Windows.ps1"
set "INSTALL_EXIT=%ERRORLEVEL%"

echo.
if not "%INSTALL_EXIT%"=="0" (
  echo 安装未完成，请查看上方错误信息和 install-logs 目录。
) else (
  echo 安装完成。
)
pause
exit /b %INSTALL_EXIT%
