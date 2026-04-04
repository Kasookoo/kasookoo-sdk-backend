@echo off
REM Kasookoo WebRTC SDK Backend - Server Management Script for Windows

if "%1"=="" (
    echo.
    echo Kasookoo WebRTC SDK Backend - Server Manager
    echo ==========================================
    echo.
    echo Usage: server.bat [command]
    echo.
    echo Commands:
    echo   start     - Start the server in background
    echo   stop      - Stop the server
    echo   restart   - Restart the server
    echo   status    - Show server status
    echo   logs      - Show recent logs
    echo   health    - Check server health
    echo   dev       - Start in development mode (foreground)
    echo.
    echo Examples:
    echo   server.bat start
    echo   server.bat status
    echo   server.bat logs
    echo.
    goto :eof
)

if "%1"=="dev" (
    echo Starting server in development mode...
    python start_server.py
    goto :eof
)

python server_manager.py %*
