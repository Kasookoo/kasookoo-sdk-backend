#!/usr/bin/env python3
"""
Server Manager for Kasookoo WebRTC SDK Backend
Provides start, stop, status, and health check functionality
"""

import os
import sys
import time
import signal
import subprocess
import psutil
import requests
import json
from pathlib import Path
from datetime import datetime
import argparse

class ServerManager:
    def __init__(self):
        self.project_root = Path(__file__).parent
        self.pid_file = self.project_root / "server.pid"
        self.log_file = self.project_root / "server.log"
        self.port = 7000
        self.host = "0.0.0.0"
        self.base_url = f"http://localhost:{self.port}"
        
    def get_server_process(self):
        """Find the server process by PID file or port"""
        # First try to get PID from file
        if self.pid_file.exists():
            try:
                with open(self.pid_file, 'r') as f:
                    pid = int(f.read().strip())
                if psutil.pid_exists(pid):
                    process = psutil.Process(pid)
                    # Verify it's our server process
                    if self.is_server_process(process):
                        return process
            except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        # If PID file doesn't work, find by port
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if self.is_server_process(proc):
                    return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None
    
    def is_server_process(self, process):
        """Check if a process is our server"""
        try:
            cmdline = ' '.join(process.cmdline())
            return ('uvicorn' in cmdline and 
                   'app.main:app' in cmdline and 
                   f'--port {self.port}' in cmdline)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
    
    def start_server(self, reload=True, background=True):
        """Start the server"""
        print("🚀 Starting Kasookoo WebRTC SDK Backend...")
        
        # Check if server is already running
        if self.is_running():
            print(f"⚠️  Server is already running on port {self.port}")
            return False
        
        # Activate virtual environment and start server
        venv_python = self.project_root / "venv" / "Scripts" / "python.exe"
        if not venv_python.exists():
            venv_python = self.project_root / "venv" / "bin" / "python"
        
        if not venv_python.exists():
            print("❌ Virtual environment not found. Please run setup first.")
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
                    print(f"✅ Server started successfully!")
                    print(f"📍 Server URL: {self.base_url}")
                    print(f"📚 API Documentation: {self.base_url}/swagger")
                    print(f"📝 Logs: {self.log_file}")
                    print(f"🆔 Process ID: {process.pid}")
                    return True
                else:
                    print("❌ Server failed to start. Check logs for details.")
                    return False
            else:
                # Start in foreground
                print(f"📍 Server will be available at: {self.base_url}")
                print(f"📚 API Documentation: {self.base_url}/swagger")
                print("Press Ctrl+C to stop the server")
                subprocess.run(cmd, cwd=self.project_root)
                return True
                
        except Exception as e:
            print(f"❌ Failed to start server: {e}")
            return False
    
    def stop_server(self):
        """Stop the server"""
        print("🛑 Stopping Kasookoo WebRTC SDK Backend...")
        
        process = self.get_server_process()
        if not process:
            print("⚠️  Server is not running")
            self.cleanup_pid_file()
            return False
        
        try:
            # Try graceful shutdown first
            process.terminate()
            
            # Wait for graceful shutdown
            try:
                process.wait(timeout=10)
                print("✅ Server stopped gracefully")
            except psutil.TimeoutExpired:
                # Force kill if graceful shutdown fails
                print("⚠️  Graceful shutdown timed out, forcing stop...")
                process.kill()
                process.wait()
                print("✅ Server stopped forcefully")
            
            self.cleanup_pid_file()
            return True
            
        except Exception as e:
            print(f"❌ Failed to stop server: {e}")
            return False
    
    def restart_server(self, reload=True):
        """Restart the server"""
        print("🔄 Restarting Kasookoo WebRTC SDK Backend...")
        self.stop_server()
        time.sleep(2)
        return self.start_server(reload=reload)
    
    def is_running(self):
        """Check if server is running"""
        process = self.get_server_process()
        if not process:
            return False
        
        try:
            # Check if process is still alive
            return process.is_running()
        except psutil.NoSuchProcess:
            return False
    
    def get_status(self):
        """Get detailed server status"""
        process = self.get_server_process()
        
        if not process:
            return {
                "running": False,
                "message": "Server is not running"
            }
        
        try:
            # Get process info
            cpu_percent = process.cpu_percent()
            memory_info = process.memory_info()
            create_time = datetime.fromtimestamp(process.create_time())
            
            # Check health endpoint
            health_status = self.check_health()
            
            return {
                "running": True,
                "pid": process.pid,
                "port": self.port,
                "host": self.host,
                "url": self.base_url,
                "cpu_percent": cpu_percent,
                "memory_mb": round(memory_info.rss / 1024 / 1024, 2),
                "started_at": create_time.isoformat(),
                "uptime": str(datetime.now() - create_time).split('.')[0],
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
            print("📝 No log file found")
            return
        
        print(f"📝 Recent server logs (last {lines} lines):")
        print("-" * 60)
        
        try:
            with open(self.log_file, 'r') as f:
                all_lines = f.readlines()
                recent_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                for line in recent_lines:
                    print(line.rstrip())
        except Exception as e:
            print(f"❌ Error reading logs: {e}")
    
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
        
        print("=" * 60)
        print("🔍 Kasookoo WebRTC SDK Backend - Server Status")
        print("=" * 60)
        
        if status["running"]:
            print(f"✅ Status: RUNNING")
            print(f"🆔 Process ID: {status.get('pid', 'Unknown')}")
            print(f"🌐 URL: {status.get('url', 'Unknown')}")
            print(f"📅 Started: {status.get('started_at', 'Unknown')}")
            print(f"⏱️  Uptime: {status.get('uptime', 'Unknown')}")
            print(f"💾 Memory: {status.get('memory_mb', 'Unknown')} MB")
            print(f"🖥️  CPU: {status.get('cpu_percent', 'Unknown')}%")
            
            # Health check
            health = status.get('health', {})
            if health.get('status') == 'healthy':
                print(f"💚 Health: HEALTHY ({health.get('response_time_ms', 0)}ms)")
            elif health.get('status') == 'unhealthy':
                print(f"💛 Health: UNHEALTHY (Status: {health.get('status_code', 'Unknown')})")
            else:
                print(f"💔 Health: UNREACHABLE")
        else:
            print(f"❌ Status: NOT RUNNING")
            print(f"📝 Message: {status.get('message', 'Unknown')}")
        
        print("=" * 60)

def main():
    parser = argparse.ArgumentParser(description="Kasookoo WebRTC SDK Backend Server Manager")
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "logs", "health"], 
                       help="Command to execute")
    parser.add_argument("--no-reload", action="store_true", help="Disable auto-reload (for production)")
    parser.add_argument("--foreground", action="store_true", help="Run in foreground (don't daemonize)")
    parser.add_argument("--lines", type=int, default=50, help="Number of log lines to show (default: 50)")
    
    args = parser.parse_args()
    
    manager = ServerManager()
    
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
        print("🏥 Health Check Results:")
        print(json.dumps(health, indent=2))

if __name__ == "__main__":
    main()
