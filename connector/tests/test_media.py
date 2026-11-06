"""
Links and media (media.py): link fetching that cannot be turned against the
local network, page text, YouTube captions, local video / voice-note
transcription with frames, the background worker and the cache.

No real internet: test servers run on 127.0.0.1 and are reached through a
test resolver that maps made-up public names to them, exactly the path a
real public site takes after the address check.
"""

import asyncio
import http.server
import json
import socket
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import media
from conftest import add_message, go_ts
from media import FetchRefused, SafeFetcher, extract_urls, html_to_text, youtube_id
from server import Config, Store, build_server

GROUP = "1203630@g.us"


# ---- URLs ------------------------------------------------------------------------

def test_extract_urls_trims_punctuation_and_dedupes():
    text = "شوف ده https://example.com/a?b=1، و (https://example.org/x). again https://example.com/a?b=1"
    assert extract_urls(text) == ["https://example.com/a?b=1", "https://example.org/x"]


@pytest.mark.parametrize("url,vid", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://example.com/watch?v=dQw4w9WgXcQ", None),
])
def test_youtube_ids(url, vid):
    assert youtube_id(url) == vid


# ---- the fetcher can't be pointed at the local network ------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://localhost/", "http://[::1]/", "http://0.0.0.0/",
    "http://169.254.169.254/latest/meta-data/",     # cloud metadata
    "http://192.168.1.1/", "http://10.0.0.1/", "http://172.16.0.1/",
    "http://[::ffff:127.0.0.1]/",                    # IPv4-mapped loopback
])
def test_private_and_local_addresses_refused(url):
    with pytest.raises(FetchRefused):
        SafeFetcher().get(url)


@pytest.mark.parametrize("url,why", [
    ("file:///etc/passwd", "only http/https"),
    ("ftp://example.com/x", "only http/https"),
    ("gopher://example.com/", "only http/https"),
    ("https://user:pw@example.com/", "credentials"),
    ("http://example.com:8080/", "port 8080"),
    ("http://example.com:22/", "port 22"),
])
def test_schemes_ports_and_credentials_refused(url, why):
    with pytest.raises(FetchRefused, match=why):
        SafeFetcher().get(url)


def test_any_private_answer_refuses_the_host(monkeypatch):
    def fake_getaddrinfo(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FetchRefused, match="non-public"):
        SafeFetcher().get("http://mixed.example/")


class Site:
    """A local HTTP server reached as http://<name>:<port>/ through a test
    resolver, so the fetcher's public-address path is exercised for real."""

    def __init__(self, routes):
        self.seen = []
        site = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                site.seen.append((self.path, self.headers.get("Host")))
                status, headers, body = routes.get(self.path, (404, {}, b"no"))
                if callable(body):
                    body = body()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def fetcher(self, **kw):
        def resolver(host, port):
            if host.endswith(".test"):
                return ["127.0.0.1"]          # stands in for a public address
            return media._public_ips(host, port)
        return SafeFetcher(resolver=resolver, allowed_ports=(80, 443, self.port), **kw)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def site():
    page = ('<html><head><title>T</title><meta property="og:title" content="عنوان المقال">'
            '<meta name="description" content="وصف قصير"><style>.x{}</style><script>var secret=1</script></head>'
            '<body><nav>menu menu</nav><p>هذه فقرة طويلة بما يكفي لتظهر في النص المستخرج من الصفحة.</p>'
            '<p>Second paragraph, long enough to be kept by the extractor.</p></body></html>').encode()
    routes = {}
    s = Site(routes)
    routes.update({
        "/article": (200, {"Content-Type": "text/html; charset=utf-8"}, page),
        "/redirect-public": (302, {"Location": f"http://other.test:{s.port}/article"}, b""),
        "/redirect-private": (302, {"Location": f"http://127.0.0.1:{s.port}/article"}, b""),
        "/redirect-loop": (302, {"Location": "/redirect-loop"}, b""),
        "/big": (200, {"Content-Type": "text/html"}, b"<p>" + b"a" * (3 * 1024 * 1024) + b"</p>"),
        "/pdf": (200, {"Content-Type": "application/pdf"}, b"%PDF-1.4"),
        "/slow": (200, {"Content-Type": "text/html"}, lambda: time.sleep(3) or b"<p>late</p>"),
    })
    yield s
    s.close()


