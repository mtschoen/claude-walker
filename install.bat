@echo off
REM Build the C++ walker and install as agent-walker.exe at %USERPROFILE%\.local\bin
REM (with a claude-walker.exe copy kept as a back-compat alias for existing
REM callers), then register the search MCP server.
REM
REM Usage: install.bat [--project [DIR]]
REM   (no flag)        register the MCP server at `user` scope (global, every project)
REM   --project        register at `local` scope for the directory you invoke from
REM   --project DIR    register at `local` scope for DIR
setlocal enabledelayedexpansion

REM Capture the invocation directory BEFORE pushd so --project can default to
REM "the project the user ran the installer from".
set "INVOCATION_DIR=%CD%"
set "SCRIPT_DIR=%~dp0"
set "MCP_SCOPE=user"
set "PROJECT_DIR="

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--project" (
    set "MCP_SCOPE=local"
    set "NEXT=%~2"
    if defined NEXT if not "!NEXT:~0,2!"=="--" (
        set "PROJECT_DIR=%~2"
        shift
    )
    shift
    goto parse_args
)
echo install.bat: unknown argument: %~1
echo Usage: install.bat [--project [DIR]]
endlocal
exit /b 2
:args_done
if /I "%MCP_SCOPE%"=="local" if not defined PROJECT_DIR set "PROJECT_DIR=%INVOCATION_DIR%"

pushd "%~dp0"

cmake -S cpp -B cpp\build -DCMAKE_BUILD_TYPE=Release || goto :error
cmake --build cpp\build --config Release -j || goto :error

set "WALKER_BIN="
for %%f in (cpp\build\Release\walker.exe cpp\build\walker.exe) do (
    if exist "%%f" (
        set "WALKER_BIN=%%f"
        goto :found
    )
)
echo install.bat: built walker.exe not found under cpp\build\
goto :error

:found
set "INSTALL_DIR=%USERPROFILE%\.local\bin"
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
copy /Y "%WALKER_BIN%" "%INSTALL_DIR%\agent-walker.exe" >nul || goto :error
REM claude-walker.exe is a deprecated back-compat alias for existing callers
REM (progress-beacon hooks, statusline lookups). Remove once every consumer
REM has migrated to the agent-walker name.
copy /Y "%WALKER_BIN%" "%INSTALL_DIR%\claude-walker.exe" >nul || goto :error
echo installed %WALKER_BIN% -^> %INSTALL_DIR%\agent-walker.exe ^(alias: claude-walker.exe^)

REM Smoke test: bare-flag invocation routes to cost mode.
"%INSTALL_DIR%\agent-walker.exe" --period 86400 --win-start 0 >nul || goto :smoke_failed
echo smoke test ok

REM Warn only if INSTALL_DIR isn't on the PERSISTED PATH (User or Machine).
REM Deliberately NOT a grep of this session's %PATH%: after the user adds the
REM dir (via :path_note or the GUI), this cmd session's %PATH% stays stale until
REM a new terminal opens, so a %PATH% check would warn on every re-run even
REM though the dir is permanently installed. The persisted PATH is also exactly
REM what fresh processes -- the recency-nudge hook, the status line -- will see.
powershell -NoProfile -Command "$d=('%INSTALL_DIR%').TrimEnd([char]92); $raw=(@([Environment]::GetEnvironmentVariable('Path','User'),[Environment]::GetEnvironmentVariable('Path','Machine')) -join ';'); if ((($raw -split ';' | ForEach-Object { $_.Trim().TrimEnd([char]92) }) -icontains $d)) { exit 0 } else { exit 1 }"
if errorlevel 1 call :path_note

call :register_mcp

popd
endlocal
exit /b 0

:register_mcp
REM Additive: a registration failure warns but does not fail the binary install.
REM The server runs out of a dedicated venv at mcp\.venv to host the `mcp` SDK,
REM so the registration doesn't depend on whatever `python` happens to be on PATH.
set "SERVER_PATH=%SCRIPT_DIR%mcp\server.py"
set "VENV_DIR=%SCRIPT_DIR%mcp\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

where claude >nul 2>&1
if errorlevel 1 (
    echo.
    echo Note: 'claude' CLI not on PATH; skipped MCP server registration.
    echo Register later with:
    echo   claude mcp add agent-walker -s user -- "%VENV_PY%" "%SERVER_PATH%"
    goto :eof
)

