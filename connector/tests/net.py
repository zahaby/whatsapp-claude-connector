"""Test helpers: talk to a running connector over its pinned loopback TLS."""

import http.client
import ssl
import urllib.error
import urllib.request
from pathlib import Path

import tls


def ctx(tls_dir: Path) -> ssl.SSLContext:
    return tls.client_context(tls.files(Path(tls_dir)).cert)


def url(port: int, path: str = "") -> str:
    return f"https://127.0.0.1:{port}{path}"


def request(req, tls_dir, timeout: float = 5) -> tuple[int, bytes]:
    """urlopen with the pinned context; HTTP errors returned, not raised."""
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx(tls_dir)) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def healthy(port: int, tls_dir) -> bool:
    try:
        return request(url(port, "/healthz"), tls_dir, timeout=2)[0] == 200
    except (OSError, ssl.SSLError):
        return False


def connection(port: int, tls_dir, timeout: float = 5) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection("127.0.0.1", port, context=ctx(tls_dir), timeout=timeout)
