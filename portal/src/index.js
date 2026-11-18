// School Desk: a no-login chat page where a class's parents ask about the school digests.
// The digests come from one parent's account, so names listed in REDACT_NAMES are replaced
// on the way in and out, the raw digest is never served, and the prompt shares class-wide
// information only.
//
//   email()  Gmail auto-forwards each "<Child> – Blended update ..." email to an address on your
//            domain; Cloudflare Email Routing hands it to this Worker, which stores it in KV.
//   fetch()  GET  /<PORTAL_PATH>           the chat page
//            GET  /<PORTAL_PATH>/api/latest label of the newest stored update (freshness stamp only)
//            POST /<PORTAL_PATH>/api/ask    {messages:[{role,content}...]} -> {answer, from}
//            Everything else is a plain 404, so the site is invisible without the link.

import Anthropic from "@anthropic-ai/sdk";
import PostalMime from "postal-mime";
import PAGE from "./page.html";
import { toDigest, senderAllowed, redactNames } from "./digest.js";
import { stableSystem, datesSystem, cleanTurns } from "./prompt.js";

const KEEP_DAYS = 90;           // stored digests expire after this
const DIGESTS_IN_PROMPT = 10;   // newest N go to Claude

const notFound = () => new Response("Not found", { status: 404, headers: { "X-Robots-Tag": "noindex, nofollow" } });

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "X-Robots-Tag": "noindex, nofollow" },
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function latestDigests(env, n) {
  // Keys are "d:<sentAt ISO>", so KV's lexicographic order is chronological.
  const keys = [];
  let cursor;
  do {
    const page = await env.DIGESTS.list({ prefix: "d:", cursor });
    keys.push(...page.keys.map(k => k.name));
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  const newest = keys.sort().reverse().slice(0, n);
  const docs = await Promise.all(newest.map(k => env.DIGESTS.get(k, "json")));
  // Redact again on read, so digests stored before a name was added to REDACT_NAMES are covered.
  return docs.filter(Boolean).map(d => ({
    ...d,
    body: redactNames(d.body, env.REDACT_NAMES),
    taught: d.taught ? redactNames(d.taught, env.REDACT_NAMES) : d.taught,
  }));
}

// Approximate daily cap on questions (KV is eventually consistent, so it can overshoot slightly).
async function underDailyCap(env) {
  const limit = Number(env.DAILY_QUESTION_LIMIT || 100);
  const day = new Intl.DateTimeFormat("en-CA", { timeZone: "Africa/Cairo" }).format(new Date());
  const key = `q:${day}`;
  const used = Number((await env.DIGESTS.get(key)) || 0);
  if (used >= limit) return false;
  await env.DIGESTS.put(key, String(used + 1), { expirationTtl: 3 * 86400 });
  return true;
}

async function ask(request, env) {
  let payload;
  try { payload = await request.json(); } catch { return json({ error: "bad_request" }, 400); }
  const turns = cleanTurns(payload && payload.messages);
  if (!turns) return json({ error: "bad_request" }, 400);

  const digests = await latestDigests(env, DIGESTS_IN_PROMPT);
  if (!digests.length) return json({ error: "no_data" }, 409);
  if (!(await underDailyCap(env))) return json({ error: "daily_limit" }, 429);

  const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY });
  try {
    const response = await client.beta.messages.create({
      model: env.MODEL || "claude-opus-5-5",
      max_tokens: 16000,
      output_config: { effort: env.EFFORT || "low" },
      betas: ["server-side-fallback-2026-07-01"],
      fallbacks: "default",
      system: [
        { type: "text", text: stableSystem({ className: env.CLASS_NAME, digests }), cache_control: { type: "ephemeral" } },
        { type: "text", text: datesSystem() },
      ],
      messages: turns,
    });
    if (response.stop_reason === "refusal") return json({ error: "refused" }, 422);
    const answer = response.content.filter(b => b.type === "text").map(b => b.text).join("").trim();
    if (!answer) return json({ error: "empty" }, 502);
    return json({ answer, from: digests[0].label });
  } catch (error) {
    if (error instanceof Anthropic.RateLimitError) return json({ error: "busy" }, 429);
    if (error instanceof Anthropic.AuthenticationError) { console.error("Anthropic API key rejected"); return json({ error: "config" }, 500); }
    if (error instanceof Anthropic.APIError) { console.error(`Anthropic API error ${error.status}`); return json({ error: "upstream" }, 502); }
    console.error("ask failed", error && error.message);
    return json({ error: "upstream" }, 502);
  }
}

export default {
  async fetch(request, env) {
    const base = "/" + String(env.PORTAL_PATH || "").replace(/^\/+|\/+$/g, "");
    if (base === "/" || base.length < 17) return notFound(); // refuse to run on a guessable path
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "");

    if (path === base && request.method === "GET") {
      const html = PAGE
        .replaceAll("__CLASS__", escapeHtml(env.CLASS_NAME || "Class"))
        .replaceAll("__BASE__", escapeHtml(base));
      return new Response(html, {
        headers: {
          "content-type": "text/html; charset=utf-8",
          "cache-control": "no-store",
          "X-Robots-Tag": "noindex, nofollow",
          "Referrer-Policy": "no-referrer",
          "X-Content-Type-Options": "nosniff",
          "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
        },
      });
    }
    if (path === `${base}/api/latest` && request.method === "GET") {
      // Only the freshness label: the raw digest is never shown to parents.
      const [d] = await latestDigests(env, 1);
      return json({ label: d ? d.label : null });
    }
    if (path === `${base}/api/ask` && request.method === "POST") return ask(request, env);
    return notFound();
  },

  async email(message, env) {
    const raw = await new Response(message.raw).arrayBuffer();
    const mail = await PostalMime.parse(raw);
    const fromHeader = mail.from && mail.from.address;
    const auth = (message.headers.get("authentication-results") || "").toLowerCase();
    // Cloudflare Email Routing records the DKIM result; a forged From fails it.
    const dkimOk = env.REQUIRE_DKIM === "false" || /\bdkim=pass\b/.test(auth);
    const text = mail.text || (mail.html || "").replace(/<br\s*\/?>/gi, "\n").replace(/<[^>]+>/g, "").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">");
    const digest = toDigest(mail.subject || "", text);

    if (digest && senderAllowed(fromHeader, env.ALLOWED_FROM) && dkimOk) {
      digest.body = redactNames(digest.body, env.REDACT_NAMES);
      await env.DIGESTS.put(`d:${digest.sentAt}`, JSON.stringify(digest), { expirationTtl: KEEP_DAYS * 86400 });
      console.log(`stored digest ${digest.id}`);
      return;
    }
    // Anything else (Gmail's forwarding-confirmation email, a reply, a spoof) goes to a person.
    if (digest) console.log(`digest ${digest.id} not stored: sender ${senderAllowed(fromHeader, env.ALLOWED_FROM) ? "ok" : "not allowed"}, dkim ${dkimOk ? "ok" : "failed"}`);
    if (env.FORWARD_TO) await message.forward(env.FORWARD_TO);
    else message.setReject("Not accepted");
  },
};