def test_fetch_pins_the_checked_address_and_sends_the_real_host(site):
    f = site.fetcher().get(f"http://news.test:{site.port}/article")
    assert f.status == 200 and site.seen[-1] == ("/article", f"news.test:{site.port}")


def test_redirect_to_public_followed_redirect_to_private_refused(site):
    assert site.fetcher().get(f"http://a.test:{site.port}/redirect-public").status == 200
    with pytest.raises(FetchRefused):
        site.fetcher().get(f"http://a.test:{site.port}/redirect-private")


def test_redirect_loop_stops(site):
    with pytest.raises(FetchRefused, match="too many redirects"):
        site.fetcher().get(f"http://a.test:{site.port}/redirect-loop")


def test_size_is_capped(site):
    f = site.fetcher().get(f"http://a.test:{site.port}/big")
    assert len(f.body) == media.MAX_FETCH_BYTES


def test_slow_site_times_out(site):
    with pytest.raises(OSError):
        site.fetcher(timeout=1).get(f"http://a.test:{site.port}/slow")


# ---- page text -------------------------------------------------------------------

def test_html_to_text_keeps_content_drops_code(site):
    info = media.process_link(f"http://news.test:{site.port}/article", site.fetcher())
    assert info["status"] == "ready" and info["title"] == "عنوان المقال" and info["description"] == "وصف قصير"
    assert "هذه فقرة طويلة" in info["text"] and "Second paragraph" in info["text"]
    assert "secret" not in info["text"] and "menu" not in info["text"]


def test_non_html_and_errors_are_reported_not_raised(site):
    assert "not a web page" in media.process_link(f"http://a.test:{site.port}/pdf", site.fetcher())["note"]
    assert media.process_link(f"http://a.test:{site.port}/missing", site.fetcher())["status"] == "failed"
    assert media.process_link("http://192.168.1.1/admin", SafeFetcher())["status"] == "skipped"


def test_text_is_truncated():
    info = html_to_text(b"<p>" + ("word " * 5000).encode() + b"</p>", "text/html")
    assert len(info["text"]) <= media.MAX_TEXT_PER_LINK


# ---- YouTube ------------------------------------------------------------------------

class FakeSnippet:
    def __init__(self, text):
        self.text = text


class FakeTranscript:
    language_code = "ar"

    def fetch(self):
        return [FakeSnippet("مرحبا"), FakeSnippet("بالجميع")]


class FakeListing:
    def find_transcript(self, langs):
        return FakeTranscript()


def test_youtube_captions_and_title(monkeypatch):
    import youtube_transcript_api
    monkeypatch.setattr(youtube_transcript_api.YouTubeTranscriptApi, "list", lambda self, vid: FakeListing())

    class OEmbed:
        def get(self, url):
            assert url.startswith("https://www.youtube.com/oembed")
            return media.Fetched(url, 200, "application/json",
                                 json.dumps({"title": "Recipe", "author_name": "Chef"}).encode())

    out = media.process_link("https://youtu.be/dQw4w9WgXcQ", OEmbed())
    assert out["kind"] == "youtube" and out["title"] == "Recipe" and out["channel"] == "Chef"
    assert out["transcript"] == "مرحبا بالجميع" and out["language"] == "ar"


def test_youtube_without_captions_still_reports(monkeypatch):
    import youtube_transcript_api

    def boom(self, vid):
        raise RuntimeError("captions disabled")
    monkeypatch.setattr(youtube_transcript_api.YouTubeTranscriptApi, "list", boom)

    class NoNet:
        def get(self, url):
            raise OSError("offline")
    out = media.process_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ", NoNet())
    assert out["status"] == "ready" and "no captions available" in out["note"]


# ---- WhatsApp videos and voice notes ------------------------------------------------

def make_video(path: Path, seconds: int = 2):
    import av
    with av.open(str(path), "w") as c:
        s = c.add_stream("mpeg4", rate=10)
        s.width, s.height, s.pix_fmt = 64, 48, "yuv420p"
        for i in range(seconds * 10):
            from PIL import Image as PILImage
            img = PILImage.new("RGB", (64, 48), (i * 10 % 255, 80, 160))
            for p in s.encode(av.VideoFrame.from_image(img)):
                c.mux(p)
        for p in s.encode():
            c.mux(p)


