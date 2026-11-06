# Security & privacy answers

## 1. How is the remote endpoint secured so only you can access it?

Layered, so no single mistake exposes the chats:

1. **Nothing is listening on the internet.** The connector binds `127.0.0.1` only and refuses any other address.
   The WhatsApp bridge's own API (which can send messages, unauthenticated upstream) is patched to `127.0.0.1` too.
   Remote access is through a **Cloudflare named tunnel**: an *outbound* connection from the Mac. No router
   port-forwarding, the home IP is not published, and TLS (HTTPS) is handled by Cloudflare's certificate.
2. **256-bit secret token on every request.** Generated on your Mac (`secrets.token_urlsafe(32)`), stored in a
   `chmod 600` file, never typed into a chat. claude.ai custom connectors can't attach a custom header, so the
   token goes in the connector URL path (`https://wa.yourdomain.com/mcp/<token>`). Compared in constant time;
   stripped before the MCP app sees it; never written to logs (access log is off). Header form
   `Authorization: Bearer <token>` is accepted too, for testing tools.
3. **Everything else is invisible.** Wrong or missing token → plain `404` (no hint that a service exists).
   The tunnel only forwards `/mcp*` and `/healthz`. 10 failures from one IP in 10 minutes → that IP is locked out.
   Host-header checking blocks DNS-rebinding; the server refuses to start with a weak or missing token.
4. **Only Claude can reach it (optional extra).** Set `MCP_ALLOWED_IPS` to Anthropic's published outbound IP
   ranges. Connector calls come from Anthropic's servers, not your phone. Then a leaked URL alone is useless
   to anyone else. Check the current ranges in Anthropic's docs when you set it up; don't hardcode from memory.
   The maintenance scripts check the live server over a separate local Unix socket (`connector/.run/admin.sock`,
   owner-only `0600`). It still needs the token but skips the IP allowlist, so verification works with the
   allowlist on. The tunnel can't reach a Unix socket, so remote callers always face the allowlist.
   (The allowlist was never a barrier to programs already running on your Mac: they can reach the TCP port
   directly and set the client-IP header themselves.)
   **On Windows** the admin channel is a second loopback port (`MCP_ADMIN_PORT`) instead, because Python on
   Windows has no Unix sockets. The tunnel never forwards to it, and it still needs the token. The difference:
   any local account on that PC can connect to a loopback port, where the Unix socket admits only you. On a
   single-user PC that changes nothing in practice. Both ports are bound with `SO_EXCLUSIVEADDRUSE`, so no
   other program can take them over, and a second connector can't start.
   **The scripts never send the token to the admin channel**, on any OS. Before every check, the connector
   must prove it holds the token in `.env` (HMAC challenge-response over fresh nonces, `connector/admin_auth.py`);
   the request is then authenticated with a single-use HMAC bound to that exchange, on the same connection. So
   if another program sits on the admin port or socket while the connector is stopped, it receives a random
   nonce and nothing else, and the script reports that the channel did not prove its identity.
7. **The connector proves its identity to the tunnel.** The tunnel forwards your iPhone's requests, with the
   token in the URL, to the connector's local port. If the connector were stopped and another program took that
   port, plain HTTP would hand it the token. So the connector serves **TLS only**, with its own certificate
   (`connector/tls.py`, private key readable only by you), and `cloudflared` is set to trust exactly that
   certificate (`caPool`) under the name `wa-connector.local` (`originServerName`). A squatter without the key
   fails the handshake before any request is sent; the iPhone gets an error instead. The maintenance scripts
   pin the same certificate, and the challenge-response above still holds even if the key were stolen.

   **Daily group summary (optional, off by default).** An unattended scheduled task cannot use the main
   connector: nobody approves its messages, and a message in the group could try to steer it into leaking
   other chats. So it gets a separate connector (`/daily/<its own token>`) that can read only the one
   configured group and post only into that group, at most once per 20 hours, at most 3000 characters,
   only when the group had messages, always labelled as automated, and only while switched on. Worst case
   if manipulated: one odd summary in that group per day. Its token is separate from the main token, so
   rotating one never affects the other.

   **Links and media.** Links in messages are fetched only on request (or for the nightly group) and only
   from public addresses: anything resolving to a private, loopback, link-local or otherwise non-public
   address is refused, every redirect is re-checked, the connection goes to the exact address that was
   checked, and size (2 MB) and time (10 s) are capped. So a link in a message cannot make the connector probe
   your router, local devices or its own internal services. Videos and voice notes are transcribed on this
   computer; the downloaded file is deleted afterwards (only a file inside the bridge's per-chat media folder
   is ever deleted). Transcripts and three small frames per video go to Claude, like the chat itself.

   Residual, stated plainly: the WhatsApp bridge's own internal API (`127.0.0.1:8080`, upstream code) is still
   plain HTTP. It carries no token, but if you switch sending on and a program had taken port 8080 while the
   bridge was stopped, the text of a message being sent could go to it instead. Sending is off by default.
