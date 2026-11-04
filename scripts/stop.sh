#!/usr/bin/env bash
# Stop everything. Remote access is off until scripts/start.sh.
source "$(dirname "$0")/common.sh"
for l in "${LABELS[@]}"; do svc_stop "$l"; done
echo "Stopped: ${LABELS[*]}"
