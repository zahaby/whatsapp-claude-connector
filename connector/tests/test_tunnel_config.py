"""The cloudflared template both setup scripts render."""
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "config" / "cloudflared.yml"


def test_tunnel_uses_http2_not_quic():
    # QUIC (the default) stalled replies bigger than a few KB on a real
    # network that drops large UDP packets; HTTP/2 over TCP does not.
    lines = [l.strip() for l in TEMPLATE.read_text(encoding="utf-8").splitlines()]
    assert "protocol: http2" in lines
