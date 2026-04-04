"""
Monitoring API endpoints for Prometheus metrics and system monitoring.
"""
import psutil
import time
from typing import Dict, Any, List
from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from app.utils.metrics import (
    update_system_metrics,
    http_requests_total,
    http_request_duration_seconds,
    system_cpu_percent,
    system_memory_total_bytes,
    system_memory_used_bytes,
    system_memory_percent,
    process_cpu_percent,
    process_memory_bytes,
    process_memory_percent,
    process_threads,
    process_open_files,
)
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/metrics", tags=["monitoring"])
async def prometheus_metrics():
    """
    Prometheus metrics endpoint.
    Returns metrics in Prometheus format for scraping.
    
    This endpoint is compatible with Prometheus server scraping.
    Access it at: GET /api/v1/monitoring/metrics
    """
    # Update system metrics before generating response
    update_system_metrics()
    
    # Generate Prometheus metrics
    metrics_output = generate_latest()
    
    return Response(
        content=metrics_output,
        media_type=CONTENT_TYPE_LATEST
    )


@router.get("/health", tags=["monitoring"])
async def health_check():
    """
    Health check endpoint.
    Returns basic health status of the API.
    """
    try:
        # Quick system check
        cpu_percent = psutil.cpu_percent(interval=0.1)
        memory = psutil.virtual_memory()
        
        # Determine health status
        is_healthy = (
            cpu_percent < 95 and  # CPU not overloaded
            memory.percent < 95   # Memory not exhausted
        )
        
        status_code = 200 if is_healthy else 503
        
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "healthy" if is_healthy else "degraded",
                "timestamp": time.time(),
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
            }
        )
    except Exception as e:
        logger.error(f"Health check failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "error": str(e),
                "timestamp": time.time(),
            }
        )


@router.get("/status", tags=["monitoring"])
async def system_status():
    """
    Detailed system status endpoint.
    Returns comprehensive system and application metrics in JSON format.
    """
    try:
        update_system_metrics()
        
        # Get system information
        cpu_count = psutil.cpu_count()
        cpu_percent = psutil.cpu_percent(interval=0.1, percpu=True)
        cpu_percent_avg = psutil.cpu_percent(interval=0.1)
        
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        # Get process information
        process = psutil.Process()
        process_info = {
            "cpu_percent": process.cpu_percent(interval=0.1),
            "memory_bytes": process.memory_info().rss,
            "memory_percent": process.memory_percent(),
            "threads": process.num_threads(),
            "status": process.status(),
            "create_time": process.create_time(),
            "uptime_seconds": time.time() - process.create_time(),
        }
        
        try:
            process_info["open_files"] = len(process.open_files())
        except (psutil.AccessDenied, AttributeError):
            process_info["open_files"] = None
        
        # Get network statistics
        net_io = psutil.net_io_counters()
        network_stats = {
            "bytes_sent": net_io.bytes_sent,
            "bytes_recv": net_io.bytes_recv,
            "packets_sent": net_io.packets_sent,
            "packets_recv": net_io.packets_recv,
            "errin": net_io.errin,
            "errout": net_io.errout,
            "dropin": net_io.dropin,
            "dropout": net_io.dropout,
        }
        
        # Get disk partitions
        disk_partitions = []
        for partition in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                disk_partitions.append({
                    "device": partition.device,
                    "mountpoint": partition.mountpoint,
                    "fstype": partition.fstype,
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                    "percent": usage.percent,
                })
            except PermissionError:
                continue
        
        return JSONResponse(
            content={
                "timestamp": time.time(),
                "system": {
                    "cpu": {
                        "count": cpu_count,
                        "percent_per_core": cpu_percent,
                        "percent_average": cpu_percent_avg,
                    },
                    "memory": {
                        "total_bytes": memory.total,
                        "available_bytes": memory.available,
                        "used_bytes": memory.used,
                        "percent": memory.percent,
                    },
                    "disk": {
                        "total_bytes": disk.total,
                        "used_bytes": disk.used,
                        "free_bytes": disk.free,
                        "percent": disk.percent,
                        "partitions": disk_partitions,
                    },
                    "network": network_stats,
                },
                "process": process_info,
            }
        )
    except Exception as e:
        logger.error(f"System status check failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error": str(e),
                "timestamp": time.time(),
            }
        )


@router.get("/metrics/json", tags=["monitoring"])
async def metrics_json():
    """
    Metrics endpoint returning data in JSON format.
    Useful for programmatic access to metrics without Prometheus format parsing.
    """
    try:
        update_system_metrics()
        
        # Collect HTTP metrics (these would need to be collected from registry)
        # For now, we'll focus on system metrics which are easier to extract
        
        # Get system metrics
        cpu_count = psutil.cpu_count()
        cpu_percent = psutil.cpu_percent(interval=0.1, percpu=True)
        
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage('/')
        
        process = psutil.Process()
        
        # Get network stats
        net_io = psutil.net_io_counters(pernic=True)
        network_by_interface = {}
        for interface, stats in net_io.items():
            network_by_interface[interface] = {
                "bytes_sent": stats.bytes_sent,
                "bytes_recv": stats.bytes_recv,
                "packets_sent": stats.packets_sent,
                "packets_recv": stats.packets_recv,
            }
        
        return JSONResponse(
            content={
                "timestamp": time.time(),
                "metrics": {
                    "http": {
                        "note": "HTTP metrics are available via Prometheus format at /metrics endpoint",
                    },
                    "system": {
                        "cpu": {
                            "count": cpu_count,
                            "percent_per_core": cpu_percent,
                            "percent_average": sum(cpu_percent) / len(cpu_percent) if cpu_percent else 0,
                        },
                        "memory": {
                            "total_bytes": memory.total,
                            "available_bytes": memory.available,
                            "used_bytes": memory.used,
                            "percent": memory.percent,
                        },
                        "disk": {
                            "total_bytes": disk.total,
                            "used_bytes": disk.used,
                            "free_bytes": disk.free,
                            "percent": disk.percent,
                        },
                        "network": network_by_interface,
                    },
                    "process": {
                        "cpu_percent": process.cpu_percent(interval=0.1),
                        "memory_bytes": process.memory_info().rss,
                        "memory_percent": process.memory_percent(),
                        "threads": process.num_threads(),
                        "uptime_seconds": time.time() - process.create_time(),
                    },
                },
            }
        )
    except Exception as e:
        logger.error(f"Metrics JSON endpoint failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error": str(e),
                "timestamp": time.time(),
            }
        )

