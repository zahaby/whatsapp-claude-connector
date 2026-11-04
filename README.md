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
| `send_message`, `send_file`, `send_audio_message` always exposed | send tool **does not exist** unless `WHATSAPP_ALLOW_SEND=true`; even then a two-step send: a single-use, 5-minute proposal bound to the exact recipient and text |
| Bridge REST API (incl. unauthenticated `/api/send`) listens on **all interfaces** `:8080` | `install.sh` patches it to `127.0.0.1` |
| Needs several tool calls to resolve a chat name, then page through messages | `get_chat_transcript("Family", days=7)`: one call, fuzzy name match, oldest-first transcript ready to summarise (good fit for voice) |
| DB opened read-write | DB opened `mode=ro` |
| Bridge prints **every message's text** to the terminal/log | `install.sh` patches it to a content-free line, so nothing shows on the screen-share or in logs |

Upstream is pinned to an audited commit (`macos/common.sh`) and left unmodified apart from two small, verified patches (bind address, no message text in output). The patched bridge was compile-checked.

## Files

One repo, shared core, one folder per operating system:

```
connector/                   SHARED CORE (Python, any OS)
  server.py                  MCP server: tools, send switch, TokenGate auth, two-step send
  verify.py                  checks what the RUNNING connector enforces, via the admin channel
  admin_channel.py           picks the OS-specific local admin channel
  admin_unix.py              macOS/Linux: owner-only Unix socket + lock file
  admin_tcp.py               Windows: loopback admin port the tunnel never forwards to
  admin_auth.py              admin-channel challenge-response: scripts never send the token
  tls.py                     loopback TLS certificate that cloudflared and the scripts pin
  .env.example               all settings; real .env is owner-only and git-ignored
  tests/                     pytest: core, HTTP, both admin channels, macOS + Windows scripts
config/cloudflared.yml       tunnel ingress (shared): only /mcp* and /healthz forwarded
macos/                       macOS PLATFORM LAYER
  install.sh                 clone+pin upstream, patch, build, venv, token, launchd
  link-whatsapp.sh           run bridge in foreground so the OWNER scans the QR
  setup-tunnel.sh            one-time Cloudflare named tunnel + DNS
  start.sh|stop.sh|status.sh
  sending.sh on|off          THE SEND SWITCH (verifies the running connector)
  rotate-token.sh            new secret; verifies old token rejected before claiming success
  connector-url.sh           copies the secret connector URL to the clipboard (--print to show)
  launchd/*.plist            bridge, connector, tunnel as login services (auto-restart)
  claude_desktop_config.example.json
windows/                     WINDOWS PLATFORM LAYER (Windows PowerShell 5.1, no admin rights)
  install.ps1 link-whatsapp.ps1 setup-tunnel.ps1 start.ps1 stop.ps1 status.ps1
  sending.ps1 rotate-token.ps1 connector-url.ps1   same behaviour as the macOS scripts
  supervise.ps1              keeps a service running, restarts it if it crashes
  common.ps1                 shared helpers
docs/HANDOVER.md             one-page restart / re-link note (macOS)
docs/HANDOVER-windows.md     the same for Windows
docs/SECURITY.md             answers: securing the endpoint; no access to messages
```

## Runbook (screen-share session; owner drives anything personal)

Prereqs: macOS, Homebrew, Claude Pro/Max (custom connectors), a domain on a free Cloudflare account.

```bash
brew install go uv cloudflared
```

```bash
git clone <this repo> ~/wa-connector && cd ~/wa-connector && macos/install.sh
```

**1. Link WhatsApp (owner only).** `macos/link-whatsapp.sh`. Owner scans the QR on their phone (WhatsApp → Settings → Linked devices). The helper looks away from the screen / pauses screen-share while history syncs. Ctrl-C once you see "Connected".

**2. Local (acceptance criterion 1).** Paste the snippet `install.sh` printed into
`~/Library/Application Support/Claude/claude_desktop_config.json` (template in `macos/`), restart Claude Desktop,
then ask: *"Summarise the Family WhatsApp chat from the last week."*

**3. Remote (criterion 2).**
```bash
macos/setup-tunnel.sh wa.yourdomain.com
```
```bash
macos/start.sh
```
```bash
macos/connector-url.sh
```
Owner (not the helper) pastes the URL into **claude.ai → Settings → Connectors → Add custom connector**, name it "WhatsApp".
It syncs to the iPhone app. Test from iPhone voice mode: *"Summarise my Family WhatsApp chat."*
Once the remote connector works you can remove the local Desktop entry so tools aren't listed twice. Desktop sees the remote connector too.

**4. Sending (criterion 3).** Off by default. `macos/sending.sh on` / `off` / `status`. Details in docs/HANDOVER.md.

## Windows

Same features and safety rules as on the Mac; only the platform layer differs.

Prereqs (then open a **new** PowerShell window so PATH updates):

```powershell
winget install -e --id Git.Git; winget install -e --id GoLang.Go; winget install -e --id BrechtSanders.WinLibs.POSIX.UCRT; winget install -e --id astral-sh.uv; winget install -e --id Cloudflare.cloudflared
```

