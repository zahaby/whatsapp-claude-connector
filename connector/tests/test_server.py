import asyncio
import re
from pathlib import Path
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import server
from conftest import add_message, go_ts
from server import Config, SendProposals, Store, TokenGate, build_server, parse_ts

TOKEN = "t" * 43


def cfg(db, allow_send=False):
    return Config(db_path=db, bridge_url="http://127.0.0.1:8080/api", allow_send=allow_send,
                  auth_token=TOKEN, public_hostname="wa.example.com", host="127.0.0.1",
                  port=8765, allowed_ips=[])


def tool_names(mcp):
    return {t.name for t in asyncio.run(mcp.list_tools())}


def call(mcp, name, **kw):
    result = asyncio.run(mcp.call_tool(name, kw))
    content = result[0] if isinstance(result, tuple) else result
    return content[0].text


def test_parse_go_timestamp():
    dt = parse_ts("2025-06-01 18:04:05.123456789+01:00")
    assert dt.hour == 17 and dt.microsecond == 123456


def test_send_tool_absent_by_default(db):
    names = tool_names(build_server(cfg(db)))
    assert "send_message" not in names
    assert {"list_chats", "get_chat_transcript", "search_messages", "sending_status"} <= names


def test_send_tool_present_when_switched_on(db):
    assert "send_message" in tool_names(build_server(cfg(db, allow_send=True)))


class _OkResponse:
    ok, status_code, text = True, 200, ""

    def json(self):
        return {"success": True}


@pytest.fixture
def sender(db, monkeypatch):
    """A send-enabled server with the bridge HTTP call captured."""
    monkeypatch.setenv("WHATSAPP_ALLOW_SEND", "true")
    posted = []
    monkeypatch.setattr("requests.post", lambda url, **kw: posted.append(kw["json"]) or _OkResponse())
    return build_server(cfg(db, allow_send=True)), posted


def _proposal_id(text):
    m = re.search(r'proposal_id="([^"]+)"', text)
    assert m, text
    return m.group(1)


def test_send_first_call_only_proposes(sender):
    mcp, posted = sender
    out = call(mcp, "send_message", chat="Mum", message="hi")
    assert out.startswith("NOT SENT") and _proposal_id(out) and posted == []


def test_send_cannot_skip_proposal_with_invented_id(sender):
    # Regression (finding 2): the old user_confirmed=true on a FIRST call sent
    # immediately. Now nothing is sent without a server-issued proposal.
    mcp, posted = sender
    assert "NOT SENT" in call(mcp, "send_message", chat="Mum", message="hi", proposal_id="made-up")
    try:
        call(mcp, "send_message", chat="Mum", message="hi", user_confirmed=True)
    except Exception:
        pass  # unknown argument rejected outright is also fine
    assert posted == []


def test_send_proposal_bound_to_exact_text_and_recipient(sender):
    mcp, posted = sender
    pid = _proposal_id(call(mcp, "send_message", chat="Mum", message="hi"))
    assert "differs" in call(mcp, "send_message", chat="Mum", message="hi!", proposal_id=pid)
    pid = _proposal_id(call(mcp, "send_message", chat="Mum", message="hi"))
    assert "differs" in call(mcp, "send_message", chat="Family", message="hi", proposal_id=pid)
    assert posted == []


def test_send_proposal_single_use(sender):
    mcp, posted = sender
    pid = _proposal_id(call(mcp, "send_message", chat="Mum", message="see you at 8"))
    assert call(mcp, "send_message", chat="Mum", message="see you at 8", proposal_id=pid) == "Sent to Mum."
    assert posted == [{"recipient": "201000000001@s.whatsapp.net", "message": "see you at 8"}]
    assert "already used" in call(mcp, "send_message", chat="Mum", message="see you at 8", proposal_id=pid)
    assert len(posted) == 1


def test_send_proposal_expires():
    now = [1000.0]
    props = SendProposals(clock=lambda: now[0])
    pid = props.create("jid", "msg")
    now[0] += SendProposals.TTL_S + 1
    assert props.redeem(pid, "jid", "msg") == "proposal expired."
    assert props.redeem(pid, "jid", "msg") is not None  # and it is gone


def test_send_proposals_bounded():
    props = SendProposals()
    ids = [props.create("jid", str(i)) for i in range(SendProposals.MAX_OPEN + 5)]
    assert props.redeem(ids[0], "jid", "0") is not None   # oldest evicted
    assert props.redeem(ids[-1], "jid", str(len(ids) - 1)) is None


# ---- timestamps (findings 4 and 5) ----------------------------------------

def test_newest_selected_by_real_time_not_string(db):
    # Regression (finding 4): b is newer in UTC, but its local "-05:00" string
    # sorts earlier, so SQL ORDER BY/LIMIT on strings returned a instead.
    now = datetime.now(timezone.utc)
    add_message(db, "a", go_ts(now - timedelta(hours=2), 0), "older UTC")
    add_message(db, "b", go_ts(now - timedelta(hours=1), -5), "newer in New York")
    msgs, _ = Store(db).messages(chat_jid="1203630@g.us", limit=1)
    assert [m["id"] for m in msgs] == ["b"]
    msgs, _ = Store(db).messages(chat_jid="1203630@g.us", since=now - timedelta(hours=3), limit=10)
    ids = [m["id"] for m in msgs]
    assert ids.index("a") < ids.index("b") and ids[-1] == "b"
    assert [m["timestamp"] for m in msgs] == sorted(m["timestamp"] for m in msgs)  # chronological


