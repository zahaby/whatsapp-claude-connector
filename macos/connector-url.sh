#!/usr/bin/env bash
# Copies the secret connector URL to the clipboard (default, so it never shows
# on a shared screen). --print writes it to the terminal instead.
source "$(dirname "$0")/common.sh"
host="$(env_get MCP_PUBLIC_HOSTNAME)"
[ -n "$host" ] || die "MCP_PUBLIC_HOSTNAME not set; run macos/setup-tunnel.sh"
url="https://$host/mcp/$(env_get MCP_AUTH_TOKEN)"
if [ "${1:-}" = "--print" ]; then
  echo "$url"
else
  printf %s "$url" | pbcopy && echo "Connector URL copied to clipboard (treat it like a password)."
fi
