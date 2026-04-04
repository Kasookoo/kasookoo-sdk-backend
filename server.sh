#!/bin/bash
# Kasookoo WebRTC SDK Backend - Server Management Script for Unix/Linux/macOS

if [ $# -eq 0 ]; then
    echo ""
    echo "Kasookoo WebRTC SDK Backend - Server Manager"
    echo "=========================================="
    echo ""
    echo "Usage: ./server.sh [command]"
    echo ""
    echo "Commands:"
    echo "  start     - Start the server in background"
    echo "  stop      - Stop the server"
    echo "  restart   - Restart the server"
    echo "  status    - Show server status"
    echo "  logs      - Show recent logs"
    echo "  health    - Check server health"
    echo "  dev       - Start in development mode (foreground)"
    echo ""
    echo "Examples:"
    echo "  ./server.sh start"
    echo "  ./server.sh status"
    echo "  ./server.sh logs"
    echo ""
    exit 0
fi

if [ "$1" = "dev" ]; then
    echo "Starting server in development mode..."
    python3 start_server.py
    exit $?
fi

python3 server_manager.py "$@"
