#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
for l in "${LABELS[@]}"; do
  if launchctl print "gui/$(id -u)/$l" >/dev/null 2>&1; then echo "running  $l"; else echo "STOPPED  $l"; fi
done
port="$(env_get MCP_PORT)"; host="$(env_get MCP_PUBLIC_HOSTNAME)"
if "$PY" "$REPO/connector/verify.py" health; then echo "local  health: ok"; else echo "local  health: DOWN"; fi
[ -n "$host" ] && echo "public health: $(curl -s -m 5 "https://$host/healthz" || echo DOWN)"
echo "sending: $( [ "$(env_get WHATSAPP_ALLOW_SEND)" = true ] && echo ENABLED || echo disabled )"
echo "bridge last log line: $(tail -n 1 "$LOGS/bridge.log" 2>/dev/null || echo none)"