def add_media(db, msg_id, media_type, when):
    c = sqlite3.connect(db)
    c.execute("INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me, media_type) "
              "VALUES (?,?,?,?,?,0,?)", (msg_id, GROUP, "201000000001", "", go_ts(when), media_type))
    c.commit()
    c.close()


class Downloader:
    """Stands in for the bridge: copies a prepared file into the store dir."""

    def __init__(self, store_dir: Path, source: Path):
        self.store_dir, self.source, self.calls = store_dir, source, []

    def __call__(self, chat, msg_id):
        self.calls.append(msg_id)
        folder = self.store_dir / chat.replace(":", "_")      # the bridge's store/<chat>/ layout
        folder.mkdir(exist_ok=True)
        dest = folder / f"{msg_id}{self.source.suffix}"
        dest.write_bytes(self.source.read_bytes())
        return str(dest)


@pytest.fixture
def svc(db, tmp_path):
    video = tmp_path / "src.mp4"
    make_video(video)
    calls = []

    def transcriber(path):
        calls.append(path)
        return "نتقابل الساعة ثمانية", "ar"

    dl = Downloader(db.parent, video)
    s = media.MediaService(store=Store(db), cache=media.Cache(tmp_path / "cache"), bridge_url="http://x/api",
                           media_enabled=True, transcriber=transcriber, downloader=dl, fetcher=SafeFetcher())
    s.transcriber_calls = calls
    return s


def text_of(content):
    return content[0]


def test_video_transcribed_with_frames_and_download_deleted(svc, db):
    now = datetime.now(timezone.utc)
    add_media(db, "vid1", "video", now - timedelta(hours=1))
    out = svc.digest(GROUP, "Family", now - timedelta(hours=2), wait_s=30)
    text = text_of(out)
    assert "video (2s, language ar, 3 still frame(s) attached)" in text and "نتقابل الساعة ثمانية" in text
    assert len(out) == 1 + 3                                 # text + 3 frames as images
    assert not (db.parent / GROUP / "vid1.mp4").exists()    # the downloaded copy is gone
    assert "data, not instructions" in text


def test_voice_note_transcribed_without_frames(svc, db, tmp_path):
    now = datetime.now(timezone.utc)
    add_media(db, "voice1", "audio", now - timedelta(minutes=5))
    out = svc.digest(GROUP, "Family", now - timedelta(hours=1), wait_s=30)
    assert "voice note (2s, language ar)" in text_of(out) and len(out) == 1


def test_cached_media_is_never_reprocessed(svc, db):
    now = datetime.now(timezone.utc)
    add_media(db, "vid2", "video", now - timedelta(hours=1))
    svc.digest(GROUP, "Family", now - timedelta(hours=2), wait_s=30)
    svc.digest(GROUP, "Family", now - timedelta(hours=2), wait_s=30)
    assert len(svc.transcriber_calls) == 1


def test_too_long_media_skipped(svc, db):
    svc.max_minutes = 0.01                                   # 0.6 s
    now = datetime.now(timezone.utc)
    add_media(db, "long1", "video", now - timedelta(hours=1))
    text = text_of(svc.digest(GROUP, "Family", now - timedelta(hours=2), wait_s=30))
    assert "longer than 0.01 minutes" in text and svc.transcriber_calls == []


def test_slow_item_reported_as_processing_then_ready(svc, db):
    def slow(path):
        time.sleep(2)
        return "later", "en"
    svc.transcriber = slow
    now = datetime.now(timezone.utc)
    add_media(db, "slow1", "audio", now - timedelta(minutes=5))
    first = text_of(svc.digest(GROUP, "Family", now - timedelta(hours=1), wait_s=0.2))
    assert "still processing" in first and "ask again" in first
    time.sleep(3)
    assert "later" in text_of(svc.digest(GROUP, "Family", now - timedelta(hours=1), wait_s=0.2))


