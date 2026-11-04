#!/usr/bin/env bash
# Start (or restart) bridge, connector and tunnel. Fails if any service fails.
source "$(dirname "$0")/common.sh"
for l in "${LABELS[@]}"; do svc_stop "$l"; svc_start "$l"; done
"$PY" "$REPO/connector/verify.py" wait-healthy || die "connector did not become healthy; see $LOGS/connector.log"
"$(dirname "$0")/status.sh"
