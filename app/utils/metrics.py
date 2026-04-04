"""
Prometheus metrics module for monitoring API and system metrics.
Uses prometheus_client for metrics and psutil for system monitoring.
"""
import time
import psutil
from functools import wraps
from typing import Callable, Any
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse
import logging

logger = logging.getLogger(__name__)

# HTTP Request Metrics
http_requests_total = Counter(
    'http_requests_total',
    'Total number of HTTP requests',
    ['method', 'endpoint', 'status_code']
)

http_request_duration_seconds = Histogram(
    'http_request_duration_seconds',
    'HTTP request duration in seconds',
    ['method', 'endpoint', 'status_code'],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
)

# System Metrics (using psutil)
system_cpu_percent = Gauge(
    'system_cpu_percent',
    'System CPU usage percentage',
    ['cpu']
)

system_cpu_count = Gauge(
    'system_cpu_count',
    'Number of CPU cores'
)

system_memory_total_bytes = Gauge(
    'system_memory_total_bytes',
    'Total system memory in bytes'
)

system_memory_available_bytes = Gauge(
    'system_memory_available_bytes',
    'Available system memory in bytes'
)

system_memory_used_bytes = Gauge(
    'system_memory_used_bytes',
    'Used system memory in bytes'
)

system_memory_percent = Gauge(
    'system_memory_percent',
    'System memory usage percentage'
)

system_disk_total_bytes = Gauge(
    'system_disk_total_bytes',
    'Total disk space in bytes',
    ['device', 'mountpoint']
)

system_disk_used_bytes = Gauge(
    'system_disk_used_bytes',
    'Used disk space in bytes',
    ['device', 'mountpoint']
)

system_disk_free_bytes = Gauge(
    'system_disk_free_bytes',
    'Free disk space in bytes',
    ['device', 'mountpoint']
)

system_disk_percent = Gauge(
    'system_disk_percent',
    'Disk usage percentage',
    ['device', 'mountpoint']
)

system_network_bytes_sent = Gauge(
    'system_network_bytes_sent',
    'Total network bytes sent (cumulative)',
    ['interface']
)

system_network_bytes_recv = Gauge(
    'system_network_bytes_recv',
    'Total network bytes received (cumulative)',
    ['interface']
)

system_network_packets_sent = Gauge(
    'system_network_packets_sent',
    'Total network packets sent (cumulative)',
    ['interface']
)

system_network_packets_recv = Gauge(
    'system_network_packets_recv',
    'Total network packets received (cumulative)',
    ['interface']
)

# Process-specific metrics
process_cpu_percent = Gauge(
    'process_cpu_percent',
    'Current process CPU usage percentage'
)

process_memory_bytes = Gauge(
    'process_memory_bytes',
    'Current process memory usage in bytes'
)

process_memory_percent = Gauge(
    'process_memory_percent',
    'Current process memory usage percentage'
)

process_threads = Gauge(
    'process_threads',
    'Number of threads in the current process'
)

process_open_files = Gauge(
    'process_open_files',
    'Number of open file descriptors in the current process'
)

# API endpoint monitoring decorator
api_endpoint_calls = Counter(
    'api_endpoint_calls_total',
    'Total number of API endpoint calls',
    ['endpoint_name', 'method', 'status']
)

api_endpoint_duration = Histogram(
    'api_endpoint_duration_seconds',
    'API endpoint execution duration in seconds',
    ['endpoint_name', 'method'],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
)


def monitor_api(endpoint_name: str = None):
    """
    Decorator to add monitoring annotations to API endpoints.
    This adds endpoint-specific metrics in addition to the middleware tracking.
    
    Usage:
        @router.get("/endpoint")
        @monitor_api("get_endpoint")
        async def my_endpoint():
            ...
    
    Args:
        endpoint_name: Custom name for the endpoint in metrics. 
                     If None, uses the function name.
    """
    def decorator(func: Callable) -> Callable:
        # Store endpoint name as metadata
        name = endpoint_name or func.__name__
        func._monitor_endpoint_name = name
        
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            # Get endpoint name
            name = getattr(func, '_monitor_endpoint_name', func.__name__)
            method = "UNKNOWN"
            request = None
            
            # Try to find Request object in args or kwargs
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
                    method = arg.method
                    break
            
            if not request:
                for key, value in kwargs.items():
                    if isinstance(value, Request):
                        request = value
                        method = value.method
                        break
            
            start_time = time.time()
            status = "success"
            
            try:
                result = await func(*args, **kwargs)
                
                # Determine status from result
                if hasattr(result, 'status_code'):
                    status_code = result.status_code
                    status = "success" if 200 <= status_code < 400 else "error"
                elif isinstance(result, dict) and 'success' in result:
                    status = "success" if result.get('success') else "error"
                else:
                    status = "success"
                
                return result
            except Exception as e:
                status = "error"
                raise
            finally:
                duration = time.time() - start_time
                
                # Record endpoint-specific metrics
                api_endpoint_calls.labels(
                    endpoint_name=name,
                    method=method,
                    status=status
                ).inc()
                
                api_endpoint_duration.labels(
                    endpoint_name=name,
                    method=method
                ).observe(duration)
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            # Handle synchronous functions (less common in FastAPI)
            name = getattr(func, '_monitor_endpoint_name', func.__name__)
            method = "UNKNOWN"
            request = None
            
            for arg in args:
                if isinstance(arg, Request):
                    request = arg
                    method = arg.method
                    break
            
            if not request:
                for key, value in kwargs.items():
                    if isinstance(value, Request):
                        request = value
                        method = value.method
                        break
            
            start_time = time.time()
            status = "success"
            
            try:
                result = func(*args, **kwargs)
                
                if hasattr(result, 'status_code'):
                    status_code = result.status_code
                    status = "success" if 200 <= status_code < 400 else "error"
                elif isinstance(result, dict) and 'success' in result:
                    status = "success" if result.get('success') else "error"
                else:
                    status = "success"
                
                return result
            except Exception as e:
                status = "error"
                raise
            finally:
                duration = time.time() - start_time
                
                api_endpoint_calls.labels(
                    endpoint_name=name,
                    method=method,
                    status=status
                ).inc()
                
                api_endpoint_duration.labels(
                    endpoint_name=name,
                    method=method
                ).observe(duration)
        
        # Return appropriate wrapper based on whether function is async
        import inspect
        if inspect.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator


