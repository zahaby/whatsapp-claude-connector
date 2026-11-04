# WhatsApp → Claude connector (family-chat summaries)

Sample implementation for: *install `lharries/whatsapp-mcp` on a Mac, connect it to
Claude Desktop, and expose it securely as a remote custom connector for the
Claude iPhone app (voice mode). Sending is off by default.*

```
iPhone Claude app ──► Anthropic cloud ──HTTPS──► Cloudflare edge ──tunnel (outbound only)──┐
Claude Desktop ──stdio──┐                                                                  │
                        ▼                                                                  ▼
                 connector/server.py  (read-only by default, token gate)  ◄── 127.0.0.1:8765
                        │ reads (SQLite, mode=ro)            │ send (only if switched on)
                        ▼                                    ▼
      ~/whatsapp-mcp/whatsapp-bridge/store/messages.db   bridge REST 127.0.0.1:8080
                        ▲
         whatsapp-bridge (upstream Go, whatsmeow)  ◄── linked device, owner scans QR
```

## Why a wrapper instead of running upstream's `main.py` directly

| Upstream as-is | This connector |
|---|---|
| stdio only, so it can't be a remote connector | stdio **and** Streamable HTTP |
| `send_message`, `send_file`, `send_audio_message` always exposed | send tool **does not exist** unless `WHATSAPP_ALLOW_SEND=true`; even then it requires a confirmation round-trip |
| Bridge REST API (incl. unauthenticated `/api/send`) listens on **all interfaces** `:8080` | `install.sh` patches it to `127.0.0.1` |
| Needs several tool calls to resolve a chat name, then page through messages | `get_chat_transcript("Family", days=7)`: one call, fuzzy name match, oldest-first transcript ready to summarise (good fit for voice) |
| DB opened read-write | DB opened `mode=ro` |
| Bridge prints **every message's text** to the terminal/log | `install.sh` patches it to a content-free line, so nothing shows on the screen-share or in logs |

Upstream is pinned to an audited commit (`scripts/common.sh`) and left unmodified apart from two small, verified patches (bind address, no message text in output). The patched bridge was compile-checked.

## Files

```
connector/server.py          MCP server (tools, send switch, TokenGate auth)
connector/.env.example       all settings; real .env is chmod 600 and git-ignored
connector/tests/             pytest: send gating, name resolution, auth gate, read-only DB
scripts/install.sh           clone+pin upstream, patch, build, venv, token, launchd
scripts/link-whatsapp.sh     run bridge in foreground so the OWNER scans the QR
scripts/setup-tunnel.sh      one-time Cloudflare named tunnel + DNS
scripts/start.sh|stop.sh|status.sh
scripts/sending.sh on|off    THE SEND SWITCH
scripts/rotate-token.sh      new secret, old URL dies instantly
scripts/connector-url.sh     prints the secret connector URL (--copy to clipboard)
launchd/*.plist              bridge, connector, tunnel as login services (auto-restart)
config/cloudflared.yml       tunnel ingress: only /mcp* and /healthz forwarded
docs/HANDOVER.md             one-page restart / re-link note
docs/SECURITY.md             answers: securing the endpoint; no access to messages
```

## Runbook (screen-share session; owner drives anything personal)

Prereqs: macOS, Homebrew, Claude Pro/Max (custom connectors), a domain on a free Cloudflare account.

```bash
brew install go uv cloudflared
```

```bash
git clone <this repo> ~/wa-connector && cd ~/wa-connector && scripts/install.sh
```

**1. Link WhatsApp (owner only).** `scripts/link-whatsapp.sh`. Owner scans the QR on their phone (WhatsApp → Settings → Linked devices). The helper looks away from the screen / pauses screen-share while history syncs. Ctrl-C once you see "Connected".

**2. Local (acceptance criterion 1).** Paste the snippet `install.sh` printed into
`~/Library/Application Support/Claude/claude_desktop_config.json` (template in `config/`), restart Claude Desktop,
then ask: *"Summarise the Family WhatsApp chat from the last week."*

**3. Remote (criterion 2).**
```bash
scripts/setup-tunnel.sh wa.yourdomain.com
```
```bash
scripts/start.sh
```
```bash
scripts/connector-url.sh --copy
```
Owner (not the helper) pastes the URL into **claude.ai → Settings → Connectors → Add custom connector**, name it "WhatsApp".
It syncs to the iPhone app. Test from iPhone voice mode: *"Summarise my Family WhatsApp chat."*
Once the remote connector works you can remove the local Desktop entry so tools aren't listed twice. Desktop sees the remote connector too.

**4. Sending (criterion 3).** Off by default. `scripts/sending.sh on` / `off` / `status`. Details in docs/HANDOVER.md.

## Tests

```bash
cd connector && uv run --group dev pytest -q
```

Covers: send tool absent by default and confirmation-gated when on; Go timestamp parsing; chat-name resolution
(exact vs ambiguous); 7-day window and sender names; DB is read-only; TokenGate path token, bearer header,
lockout after 10 failures, IP allowlist; HTTP mode refuses weak tokens. Also smoke-tested end to end with the MCP
Python client over Streamable HTTP (no token → 404, token → tools; wrong Host header → 421).

## Known limits / things to verify on the day

* **Voice mode + connectors:** confirm on the owner's iPhone app version that voice mode calls connector tools.
  If it doesn't, the fallback is dictation in a normal chat. The connector is the same either way.
* **The Mac must be awake and online** for remote use (System Settings → Battery → prevent sleep when on power;
  or `caffeinate`). It's the only place the data lives.
* WhatsApp may ask to re-link a linked device after a few weeks; see HANDOVER.
* The bridge only stores history WhatsApp syncs to linked devices, plus everything after linking. Very old
  threads may be missing.
* whatsmeow is an unofficial client. A small risk of WhatsApp restricting the account exists; read-only use
  is lowest risk.
