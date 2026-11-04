#!/usr/bin/env bash
# Patch the upstream bridge in $UPSTREAM (already checked out at the pinned
# commit) and move it to a WhatsApp library version WhatsApp still accepts.
# Every patch is verified; any mismatch stops with "Patch failed".
# Called by install.sh, and by CI, which then really builds the result.
source "$(dirname "$0")/common.sh"

echo "==> Hardening bridge: REST API (incl. unauthenticated /api/send) binds to 127.0.0.1 only"
MAIN="$UPSTREAM/whatsapp-bridge/main.go"
sed_inplace 's|serverAddr := fmt.Sprintf(":%d", port)|serverAddr := fmt.Sprintf("127.0.0.1:%d", port)|' "$MAIN"
grep -q 'fmt.Sprintf("127.0.0.1:%d", port)' "$MAIN" || { echo "Patch failed: upstream changed, review main.go"; exit 1; }

echo "==> Privacy: stop the bridge printing message text to the terminal/log"
# Upstream echoes every message ("[time] <- sender: text"). That would show on a
# screen-share and land in ~/Library/Logs. Replace with a content-free line.
sed_inplace -E 's|fmt\.Printf\("\[%s\] %s %s: (\[%s: %s\] )?%s\\n", timestamp, direction, sender(, mediaType, filename)?, content\)|fmt.Printf("[%s] %s message stored\\n", timestamp, direction)|' "$MAIN"
if grep -qE 'fmt\.Printf\(.*sender.*content\)' "$MAIN"; then
  echo "Patch failed: bridge still prints message content, review main.go"; exit 1
fi

echo "==> Current WhatsApp library (whatsmeow $WHATSMEOW_VERSION); WhatsApp refuses outdated clients"
# Newer whatsmeow takes a context.Context first in these five calls.
sed_inplace -E \
  -e 's/client\.Download\(downloader\)/client.Download(context.Background(), downloader)/' \
  -e 's/sqlstore\.New\("sqlite3",/sqlstore.New(context.Background(), "sqlite3",/' \
  -e 's/container\.GetFirstDevice\(\)/container.GetFirstDevice(context.Background())/' \
  -e 's/client\.GetGroupInfo\(jid\)/client.GetGroupInfo(context.Background(), jid)/' \
  -e 's/client\.Store\.Contacts\.GetContact\(jid\)/client.Store.Contacts.GetContact(context.Background(), jid)/' \
  "$MAIN"
for call in 'client.Download(context.Background(), downloader)' 'sqlstore.New(context.Background(), "sqlite3",' \
            'container.GetFirstDevice(context.Background())' 'client.GetGroupInfo(context.Background(), jid)' \
            'client.Store.Contacts.GetContact(context.Background(), jid)'; do
  grep -qF "$call" "$MAIN" || { echo "Patch failed: upstream changed ($call), review main.go"; exit 1; }
done
( cd "$UPSTREAM/whatsapp-bridge" && go get "go.mau.fi/whatsmeow@$WHATSMEOW_VERSION" && go mod tidy ) \
  || { echo "Could not fetch whatsmeow $WHATSMEOW_VERSION"; exit 1; }