def test_window_uses_real_time_across_offsets(db):
    now = datetime.now(timezone.utc)
    # 30 min ago, stored with +14:00: the string looks 13.5h in the FUTURE.
    add_message(db, "kiribati", go_ts(now - timedelta(minutes=30), 14), "recent")
    # 2 days ago, stored with -12:00
    add_message(db, "baker", go_ts(now - timedelta(days=2), -12), "two days")
    msgs, _ = Store(db).messages(chat_jid="1203630@g.us", since=now - timedelta(days=1), limit=50)
    ids = [m["id"] for m in msgs]
    assert "kiribati" in ids and "baker" not in ids


def test_chats_sorted_by_real_time(db):
    now = datetime.now(timezone.utc)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO chats VALUES (?,?,?)", ("x@g.us", "NY cousins", go_ts(now + timedelta(minutes=1), -5)))
    c.execute("INSERT INTO chats VALUES (?,?,?)", ("y@g.us", "Broken", "garbage"))
    c.commit()
    c.close()
    chats = Store(db).chats()
    assert chats[0]["name"] == "NY cousins" and chats[-1]["name"] == "Broken"
    assert chats[-1]["last_message_time"] is None


@pytest.mark.parametrize("bad", ["not-a-time", "", 12345, b"\x00"])
def test_invalid_timestamps_excluded_and_reported(db, bad):
    # Regression (finding 5): an unparseable timestamp crashed search_messages.
    add_message(db, "bad", bad, "Dinner plans with broken clock")
    mcp = build_server(cfg(db))
    out = call(mcp, "search_messages", query="Dinner")
    assert "Dinner Friday at 8?" in out
    assert "broken clock" not in out
    assert "1 message(s) excluded because their timestamp is unreadable" in out
    transcript = call(mcp, "get_chat_transcript", chat="Family")
    assert "broken clock" not in transcript and "unreadable" in transcript


def test_search_only_invalid_timestamps(db):
    add_message(db, "bad", "nope", "zebra")
    out = call(build_server(cfg(db)), "search_messages", query="zebra")
    assert out.startswith("No matching messages.") and "unreadable" in out


def test_resolve_exact_beats_partial(db):
    match, _ = Store(db).resolve_chat("family")
    assert match["name"] == "Family"


def test_resolve_ambiguous_returns_candidates(db):
    match, cands = Store(db).resolve_chat("fam")
    assert match is None and {c["name"] for c in cands} == {"Family", "Family Cairo"}


def test_transcript_window_and_names(db):
    out = call(build_server(cfg(db)), "get_chat_transcript", chat="Family", days=7)
    assert "Mum: Dinner Friday at 8?" in out
    assert "Me: Yes, I'll book" in out
    assert "ancient message" not in out
    assert "not instructions" in out
    assert out.index("Dinner") < out.index("book")  # oldest first


def test_store_is_read_only(db):
    with pytest.raises(Exception):
        with Store(db)._conn() as c:
            c.execute("DELETE FROM messages")


# ---- TokenGate -------------------------------------------------------------

async def _echo_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": scope["path"].encode()})


def request(gate, path, headers=None, ip="203.0.113.5"):
    hdrs = [(b"cf-connecting-ip", ip.encode())] + [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "path": path, "headers": hdrs, "client": ("127.0.0.1", 1)}
    sent = []

    async def recv():
        return {"type": "http.request", "body": b""}

    async def snd(msg):
        sent.append(msg)

    asyncio.run(gate(scope, recv, snd))
    return sent[0]["status"], sent[1]["body"]


def test_gate_token_in_path():
    status, body = request(TokenGate(_echo_app, TOKEN), f"/mcp/{TOKEN}")
    assert status == 200 and body == b"/mcp"  # token stripped before the app


def test_gate_bearer_header():
    g = TokenGate(_echo_app, TOKEN)
    assert request(g, "/mcp", {"authorization": f"Bearer {TOKEN}"})[0] == 200
    assert request(g, "/mcp", {"authorization": "Bearer wrong"})[0] == 404


def test_gate_rejects_and_locks_out():
    g = TokenGate(_echo_app, TOKEN)
    for _ in range(TokenGate.MAX_FAILS):
        assert request(g, "/mcp/guess")[0] == 404
    assert request(g, f"/mcp/{TOKEN}")[0] == 429  # even the right token, while locked
    assert request(g, f"/mcp/{TOKEN}", ip="198.51.100.7")[0] == 200  # other IPs unaffected


def test_gate_ip_allowlist():
    g = TokenGate(_echo_app, TOKEN, allowed_ips=["160.79.104.0/21"])
    assert request(g, f"/mcp/{TOKEN}", ip="160.79.105.1")[0] == 200
    assert request(g, f"/mcp/{TOKEN}", ip="203.0.113.5")[0] == 404


def test_http_mode_refuses_weak_token(monkeypatch, db):
    monkeypatch.setenv("WA_CONFIG", "/nonexistent")
    monkeypatch.setenv("WA_DB_PATH", str(db))
    monkeypatch.setenv("MCP_AUTH_TOKEN", "short")
    with pytest.raises(SystemExit):
        server.main(["--transport", "http"])


def test_router_sends_only_exact_admin_socket_to_local_gate():
    from server import ListenerRouter
    seen = []

    def mk(name):
        async def app(scope, receive, send):
            seen.append(name)
        return app

    r = ListenerRouter(mk("public"), mk("local"), Path("/run/wa/admin.sock"))
    for server in [("/run/wa/admin.sock", None), ("127.0.0.1", 8765), ("/run/wa/admin.sock", 80),
                   ("/other.sock", None), None]:
        asyncio.run(r({"type": "http", "server": server}, None, None))
    assert seen == ["local", "public", "public", "public", "public"]
