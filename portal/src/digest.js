// Turn a "<Child> – Blended update <DD Mon YYYY> <H AM|PM>" email into a stored digest.
// Pure functions only (no Worker APIs) so they run under `node --test`.

const MONTHS = { Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5, Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11 };
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

// "Ali – Blended update 18 Nov 2026 9 PM", "... 19 Nov 2026 7 AM" (also "Fwd: ...", "–" or "-",
// optional " – could not reach Blended"). The run hour can be any hour, AM or PM.
const SUBJECT_RE = /Blended update\s+(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})\s+(\d{1,2})\s*(AM|PM)\b/i;

export function parseSubject(subject) {
  const m = SUBJECT_RE.exec(subject || "");
  if (!m) return null;
  const day = Number(m[1]);
  const mon = m[2][0].toUpperCase() + m[2].slice(1, 3).toLowerCase();
  const year = Number(m[3]);
  const hour12 = Number(m[4]);
  const ampm = m[5].toUpperCase();
  if (!(mon in MONTHS) || hour12 < 1 || hour12 > 12) return null;
  const date = new Date(Date.UTC(year, MONTHS[mon], day));
  if (date.getUTCDate() !== day) return null;
  const hour24 = (hour12 % 12) + (ampm === "PM" ? 12 : 0);
  // A run labelled "6 PM" runs at ~5:50 PM Cairo (UTC+2 in winter, UTC+3 in summer). The exact
  // instant only orders the digests, so use UTC+2 and ten minutes before the labelled hour.
  const sentAt = new Date(Date.UTC(year, MONTHS[mon], day, hour24 - 2, -10)).toISOString();
  const iso = date.toISOString().slice(0, 10);
  return {
    id: `${iso}-${hour12}${ampm.toLowerCase()}`,
    sentAt,
    label: `${WEEKDAYS[date.getUTCDay()]} ${day} ${mon}, ${hour12} ${ampm}`,
    unreachable: /could not reach/i.test(subject),
  };
}

// Gmail wraps links as https://www.google.com/url?q=<real>&source=...; keep the real link.
export function unwrapLinks(text) {
  return text.replace(/https?:\/\/www\.google\.com\/url\?q=([^\s&]+)[^\s]*/g, (_, q) => {
    try { return decodeURIComponent(q); } catch { return q; }
  });
}

// Remove a manual-forward header block ("---------- Forwarded message ---------" + From/Date/Subject/To lines).
export function stripForwardHeader(text) {
  const i = text.indexOf("---------- Forwarded message ---------");
  if (i === -1) return text;
  const rest = text.slice(i).split("\n").slice(1);
  let k = 0;
  while (k < rest.length && /^(From|Date|Subject|To|Cc):/i.test(rest[k].trim())) k++;
  return rest.slice(k).join("\n");
}

export function cleanBody(text) {
  let t = (text || "").replace(/\r\n/g, "\n");
  t = stripForwardHeader(t);
  // A "Private" / "PRIVATE:" heading line marks family-only items: drop it and everything after.
  const priv = /^[ \t*-]*private\b[^\n]*$/im.exec(t);
  if (priv) t = t.slice(0, priv.index);
  t = unwrapLinks(t);
  t = t.replace(/\*/g, "");
  t = t.replace(/\n{3,}/g, "\n\n").trim();
  return t.slice(0, 20000);
}

// subject + plain-text body -> digest record, or null when the email is not a digest.
export function toDigest(subject, text) {
  const meta = parseSubject(subject);
  if (!meta) return null;
  const body = cleanBody(text);
  if (!body) return null;
  return { id: meta.id, sentAt: meta.sentAt, label: meta.label, body, unreachable: meta.unreachable };
}

// Replace each listed name (comma-separated, any script) with "[name]". Longest first so
// "Ali Mohamed" goes before "Ali"; whole words only, case-insensitive.
export function redactNames(text, nameList) {
  const names = (nameList || "")
    .split(",")
    .map(s => s.trim())
    .filter(s => s.length >= 2)
    .sort((a, b) => b.length - a.length);
  let t = text || "";
  for (const n of names) {
    const esc = n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
    t = t.replace(new RegExp(`(?<![\\p{L}\\p{N}])${esc}(?![\\p{L}\\p{N}])`, "giu"), "[name]");
  }
  return t;
}

export function senderAllowed(fromAddress, allowedList) {
  const from = (fromAddress || "").trim().toLowerCase();
  if (!from) return false;
  return (allowedList || "")
    .split(",")
    .map(s => s.trim().toLowerCase())
    .filter(Boolean)
    .includes(from);
}
