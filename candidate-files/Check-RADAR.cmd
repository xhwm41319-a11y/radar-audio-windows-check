@echo off
call "%~dp0Start-RADAR.cmd" --check
if errorlevel 1 exit /b 2
"%~dp0CoverLab\runtime\python\python.exe" -X utf8 -B "%~dp0CoverLab\CoverLab\native_windows_acceptance.py"
set "result=%errorlevel%"
pause
exit /b %result%