class PrometheusMiddleware(BaseHTTPMiddleware):
    """
    Middleware to track HTTP requests and expose metrics to Prometheus.
    """
    
    # Paths to exclude from monitoring (monitoring endpoints themselves)
    EXCLUDED_PATHS = [
        "/metrics",
        "/api/v1/monitoring/metrics",
        "/api/v1/monitoring/health",
        "/api/v1/monitoring/status",
        "/api/v1/monitoring/metrics/json",
    ]
    
    async def dispatch(self, request: Request, call_next):
        # Skip monitoring endpoints to avoid recursion and unnecessary overhead
        if any(request.url.path.startswith(path) for path in self.EXCLUDED_PATHS):
            return await call_next(request)
        
        method = request.method
        path = request.url.path
        
        # Start timer
        start_time = time.time()
        
        # Process request
        response = await call_next(request)
        
        # Calculate duration
        duration = time.time() - start_time
        
        # Get status code
        status_code = response.status_code
        
        # Record metrics
        http_requests_total.labels(
            method=method,
            endpoint=path,
            status_code=status_code
        ).inc()
        
        http_request_duration_seconds.labels(
            method=method,
            endpoint=path,
            status_code=status_code
        ).observe(duration)
        
        return response


def update_system_metrics():
    """
    Update all system metrics using psutil.
    Should be called periodically (e.g., every few seconds).
    """
    try:
        # CPU metrics
        cpu_percent = psutil.cpu_percent(interval=None, percpu=True)
        for i, cpu_usage in enumerate(cpu_percent):
            system_cpu_percent.labels(cpu=str(i)).set(cpu_usage)
        
        system_cpu_count.set(psutil.cpu_count())
        
        # Memory metrics
        mem = psutil.virtual_memory()
        system_memory_total_bytes.set(mem.total)
        system_memory_available_bytes.set(mem.available)
        system_memory_used_bytes.set(mem.used)
        system_memory_percent.set(mem.percent)
        
        # Disk metrics
        disk_partitions = psutil.disk_partitions()
        for partition in disk_partitions:
            try:
                disk_usage = psutil.disk_usage(partition.mountpoint)
                device = partition.device
                mountpoint = partition.mountpoint
                
                system_disk_total_bytes.labels(
                    device=device,
                    mountpoint=mountpoint
                ).set(disk_usage.total)
                
                system_disk_used_bytes.labels(
                    device=device,
                    mountpoint=mountpoint
                ).set(disk_usage.used)
                
                system_disk_free_bytes.labels(
                    device=device,
                    mountpoint=mountpoint
                ).set(disk_usage.free)
                
                system_disk_percent.labels(
                    device=device,
                    mountpoint=mountpoint
                ).set(disk_usage.percent)
            except PermissionError:
                # Skip partitions we don't have permission to access
                continue
        
        # Network metrics
        net_io = psutil.net_io_counters(pernic=True)
        for interface, stats in net_io.items():
            system_network_bytes_sent.labels(interface=interface).set(stats.bytes_sent)
            system_network_bytes_recv.labels(interface=interface).set(stats.bytes_recv)
            system_network_packets_sent.labels(interface=interface).set(stats.packets_sent)
            system_network_packets_recv.labels(interface=interface).set(stats.packets_recv)
        
        # Process metrics
        process = psutil.Process()
        process_cpu_percent.set(process.cpu_percent(interval=None))
        
        process_mem_info = process.memory_info()
        process_memory_bytes.set(process_mem_info.rss)
        process_memory_percent.set(process.memory_percent())
        
        process_threads.set(process.num_threads())
        
        try:
            process_open_files.set(len(process.open_files()))
        except (psutil.AccessDenied, AttributeError):
            # Some systems may not allow access to open files
            pass
            
    except Exception as e:
        logger.error(f"Error updating system metrics: {e}", exc_info=True)


def get_metrics_response() -> StarletteResponse:
    """
    Generate Prometheus metrics response.
    Updates system metrics before generating the response.
    """
    # Update system metrics before generating response
    update_system_metrics()
    
    # Generate Prometheus metrics
    metrics_output = generate_latest()
    
    return StarletteResponse(
        content=metrics_output,
        media_type=CONTENT_TYPE_LATEST
    )

