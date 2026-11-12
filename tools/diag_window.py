"""Diagnose why a transcript reply does not arrive. Prints NO message text:
only times, lengths and which unusual characters a message contains.

  connector\.venv\Scripts\python.exe tools\diag_window.py [hours]
"""
import json
import os
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "connector"))
os.environ.setdefault("WA_CONFIG", str(ROOT / "connector" / ".env"))
from server import Config, Store  # noqa: E402


def flags(s: str) -> list[str]:
    out = set()
    for ch in s:
        o = ord(ch)
        if 0xD800 <= o <= 0xDFFF:
            out.add("SURROGATE")
        elif o < 32 and ch not in "\n\t\r":
            out.add(f"CTRL-{o:02x}")
        elif 0x7F <= o <= 0x9F:
            out.add(f"C1-{o:02x}")
        elif o in (0xFFFE, 0xFFFF) or 0xFDD0 <= o <= 0xFDEF:
            out.add("NONCHAR")
        elif o in (0x2028, 0x2029):
            out.add("LINE-SEP")
        elif unicodedata.category(ch) == "Co":
            out.add("PRIVATE-USE")
    return sorted(out)


def main():
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    cfg = Config.from_env()
    store = Store(cfg.db_path)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    msgs, unreadable = store.messages(chat_jid=cfg.autopost_chat, since=since, limit=1500)
    print(f"{len(msgs)} message(s) in the last {hours} h; unreadable timestamps: {unreadable}")
    total = 0
    for m in msgs:
        c = m["content"]
        total += len(c.encode("utf-8", "surrogatepass"))
        f = flags(c) + [f"name:{x}" for x in flags(m["sender"])]
        print(f"{m['timestamp'].astimezone():%H:%M} len={len(c):6d} media={m['media_type'] or '-':8s} "
              f"{'  '.join(f) if f else 'ok'}")
    print(f"total text bytes: {total}")
    # The same serialisation the server uses for its reply.
    from mcp.types import CallToolResult, TextContent
    text = "\n".join(f"{m['sender']}: {m['content']}" for m in msgs)
    try:
        body = CallToolResult(content=[TextContent(type="text", text=text)]).model_dump_json()
        json.loads(body)
        print(f"reply serialises fine: {len(body.encode('utf-8', 'surrogatepass'))} bytes")
    except Exception as e:  # noqa: BLE001
        print(f"REPLY SERIALISATION FAILS: {type(e).__name__}: {str(e)[:200]}")


if __name__ == "__main__":
    main()
