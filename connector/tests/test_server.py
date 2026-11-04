import asyncio
import json

import pytest

import server
from server import Config, Store, TokenGate, build_server, parse_ts

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


def test_send_requires_confirmation(db, monkeypatch):
    monkeypatch.setenv("WHATSAPP_ALLOW_SEND", "true")
    posted = []
    monkeypatch.setattr("requests.post", lambda *a, **k: posted.append(k))
    out = call(build_server(cfg(db, allow_send=True)), "send_message", chat="Mum", message="hi")
    assert "Not sent" in out and not posted


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
