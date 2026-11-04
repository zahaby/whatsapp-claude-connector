#!/usr/bin/env bash
# Run in a normal Terminal window. The OWNER scans the QR code with their phone
# (WhatsApp > Settings > Linked devices > Link a device).
# Also used for re-linking. Pass --fresh to wipe the local session first.
source "$(dirname "$0")/common.sh"
svc_stop com.wa.bridge
if [ "${1:-}" = "--fresh" ]; then
  echo "Removing local WhatsApp session + message cache (your phone is unaffected)."
  rm -f "$UPSTREAM/whatsapp-bridge/store/whatsapp.db" "$UPSTREAM/whatsapp-bridge/store/messages.db"
fi
cd "$UPSTREAM/whatsapp-bridge"
echo "When you see 'Connected to WhatsApp' and history has synced, press Ctrl-C."
echo "Then run scripts/start.sh to run it in the background."
./whatsapp-bridge || true
