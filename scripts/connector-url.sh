#!/usr/bin/env bash
# Prints the secret connector URL. Treat it like a password.
source "$(dirname "$0")/common.sh"
host="$(env_get MCP_PUBLIC_HOSTNAME)"
[ -n "$host" ] || { echo "MCP_PUBLIC_HOSTNAME not set; run scripts/setup-tunnel.sh"; exit 1; }
url="https://$host/mcp/$(env_get MCP_AUTH_TOKEN)"
if [ "${1:-}" = "--copy" ]; then printf %s "$url" | pbcopy; echo "Copied to clipboard."; else echo "$url"; fi
