import { test } from "node:test";
import assert from "node:assert/strict";
import { parseSubject, toDigest, cleanBody, senderAllowed, redactNames } from "../src/digest.js";
import { schoolDates, cleanTurns, stableSystem } from "../src/prompt.js";

test("parses the digest subject into id, ordering time and label", () => {
  const m = parseSubject("Ali – Blended update 18 Nov 2026 9 PM");
  assert.deepEqual(m, { id: "2026-11-18-9pm", sentAt: "2026-11-18T18:50:00.000Z", label: "Wed 18 Nov, 9 PM", unreachable: false });
  assert.equal(parseSubject("Fwd: Ali - Blended update 5 Nov 2026 6 PM").id, "2026-11-05-6pm");
  assert.equal(parseSubject("Ali – Blended update 18 Nov 2026 6 PM – could not reach Blended").unreachable, true);
});

test("accepts any run hour, AM or PM, and orders runs within a day", () => {
  const am = parseSubject("Ali – Blended update 19 Nov 2026 7 AM");
  assert.deepEqual(am, { id: "2026-11-19-7am", sentAt: "2026-11-19T04:50:00.000Z", label: "Thu 19 Nov, 7 AM", unreachable: false });
  const pm3 = parseSubject("Ali – Blended update 19 Nov 2026 3 PM");
  assert.equal(pm3.id, "2026-11-19-3pm");
  assert.equal(pm3.sentAt, "2026-11-19T12:50:00.000Z");
  assert.ok(am.sentAt < pm3.sentAt && pm3.sentAt < parseSubject("x Blended update 19 Nov 2026 6 PM").sentAt);
  assert.equal(parseSubject("x Blended update 19 Nov 2026 12 PM").sentAt, "2026-11-19T09:50:00.000Z");
  assert.equal(parseSubject("x Blended update 19 Nov 2026 12 AM").sentAt, "2026-11-18T21:50:00.000Z");
  assert.equal(parseSubject("x Blended update 19 Nov 2026 13 PM"), null);
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

test("system prompt lists the newest digest first, includes taught notes and is class-wide", () => {
  const s = stableSystem({ className: "Year 1E", digests: [
    { label: "Wed 18 Nov, 9 PM", body: "B2", taught: "Arabic – Sun: ف" },
    { label: "Wed 18 Nov, 6 PM", body: "B1" },
  ] });
  assert.ok(s.indexOf("Wed 18 Nov, 9 PM") < s.indexOf("Wed 18 Nov, 6 PM"));
  assert.match(s, /TAUGHT \/ COVERED[^]*Arabic – Sun: ف/);
  assert.match(s, /parents of children in Year 1E/);
  assert.match(s, /Share class-wide information only/);
  assert.match(s, /runs at about 6 PM and 9 PM Cairo time/);
  assert.match(stableSystem({ className: "Y", digests: [], updateTimes: "about 7 AM and 3 PM" }), /runs at about 7 AM and 3 PM/);
  assert.doesNotMatch(s, /\bson\b/);
});

test("redactNames hides names in any case and script, whole words only", () => {
  const names = "Ali, Ali Mohamed El Zahaby, Riham, علي";
  assert.equal(redactNames("Ali – Blended update", names), "[name] – Blended update");
  assert.equal(redactNames("help ALI memorise his line", names), "help [name] memorise his line");
  assert.equal(redactNames("Ali Mohamed El Zahaby, Year 1E", names), "[name], Year 1E");
  assert.equal(redactNames("Ali  Mohamed\nEl Zahaby", names), "[name]");
  assert.equal(redactNames("رسالة إلى علي اليوم", names), "رسالة إلى [name] اليوم");
  assert.equal(redactNames("Alignment and Italia stay", names), "Alignment and Italia stay");
  assert.equal(redactNames("Riham's note", names), "[name]'s note");
  assert.equal(redactNames("nothing to hide", ""), "nothing to hide");
});

test("a Private heading and everything after it is dropped", () => {
  assert.equal(cleanBody("TO DO\n- Spelling test\n\n*PRIVATE:*\n- Note about one child"), "TO DO\n- Spelling test");
  assert.equal(cleanBody("Private\nsecret"), "");
  assert.equal(cleanBody("Privately funded trip on Sun"), "Privately funded trip on Sun");
});
