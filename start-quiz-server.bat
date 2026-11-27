@echo off
REM Double-click this file to start the quiz server on Windows.
REM If Python is not installed, it tries to install it for you (no admin needed).
setlocal
cd /d "%~dp0"
title Classroom Quiz Server

set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY (
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\python.exe" set "PY="%%D\python.exe""
  )
)

if not defined PY (
  echo Python is not installed. Installing it now - this takes a minute or two...
  echo.
  winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements
  for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    if exist "%%D\python.exe" set "PY="%%D\python.exe""
  )
)

if not defined PY (
  echo.
  echo Could not install Python automatically.
  echo Please open the Microsoft Store, search "Python 3.12", click Get,
  echo then double-click this file again. If the Store is blocked, ask IT
  echo to install Python for you.
  echo.
  pause
  exit /b 1
)

%PY% quiz_server.py
pause
