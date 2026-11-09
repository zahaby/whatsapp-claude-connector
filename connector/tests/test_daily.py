"""
Daily group summary (daily.py): a separate, minimal connector for an
unattended 12 AM scheduled task. It can read and post ONLY to the configured
group, at most once per interval, never when the group was quiet, and only
while switched on. These tests pin every one of those limits.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import daily
import net
from server import Config

GROUP = "1203630@g.us"          # "Family" in the conftest database
OTHER = "1203631@g.us"          # "Family Cairo": must never be readable or postable
TOKEN = "M" * 43
DAILY_TOKEN = "D" * 43
SERVER = Path(__file__).resolve().parents[1] / "server.py"


def cfg(db, **kw):
    base = dict(db_path=db, bridge_url="http://127.0.0.1:1/api", allow_send=False, auth_token=TOKEN,
                public_hostname="", host="127.0.0.1", port=1, allowed_ips=[], autopost=True,
                autopost_chat=GROUP, daily_token=DAILY_TOKEN)
    base.update(kw)
    return Config(**base)


class FakeBridge:
    """Stands in for daily.bridge_sender: returns (outcome, detail)."""

    def __init__(self, outcome="sent"):
        self.outcome, self.sent = outcome, []

    def __call__(self, jid, text):
        self.sent.append((jid, text))
        return self.outcome, {"sent": "sent", "failed": "bridge not reachable",
                              "unknown": "bridge did not confirm in time"}.get(self.outcome, "?")


@pytest.fixture
def setup(db, tmp_path, monkeypatch):
    monkeypatch.setenv("WHATSAPP_AUTOPOST", "true")
    from server import Store
    bridge = FakeBridge()
    now = [datetime.now(timezone.utc)]
    state = daily.DailyState(tmp_path / "state")
    mcp = daily.build_daily_server(cfg(db), Store(db), state, send=bridge, clock=lambda: now[0])
    return mcp, bridge, state, now


def call(mcp, name, **kw):
    result = asyncio.run(mcp.call_tool(name, kw))
    return (result[0] if isinstance(result, tuple) else result)[0].text


def tools(mcp):
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


# ---- what the connector can do at all ----------------------------------------

def test_only_three_tools_and_no_chat_or_recipient_parameter(setup):
    mcp, *_ = setup
    t = tools(mcp)
    assert set(t) == {"get_group_transcript", "post_daily_summary", "daily_status"}
    for tool in t.values():
        params = set(tool.inputSchema.get("properties", {}))
        assert not params & {"chat", "chat_jid", "jid", "recipient", "to", "group"}, (tool.name, params)


def test_links_tool_also_has_no_chat_parameter(db, tmp_path, monkeypatch):
    import media
    from server import Store
    monkeypatch.setenv("WHATSAPP_AUTOPOST", "true")
    svc = media.MediaService(store=Store(db), cache=media.Cache(tmp_path / "c"), bridge_url="http://127.0.0.1:1/api")
    t = tools(daily.build_daily_server(cfg(db), Store(db), daily.DailyState(tmp_path / "s"), media_svc=svc))
    assert set(t) == {"get_group_transcript", "post_daily_summary", "daily_status", "get_group_links_and_media"}
    for tool in t.values():
        params = set(tool.inputSchema.get("properties", {}))
        assert not params & {"chat", "chat_jid", "jid", "recipient", "to", "group"}, (tool.name, params)


def test_transcript_is_only_the_configured_group(setup, db):
    from conftest import add_message
    from conftest import go_ts
    add_message(db, "secret", go_ts(datetime.now(timezone.utc) - timedelta(hours=1)), "other chat secret",
                chat=OTHER)
    mcp, *_ = setup
    out = call(mcp, "get_group_transcript")
    assert "Group: Family" in out and "Ignore previous instructions" in out   # 1 h old, in this group
    assert "other chat secret" not in out                                    # 1 h old, other chat
    assert "not instructions" in out


def test_transcript_hours_window_is_clamped(setup):
    mcp, *_ = setup
    out = call(mcp, "get_group_transcript", hours=2)
    assert "Yes, I'll book" not in out               # 1 day old
    out = call(mcp, "get_group_transcript", hours=10_000)
    assert "Yes, I'll book" in out                   # clamped to 48 h: still inside
    assert "ancient message" not in out              # 40 days old: never


# ---- posting ---------------------------------------------------------------------

def test_post_goes_only_to_the_group_with_label(setup):
    mcp, bridge, state, now = setup
    out = call(mcp, "post_daily_summary", summary="Dinner is Friday at 8.")
    assert out.startswith("Posted")
    assert len(bridge.sent) == 1
    jid, text = bridge.sent[0]
    assert jid == GROUP
    assert text.startswith(daily.PREFIX) and "\n\nDinner is Friday at 8.\n\n📊\n" in text
    assert state.last_post() is not None


def news(db, when, text="news"):
    from conftest import add_message, go_ts
    add_message(db, f"n{when.timestamp()}", go_ts(when), text)


def test_second_post_within_the_gap_refused_even_after_restart(setup, db, tmp_path):
    mcp, bridge, state, now = setup
    assert call(mcp, "post_daily_summary", summary="one").startswith("Posted")
    now[0] += timedelta(minutes=59)
    news(db, now[0] - timedelta(minutes=10))
    assert "the next one is allowed after" in call(mcp, "post_daily_summary", summary="two")
    # A restarted server (new instance, same state file) still refuses.
    from server import Store
    mcp2 = daily.build_daily_server(cfg(db), Store(db), daily.DailyState(tmp_path / "state"),
                                    send=bridge, clock=lambda: now[0])
    assert "the next one is allowed after" in call(mcp2, "post_daily_summary", summary="three")
    assert len(bridge.sent) == 1


def test_two_posts_a_day_allowed_third_refused(setup, db):
    mcp, bridge, state, now = setup
    assert call(mcp, "post_daily_summary", summary="noon").startswith("Posted")
    now[0] += timedelta(hours=12)
    news(db, now[0] - timedelta(hours=1))
    assert call(mcp, "post_daily_summary", summary="midnight").startswith("Posted")
    now[0] += timedelta(hours=2)
    news(db, now[0] - timedelta(minutes=30))
    out = call(mcp, "post_daily_summary", summary="third")
    assert "already posted 2 time(s) in the last 24 hours (the limit is 2)" in out
    assert len(bridge.sent) == 2


def test_limit_is_a_rolling_24_hours(setup, db):
    mcp, bridge, state, now = setup
    first = now[0]
    assert call(mcp, "post_daily_summary", summary="a").startswith("Posted")
    now[0] = first + timedelta(hours=2)
    news(db, now[0] - timedelta(minutes=30))
    assert call(mcp, "post_daily_summary", summary="b").startswith("Posted")
    now[0] = first + timedelta(hours=23, minutes=59)
    news(db, now[0] - timedelta(minutes=30))
    assert "already posted 2" in call(mcp, "post_daily_summary", summary="c")
    now[0] = first + timedelta(hours=24, minutes=1)        # the first post left the window
    assert call(mcp, "post_daily_summary", summary="c").startswith("Posted")


def test_max_per_day_is_configurable(db, tmp_path, monkeypatch):
    monkeypatch.setenv("WHATSAPP_AUTOPOST", "true")
    from server import Store
    bridge = FakeBridge()
    now = [datetime.now(timezone.utc)]
    mcp = daily.build_daily_server(cfg(db, autopost_max_per_day=1), Store(db), daily.DailyState(tmp_path / "s"),
                                   send=bridge, clock=lambda: now[0])
    assert call(mcp, "post_daily_summary", summary="a").startswith("Posted")
    now[0] += timedelta(hours=3)
    news(db, now[0] - timedelta(minutes=30))
    assert "the limit is 1" in call(mcp, "post_daily_summary", summary="b")


def test_nothing_new_since_last_post_is_not_posted(setup, db):
    mcp, bridge, state, now = setup
    assert call(mcp, "post_daily_summary", summary="a").startswith("Posted")
    now[0] += timedelta(hours=3)                            # no message since the first post
    assert "no new messages since the last summary" in call(mcp, "post_daily_summary", summary="b")


def test_transcript_starts_at_the_last_post(setup, db):
    mcp, bridge, state, now = setup
    assert "Ignore previous instructions" in call(mcp, "get_group_transcript")   # 1 h old, no post yet
    assert call(mcp, "post_daily_summary", summary="a").startswith("Posted")
    now[0] += timedelta(hours=3)
    news(db, now[0] - timedelta(hours=1), "after the post")
    out = call(mcp, "get_group_transcript")
    assert "after the post" in out and "Ignore previous instructions" not in out
    assert "Ignore previous instructions" in call(mcp, "get_group_transcript", hours=6)   # explicit window


def test_transcript_never_more_than_24h_without_a_recent_post(setup, db):
    mcp, bridge, state, now = setup
    state.record(now[0] - timedelta(days=3), GROUP)          # last post long ago
    out = call(mcp, "get_group_transcript")
    assert "Yes, I'll book" not in out                       # 1 day old: outside the 24 h cap
    assert "Ignore previous instructions" in out


def test_status_shows_daily_count_and_length_limit(setup):
    mcp, *_ = setup
    call(mcp, "post_daily_summary", summary="a")
    out = call(mcp, "daily_status")
    assert "Posts in the last 24 hours: 1 of 2" in out and "Longest post: 7000 characters" in out


def test_old_one_post_state_format_still_read(tmp_path):
    st = daily.DailyState(tmp_path)
    st.path.write_text(json.dumps({"last_post_utc": "2026-11-04T22:00:00+00:00", "chat": GROUP,
                                   "outcome": "unknown"}))
    assert st.posts() == [(datetime(2026, 11, 4, 22, tzinfo=timezone.utc), "unknown")]
    st.record(datetime(2026, 11, 5, 22, tzinfo=timezone.utc), GROUP)
    assert [o for _, o in st.posts()] == ["unknown", "sent"]


def test_state_keeps_only_a_week(tmp_path):
    st = daily.DailyState(tmp_path)
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    for d in range(20):
        st.record(base + timedelta(days=d), GROUP)
    assert len(st.posts()) == 8                               # 7 days back + the new one


def test_quiet_group_is_not_posted(db, tmp_path, monkeypatch):
    monkeypatch.setenv("WHATSAPP_AUTOPOST", "true")
    from server import Store
    bridge = FakeBridge()
    later = datetime.now(timezone.utc) + timedelta(days=5)   # nothing in the last 24h then
    mcp = daily.build_daily_server(cfg(db), Store(db), daily.DailyState(tmp_path / "s"), send=bridge,
                                   clock=lambda: later)
    assert "no new messages since the last summary" in call(mcp, "post_daily_summary", summary="x")
    assert bridge.sent == []


@pytest.mark.parametrize("summary,reason", [("", "empty"), ("   \n ", "empty"), ("x" * 7001, "limit is 7000")])
def test_empty_or_too_long_refused(setup, summary, reason):
    mcp, bridge, *_ = setup
    assert reason in call(mcp, "post_daily_summary", summary=summary)
    assert bridge.sent == []


def test_switched_off_at_call_time_refuses(setup, monkeypatch):
    mcp, bridge, *_ = setup
    monkeypatch.setenv("WHATSAPP_AUTOPOST", "false")
    assert "switched off" in call(mcp, "post_daily_summary", summary="hello")
    assert bridge.sent == []


def test_failed_send_does_not_use_up_the_day(db, tmp_path, monkeypatch):
    monkeypatch.setenv("WHATSAPP_AUTOPOST", "true")
    from server import Store
    bridge = FakeBridge("failed")
    state = daily.DailyState(tmp_path / "s")
    mcp = daily.build_daily_server(cfg(db), Store(db), state, send=bridge)
    assert "NOT POSTED" in call(mcp, "post_daily_summary", summary="hello")
    assert state.last_post() is None
    bridge.outcome = "sent"
    assert call(mcp, "post_daily_summary", summary="hello").startswith("Posted")


def test_summary_text_never_logged(setup, caplog):
    mcp, *_ = setup
    caplog.set_level("INFO")
    call(mcp, "post_daily_summary", summary="PRIVATE-FAMILY-NEWS")
    assert "PRIVATE-FAMILY-NEWS" not in caplog.text and "daily summary posted" in caplog.text


def test_state_file_is_owner_only(setup):
    mcp, bridge, state, _ = setup
    call(mcp, "post_daily_summary", summary="x")
    if sys.platform != "win32":
        assert oct(state.path.stat().st_mode & 0o777) == "0o600"
        assert oct(state.path.parent.stat().st_mode & 0o777) == "0o700"


def test_corrupt_state_file_treated_as_never_posted(tmp_path):
    st = daily.DailyState(tmp_path)
    st.path.write_text("{not json")
    assert st.last_post() is None


# ---- configuration checks ----------------------------------------------------------

@pytest.mark.parametrize("kw,needle", [
    ({"autopost_chat": ""}, "no group is set"),
    ({"daily_token": "short"}, "MCP_DAILY_TOKEN"),
    ({"daily_token": TOKEN}, "must differ"),
])
def test_config_problems(db, kw, needle):
    assert needle in daily.config_problem(cfg(db, **kw))


def test_off_means_no_problem_and_no_endpoint(db):
    assert daily.config_problem(cfg(db, autopost=False, autopost_chat="", daily_token="")) is None


# ---- the real server, end to end ---------------------------------------------------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(db, tmp_path, **extra):
    port, admin = free_port(), free_port()
    while admin == port:
        admin = free_port()
    env = {**os.environ, "WA_CONFIG": str(tmp_path / "none"), "WA_DB_PATH": str(db), "MCP_AUTH_TOKEN": TOKEN,
           "MCP_PORT": str(port), "MCP_ADMIN_CHANNEL": "tcp", "MCP_ADMIN_PORT": str(admin),
           "MCP_TLS_DIR": str(tmp_path / "tls"), "WA_STATE_DIR": str(tmp_path / "state"),
           "WHATSAPP_AUTOPOST": "true", "WA_AUTOPOST_CHAT": GROUP, "MCP_DAILY_TOKEN": DAILY_TOKEN, **extra}
    log = tmp_path / "c.log"
    proc = subprocess.Popen([sys.executable, str(SERVER), "--transport", "http", "--log-file", str(log)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for _ in range(150):
        if net.healthy(port, tmp_path / "tls"):
            return proc, port, env
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    proc.kill()
    return proc, None, env


@pytest.fixture
def daily_server(db, tmp_path):
    proc, port, env = start_server(db, tmp_path)
    assert port, (tmp_path / "c.log").read_text() if (tmp_path / "c.log").exists() else proc.stderr.read()
    yield port, tmp_path / "tls", env
    proc.terminate()
    proc.wait(10)


def mcp_tools(port, tls_dir, path):
    def pinned(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, timeout=timeout or httpx.Timeout(30), auth=auth,
                                 verify=net.ctx(tls_dir), follow_redirects=True)

    async def go():
        async with streamablehttp_client(net.url(port, path), httpx_client_factory=pinned) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name for t in (await s.list_tools()).tools}
    return asyncio.run(go())


def status_of(port, tls_dir, path):
    req = __import__("urllib.request").request.Request(
        net.url(port, path), method="POST", data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    return net.request(req, tls_dir)[0]


def test_daily_endpoint_serves_only_its_tools(daily_server):
    port, tls_dir, _ = daily_server
    assert mcp_tools(port, tls_dir, f"/daily/{DAILY_TOKEN}") == {
        "get_group_transcript", "post_daily_summary", "daily_status", "get_group_links_and_media"}
    main = mcp_tools(port, tls_dir, f"/mcp/{TOKEN}")
    assert "autopost_status" in main and "post_daily_summary" not in main


@pytest.mark.parametrize("path", [f"/daily/{TOKEN}", f"/mcp/{DAILY_TOKEN}", "/daily/wrong", "/daily"])
def test_tokens_are_not_interchangeable(daily_server, path):
    port, tls_dir, _ = daily_server
    assert status_of(port, tls_dir, path) == 404


def test_endpoint_absent_when_switched_off(db, tmp_path):
    proc, port, _ = start_server(db, tmp_path, WHATSAPP_AUTOPOST="false")
    try:
        assert port
        assert status_of(port, tmp_path / "tls", f"/daily/{DAILY_TOKEN}") == 404
        assert "autopost_status" not in mcp_tools(port, tmp_path / "tls", f"/mcp/{TOKEN}")
    finally:
        proc.terminate()
        proc.wait(10)


def test_server_refuses_to_start_half_configured(db, tmp_path):
    proc, port, _ = start_server(db, tmp_path, WA_AUTOPOST_CHAT="")
    proc.wait(10)
    assert port is None and proc.returncode != 0
    assert "no group is set" in (tmp_path / "c.log").read_text()


def test_verify_reports_autopost_state(daily_server):
    port, tls_dir, env = daily_server
    v = Path(__file__).resolve().parents[1] / "verify.py"
    on = subprocess.run([sys.executable, str(v), "autopost", "--expect", "on"], env=env, capture_output=True, text=True)
    off = subprocess.run([sys.executable, str(v), "autopost", "--expect", "off"], env=env, capture_output=True, text=True)
    assert on.returncode == 0 and off.returncode == 1


# ---- CLI used by the scripts -------------------------------------------------------

def cli(db, tmp_path, *args):
    env = {**os.environ, "WA_CONFIG": str(tmp_path / "none"), "WA_DB_PATH": str(db),
           "WA_STATE_DIR": str(tmp_path / "state")}
    return subprocess.run([sys.executable, str(SERVER.parent / "daily.py"), *args], env=env,
                          capture_output=True, text=True, timeout=30)


def test_cli_resolve_exact_ambiguous_and_missing(db, tmp_path):
    r = cli(db, tmp_path, "resolve", "Family")
    assert r.returncode == 0 and r.stdout.strip() == f"{GROUP}\tFamily"
    r = cli(db, tmp_path, "resolve", "fam")
    assert r.returncode == 1 and "Family Cairo" in r.stderr
    r = cli(db, tmp_path, "resolve", "Nonexistent group")
    assert r.returncode == 1 and "no chat matches" in r.stderr


def test_cli_reset_limit(db, tmp_path):
    st = daily.DailyState(tmp_path / "state")
    st.record(datetime.now(timezone.utc), GROUP)
    assert cli(db, tmp_path, "reset-limit").returncode == 0
    assert st.last_post() is None


# ---- uncertain delivery (the real-PC "Bad Gateway" case) ---------------------------

def test_uncertain_send_blocks_retries_until_reset(db, tmp_path, monkeypatch):
    # The bridge got the message but did not confirm: it may have been
    # delivered, so an unattended retry could post it twice.
    monkeypatch.setenv("WHATSAPP_AUTOPOST", "true")
    from server import Store
    bridge = FakeBridge("unknown")
    state = daily.DailyState(tmp_path / "s")
    mcp = daily.build_daily_server(cfg(db), Store(db), state, send=bridge)
    out = call(mcp, "post_daily_summary", summary="hello")
    assert out.startswith("UNCERTAIN") and "Do NOT retry" in out
    bridge.outcome = "sent"
    assert "may or may not have been delivered" in call(mcp, "post_daily_summary", summary="hello")
    assert len(bridge.sent) == 1                                   # no second attempt reached the bridge
    assert "NOT confirmed" in call(mcp, "daily_status")
    state.reset()                                                  # a person checked: it did not arrive
    assert call(mcp, "post_daily_summary", summary="hello").startswith("Posted")


@pytest.mark.parametrize("bad", [(False, "x"), (True, "x"), ("ok", "x"), (None, None)])
def test_only_an_explicit_sent_counts_as_delivered(db, tmp_path, monkeypatch, bad):
    monkeypatch.setenv("WHATSAPP_AUTOPOST", "true")
    from server import Store
    state = daily.DailyState(tmp_path / "s")
    mcp = daily.build_daily_server(cfg(db), Store(db), state, send=lambda j, t: bad)
    assert call(mcp, "post_daily_summary", summary="hello").startswith("NOT POSTED")
    assert state.last_post() is None


def test_old_state_file_without_outcome_reads_as_sent(tmp_path):
    st = daily.DailyState(tmp_path)
    st.path.write_text(json.dumps({"last_post_utc": "2026-11-04T22:00:00+00:00", "chat": GROUP}))
    assert st.last()[1] == "sent"


def test_slow_bridge_does_not_freeze_the_server(db, tmp_path):
    # Regression (real PC): a send to a large group took long; the tool ran on
    # the event loop and froze the server, so the tunnel got "Bad Gateway".
    import http.server
    import threading

    class SlowBridge(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(6)
            body = b'{"success": true, "message": "sent"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    bridge = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowBridge)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    proc, port, env = start_server(db, tmp_path, WA_BRIDGE_URL=f"http://127.0.0.1:{bridge.server_address[1]}/api")
    try:
        assert port
        result = {}

        def post():
            def pinned(headers=None, timeout=None, auth=None):
                return httpx.AsyncClient(headers=headers, timeout=timeout or httpx.Timeout(60), auth=auth,
                                         verify=net.ctx(tmp_path / "tls"), follow_redirects=True)

            async def go():
                async with streamablehttp_client(net.url(port, f"/daily/{DAILY_TOKEN}"),
                                                 httpx_client_factory=pinned) as (r, w, _):
                    async with ClientSession(r, w) as s:
                        await s.initialize()
                        res = await s.call_tool("post_daily_summary", {"summary": "slow day"})
                        result["text"] = res.content[0].text
            asyncio.run(go())

        t = threading.Thread(target=post)
        t.start()
        time.sleep(1.5)                                   # the post is now waiting on the slow bridge
        start = time.monotonic()
        for _ in range(3):                                # new TLS connections + requests still served
            assert net.healthy(port, tmp_path / "tls")
        assert time.monotonic() - start < 2, "server froze while the bridge was slow"
        t.join(30)
        assert result.get("text", "").startswith("Posted")
    finally:
        proc.terminate()
        proc.wait(10)
        bridge.shutdown()


# ---- participation percentages -------------------------------------------------

def _msg(sender_id, sender, content="hi"):
    return {"sender_id": sender_id, "sender": sender, "content": content}


def test_participation_is_counted_per_member_and_adds_to_100():
    msgs = ([_msg("1", "خولي")] * 3 + [_msg("2", "شريف")] * 2 + [_msg("3", "حسين")] * 5)
    assert daily.participation(msgs) == "📊\nحسين 50%\nخولي 30%\nشريف 20%"
    thirds = [_msg("1", "A"), _msg("2", "B"), _msg("3", "C")]
    lines = daily.participation(thirds).splitlines()[1:]
    assert sum(int(l.split()[-1].rstrip("%")) for l in lines) == 100


def test_participation_same_name_different_people_kept_apart_and_tiny_share():
    msgs = [_msg("1", "Ahmed")] * 300 + [_msg("2", "Ahmed")] * 99 + [_msg("3", "Rare")]
    assert daily.participation(msgs) == "📊\nAhmed 75%\nAhmed 25%\nRare <1%"


def test_participation_skips_earlier_summaries_and_names_me():
    msgs = [_msg("me", "Me", daily.PREFIX + ", Mon ..."), _msg("me", "Me"), _msg("1", "Mum")]
    assert daily.participation(msgs, "Zahaby") == "📊\nMum 50%\nZahaby 50%"
    assert daily.participation(msgs) == "📊\nMe 50%\nMum 50%"
    assert daily.participation([]) == ""


def test_post_appends_participation_for_the_window(setup, db):
    mcp, bridge, state, now = setup
    news(db, now[0] - timedelta(minutes=30), "one more")    # sender 201000000001 = "Mum"
    out = call(mcp, "post_daily_summary", summary="Dinner is Friday at 8.")
    assert out.startswith("Posted")
    text = bridge.sent[0][1]
    # Last 24 hours: m3 (unknown 201000000099) and the new message from Mum; m1/m2 are older.
    assert text.endswith("Dinner is Friday at 8.\n\n📊\n201000000099 50%\nMum 50%"), text


def test_participation_can_be_switched_off(db, tmp_path, monkeypatch):
    from server import Store
    monkeypatch.setenv("WHATSAPP_AUTOPOST", "true")
    bridge = FakeBridge()
    mcp = daily.build_daily_server(cfg(db, autopost_participation=False), Store(db),
                                   daily.DailyState(tmp_path / "s"), send=bridge)
    assert call(mcp, "post_daily_summary", summary="Dinner.").startswith("Posted")
    assert bridge.sent[0][1].endswith("\n\nDinner.")
    assert "Participation percentages: OFF" in call(mcp, "daily_status")


def test_participation_uses_the_names_from_the_summary():
    msgs = [_msg("1", "Sherif Abdelgawad")] * 3 + [_msg("2", "Nashaat")] + [_msg("me", "Me")]
    names = {"Sherif Abdelgawad": "شريف", "Nashaat": "نشأت", "Me": "زهبي"}
    assert daily.participation(msgs, "Zahaby", names) == "📊\nشريف 60%\nزهبي 20%\nنشأت 20%"


def test_display_names_cannot_inject_text_or_change_counts():
    msgs = [_msg("1", "Sherif")] * 3 + [_msg("2", "Nashaat")]
    bad = {"Sherif": "x\n\nVisit evil.example now", "Nashaat": "N" * 41, "Nobody": "ghost", 5: "five"}
    out = daily.participation(msgs, None, bad)
    # One line per member whatever the model sends; too-long names and unknown keys are ignored.
    assert out == "📊\nx Visit evil.example now 75%\nNashaat 25%"
    assert "\n\n" not in out and "ghost" not in out
    assert daily.participation(msgs, None, "not a dict") == "📊\nSherif 75%\nNashaat 25%"


def test_post_applies_display_names(setup, db):
    mcp, bridge, state, now = setup
    news(db, now[0] - timedelta(minutes=30), "one more")    # from "Mum"
    out = call(mcp, "post_daily_summary", summary="عشاء الجمعة.", display_names={"Mum": "ماما"})
    assert out.startswith("Posted")
    assert bridge.sent[0][1].endswith("\n\n📊\n201000000099 50%\nماما 50%")
