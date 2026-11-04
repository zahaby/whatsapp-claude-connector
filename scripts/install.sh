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
git -C "$UPSTREAM" checkout -q "$UPSTREAM_COMMIT"

echo "==> Hardening bridge: REST API (incl. unauthenticated /api/send) binds to 127.0.0.1 only"
MAIN="$UPSTREAM/whatsapp-bridge/main.go"
/usr/bin/sed -i '' 's|serverAddr := fmt.Sprintf(":%d", port)|serverAddr := fmt.Sprintf("127.0.0.1:%d", port)|' "$MAIN"
grep -q 'fmt.Sprintf("127.0.0.1:%d", port)' "$MAIN" || { echo "Patch failed: upstream changed, review main.go"; exit 1; }

echo "==> Privacy: stop the bridge printing message text to the terminal/log"
# Upstream echoes every message ("[time] <- sender: text"). That would show on a
# screen-share and land in ~/Library/Logs. Replace with a content-free line.
/usr/bin/sed -i '' -E 's|fmt\.Printf\("\[%s\] %s %s: (\[%s: %s\] )?%s\\n", timestamp, direction, sender(, mediaType, filename)?, content\)|fmt.Printf("[%s] %s message stored\\n", timestamp, direction)|' "$MAIN"
if grep -qE 'fmt\.Printf\(.*sender.*content\)' "$MAIN"; then
  echo "Patch failed: bridge still prints message content, review main.go"; exit 1
fi

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
[ -n "$(env_get MCP_AUTH_TOKEN)" ] || "$REPO/scripts/rotate-token.sh" --no-restart

echo "==> launchd services"
mkdir -p "$LA" "$LOGS"
for label in "${LABELS[@]}"; do
  /usr/bin/sed -e "s|__HOME__|$HOME|g" -e "s|__REPO__|$REPO|g" -e "s|__UPSTREAM__|$UPSTREAM|g" \
    -e "s|__CLOUDFLARED__|$(command -v cloudflared)|g" \
    "$REPO/launchd/$label.plist" > "$LA/$label.plist"
done

PY="$REPO/connector/.venv/bin/python"
cat <<MSG

Install done. Next steps (see README):
  1. Link WhatsApp (you scan the QR):    scripts/link-whatsapp.sh
  2. Claude Desktop config entry:
     "whatsapp-family": {"command": "$PY",
                         "args": ["$REPO/connector/server.py", "--transport", "stdio"]}
  3. Tunnel (once): scripts/setup-tunnel.sh <hostname>, then scripts/start.sh
MSG
