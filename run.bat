@echo off
title Attendance Report Generator
cd /d "%~dp0"

echo ====================================================================
echo   Starting Biometric ^& TimeChamp Attendance Report Generator...
echo ====================================================================
echo.

:: Detect Python
set "PYTHON_CMD="
where python >nul 2>nul
if %errorlevel% equ 0 (
    set "PYTHON_CMD=python"
) else (
    where py >nul 2>nul
    if %errorlevel% equ 0 (
        set "PYTHON_CMD=py -3"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "PYTHON_CMD="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
    ) else (
        echo [ERROR] Python was not found on your system!
        echo Please ensure Python is installed and added to your PATH.
        echo.
        pause
        exit /b 1
    )
)

:: Run the application
%PYTHON_CMD% app.py

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] The application stopped unexpectedly with error code %errorlevel%.
    echo.
    pause
)
