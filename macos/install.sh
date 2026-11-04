#!/usr/bin/env bash
# One-time install on the Mac. Safe to re-run.
source "$(dirname "$0")/common.sh"

echo "==> Checking tools (Homebrew: go uv cloudflared)"
for t in go uv cloudflared; do
  command -v "$t" >/dev/null || { echo "Missing $t. Run: brew install $t"; exit 1; }
done

echo "==> Upstream lharries/whatsapp-mcp at $UPSTREAM (pinned $UPSTREAM_COMMIT)"
if [ ! -d "$UPSTREAM/.git" ]; then
  git clone https://github.com/lharries/whatsapp-mcp.git "$UPSTREAM"
fi
git -C "$UPSTREAM" fetch -q origin
git -C "$UPSTREAM" checkout -q -- .            # drop earlier patches: always patch clean upstream
git -C "$UPSTREAM" checkout -q "$UPSTREAM_COMMIT"

"$REPO/macos/patch-bridge.sh"

echo "==> Building bridge"
( cd "$UPSTREAM/whatsapp-bridge" && CGO_ENABLED=1 go build -o whatsapp-bridge . )
chmod 700 "$UPSTREAM/whatsapp-bridge/store" 2>/dev/null || true

echo "==> Connector virtualenv"
( cd "$REPO/connector" && uv sync -q )

if [ ! -f "$ENV_FILE" ]; then
  cp "$REPO/connector/.env.example" "$ENV_FILE"
  env_set WA_DB_PATH "$UPSTREAM/whatsapp-bridge/store/messages.db"
fi
chmod 600 "$ENV_FILE"
if [ -z "$(env_get MCP_AUTH_TOKEN)" ]; then gen_token; echo "Generated connector token."; fi

echo "==> Loopback TLS certificate (cloudflared and the scripts pin it)"
MCP_TLS_DIR="${MCP_TLS_DIR:-$(env_get MCP_TLS_DIR)}" "$PY" "$REPO/connector/tls.py" ensure >/dev/null || die "could not create the TLS certificate"

echo "==> launchd services"
mkdir -p "$LA" "$LOGS"
for label in "${LABELS[@]}"; do
  sed -e "s|__HOME__|$HOME|g" -e "s|__REPO__|$REPO|g" -e "s|__UPSTREAM__|$UPSTREAM|g" \
    -e "s|__CLOUDFLARED__|$(command -v cloudflared)|g" \
    "$REPO/macos/launchd/$label.plist" > "$LA/$label.plist"
done

cat <<MSG

Install done. Next steps (see README):
  1. Link WhatsApp (you scan the QR):    macos/link-whatsapp.sh
  2. Claude Desktop config entry:
     "whatsapp-family": {"command": "$PY",
                         "args": ["$REPO/connector/server.py", "--transport", "stdio"]}
  3. Tunnel (once): macos/setup-tunnel.sh <hostname>, then macos/start.sh
MSG
