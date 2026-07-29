#!/usr/bin/env bash
# create-broken-service.sh
# Intentionally breaks a service for testing PatchPilot health check failure and rollback.
# Usage: ./create-broken-service.sh [service] [mode]
#   service: nginx, ssh, custom
#   mode:    stop, misconfig, oom
set -euo pipefail

SERVICE="${1:-nginx}"
MODE="${2:-stop}"

echo "=== Creating broken service: ${SERVICE} (mode: ${MODE}) ==="

case "${SERVICE}" in
    nginx)
        case "${MODE}" in
            stop)
                echo "Stopping nginx..."
                sudo systemctl stop nginx
                ;;
            misconfig)
                echo "Breaking nginx config..."
                sudo sed -i 's/listen 80;/listen 99999;/' /etc/nginx/sites-enabled/default 2>/dev/null || true
                sudo sed -i 's/listen 8080;/listen 99999;/' /etc/nginx/nginx.conf 2>/dev/null || true
                sudo systemctl restart nginx || true
                ;;
        esac
        ;;
    ssh)
        sudo systemctl stop ssh
        ;;
    journal)
        # Simulate journal errors by writing to syslog
        logger -p user.err "OutOfMemory: simulated OOM for testing PatchPilot journal check"
        logger -p user.err "database connection failed: simulated for testing"
        ;;
    custom)
        # Create a script that always fails
        echo -e '#!/bin/bash\nexit 1' | sudo tee /usr/local/bin/patchpilot-test-check.sh > /dev/null
        sudo chmod +x /usr/local/bin/patchpilot-test-check.sh
        ;;
    *)
        echo "Unknown service: ${SERVICE}"
        exit 1
        ;;
esac

echo "[DONE] Service ${SERVICE} is now broken"
