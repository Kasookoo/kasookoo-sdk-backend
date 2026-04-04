#!/bin/bash
echo "=== Checking Grafana Status ==="
docker ps | grep grafana || true
docker logs --tail 50 grafana 2>&1 || true
