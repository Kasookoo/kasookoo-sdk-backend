#!/usr/bin/env python3
"""
Simple Server Manager for Kasookoo WebRTC SDK Backend
Provides start, stop, status, and health check functionality
"""

import os
import sys
import time
import subprocess
import requests
import json
import logging
from pathlib import Path
from datetime import datetime
import argparse

class SimpleServerManager:
    def __init__(self):
        self.project_root = Path(__file__).parent
        self.pid_file = self.project_root / "server.pid"
        self.log_file = self.project_root / "server.log"
        self.port = 7000
        self.host = "0.0.0.0"
        self.base_url = f"http://localhost:{self.port}"
        
        # Setup logging
        self.setup_logging()
    
    def setup_logging(self):
        """Setup logging configuration"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        self.logger = logging.getLogger(__name__)
        
    def get_server_pid(self):
        """Get server PID from file"""
        if self.pid_file.exists():
            try:
                with open(self.pid_file, 'r') as f:
                    return int(f.read().strip())
            except (ValueError, FileNotFoundError):
                return None
        return None
    
    def is_process_running(self, pid):
        """Check if a process is running (Windows compatible)"""
        try:
            # Try to send signal 0 to check if process exists
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
    
    def start_server(self, reload=True, background=True):
        """Start the server"""
        self.logger.info("Starting Kasookoo WebRTC SDK Backend...")
        
        # Check if server is already running
        if self.is_running():
            self.logger.warning(f"Server is already running on port {self.port}")
            return False
        
        # Activate virtual environment and start server
        venv_python = self.project_root / "venv" / "Scripts" / "python.exe"
        if not venv_python.exists():
            venv_python = self.project_root / "venv" / "bin" / "python"
        
        if not venv_python.exists():
            self.logger.error("Virtual environment not found. Please run setup first.")
            return False
        
        # Build uvicorn command
        cmd = [
            str(venv_python), "-m", "uvicorn",
            "app.main:app",
            "--host", self.host,
            "--port", str(self.port),
            "--log-level", "info"
        ]
        
        if reload:
            cmd.append("--reload")
        
        try:
            if background:
                # Start in background
                with open(self.log_file, 'w') as log:
                    process = subprocess.Popen(
                        cmd,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        cwd=self.project_root
                    )
                
                # Save PID
                with open(self.pid_file, 'w') as f:
                    f.write(str(process.pid))
                
                # Wait a moment and check if it started successfully
                time.sleep(3)
                if self.is_running():
                    self.logger.info("Server started successfully!")
                    self.logger.info(f"Server URL: {self.base_url}")
                    self.logger.info(f"API Documentation: {self.base_url}/swagger")
                    self.logger.info(f"Logs: {self.log_file}")
                    self.logger.info(f"Process ID: {process.pid}")
                    return True
                else:
                    self.logger.error("Server failed to start. Check logs for details.")
                    return False
            else:
                # Start in foreground
                self.logger.info(f"Server will be available at: {self.base_url}")
                self.logger.info(f"API Documentation: {self.base_url}/swagger")
                print("Press Ctrl+C to stop the server")
                subprocess.run(cmd, cwd=self.project_root)
                return True
                
        except Exception as e:
            self.logger.error(f"Failed to start server: {e}")
            return False
    
    def stop_server(self):
        """Stop the server"""
        self.logger.info("Stopping Kasookoo WebRTC SDK Backend...")
        
        pid = self.get_server_pid()
        if not pid or not self.is_process_running(pid):
            self.logger.warning("Server is not running")
            self.cleanup_pid_file()
            return False
        
        try:
            # Try graceful shutdown first
            os.kill(pid, 15)  # SIGTERM
            
            # Wait for graceful shutdown
            for i in range(10):
                if not self.is_process_running(pid):
                    self.logger.info("Server stopped gracefully")
                    self.cleanup_pid_file()
                    return True
                time.sleep(1)
            
            # Force kill if graceful shutdown fails
            self.logger.warning("Graceful shutdown timed out, forcing stop...")
            os.kill(pid, 9)  # SIGKILL
            time.sleep(1)
            
            if not self.is_process_running(pid):
                self.logger.info("Server stopped forcefully")
                self.cleanup_pid_file()
                return True
            else:
                self.logger.error("Failed to stop server")
                return False
            
        except Exception as e:
            self.logger.error(f"Failed to stop server: {e}")
            return False
    
    def restart_server(self, reload=True):
        """Restart the server"""
        self.logger.info("Restarting Kasookoo WebRTC SDK Backend...")
        self.stop_server()
        time.sleep(2)
        return self.start_server(reload=reload)
    
    def is_running(self):
        """Check if server is running"""
        pid = self.get_server_pid()
        if not pid:
            return False
        
        if not self.is_process_running(pid):
            self.cleanup_pid_file()
            return False
        
        # Also check if the port is responding
        try:
            response = requests.get(f"{self.base_url}/api/v1/sip/health", timeout=2)
            return response.status_code == 200
        except:
            return False
    
    def get_status(self):
        """Get detailed server status"""
        pid = self.get_server_pid()
        
        if not pid or not self.is_process_running(pid):
            return {
                "running": False,
                "message": "Server is not running"
            }
        
        try:
            # Check health endpoint
            health_status = self.check_health()
            
            return {
                "running": True,
                "pid": pid,
                "port": self.port,
                "host": self.host,
                "url": self.base_url,
                "health": health_status
            }
        except Exception as e:
            return {
                "running": True,
                "error": f"Could not get detailed status: {e}"
            }
    
    def check_health(self):
        """Check server health via API"""
        try:
            response = requests.get(f"{self.base_url}/api/v1/sip/health", timeout=5)
            if response.status_code == 200:
                return {
                    "status": "healthy",
                    "response_time_ms": round(response.elapsed.total_seconds() * 1000, 2),
                    "data": response.json()
                }
            else:
                return {
                    "status": "unhealthy",
                    "status_code": response.status_code
                }
        except requests.exceptions.RequestException as e:
            return {
                "status": "unreachable",
                "error": str(e)
            }
    
    def show_logs(self, lines=50):
        """Show recent server logs"""
        if not self.log_file.exists():
            self.logger.info("No log file found")
            return
        
        self.logger.info(f"Showing recent server logs (last {lines} lines)")
        print("-" * 60)
        
        try:
            with open(self.log_file, 'r') as f:
                all_lines = f.readlines()
                recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                for line in recent_lines:
                    print(line.rstrip())
        except Exception as e:
            self.logger.error(f"Error reading logs: {e}")
    
    def cleanup_pid_file(self):
        """Remove PID file if it exists"""
        if self.pid_file.exists():
            try:
                self.pid_file.unlink()
            except Exception:
                pass
    
    def print_status(self):
        """Print formatted status information"""
        status = self.get_status()
        
        # Use print for status display (console output)
        print("=" * 60)
        print("Kasookoo WebRTC SDK Backend - Server Status")
        print("=" * 60)
        
        if status["running"]:
            print(f"[OK] Status: RUNNING")
            print(f"[ID] Process ID: {status.get('pid', 'Unknown')}")
            print(f"[URL] URL: {status.get('url', 'Unknown')}")
            
            # Health check
            health = status.get('health', {})
            if health.get('status') == 'healthy':
                print(f"[HEALTHY] Health: OK ({health.get('response_time_ms', 0)}ms)")
            elif health.get('status') == 'unhealthy':
                print(f"[WARNING] Health: UNHEALTHY (Status: {health.get('status_code', 'Unknown')})")
            else:
                print(f"[ERROR] Health: UNREACHABLE")
        else:
            print(f"[STOPPED] Status: NOT RUNNING")
            print(f"[INFO] Message: {status.get('message', 'Unknown')}")
        
        print("=" * 60)
        
        # Log the status check
        self.logger.info(f"Status check completed - Server running: {status['running']}")

def main():
    parser = argparse.ArgumentParser(description="Kasookoo WebRTC SDK Backend Server Manager")
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "logs", "health"], 
                       help="Command to execute")
    parser.add_argument("--no-reload", action="store_true", help="Disable auto-reload (for production)")
    parser.add_argument("--foreground", action="store_true", help="Run in foreground (don't daemonize)")
    parser.add_argument("--lines", type=int, default=50, help="Number of log lines to show (default: 50)")
    
    args = parser.parse_args()
    
    manager = SimpleServerManager()
    
    if args.command == "start":
        manager.start_server(reload=not args.no_reload, background=not args.foreground)
    elif args.command == "stop":
        manager.stop_server()
    elif args.command == "restart":
        manager.restart_server(reload=not args.no_reload)
    elif args.command == "status":
        manager.print_status()
    elif args.command == "logs":
        manager.show_logs(lines=args.lines)
    elif args.command == "health":
        health = manager.check_health()
        manager.logger.info("Health Check Results:")
        print(json.dumps(health, indent=2))

if __name__ == "__main__":
    main()
