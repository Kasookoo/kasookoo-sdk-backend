#!/usr/bin/env python3
"""
Simple startup script for Kasookoo WebRTC SDK Backend
Run this script to start the server on port 7000 in foreground mode
"""

import uvicorn
import os
import sys
import logging
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("Starting Kasookoo WebRTC SDK Backend...")
    logger.info("Server will be available at: http://localhost:7000")
    logger.info("API Documentation: http://localhost:7000/swagger")
    logger.info("Environment: Development")
    logger.info("Tip: Use 'python server_manager_simple.py start' for background mode")
    print("=" * 50)
    
    try:
        uvicorn.run(
            "app.main:app",
            host="127.0.0.1",
            port=7000,
            reload=True,
            log_level="info",
            access_log=True
        )
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server failed to start: {e}")
        sys.exit(1)
