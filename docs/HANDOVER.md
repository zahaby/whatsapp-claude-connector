# WhatsApp connector: one-page handover

**What runs on the Mac** (all start at login, auto-restart if they crash):
`com.wa.bridge` (WhatsApp link) · `com.wa.connector` (Claude tools, 127.0.0.1:8765) · `com.wa.tunnel` (Cloudflare, gives the iPhone access).
Code: `~/wa-connector`. Upstream bridge: `~/whatsapp-mcp`. Logs: `~/Library/Logs/wa-connector/`.

## Is it working?
```bash
~/wa-connector/scripts/status.sh
```
You want three `running` lines, `local health: ok`, `public health: ok`.

## Restart everything
```bash
~/wa-connector/scripts/start.sh
```
Fixes most problems (after a macOS update, network change, "connector not responding" on iPhone).
Stop all remote access immediately: `~/wa-connector/scripts/stop.sh`.

## Re-link WhatsApp (QR code)
Do this when `bridge.log` says logged out or keeps showing a QR, summaries are missing recent messages, or WhatsApp on your phone no longer lists the Mac under *Linked devices*.
1. `~/wa-connector/scripts/link-whatsapp.sh`
2. Phone: WhatsApp → Settings → **Linked devices** → **Link a device** → scan the QR in Terminal.
3. Wait for "Connected" and the history sync to settle, press **Ctrl-C**, then run `scripts/start.sh`.

If messages look out of sync or duplicated, use `scripts/link-whatsapp.sh --fresh` instead. It wipes the Mac's local copy only and you re-scan.
To cut the Mac off completely: phone → Linked devices → tap the Mac → **Log out**.

## Sending switch (off by default)
```bash
~/wa-connector/scripts/sending.sh on
```
```bash
~/wa-connector/scripts/sending.sh off
```
Each command restarts the connector and only prints **Verified** once the running server is confirmed.
If it prints **ERROR** instead, assume the old setting is still live and run `scripts/stop.sh`.
Check with `scripts/sending.sh status`, or ask Claude *"Is WhatsApp sending enabled?"*.
After switching: open a **new** chat in the Claude app, and quit/reopen Claude Desktop.
When on, sending takes two steps: Claude proposes the exact text, you say yes, then it sends. In claude.ai →
Settings → Connectors → WhatsApp, keep *send_message* on **"Ask every time"**: that approval prompt is your real
safeguard.

## Token / connector URL
The connector URL contains a secret. Treat it like a password and never paste it in a chat.
Lost phone or suspect a leak? Run `scripts/rotate-token.sh`. It only prints **Verified** after checking that the running connector rejects the old token. Then run `scripts/connector-url.sh` (copies the new URL to the clipboard) and paste it into claude.ai → Settings → Connectors → "WhatsApp".

## Keep in mind
* The Mac must be **on, awake and online** for the iPhone to work.
* Update upstream only on purpose: edit `UPSTREAM_COMMIT` in `scripts/common.sh`, review the diff, re-run `scripts/install.sh`.
* Logs never contain message text or the token.
