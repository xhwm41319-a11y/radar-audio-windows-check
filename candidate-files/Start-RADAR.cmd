@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion
set "bundle=%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%bundle%verify-bootstrap.ps1" "%bundle%."
if errorlevel 1 goto failed
"%bundle%.bootstrap\python\python.exe" -X utf8 -I -B "%bundle%install_windows.py" %*
set "result=%errorlevel%"
if not "%result%"=="0" goto failed
exit /b 0
:failed
echo.
echo RADAR stopped. The installation log is in CoverLab\runtime when installation had started.
pause
exit /b 2