call :ensure_venv
set "VENV_READY=!errorlevel!"
if "!VENV_READY!"=="0" (
    REM Idempotent: pip install is fast when the wheel is already cached.
    "%VENV_PY%" -m pip install --quiet --upgrade mcp
    if errorlevel 1 (
        echo warning: failed to install 'mcp' SDK into %VENV_DIR%
        set "VENV_READY=1"
    )
)
if not "!VENV_READY!"=="0" (
    echo.
    echo Note: MCP server venv isn't ready. Registration will still land, but the
    echo server won't start until the venv exists and has the 'mcp' SDK installed.
)

if /I "%MCP_SCOPE%"=="local" (
    pushd "%PROJECT_DIR%"
    claude mcp remove claude-walker -s local >nul 2>&1
    claude mcp remove agent-walker -s local >nul 2>&1
    claude mcp add agent-walker -s local -- "%VENV_PY%" "%SERVER_PATH%"
    set "MCP_RC=!errorlevel!"
    popd
    if not "!MCP_RC!"=="0" (
        echo warning: MCP registration ^(local scope^) failed for %PROJECT_DIR%
    ) else (
        echo registered agent-walker MCP server ^(local scope^) for %PROJECT_DIR%
    )
) else (
    claude mcp remove claude-walker -s user >nul 2>&1
    claude mcp remove agent-walker -s user >nul 2>&1
    claude mcp add agent-walker -s user -- "%VENV_PY%" "%SERVER_PATH%"
    if errorlevel 1 (
        echo warning: MCP registration ^(user scope^) failed
    ) else (
        echo registered agent-walker MCP server ^(user/global scope^)
    )
)
goto :eof

:ensure_venv
REM If the venv already exists, no work to do (pip upgrade runs unconditionally
REM in the caller, so a stale venv self-heals as long as its python is usable).
if exist "%VENV_PY%" exit /b 0
REM Find Python >=3.10 (the `mcp` SDK's floor). The `py` launcher (PEP 397) is
REM the canonical Windows tool for picking a specific version. Try newest first.
set "PY_LAUNCHER="
for %%v in (3.13 3.12 3.11 3.10) do (
    if not defined PY_LAUNCHER (
        py -%%v --version >nul 2>&1
        if not errorlevel 1 set "PY_LAUNCHER=py -%%v"
    )
)
REM Fall back to bare `python` if the launcher isn't installed -- then verify
REM the version meets the floor before using it.
if not defined PY_LAUNCHER (
    where python >nul 2>&1
    if not errorlevel 1 (
        python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY_LAUNCHER=python"
    )
)
if not defined PY_LAUNCHER (
    echo warning: no Python ^>=3.10 found on PATH; can't create %VENV_DIR%
    echo          install Python 3.10+ ^(see https://www.python.org/downloads/^) and re-run.
    exit /b 1
)
echo creating MCP server venv at %VENV_DIR% (using %PY_LAUNCHER%)
%PY_LAUNCHER% -m venv "%VENV_DIR%"
if errorlevel 1 exit /b 1
exit /b 0

:path_note
REM Do NOT suggest `setx PATH "%PATH%;..."` here. At a cmd prompt %PATH% is the
REM merged system+user PATH, so setx (no /M) writes the whole thing into the
REM *user* PATH (duplicating every system entry) and silently truncates at 1024
REM chars, dropping the tail. The PowerShell user-scope SetEnvironmentVariable
REM below reads only the user PATH and has no length cap.
echo.
echo Note: %INSTALL_DIR% is not on PATH. Add it before the recency-nudge
echo hook or status line can find agent-walker (or its claude-walker alias)
echo by name. To add it to your
echo User PATH safely (no setx 1024-char truncation, no system/user merge):
echo   powershell -NoProfile -Command "$p=[Environment]::GetEnvironmentVariable('Path','User'); if(-not $p){$p=''}; if(($p -split ';') -notcontains '%INSTALL_DIR%'){[Environment]::SetEnvironmentVariable('Path',($p.TrimEnd(';')+';%INSTALL_DIR%').TrimStart(';'),'User')}"
echo Or edit it via the GUI: rundll32 sysdm.cpl,EditEnvironmentVariables
echo Then open a new terminal for the change to take effect.
goto :eof

:smoke_failed
echo smoke test FAILED
popd
endlocal
exit /b 1

:error
popd
endlocal
exit /b 1
