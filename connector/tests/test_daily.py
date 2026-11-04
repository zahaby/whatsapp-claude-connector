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
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def __call__(self, jid, text):
        self.sent.append((jid, text))
        return (True, "sent") if self.ok else (False, "bridge not reachable")


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
    assert text.startswith(daily.PREFIX) and text.endswith("Dinner is Friday at 8.")
    assert state.last_post() is not None


def test_second_post_within_interval_refused_even_after_restart(setup, db, tmp_path):
    mcp, bridge, state, now = setup
    assert call(mcp, "post_daily_summary", summary="one").startswith("Posted")
    assert "already posted" in call(mcp, "post_daily_summary", summary="two")
    # A restarted server (new instance, same state file) still refuses.
    from server import Store
    mcp2 = daily.build_daily_server(cfg(db), Store(db), daily.DailyState(tmp_path / "state"),
                                    send=bridge, clock=lambda: now[0])
    assert "already posted" in call(mcp2, "post_daily_summary", summary="three")
    assert len(bridge.sent) == 1


def test_post_allowed_again_after_interval(setup, db):
    from conftest import add_message, go_ts
    mcp, bridge, state, now = setup
    assert call(mcp, "post_daily_summary", summary="day one").startswith("Posted")
    now[0] += timedelta(hours=20, minutes=1)
    add_message(db, "next-day", go_ts(now[0] - timedelta(hours=1)), "new news")  # group active again
    assert call(mcp, "post_daily_summary", summary="day two").startswith("Posted")
    assert [t.endswith(x) for (_, t), x in zip(bridge.sent, ["day one", "day two"])] == [True, True]


def test_just_before_interval_still_refused(setup):
    mcp, bridge, state, now = setup
    assert call(mcp, "post_daily_summary", summary="day one").startswith("Posted")
    now[0] += timedelta(hours=19, minutes=59)
    assert "already posted" in call(mcp, "post_daily_summary", summary="too soon")
    assert len(bridge.sent) == 1


def test_quiet_group_is_not_posted(db, tmp_path, monkeypatch):
    monkeypatch.setenv("WHATSAPP_AUTOPOST", "true")
    from server import Store
    bridge = FakeBridge()
    later = datetime.now(timezone.utc) + timedelta(days=5)   # nothing in the last 24h then
    mcp = daily.build_daily_server(cfg(db), Store(db), daily.DailyState(tmp_path / "s"), send=bridge,
                                   clock=lambda: later)
    assert "no messages in the last 24 hours" in call(mcp, "post_daily_summary", summary="x")
    assert bridge.sent == []


@pytest.mark.parametrize("summary,reason", [("", "empty"), ("   \n ", "empty"), ("x" * 3001, "limit is 3000")])
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
    bridge = FakeBridge(ok=False)
    state = daily.DailyState(tmp_path / "s")
    mcp = daily.build_daily_server(cfg(db), Store(db), state, send=bridge)
    assert "NOT POSTED" in call(mcp, "post_daily_summary", summary="hello")
    assert state.last_post() is None
    bridge.ok = True
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
        "get_group_transcript", "post_daily_summary", "daily_status"}
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
