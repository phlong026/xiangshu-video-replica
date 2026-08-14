@echo off
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if errorlevel 1 goto no_python
python install_skill.py
goto finished

:use_py
py -3 install_skill.py
goto finished

:no_python
echo 安装失败：未找到 Python 3。请先安装 Python 3 后重试。
exit /b 1

:finished
if errorlevel 1 (
  echo.
  echo 安装未完成，请查看上方错误信息。
) else (
  echo.
  echo 安装完成。
)
pause
