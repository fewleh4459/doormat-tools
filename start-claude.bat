@echo off
REM ============================================================
REM  Claude Code Launcher — Remote Control Enabled
REM  Run this file each time you start work on this machine.
REM ============================================================

cd /d "C:\Claude"

echo.
echo  ================================================
echo   Claude Code Workspace Launcher
echo   Remote Control: ENABLED
echo   Auto-Allow:     ENABLED
echo   Smart Routing:  ENABLED
echo  ================================================
echo.

claude remote-control --name "Olly Workspace"