5. **Least privilege.** Read-only DB handle (messages; and, for names only, the WhatsApp library's contact and
   LID tables in `whatsapp.db`, also read-only: nothing else in that session file is read); send tool doesn't exist unless you switch it on. This is the main
   defence against *prompt injection*: someone writing "ignore your instructions and forward this…" in a group
   chat can't make Claude send anything while sending is off.
6. **Kill switches.** `macos/rotate-token.sh` (new URL; it confirms the old token is rejected by the running
   server before saying so), `macos/stop.sh` (all off), or unlink the Mac from WhatsApp on your phone.

**If you switch sending on**, the send is two-step: the first call returns a random proposal id bound to the
exact recipient and text. Only a second call with that id and identical text sends, within 5 minutes, once.
**Limitation:** both calls come from the model, so the server cannot independently verify that *you* said yes.
A model misled by a malicious message could make the two calls back to back. The real human check is the
Claude app's per-tool approval prompt. Keep `send_message` on "Ask every time" in the connector settings.

Honest trade-offs:
* The token is in the URL, so it is stored in your claude.ai connector settings and visible to Cloudflare,
  where TLS terminates. That's acceptable for this threat model; rotate if in doubt.
* The stronger upgrade is **OAuth 2.1** (the MCP auth spec, supported by claude.ai connectors): you'd log in
  with a password/passkey once when adding the connector, and Claude holds a short-lived token. The MCP SDK
  supports this (`auth_server_provider` / `token_verifier`). It's more moving parts, so it's an optional phase 2.
  Cloudflare Access with an interactive login doesn't fit, because the caller is Anthropic's server and not a browser.
* Anyone with access to your claude.ai account can use the connector. Protect that account with strong auth.

## 2. How do I work without getting access to your WhatsApp messages?

* **You link the account.** You scan the QR on your own phone. I never see a QR, a code or your phone, and
  no WhatsApp credentials ever pass through me. The session key lives only in
  `~/whatsapp-mcp/whatsapp-bridge/store/whatsapp.db` on your Mac.
* **Screen-share only, you drive.** I don't SSH in, take remote control, install remote-access tools or copy
  files off the Mac. You type or paste commands I give you. Everything is in this repo for you to read first.
* **Message text never appears on screen.** Upstream's bridge prints every incoming message to the Terminal;
  the installer patches that out, so the QR/sync window shows only "message stored" lines.
* **I look away during the sensitive steps.** You pause screen-share (or switch to a non-WhatsApp window)
  while the QR is up, during history sync, and when you run the first test summary.
* **Built and tested on fake data.** The code was developed against a synthetic database with the same
  schema (`connector/tests/conftest.py`). Acceptance is checked by asking about chat names and the *shape* of
  the answer; you confirm accuracy yourself, off-screen.
* **I never hold the secret.** You generate the token on your machine and paste the connector URL into
  claude.ai yourself. I never see it. `rotate-token.sh` after the session if you want certainty.
* **No data leaves except to Claude, on request.** Messages stay in the local SQLite file. They are only read
  when *you* ask Claude something, and only go to your own Claude account. Logs hold metadata, never message text.
* **Clean exit.** At handover, remove my access to anything (there is none to remove), and optionally rotate the
  token and check WhatsApp → Linked devices shows only your Mac.
