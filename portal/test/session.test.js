import { test } from "node:test";
import assert from "node:assert/strict";
import { issueSession, readSession, cookieValue, verifyTurnstile } from "../src/session.js";

const KEY = "test-secret-key";

test("a fresh session reads back to its id", async () => {
  const v = await issueSession(KEY, 3600);
  const sid = await readSession(KEY, v);
  assert.ok(sid && v.startsWith(sid + "."));
});

test("expired, tampered, wrong-key and malformed sessions are rejected", async () => {
  const now = Date.now();
  const v = await issueSession(KEY, 60, now);
  assert.equal(await readSession(KEY, v, now + 61_000), null);
  const [sid, exp, sig] = v.split(".");
  assert.equal(await readSession(KEY, `${sid}.${Number(exp) + 9999}.${sig}`), null);
  assert.equal(await readSession(KEY, `${sid}x.${exp}.${sig}`), null);
  assert.equal(await readSession("other-key", v), null);
  assert.equal(await readSession(KEY, "garbage"), null);
  assert.equal(await readSession(KEY, null), null);
  assert.equal(await readSession("", v), null);
});

test("cookieValue finds the named cookie only", () => {
  assert.equal(cookieValue("a=1; sd=abc.123.xyz; b=2", "sd"), "abc.123.xyz");
  assert.equal(cookieValue("xsd=1", "sd"), null);
  assert.equal(cookieValue(null, "sd"), null);
});

test("verifyTurnstile trusts only success:true from Cloudflare", async () => {
  const fake = body => async (url, init) => {
    assert.equal(url, "https://challenges.cloudflare.com/turnstile/v0/siteverify");
    assert.equal(init.body.get("secret"), "s");
    assert.equal(init.body.get("response"), "tok");
    return { json: async () => body };
  };
  assert.equal(await verifyTurnstile("s", "tok", "1.2.3.4", fake({ success: true })), true);
  assert.equal(await verifyTurnstile("s", "tok", null, fake({ success: false })), false);
  assert.equal(await verifyTurnstile("s", "tok", null, async () => { throw new Error("down"); }), false);
  assert.equal(await verifyTurnstile("s", "", null, fake({ success: true })), false);
  assert.equal(await verifyTurnstile("", "tok", null, fake({ success: true })), false);
});