```powershell
git clone <this repo> $HOME\wa-connector; cd $HOME\wa-connector; powershell -ExecutionPolicy Bypass -File windows\install.ps1
```

Then the same four steps as on the Mac, with `windows\<name>.ps1` instead of `macos/<name>.sh`:
`link-whatsapp.ps1` (owner scans the QR), merge the printed snippet into `%APPDATA%\Claude\claude_desktop_config.json`,
`setup-tunnel.ps1 wa.yourdomain.com`, `start.ps1`, `connector-url.ps1`. Send switch: `sending.ps1 on|off|status`.
Run them as `powershell -ExecutionPolicy Bypass -File windows\<name>.ps1 ...` unless your execution policy allows local scripts.

How it differs from macOS, and why:

| | macOS | Windows |
|---|---|---|
| Background services | launchd agents | hidden `supervise.ps1` per service (restarts on crash) + a Startup-folder shortcut. No admin rights, no Task Scheduler. |
| Local admin channel | owner-only Unix socket | loopback port `MCP_ADMIN_PORT` (default `MCP_PORT`+1), bound exclusively. Python on Windows has no Unix sockets. |
| `.env` protection | `chmod 600` | ACL: inheritance removed, only your account |
| Bridge build | `go build` | `go build` with `CGO_ENABLED=1` and gcc (WinLibs) |

Not covered by CI on Windows: the real WhatsApp bridge and Cloudflare tunnel binaries (the tests run the real
connector, supervisor, start/stop, rotation and send switch). Check once on the PC:
`windows\start.ps1`, `windows\rotate-token.ps1`, `windows\sending.ps1 on`, `windows\sending.ps1 off`, `windows\status.ps1`.
Each must print `Verified` / `running`.

## Tests

```bash
cd connector && uv run --group dev pytest -q
```

93 tests, four files:

* `test_server.py`: send tool absent by default; two-step send (no send on first call, invented / reused /
  expired / mismatched proposals rejected); chat-name resolution; newest messages chosen by real UTC time across
  mixed offsets; unreadable timestamps excluded and reported, never a crash; read-only DB; TokenGate.
* `test_admin_socket.py`: the local socket never deletes or re-permissions anything it didn't create. Regular
  files, symlinks, directories and any socket the connector didn't record are refused. A socket is replaced only
  when no connector holds its lock (`admin.sock.lock`, released by the kernel even on SIGKILL) and it is the exact
  socket the previous connector recorded. An existing lock file is reused only if it is in the connector's own
  format (`wa-connector-admin-lock v1` header, or empty); any other file there is refused byte-for-byte untouched. Busy live sockets (full backlog, which looks "refused" on macOS) are
  never deleted; existing directories keep their mode (group/world-writable ones are refused); shutdown removes only
  the exact socket this process bound.
* `test_http.py`: the real server process with the MCP client: no token → 404, token URL → tools and transcript,
  wrong Host → 421, token never in logs; with `MCP_ALLOWED_IPS` set, the local socket still works but the TCP
  port keeps rejecting non-allowlisted callers.
* `test_scripts.py`: runs the real scripts with stubbed `launchctl`/`go`/`uv`/`cloudflared`. The `launchctl`
  stub runs the real connector, so: a fresh install with no hostname completes and writes all three services;
  token rotation is verified against the live process; a failing or silent restart makes rotation and the
  sending switch exit non-zero with a warning that is shown to be true. Rotation and the switch run both with and
  without `MCP_ALLOWED_IPS`. The stub's process waits are bounded and handle zombies, so the suite cannot hang
  in containers.

**Not covered by the tests: real launchd on a Mac.** On the Mac, check by hand once (5 minutes):

```bash
macos/start.sh && macos/rotate-token.sh && macos/sending.sh on && macos/sending.sh off && macos/status.sh
```

Each step must print `Verified`. Then try the failure path: `launchctl bootout gui/$(id -u)/com.wa.connector`,
run `macos/sending.sh status` (should say "not running"), then `macos/start.sh`.

## Known limits / things to verify on the day

* **Upgrading an install made before loopback TLS:** re-run `macos/setup-tunnel.sh <hostname>` (or
  `windows\setup-tunnel.ps1 <hostname>`) once, then restart. The old tunnel config points at `http://`, which
  the connector no longer serves.
* **First real remote test:** CI can't run `cloudflared` against Cloudflare. On the day, after `setup-tunnel` and
  `start`, check `status` shows `public health: ok`. If it shows DOWN, `~/Library/Logs/wa-connector/tunnel.log`
  (or `%LOCALAPPDATA%\wa-connector\logs\tunnel.log`) names the TLS error.

* **Voice mode + connectors:** confirm on the owner's iPhone app version that voice mode calls connector tools.
  If it doesn't, the fallback is dictation in a normal chat. The connector is the same either way.
* **The Mac must be awake and online** for remote use (System Settings → Battery → prevent sleep when on power;
  or `caffeinate`). It's the only place the data lives.
* WhatsApp may ask to re-link a linked device after a few weeks; see HANDOVER.
* The bridge only stores history WhatsApp syncs to linked devices, plus everything after linking. Very old
  threads may be missing.
* whatsmeow is an unofficial client. A small risk of WhatsApp restricting the account exists; read-only use
  is lowest risk.
