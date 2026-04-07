import json
import logging
import os
import traceback
import time
import sys
from contextlib import contextmanager
from datetime import datetime, date
from functools import wraps
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Callable, Optional


def make_json_serializable(obj: Any) -> Any:
    """
    Recursively convert non-JSON-serializable objects to serializable types.
    Handles ObjectId, datetime, date, and other common non-serializable types.
    """
    try:
        # Try to import bson.ObjectId (may not be available in all environments)
        from bson import ObjectId
        if isinstance(obj, ObjectId):
            return str(obj)
    except ImportError:
        pass
    
    # Handle datetime objects
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    
    # Handle dictionaries
    if isinstance(obj, dict):
        return {key: make_json_serializable(value) for key, value in obj.items()}
    
    # Handle lists and tuples
    if isinstance(obj, (list, tuple)):
        return [make_json_serializable(item) for item in obj]
    
    # Handle sets
    if isinstance(obj, set):
        return [make_json_serializable(item) for item in obj]
    
    # Try to convert to string if it's not a basic JSON type
    try:
        # Test if it's JSON serializable
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        # If not serializable, convert to string representation
        return str(obj)


class LiveKitJSONFormatter(logging.Formatter):
    """Logging formatter that outputs logs in LiveKit-style structured JSON format.
    
    Format: {"level":"info","ts":1234567890.123,"logger":"module.name","caller":"file.py:123","msg":"message",...}
    """
    
    def _get_caller(self, record: logging.LogRecord) -> str:
        """Get caller information in format 'filename:lineno'"""
        if record.pathname:
            filename = os.path.basename(record.pathname)
            return f"{filename}:{record.lineno}"
        return "unknown:0"
    
    def _get_timestamp_nanos(self) -> float:
        """Get current timestamp in seconds with nanosecond precision (like Go's time.Now().UnixNano() / 1e9)"""
        return time.time()
    
    def _level_to_string(self, level: int) -> str:
        """Convert numeric log level to string (info, warn, error, debug)"""
        level_map = {
            logging.DEBUG: "debug",
            logging.INFO: "info",
            logging.WARNING: "warn",
            logging.ERROR: "error",
            logging.CRITICAL: "error",  # LiveKit uses "error" for critical
        }
        return level_map.get(level, "info")
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as LiveKit-style JSON"""
        # Base log structure matching LiveKit format
        log_entry = {
            "level": self._level_to_string(record.levelno),
            "ts": self._get_timestamp_nanos(),
            "logger": record.name,
            "caller": self._get_caller(record),
        }
        
        # Handle the message
        message = record.msg
        
        # If message is a dict, merge it into log_entry (LiveKit style)
        if isinstance(message, dict):
            # Create a copy to avoid modifying the original
            message_copy = dict(message)
            
            # Extract 'msg' if present, otherwise use a default
            if "msg" in message_copy:
                log_entry["msg"] = message_copy.pop("msg")
            elif "message" in message_copy:
                log_entry["msg"] = message_copy.pop("message")
            elif "event" in message_copy:
                log_entry["msg"] = message_copy.pop("event")
            else:
                log_entry["msg"] = "log_entry"
            
            # Merge remaining fields from message dict
            for key, value in message_copy.items():
                # Convert non-serializable values
                log_entry[key] = make_json_serializable(value)
        
        # If message is a string, use it as msg
        elif isinstance(message, str):
            stripped = message.strip()
            # Check if it's a JSON string
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, dict):
                        # Create a copy to avoid modifying the original
                        parsed_copy = dict(parsed)
                        
                        # Merge JSON dict into log_entry
                        if "msg" in parsed_copy:
                            log_entry["msg"] = parsed_copy.pop("msg")
                        elif "message" in parsed_copy:
                            log_entry["msg"] = parsed_copy.pop("message")
                        else:
                            log_entry["msg"] = "log_entry"
                        
                        for key, value in parsed_copy.items():
                            log_entry[key] = make_json_serializable(value)
                    else:
                        log_entry["msg"] = stripped
                except (ValueError, TypeError):
                    log_entry["msg"] = stripped
            else:
                log_entry["msg"] = stripped
        
        # For other types, convert to string
        else:
            log_entry["msg"] = str(message)
        
        # Handle exception info
        if record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            log_entry["error"] = {
                "type": exc_type.__name__ if exc_type else None,
                "message": str(exc_value) if exc_value else None,
                "traceback": traceback.format_exception(exc_type, exc_value, exc_tb) if exc_tb else None
            }
        
        # Add any extra fields from record
        if hasattr(record, 'extra_fields') and isinstance(record.extra_fields, dict):
            for key, value in record.extra_fields.items():
                if key not in log_entry:  # Don't overwrite existing fields
                    log_entry[key] = make_json_serializable(value)
        
        # Convert to JSON string (compact, not pretty-printed, like LiveKit)
        try:
            return json.dumps(log_entry, ensure_ascii=False, separators=(',', ':'))
        except (TypeError, ValueError) as e:
            # Fallback if serialization fails
            log_entry["msg"] = f"Log serialization error: {str(e)}. Original: {str(message)}"
            return json.dumps(log_entry, ensure_ascii=False, separators=(',', ':'))


class PrettyJSONFormatter(logging.Formatter):
    """Logging formatter that pretty-prints dict/list/JSON string payloads."""

    def _prettify(self, record: logging.LogRecord) -> str:
        message = record.msg
        # Note: Line number is already included in the format string, so we don't add it here
        # to avoid duplication

        if isinstance(message, (dict, list)):
            try:
                # Convert non-serializable objects to serializable types
                serializable_message = make_json_serializable(message)
                pretty = json.dumps(serializable_message, indent=2, ensure_ascii=False)
                return pretty
            except (TypeError, ValueError) as e:
                # If serialization still fails, return string representation
                return str(message)

        if isinstance(message, str):
            stripped = message.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    # Convert non-serializable objects in parsed JSON
                    serializable_parsed = make_json_serializable(parsed)
                    pretty = json.dumps(serializable_parsed, indent=2, ensure_ascii=False)
                    return pretty
                except (ValueError, TypeError):
                    pass
        # For non-JSON messages, return as-is (line number is in format string)
        return str(message)

    def format(self, record: logging.LogRecord) -> str:
        # Preserve original message
        original_msg = record.msg

        try:
            # Include exception info if present
            if record.exc_info:
                # Format exception with traceback
                exc_text = self.formatException(record.exc_info)
                if isinstance(original_msg, (dict, list)):
                    # Add exception to structured log
                    if isinstance(original_msg, dict):
                        original_msg = {**original_msg, "exception": exc_text}
                    else:
                        original_msg = {"message": original_msg, "exception": exc_text}
                elif isinstance(original_msg, str):
                    original_msg = f"{original_msg}\n{exc_text}"
                else:
                    original_msg = f"{str(original_msg)}\n{exc_text}"
            
            record.msg = self._prettify(record)
            record.args = ()  # Avoid double formatting
            formatted = super().format(record)
        finally:
            # Restore original message so other handlers (if any) remain unaffected
            record.msg = original_msg
        return formatted


def configure_pretty_logging(
    format_string: str = None,
    level: int = logging.INFO,
    log_dir: str = "logs",
    log_file_prefix: str = "app",
    backup_count: int = 30,
    when: str = "midnight",
    interval: int = 1,
    separate_error_log: bool = True,
    use_livekit_format: bool = True
) -> None:
    """Configure root logger with LiveKit-style JSON formatting and daily rotating file logs.
    
    Args:
        format_string: Log format string (ignored if use_livekit_format=True). 
                      Kept for backward compatibility.
        level: Logging level (default: INFO)
        log_dir: Directory for log files (default: "logs")
        log_file_prefix: Prefix for log file names (default: "app")
        backup_count: Number of backup log files to keep (default: 30 days)
        when: When to rotate logs. Options: 'S' (seconds), 'M' (minutes), 'H' (hours), 
              'D' (days), 'midnight' (default: "midnight")
        interval: Interval for rotation (default: 1, meaning every day at midnight)
        separate_error_log: Create separate error log file for ERROR and CRITICAL logs (default: True)
        use_livekit_format: Use LiveKit-style JSON format (default: True)
    """
    # Ensure log directory exists
    os.makedirs(log_dir, exist_ok=True)

    # Choose formatter based on format preference
    if use_livekit_format:
        formatter = LiveKitJSONFormatter()
    else:
        # Default format includes line numbers if not provided
        if format_string is None:
            format_string = '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
        formatter = PrettyJSONFormatter(format_string)

    # Console handler for immediate output
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)

    # File handler with daily rotation
    log_file_path = os.path.join(log_dir, f"{log_file_prefix}.log")
    file_handler = TimedRotatingFileHandler(
        filename=log_file_path,
        when=when,
        interval=interval,
        backupCount=backup_count,
        encoding="utf-8",
        utc=True,
        delay=False
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    
    # Set suffix for rotated files (date-based naming)
    # Format: app.log.2024-01-01, app.log.2024-01-02, etc.
    file_handler.suffix = "%Y-%m-%d"
    
    # Separate error log file (ERROR and CRITICAL only)
    error_handler = None
    if separate_error_log:
        error_log_path = os.path.join(log_dir, f"{log_file_prefix}-error.log")
        error_handler = TimedRotatingFileHandler(
            filename=error_log_path,
            when=when,
            interval=interval,
            backupCount=backup_count,
            encoding="utf-8",
            utc=True,
            delay=False
        )
        error_handler.setFormatter(formatter)
        error_handler.setLevel(logging.ERROR)  # Only ERROR and CRITICAL
        error_handler.suffix = "%Y-%m-%d"
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers = []  # Clear existing handlers
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    if error_handler:
        root_logger.addHandler(error_handler)
    
    # Log the configuration (use a temporary handler if logger not ready)
    try:
        logger = logging.getLogger(__name__)
        logger.info({
            "msg": "logging_configured",
            "event": "logging_configured",
            "log_dir": log_dir,
            "log_file": log_file_path,
            "backup_count": backup_count,
            "rotation": f"daily at {when}",
            "level": logging.getLevelName(level),
            "format": "livekit_json" if use_livekit_format else "pretty_json"
        })
    except Exception:
        # If logging fails during configuration, print to console
        format_type = "LiveKit JSON" if use_livekit_format else "Pretty JSON"
        print(f"Logging configured ({format_type}): {log_file_path}, rotation: daily at {when}, backups: {backup_count}")


def log_json(logger: logging.Logger, label: str, payload: Any) -> None:
    """Emit structured logging data to benefit from the pretty JSON formatter."""
    try:
        logger.info({label: payload})
    except Exception:
        logger.info(f"{label}: {payload}")


def cleanup_old_logs(log_dir: str, log_file_prefix: str, days_to_keep: int = 30) -> int:
    """
    Manually clean up old log files that are older than the specified number of days.
    
    Args:
        log_dir: Directory containing log files
        log_file_prefix: Prefix of log files to clean up
        days_to_keep: Number of days of logs to keep (default: 30)
    
    Returns:
        Number of files deleted
    """
    import glob
    import time
    
    if not os.path.exists(log_dir):
        return 0
    
    # Calculate cutoff time (days_to_keep days ago)
    cutoff_time = time.time() - (days_to_keep * 24 * 60 * 60)
    
    # Pattern to match rotated log files: prefix.log.YYYY-MM-DD
    pattern = os.path.join(log_dir, f"{log_file_prefix}.log.*")
    deleted_count = 0
    
    try:
        for log_file in glob.glob(pattern):
            try:
                # Check if file is older than cutoff
                if os.path.getmtime(log_file) < cutoff_time:
                    os.remove(log_file)
                    deleted_count += 1
                    logger = logging.getLogger(__name__)
                    logger.debug(f"Deleted old log file: {log_file}")
            except (OSError, IOError) as e:
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to delete log file {log_file}: {e}")
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error during log cleanup: {e}")
    
    return deleted_count


def log_exception(logger: logging.Logger, message: str, exc_info: Exception = None, **kwargs) -> None:
    """
    Log an exception with full stack trace in a structured format.
    
    Args:
        logger: Logger instance
        message: Error message
        exc_info: Exception object (optional, will use current exception if None)
        **kwargs: Additional context to include in the log
    """
    import sys
    
    exc_type, exc_value, exc_traceback = sys.exc_info() if exc_info is None else (
        type(exc_info), exc_info, exc_info.__traceback__
    )
    
    log_data = {
        "event": "exception",
        "message": message,
        "exception_type": exc_type.__name__ if exc_type else None,
        "exception_message": str(exc_value) if exc_value else None,
        "traceback": traceback.format_exception(exc_type, exc_value, exc_traceback) if exc_traceback else None,
        **kwargs
    }
    
    logger.error(log_data, exc_info=exc_info)


@contextmanager
def log_execution_time(logger: logging.Logger, operation_name: str, **context):
    """
    Context manager to log execution time of an operation.
    
    Usage:
        with log_execution_time(logger, "database_query", query_id="123"):
            # Your code here
            result = db.query(...)
    """
    start_time = time.time()
    try:
        logger.debug({
            "event": "operation_started",
            "operation": operation_name,
            **context
        })
        yield
    except Exception as e:
        elapsed = time.time() - start_time
        log_exception(logger, f"Operation '{operation_name}' failed", exc_info=e, 
                     operation=operation_name, execution_time_seconds=elapsed, **context)
        raise
    else:
        elapsed = time.time() - start_time
        logger.info({
            "event": "operation_completed",
            "operation": operation_name,
            "execution_time_seconds": round(elapsed, 4),
            **context
        })


def log_performance(func: Callable = None, *, logger: Optional[logging.Logger] = None, 
                   operation_name: Optional[str] = None):
    """
    Decorator to log function execution time and performance metrics.
    
    Usage:
        @log_performance(logger=logger, operation_name="get_user")
        async def get_user(user_id: str):
            # Your code here
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        async def async_wrapper(*args, **kwargs):
            func_logger = logger or logging.getLogger(f.__module__)
            op_name = operation_name or f"{f.__module__}.{f.__name__}"
            
            with log_execution_time(func_logger, op_name, 
                                  function=f.__name__,
                                  args=str(args)[:200],  # Limit length
                                  kwargs=str(kwargs)[:200]):
                return await f(*args, **kwargs)
        
        @wraps(f)
        def sync_wrapper(*args, **kwargs):
            func_logger = logger or logging.getLogger(f.__module__)
            op_name = operation_name or f"{f.__module__}.{f.__name__}"
            
            with log_execution_time(func_logger, op_name,
                                  function=f.__name__,
                                  args=str(args)[:200],
                                  kwargs=str(kwargs)[:200]):
                return f(*args, **kwargs)
        
        # Return appropriate wrapper based on whether function is async
        import asyncio
        if asyncio.iscoroutinefunction(f):
            return async_wrapper
        else:
            return sync_wrapper
    
    if func is None:
        return decorator
    else:
        return decorator(func)


