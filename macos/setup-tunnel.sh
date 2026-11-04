#!/usr/bin/env bash
# One-time Cloudflare Tunnel setup. Needs a domain on your Cloudflare account.
#   macos/setup-tunnel.sh wa.yourdomain.com
# Outbound-only: no router ports are opened, the Mac's IP is never exposed.
source "$(dirname "$0")/common.sh"
HOSTNAME="${1:?usage: $0 <hostname e.g. wa.example.com>}"
# The connector's TLS certificate, which cloudflared will pin (created if missing).
CA_POOL="$(MCP_TLS_DIR="${MCP_TLS_DIR:-$(env_get MCP_TLS_DIR)}" "$PY" "$REPO/connector/tls.py" ensure)" || die "could not prepare the TLS certificate"
NAME=wa-connector
[ -f "$HOME/.cloudflared/cert.pem" ] || cloudflared tunnel login   # browser login, owner does this
mkdir -p "$HOME/.cloudflared"
cloudflared tunnel info "$NAME" >/dev/null 2>&1 || cloudflared tunnel create "$NAME"
# Options must come BEFORE the tunnel name: cloudflared stops reading flags
# at the first plain argument ("accepts exactly one argument" otherwise).
TUNNEL_ID="$(cloudflared tunnel info -o json "$NAME" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')" \
  || die "could not read the tunnel id (cloudflared tunnel info -o json $NAME)"
[[ "$TUNNEL_ID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || die "unexpected tunnel id: $TUNNEL_ID"
cloudflared tunnel route dns "$NAME" "$HOSTNAME"
sed -e "s|__TUNNEL_ID__|$TUNNEL_ID|g" -e "s|__HOME__|$HOME|g" -e "s|__HOSTNAME__|$HOSTNAME|g" \
  -e "s|__PORT__|$(env_get MCP_PORT)|g" -e "s|__CA_POOL__|$CA_POOL|g" \
  "$REPO/config/cloudflared.yml" > "$HOME/.cloudflared/wa-connector.yml"
env_set MCP_PUBLIC_HOSTNAME "$HOSTNAME"
echo "Tunnel ready: https://$HOSTNAME  ->  127.0.0.1:$(env_get MCP_PORT)"
