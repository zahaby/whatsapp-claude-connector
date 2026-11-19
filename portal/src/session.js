// Signed visitor sessions issued after a Cloudflare Turnstile check.
// Cookie value: "<sid>.<expiresEpochSeconds>.<hmac>" with HMAC-SHA256 over "<sid>.<exp>".
// Uses WebCrypto only, so it runs in Workers and under `node --test`.

const enc = new TextEncoder();

function b64url(bytes) {
  let s = "";
  for (const b of new Uint8Array(bytes)) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function hmac(key, data) {
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return b64url(await crypto.subtle.sign("HMAC", k, enc.encode(data)));
}

function sameString(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export async function issueSession(key, ttlSeconds, now = Date.now()) {
  const sid = b64url(crypto.getRandomValues(new Uint8Array(12)));
  const exp = Math.floor(now / 1000) + ttlSeconds;
  return `${sid}.${exp}.${await hmac(key, `${sid}.${exp}`)}`;
}

// Returns the session id when the value is authentic and not expired, else null.
export async function readSession(key, value, now = Date.now()) {
  if (!key || typeof value !== "string") return null;
  const parts = value.split(".");
  if (parts.length !== 3) return null;
  const [sid, exp, sig] = parts;
  if (!/^[A-Za-z0-9_-]{8,40}$/.test(sid) || !/^\d{9,12}$/.test(exp)) return null;
  if (Number(exp) * 1000 <= now) return null;
  return sameString(await hmac(key, `${sid}.${exp}`), sig) ? sid : null;
}

export function cookieValue(header, name) {
  for (const part of (header || "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0 && part.slice(0, i).trim() === name) return part.slice(i + 1).trim();
  }
  return null;
}

// Ask Cloudflare whether a Turnstile token is valid (single use, ~5 minutes).
export async function verifyTurnstile(secret, token, ip, fetchImpl = fetch) {
  if (!secret || typeof token !== "string" || !token || token.length > 2048) return false;
  const form = new FormData();
  form.append("secret", secret);
  form.append("response", token);
  if (ip) form.append("remoteip", ip);
  try {
    const r = await fetchImpl("https://challenges.cloudflare.com/turnstile/v0/siteverify", { method: "POST", body: form });
    const d = await r.json();
    return d && d.success === true;
  } catch {
    return false;
  }
}