def sanitize_log_data(data: dict, sensitive_keys: list = None) -> dict:
    """
    Remove or mask sensitive data from log entries.
    
    Args:
        data: Dictionary to sanitize
        sensitive_keys: List of keys to mask (default: common sensitive keys)
    
    Returns:
        Sanitized dictionary
    """
    if sensitive_keys is None:
        sensitive_keys = [
            'password', 'hashed_password', 'token', 'access_token', 
            'refresh_token', 'api_key', 'secret', 'authorization',
            'auth', 'credential', 'private_key', 'secret_key'
        ]
    
    sanitized = {}
    for key, value in data.items():
        key_lower = key.lower()
        # Check if any sensitive key is in the current key
        if any(sensitive in key_lower for sensitive in sensitive_keys):
            sanitized[key] = "***REDACTED***"
        elif isinstance(value, dict):
            sanitized[key] = sanitize_log_data(value, sensitive_keys)
        elif isinstance(value, list):
            sanitized[key] = [
                sanitize_log_data(item, sensitive_keys) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            sanitized[key] = value
    
    return sanitized


def log_request_response(logger: logging.Logger, request_data: dict, 
                        response_data: dict = None, status_code: int = None,
                        execution_time: float = None, sanitize: bool = True):
    """
    Log HTTP request and response in a structured format.
    
    Args:
        logger: Logger instance
        request_data: Request data (method, path, headers, body, etc.)
        response_data: Response data (status, body, etc.)
        status_code: HTTP status code
        execution_time: Request execution time in seconds
        sanitize: Whether to sanitize sensitive data (default: True)
    """
    log_entry = {
        "event": "http_request",
        "request": sanitize_log_data(request_data) if sanitize else request_data,
    }
    
    if response_data is not None:
        log_entry["response"] = sanitize_log_data(response_data) if sanitize else response_data
    
    if status_code is not None:
        log_entry["status_code"] = status_code
    
    if execution_time is not None:
        log_entry["execution_time_seconds"] = round(execution_time, 4)
    
    # Log at appropriate level based on status code
    if status_code and status_code >= 500:
        logger.error(log_entry)
    elif status_code and status_code >= 400:
        logger.warning(log_entry)
    else:
        logger.info(log_entry)


class UvicornAccessLogFormatter(LiveKitJSONFormatter):
    """Custom formatter for uvicorn access logs in JSON format."""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format uvicorn access log record as JSON."""
        import re
        
        # Parse the uvicorn access log message
        # Format: "127.0.0.1:58128 - "GET /api/v1/bot/sdk-sip/health HTTP/1.1" 200 OK"
        message = record.getMessage()
        
        # Extract information from the access log message
        # Pattern: IP:PORT - "METHOD PATH PROTOCOL" STATUS STATUS_TEXT
        pattern = r'(\S+):(\d+)\s+-\s+"(\S+)\s+(\S+)\s+([^"]+)"\s+(\d+)\s+(.+)'
        match = re.match(pattern, message)
        
        if match:
            client_ip, client_port, method, path, protocol, status_code, status_text = match.groups()
            
            log_entry = {
                "level": self._level_to_string(record.levelno),
                "ts": self._get_timestamp_nanos(),
                "logger": "uvicorn.access",
                "caller": self._get_caller(record),
                "msg": "http_request",
                "event": "http_access",
                "client_ip": client_ip,
                "client_port": int(client_port),
                "method": method,
                "path": path,
                "protocol": protocol,
                "status_code": int(status_code),
                "status_text": status_text.strip()
            }
        else:
            # Fallback if pattern doesn't match
            log_entry = {
                "level": self._level_to_string(record.levelno),
                "ts": self._get_timestamp_nanos(),
                "logger": "uvicorn.access",
                "caller": self._get_caller(record),
                "msg": message,
                "event": "http_access"
            }
        
        # Convert to JSON string
        try:
            return json.dumps(log_entry, ensure_ascii=False, separators=(',', ':'))
        except (TypeError, ValueError) as e:
            log_entry["msg"] = f"Log serialization error: {str(e)}. Original: {message}"
            return json.dumps(log_entry, ensure_ascii=False, separators=(',', ':'))


def configure_uvicorn_access_logging():
    """Configure uvicorn access logger to use JSON format."""
    # Get uvicorn access logger
    access_logger = logging.getLogger("uvicorn.access")
    
    # Remove existing handlers
    access_logger.handlers = []
    
    # Create console handler with JSON formatter
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(UvicornAccessLogFormatter())
    console_handler.setLevel(logging.INFO)
    
    # Add handler to access logger
    access_logger.addHandler(console_handler)
    access_logger.setLevel(logging.INFO)
    access_logger.propagate = False  # Don't propagate to root logger to avoid duplicate logs
