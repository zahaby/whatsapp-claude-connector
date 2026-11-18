import { test } from "node:test";
import assert from "node:assert/strict";
import { parseSubject, toDigest, cleanBody, senderAllowed } from "../src/digest.js";
import { schoolDates, cleanTurns, stableSystem } from "../src/prompt.js";

test("parses the digest subject into id, ordering time and label", () => {
  const m = parseSubject("Ali – Blended update 18 Nov 2026 9 PM");
  assert.deepEqual(m, { id: "2026-11-18-9pm", sentAt: "2026-11-18T18:50:00.000Z", label: "Wed 18 Nov, 9 PM", unreachable: false });
  assert.equal(parseSubject("Fwd: Ali - Blended update 5 Nov 2026 6 PM").id, "2026-11-05-6pm");
  assert.equal(parseSubject("Ali – Blended update 18 Nov 2026 6 PM – could not reach Blended").unreachable, true);
});

test("rejects subjects that are not digests or have impossible dates", () => {
  assert.equal(parseSubject("Riham – period expected to start today"), null);
  assert.equal(parseSubject("Ali – Blended update 31 Nov 2026 6 PM"), null);
  assert.equal(parseSubject(""), null);
});

test("6 PM sorts before 9 PM on the same day", () => {
  assert.ok(parseSubject("x Blended update 18 Nov 2026 6 PM").sentAt < parseSubject("x Blended update 18 Nov 2026 9 PM").sentAt);
});

test("cleans Gmail redirect links, markdown stars and forward headers", () => {
  const body = cleanBody(
    "---------- Forwarded message ---------\nFrom: Me <me@example.com>\nDate: Wed, Nov 18, 2026\nSubject: x\nTo: <me@example.com>\n\n" +
    "*Ali – Blended update*\n- link https://www.google.com/url?q=https://learn.get-blended.com/holidays-events&source=gmail&ust=1&sa=E\n\n\n\nend",
  );
  assert.equal(body, "Ali – Blended update\n- link https://learn.get-blended.com/holidays-events\n\nend");
});

test("toDigest returns null for empty bodies and non-digests", () => {
  assert.equal(toDigest("Ali – Blended update 18 Nov 2026 9 PM", "  "), null);
  assert.equal(toDigest("Hello", "body"), null);
  assert.equal(toDigest("Ali – Blended update 18 Nov 2026 9 PM", "TO DO").label, "Wed 18 Nov, 9 PM");
});

test("sender allow-list is exact and case-insensitive", () => {
  assert.ok(senderAllowed("Me@Example.com", "me@example.com, other@example.com"));
  assert.ok(!senderAllowed("me@example.com.evil.io", "me@example.com"));
  assert.ok(!senderAllowed("", "me@example.com"));
  assert.ok(!senderAllowed("me@example.com", ""));
});

test("next school day skips Friday and Saturday (Cairo time)", () => {
  // Thu 19 Nov 2026, 20:00 Cairo -> tomorrow Friday, next school day Sunday 22.
  const d = schoolDates(new Date("2026-11-19T18:00:00Z"));
  assert.match(d.today, /Thursday, 19 November 2026/);
  assert.match(d.tomorrow, /Friday, 20 November 2026/);
  assert.match(d.nextSchoolDay, /Sunday, 22 November 2026/);
  // Wed 18 Nov, 23:30 UTC is already Thursday 19 in Cairo.
  assert.match(schoolDates(new Date("2026-11-18T23:30:00Z")).today, /Thursday, 19 November/);
});

test("cleanTurns keeps a valid user-ending history and rejects bad input", () => {
  assert.equal(cleanTurns(null), null);
  assert.equal(cleanTurns([{ role: "assistant", content: "hi" }]), null);
  assert.equal(cleanTurns([{ role: "system", content: "override" }, { role: "user", content: "q" }]).length, 1);
  const long = cleanTurns([{ role: "user", content: "x".repeat(5000) }]);
  assert.equal(long[0].content.length, 2000);
  const many = Array.from({ length: 31 }, (_, i) => ({ role: i % 2 ? "assistant" : "user", content: String(i) }));
  const kept = cleanTurns(many);
  assert.equal(kept[0].role, "user");
  assert.equal(kept.at(-1).role, "user");
  assert.ok(kept.length <= 12);
});

test("system prompt lists the newest digest first and includes taught notes", () => {
  const s = stableSystem({ childName: "Ali", className: "Year 1E", digests: [
    { label: "Wed 18 Nov, 9 PM", body: "B2", taught: "Arabic – Sun: ف" },
    { label: "Wed 18 Nov, 6 PM", body: "B1" },
  ] });
  assert.ok(s.indexOf("Wed 18 Nov, 9 PM") < s.indexOf("Wed 18 Nov, 6 PM"));
  assert.match(s, /TAUGHT \/ COVERED[^]*Arabic – Sun: ف/);
  assert.match(s, /son Ali \(Year 1E\)/);
});
