#!/usr/bin/env bash
source "$(dirname "$0")/common.sh"
for l in "${LABELS[@]}"; do
  if launchctl print "gui/$(id -u)/$l" >/dev/null 2>&1; then echo "running  $l"; else echo "STOPPED  $l"; fi
done
port="$(env_get MCP_PORT)"; host="$(env_get MCP_PUBLIC_HOSTNAME)"
echo "local  health: $(curl -s -m 3 "http://127.0.0.1:${port:-8765}/healthz" || echo DOWN)"
[ -n "$host" ] && echo "public health: $(curl -s -m 5 "https://$host/healthz" || echo DOWN)"
echo "sending: $( [ "$(env_get WHATSAPP_ALLOW_SEND)" = true ] && echo ENABLED || echo disabled )"
echo "bridge last log line: $(tail -n 1 "$LOGS/bridge.log" 2>/dev/null || echo none)"
