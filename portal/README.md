# School Desk: no-login parents' chat for the Blended digests

A small Cloudflare Worker that lets parents ask questions like *"What's tomorrow's homework?"* or
*"إيه واجب بكرة؟"* and get answers taken only from the daily Blended digest emails.

```
Blended task ──email──► your Gmail ──filter: auto-forward──► digest@<your-domain>
                                                                  │ Cloudflare Email Routing
                                                                  ▼
                                   Worker "school-desk" ── stores the digest in KV (90 days)
                                        ▲        │
  phone / laptop ──HTTPS──► https://school.<your-domain>/<secret-path>      └──► Claude API (your key)
```

- **No login.** Anyone who has the full link can use it. The path is random (at least 16 characters), the
  site root and every other path return 404, and pages carry `noindex`. Treat the link like a password:
  share it only with family. To revoke it, set a new `PORTAL_PATH` (step 4) and redeploy.
- **Runs on Cloudflare**, not on your PC: it works while ZahabY-PC or the Mac is off.
- **The scheduled task needs no change.** Gmail forwards the digest emails it already sends.
- **Cost control.** Questions go to the Claude API on your key, capped at `DAILY_QUESTION_LIMIT`
  per day (default 100). Also set a monthly spend limit on the key in the Claude Console.
- Nothing personal is in this folder: the child's name, email addresses and the secret path are
  deploy-time settings.

## Files

```
src/index.js     Worker: page, /api/latest, /api/ask, and the email handler
src/digest.js    parse a digest email (subject -> id/date, clean links, strip forward headers)
src/prompt.js    system prompt, Cairo dates and Sunday–Thursday school week, chat-history checks
src/page.html    the chat page (mobile-first, English/Arabic, light/dark)
test/            node --test unit tests (npm test)
wrangler.toml    Worker config: route, KV binding, non-secret settings
```

## Deploy (one time, about 20 minutes)

Prerequisites: Node.js 20+, the Cloudflare account that holds your domain (the same one the
WhatsApp tunnel uses), and a Claude API key from https://platform.claude.com (set a monthly spend
limit on it).

1. Install and log in:

   ```bash
   cd portal
   npm install
   npx wrangler login
   ```

2. Create the storage and paste the printed `id` into `wrangler.toml` (`REPLACE_WITH_KV_NAMESPACE_ID`):

   ```bash
   npx wrangler kv namespace create DIGESTS
   ```

3. In `wrangler.toml`, replace `school.example.com` with your subdomain (e.g. `school.<your-domain>`) and set
   `CHILD_NAME` / `CLASS_NAME` (e.g. `"Ali"` / `"Year 1E"`). Don't commit those values if the repo is public.

4. Set the secrets. Each command prompts for the value, so nothing lands in your shell history.

   ```bash
   node -e "console.log(require('crypto').randomBytes(18).toString('base64url'))"
   ```

   Copy that output: it is your secret path. Then:

   ```bash
   npx wrangler secret put PORTAL_PATH
   ```

   ```bash
   npx wrangler secret put ANTHROPIC_API_KEY
   ```

   `ALLOWED_FROM` is the address the digest emails come from (your Gmail address):

   ```bash
   npx wrangler secret put ALLOWED_FROM
   ```

   `FORWARD_TO` is where anything that is not a digest goes (your Gmail address again; it must be a
   verified destination in step 6):

   ```bash
   npx wrangler secret put FORWARD_TO
   ```

5. Deploy:

   ```bash
   npm run deploy
   ```

   The site is now at `https://school.<your-domain>/<PORTAL_PATH>`. It says "no school updates yet" until step 8.

6. Cloudflare dashboard → your domain → **Email** → **Email Routing**:
   - **Enable Email Routing.** It adds MX records. **If this domain already receives email somewhere
     else, stop:** enabling it would take over that mail. Use a subdomain or ask before going on.
   - **Destination addresses** → add your Gmail address and click the verification link Cloudflare emails you.
   - **Routing rules** → **Create address** `digest@<your-domain>` → action **Send to a Worker** → `school-desk`.

7. Gmail (web) → Settings → **Forwarding and POP/IMAP** → **Add a forwarding address** → `digest@<your-domain>`.
   Gmail sends a confirmation email there. The Worker passes it on to your Gmail (via `FORWARD_TO`);
   open it and confirm. Then Settings → **Filters** → **Create a new filter**:
   - From: your Gmail address, Subject: `Blended update`
   - **Forward it to** `digest@<your-domain>` (keep Gmail's copy; don't archive or delete).

8. Backfill: Gmail filters only apply to new mail, so forward the last week's "Blended update" emails to
   `digest@<your-domain>` by hand (normal **Forward**; the Worker strips the forward header).
   Refresh the site: the header shows the newest update.

## Check and run it

- Logs while you test: `npx wrangler tail` shows `stored digest 2026-11-19-6pm` for each email, or
  `digest ... not stored: sender not allowed / dkim failed` with the reason.
- If digests are forwarded back to your Gmail instead of stored and the log says `dkim failed`, set
  `REQUIRE_DKIM = "false"` in `wrangler.toml` and redeploy (the sender allow-list still applies).
- Unit tests: `npm test`. Bundle check without deploying: `npm run check`.
- Settings in `wrangler.toml`: `MODEL` (default `claude-opus-5-5`), `EFFORT` (default `low`),
  `DAILY_QUESTION_LIMIT`. For lower cost, set `MODEL = "claude-haiku-5-5"`.

## "What did he take today?"

Answers are only as detailed as the digests. To get day-by-day lesson content, add a short
**"Taught this week"** section to the scheduled task's email (from the Arabic/Islamic weekly
plans etc.). The Worker stores the whole email, and the chat already looks for that section.
