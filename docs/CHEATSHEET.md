# WhatsApp connector: cheat-sheet (Windows)

Run everything from the install folder, in PowerShell:

```powershell
cd $HOME\wa-connector
```

Every command below is `powershell -ExecutionPolicy Bypass -File windows\<script>.ps1 ...`.

## After a restart

Nothing to do: log in to Windows and the bridge, connector and tunnel start by themselves within about a minute.
Then check:

```powershell
powershell -ExecutionPolicy Bypass -File windows\status.ps1
```

Good = 3× `running`, `local health: ok`, `public health: ok`.
If anything is DOWN or STOPPED:

```powershell
powershell -ExecutionPolicy Bypass -File windows\start.ps1
```

`public health: DOWN` right after a start: wait 30 seconds and run `status` again.

## Everyday commands

| Task | Command (`powershell -ExecutionPolicy Bypass -File ...`) |
|---|---|
| Check everything | `windows\status.ps1` |
| Start / restart everything | `windows\start.ps1` |
| Stop everything (cuts remote access) | `windows\stop.ps1` |
| Daily summary: status (group, last post) | `windows\daily-summary.ps1 status` |
| Daily summary: allow the next post now | `windows\daily-summary.ps1 reset-limit` |
| Daily summary: on / off | `windows\daily-summary.ps1 on` · `windows\daily-summary.ps1 off` |
| Daily summary: change the group | `windows\daily-summary.ps1 set "Group name"` |
| Sending switch: on / off / status | `windows\sending.ps1 on` · `off` · `status` |
| Video / voice-note transcription: on / off / status | `windows\media-setup.ps1 on` · `off` · `status` |
| Re-link WhatsApp (QR code) | `windows\link-whatsapp.ps1`, scan, wait for "Connected", Ctrl-C, then `windows\start.ps1` |

Commands that change something (`on`, `off`, `set`, `rotate`) must end with **Verified**.
**ERROR** means the old setting may still be live: run `windows\stop.ps1`, then `windows\start.ps1`, then try again.

## Connector URLs (they are passwords)

| Task | Command |
|---|---|
| Copy the main connector URL | `windows\connector-url.ps1` |
| Copy the daily-summary connector URL | `windows\daily-summary.ps1 url` |
| New main URL (leak / lost phone) | `windows\rotate-token.ps1`, then paste the new URL in claude.ai → Settings → Connectors |
| New daily-summary URL | `windows\daily-summary.ps1 rotate`, then paste it into the *WhatsApp Daily* connector |

## Update to the latest version

```powershell
git checkout main; git pull; powershell -ExecutionPolicy Bypass -File windows\start.ps1
```

## When something looks wrong

| Symptom | Do this |
|---|---|
| A daily post says **UNCERTAIN** / status says *delivery NOT confirmed* | Check the group. Not there? `windows\daily-summary.ps1 reset-limit`, then run the task again |
| Summaries miss recent messages | Re-link WhatsApp (table above) |
| `public health: DOWN` for more than a minute | Send the output of `Get-Content "$env:LOCALAPPDATA\wa-connector\logs\tunnel.log" -Tail 25` |
| Anything else | Logs: `%LOCALAPPDATA%\wa-connector\logs\` (never contain message text or tokens) |

## Keep in mind

* Services start when you **log in**, not at boot: after an unattended restart (e.g. Windows Update at night) nothing runs until someone logs in.
* The PC must be **on and awake** at 12 AM: Settings → System → Power → Sleep: **Never** when plugged in.
* Links and videos: ask Claude *"what links and videos were shared in Family today?"*. Long videos take a few minutes to transcribe; if it says "still processing", ask again later.
* The daily summary posts at most twice per 24 hours (60 minutes apart), up to 7000 characters. A manual test uses one of the two: run `reset-limit` afterwards to keep both.
* Each summary ends with every member's share of the messages in that period (e.g. `خولي 30%`), counted by the connector, not guessed by Claude; names are written as in the summary (Claude supplies them, the numbers stay exact). Your own messages appear under your WhatsApp profile name. To drop it: set `WA_AUTOPOST_PARTICIPATION=false` in the connector `.env` and run `start`.

On a Mac the same commands are `macos/<script>.sh` (for example `macos/status.sh`, `macos/daily-summary.sh reset-limit`), run from `~/wa-connector`; logs are in `~/Library/Logs/wa-connector/`.