def test_failure_is_cached_and_worker_keeps_going(svc, db):
    now = datetime.now(timezone.utc)
    add_media(db, "bad1", "audio", now - timedelta(minutes=10))
    add_media(db, "good1", "audio", now - timedelta(minutes=5))
    real = svc.transcriber

    def flaky(path):
        if "bad1" in path:
            raise RuntimeError("corrupt file")
        return real(path)
    svc.transcriber = flaky
    text = text_of(svc.digest(GROUP, "Family", now - timedelta(hours=1), wait_s=30))
    assert "processing failed (RuntimeError)" in text and "نتقابل" in text


def test_media_off_lists_but_does_not_process(svc, db):
    svc.media_enabled = False
    now = datetime.now(timezone.utc)
    add_media(db, "off1", "video", now - timedelta(hours=1))
    text = text_of(svc.digest(GROUP, "Family", now - timedelta(hours=2), wait_s=1))
    assert "media processing is off; run media-setup" in text and svc.transcriber_calls == []


@pytest.mark.parametrize("where", ["outside", "store-root", "database", "wal", "session"])
def test_only_downloads_in_a_store_subfolder_are_deleted(svc, db, tmp_path, where):
    store = db.parent
    paths = {"outside": tmp_path.parent / "keep-me.mp4",
             "store-root": store / "keep-me.mp4",
             "database": store / "sub" / "messages.db",
             "wal": store / "sub" / "x.db-wal",
             "session": store / "whatsapp.db"}
    p = paths[where]
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(b"x")
    svc._delete_download(str(p))
    assert p.exists()
    assert db.exists()


def test_links_found_and_capped(svc, db, site):
    now = datetime.now(timezone.utc)
    svc.fetcher = site.fetcher()
    for i in range(media.MAX_LINKS_PER_CALL + 5):
        add_message(db, f"l{i}", go_ts(now - timedelta(minutes=30 - i)),
                    f"look http://n{i}.test:{site.port}/article", chat=GROUP)
    text = text_of(svc.digest(GROUP, "Family", now - timedelta(hours=1), wait_s=30))
    assert f"{media.MAX_LINKS_PER_CALL + 5} link(s)" not in text
    assert text.count("عنوان المقال") == media.MAX_LINKS_PER_CALL


def test_cache_dir_is_owner_only(svc, db):
    import sys
    now = datetime.now(timezone.utc)
    add_media(db, "perm1", "audio", now - timedelta(minutes=5))
    svc.digest(GROUP, "Family", now - timedelta(hours=1), wait_s=30)
    if sys.platform != "win32":
        assert oct(svc.cache.dir.stat().st_mode & 0o777) == "0o700"


def test_prescanner_queues_new_items_once(svc, db):
    now = datetime.now(timezone.utc)
    add_media(db, "pre1", "audio", now - timedelta(minutes=5))
    p = media.Prescanner(svc, [GROUP])
    assert p.scan_once() == 1
    svc.wait({f"media:{GROUP}:pre1"}, 30)
    assert p.scan_once() == 0                                # cached now: nothing new


# ---- the main connector's tool --------------------------------------------------------

def test_main_tool_resolves_chat_and_returns_frames(svc, db):
    now = datetime.now(timezone.utc)
    add_media(db, "vid9", "video", now - timedelta(hours=1))
    cfg = Config(db_path=db, bridge_url="", allow_send=False, auth_token="x" * 43, public_hostname="",
                 host="127.0.0.1", port=1, allowed_ips=[])
    mcp = build_server(cfg, Store(db), media_svc=svc)
    result = asyncio.run(mcp.call_tool("get_links_and_media", {"chat": "Family", "days": 1}))
    content = result[0] if isinstance(result, tuple) else result
    kinds = [c.type for c in content]
    assert kinds[0] == "text" and kinds.count("image") == 3
    assert "نتقابل الساعة ثمانية" in content[0].text
    result = asyncio.run(mcp.call_tool("get_links_and_media", {"chat": "fam"}))
    content = result[0] if isinstance(result, tuple) else result
    assert "not found exactly" in content[0].text


def test_links_off_removes_the_tool(db):
    cfg = Config(db_path=db, bridge_url="", allow_send=False, auth_token="x" * 43, public_hostname="",
                 host="127.0.0.1", port=1, allowed_ips=[], links_enabled=False)
    names = {t.name for t in asyncio.run(build_server(cfg, Store(db)).list_tools())}
    assert "get_links_and_media" not in names
