@echo off
rem ============================================================
rem  One-click sync: copy code from HuggingFace_Spaces to runx
rem  Source path is hardcoded. Double-click pauses at the end;
rem  pass /q for no pause (automation: sync.bat /q)
rem  runx-only files (main.py, Dockerfile, sync.bat) are untouched
rem ============================================================
setlocal
set "SRC=D:\test\github\HuggingFace_Spaces"
set "DST=%~dp0"
set "FAIL=0"

if not exist "%SRC%\app.py" goto :nosrc

copy /Y "%SRC%\app.py"           "%DST%app.py"           >nul || set "FAIL=1"
copy /Y "%SRC%\requirements.txt" "%DST%requirements.txt" >nul || set "FAIL=1"
copy /Y "%SRC%\README.md"        "%DST%README.md"        >nul || set "FAIL=1"
copy /Y "%SRC%\AGENTS.md"        "%DST%AGENTS.md"        >nul || set "FAIL=1"

rem robocopy exit codes 0-7 are success, 8+ are failures
robocopy "%SRC%\multi_mqtt" "%DST%multi_mqtt" /E /XD __pycache__ .pytest_cache /XF *.pyc *.pyo >nul
if errorlevel 8 set "FAIL=1"

if "%FAIL%"=="0" goto :ok
echo [sync FAILED] some files could not be copied
goto :end

:nosrc
echo [sync FAILED] source app.py not found: %SRC%
goto :end

:ok
echo [sync OK] %SRC% -^> %DST:~0,-1%

:end
if /I not "%~1"=="/q" pause
endlocal
