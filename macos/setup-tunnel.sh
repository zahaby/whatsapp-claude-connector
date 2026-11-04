#!/usr/bin/env bash
# One-time Cloudflare Tunnel setup. Needs a domain on your Cloudflare account.
#   macos/setup-tunnel.sh wa.yourdomain.com
# Outbound-only: no router ports are opened, the Mac's IP is never exposed.
source "$(dirname "$0")/common.sh"
HOSTNAME="${1:?usage: $0 <hostname e.g. wa.example.com>}"
NAME=wa-connector
[ -f "$HOME/.cloudflared/cert.pem" ] || cloudflared tunnel login   # browser login, owner does this
cloudflared tunnel info "$NAME" >/dev/null 2>&1 || cloudflared tunnel create "$NAME"
TUNNEL_ID="$(cloudflared tunnel info "$NAME" -o json | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')"
cloudflared tunnel route dns "$NAME" "$HOSTNAME"
sed -e "s|__TUNNEL_ID__|$TUNNEL_ID|g" -e "s|__HOME__|$HOME|g" -e "s|__HOSTNAME__|$HOSTNAME|g" \
  -e "s|__PORT__|$(env_get MCP_PORT)|g" "$REPO/config/cloudflared.yml" > "$HOME/.cloudflared/wa-connector.yml"
env_set MCP_PUBLIC_HOSTNAME "$HOSTNAME"
echo "Tunnel ready: https://$HOSTNAME  ->  127.0.0.1:$(env_get MCP_PORT)"
