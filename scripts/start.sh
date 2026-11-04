#!/usr/bin/env bash
# Start (or restart) bridge, connector and tunnel as background services.
source "$(dirname "$0")/common.sh"
for l in "${LABELS[@]}"; do svc_stop "$l"; svc_start "$l"; done
sleep 3
"$(dirname "$0")/status.sh"
