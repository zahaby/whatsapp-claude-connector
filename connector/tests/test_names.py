"""
Names for people WhatsApp identifies by an anonymous LID instead of a phone
number (reported on a real PC: group summaries showed long numbers).

The fixture adds the WhatsApp library's session DB (whatsapp.db) next to
messages.db, with the two tables the connector reads, in the library's own
schema (whatsmeow upgrades/00-latest-schema.sql): whatsmeow_contacts keyed by
full JID, and whatsmeow_lid_map with bare user parts.
"""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import add_message, go_ts
from server import Config, Store, build_server

GROUP = "1203630@g.us"
PN_SAVED, LID_SAVED = "201000000201", "90000000000201"       # saved in your phone, under the phone JID
PN_PUSH, LID_PUSH = "201000000202", "90000000000202"         # only their own WhatsApp name, under the LID
PN_MAPONLY, LID_MAPONLY = "201000000203", "90000000000203"   # mapping known, no name anywhere
LID_UNKNOWN = "90000000000299"                               # nothing known at all
LID_DIRECT = "90000000000204"                                # a direct chat that only exists as a LID


def make_session_db(path: Path, with_lid_table: bool = True):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE whatsmeow_contacts (our_jid TEXT, their_jid TEXT, first_name TEXT, full_name TEXT,
            push_name TEXT, business_name TEXT, redacted_phone TEXT, PRIMARY KEY (our_jid, their_jid));
    """)
    rows = [
        (f"{PN_SAVED}@s.whatsapp.net", "Sam", "Sam Saved", "sammy-self", None),
        (f"{LID_PUSH}@lid", None, None, "Pat (own name)", None),
        (f"{LID_DIRECT}@lid", "Dina", "Dina Direct", None, None),
        ("201000000001@s.whatsapp.net", "Mum", "Mum Contact", "mum-self", None),  # also has a chat name
    ]
    c.executemany("INSERT INTO whatsmeow_contacts VALUES ('me@s.whatsapp.net', ?, ?, ?, ?, ?, NULL)", rows)
    if with_lid_table:
        c.execute("CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT UNIQUE NOT NULL)")
        c.executemany("INSERT INTO whatsmeow_lid_map VALUES (?, ?)",
                      [(LID_SAVED, PN_SAVED), (LID_PUSH, PN_PUSH), (LID_MAPONLY, PN_MAPONLY)])
    c.commit()
    c.close()


@pytest.fixture
def lid_db(db):
    now = datetime.now(timezone.utc)
    for i, (sender, text) in enumerate([(LID_SAVED, "saved-by-lid"), (LID_PUSH, "push-only"),
                                        (LID_MAPONLY, "map-only"), (LID_UNKNOWN, "nobody-knows"),
                                        (PN_SAVED, "saved-by-phone")]):
        add_message(db, f"lid{i}", go_ts(now - timedelta(minutes=30 - i)), text, chat=GROUP, sender=sender)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO chats VALUES (?,?,?)", (f"{LID_DIRECT}@lid", LID_DIRECT, go_ts(now)))
    c.commit()
    c.close()
    make_session_db(db.with_name("whatsapp.db"))
    return db


def senders(db) -> dict:
    msgs, _ = Store(db).messages(chat_jid=GROUP, limit=50)
    return {m["content"]: m["sender"] for m in msgs}


def test_lid_sender_gets_saved_contact_name_via_mapping(lid_db):
    assert senders(lid_db)["saved-by-lid"] == "Sam Saved"


def test_phone_sender_gets_saved_contact_name(lid_db):
    assert senders(lid_db)["saved-by-phone"] == "Sam Saved"


def test_lid_sender_falls_back_to_their_own_whatsapp_name(lid_db):
    assert senders(lid_db)["push-only"] == "Pat (own name)"


def test_lid_sender_with_mapping_only_shows_phone_number(lid_db):
    assert senders(lid_db)["map-only"] == "+" + PN_MAPONLY


def test_unknown_lid_stays_as_is(lid_db):
    assert senders(lid_db)["nobody-knows"] == LID_UNKNOWN


def test_saved_name_beats_chat_name_and_push_name(lid_db):
    assert senders(lid_db)["Dinner Friday at 8?"] == "Mum Contact"


def test_lid_direct_chat_is_named_and_findable(lid_db):
    chats = {c["jid"]: c["name"] for c in Store(lid_db).chats()}
    assert chats[f"{LID_DIRECT}@lid"] == "Dina Direct"
    match, _ = Store(lid_db).resolve_chat("Dina Direct")
    assert match and match["jid"] == f"{LID_DIRECT}@lid"


def test_transcript_shows_names_not_numbers(lid_db):
    cfg = Config(db_path=lid_db, bridge_url="", allow_send=False, auth_token="x" * 43, public_hostname="",
                 host="127.0.0.1", port=1, allowed_ips=[])
    import asyncio
    result = asyncio.run(build_server(cfg).call_tool("get_chat_transcript", {"chat": "Family"}))
    text = (result[0] if isinstance(result, tuple) else result)[0].text
    assert "Sam Saved: saved-by-lid" in text and "Pat (own name): push-only" in text
    assert LID_SAVED not in text and LID_PUSH not in text


def test_session_db_is_only_read(lid_db):
    session = lid_db.with_name("whatsapp.db")
    before = hashlib.sha256(session.read_bytes()).hexdigest()
    senders(lid_db)
    Store(lid_db).chats()
    assert hashlib.sha256(session.read_bytes()).hexdigest() == before


def test_no_session_db_keeps_old_behaviour(db):
    assert not db.with_name("whatsapp.db").exists()
    assert senders(db)["Dinner Friday at 8?"] == "Mum"          # from the chat list, as before


def test_older_library_without_lid_table_still_uses_contacts(db):
    make_session_db(db.with_name("whatsapp.db"), with_lid_table=False)
    assert senders(db)["Dinner Friday at 8?"] == "Mum Contact"


def test_unreadable_session_db_never_breaks_summaries(db):
    db.with_name("whatsapp.db").write_bytes(b"not a database at all")
    assert senders(db)["Dinner Friday at 8?"] == "Mum"


def test_my_name_from_session_db(db):
    from server import Store
    session = db.with_name("whatsapp.db")
    assert Store(db).my_name() is None                     # no session DB
    make_session_db(session)
    assert Store(db).my_name() is None                     # no device table (older library)
    c = sqlite3.connect(session)
    c.execute("CREATE TABLE whatsmeow_device (jid TEXT PRIMARY KEY, push_name TEXT)")
    c.execute("INSERT INTO whatsmeow_device VALUES ('me@s.whatsapp.net', ' زهبي ')")
    c.commit()
    c.close()
    assert Store(db).my_name() == "زهبي"
