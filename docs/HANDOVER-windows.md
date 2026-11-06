# WhatsApp connector on Windows: one-page handover

**What runs on the PC** (started at login by a shortcut in your Startup folder; each restarts itself if it crashes):
`bridge` (WhatsApp link) · `connector` (Claude tools, 127.0.0.1:8765, admin port 8766) · `tunnel` (Cloudflare, gives the iPhone access).
Code: `%USERPROFILE%\wa-connector`. Upstream bridge: `%USERPROFILE%\whatsapp-mcp`. Logs: `%LOCALAPPDATA%\wa-connector\logs`.
Run every command below from `%USERPROFILE%\wa-connector` as `powershell -ExecutionPolicy Bypass -File windows\<script>.ps1`.

## Is it working?
`windows\status.ps1` → you want three `running` lines, `local health: ok`, `public health: ok`.

## Restart everything
`windows\start.ps1`. Fixes most problems (after Windows Update, sleep, network change, "connector not responding" on iPhone).
Stop all remote access immediately: `windows\stop.ps1`.

## Re-link WhatsApp (QR code)
When `bridge.log` says logged out, summaries miss recent messages, or your phone no longer lists the PC under *Linked devices*:
1. `windows\link-whatsapp.ps1`
2. Phone: WhatsApp → Settings → **Linked devices** → **Link a device** → scan the QR in the PowerShell window.
3. Wait for "Connected" and the history sync, press **Ctrl-C**, then `windows\start.ps1`.

Out of sync or duplicated messages: `windows\link-whatsapp.ps1 -Fresh` (wipes the PC's local copy only; re-scan).
Cut the PC off completely: phone → Linked devices → tap the PC → **Log out**.

## Sending switch (off by default)
`windows\sending.ps1 on` / `windows\sending.ps1 off` / `windows\sending.ps1 status`.
It only prints **Verified** once the running connector is confirmed. **ERROR** means assume the old setting is live; run `windows\stop.ps1`.
After switching: new chat in the Claude app; quit and reopen Claude Desktop. Keep *send_message* on **"Ask every time"** in claude.ai → Settings → Connectors → WhatsApp.

## Daily group summary (if you set it up)
A scheduled Claude task posts one summary a night into one group, through its own minimal connector.
`windows\daily-summary.ps1 status` shows the group and the last post. Stop it: `windows\daily-summary.ps1 off` (prints **Verified**).
Its URL leaked? `windows\daily-summary.ps1 rotate`, then paste the new one (`windows\daily-summary.ps1 url`) into the *WhatsApp Daily* connector in claude.ai.

## Links and videos
Claude can summarise links, YouTube videos, and (once `windows\media-setup.ps1 on` is done) WhatsApp videos and voice notes,
transcribed on this computer. `windows\media-setup.ps1 status` shows whether transcription is on; `off` switches it off.

## Token / connector URL
The URL contains a secret: treat it like a password. Leak or lost phone: `windows\rotate-token.ps1` (prints **Verified** only after the
running connector rejects the old token), then `windows\connector-url.ps1` (copies the new URL) and paste it into claude.ai → Settings → Connectors.

## Keep in mind
* The PC must be **on, awake and online**: Settings → System → Power → Sleep: *Never* when plugged in.
* Update upstream only on purpose: edit `$UpstreamCommit` in `windows\common.ps1`, review the diff, re-run `windows\install.ps1`.
* Logs never contain message text or the token.
