@echo off
REM Double-click this file to start the quiz server on Windows.
cd /d "%~dp0"
where py >nul 2>nul && (py quiz_server.py) || (python quiz_server.py)
pause
