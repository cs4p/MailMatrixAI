"""
Fetch up to 500 emails from Fastmail and seed them into the local Dovecot container.

Usage:
    FASTMAIL_USER=you@fastmail.com FASTMAIL_PASSWORD=app-password python seed_dovecot.py

Optional env vars:
    FASTMAIL_COUNT   — number of emails to fetch (default: 500)
    DOVECOT_HOST     — local Dovecot host (default: localhost)
    DOVECOT_PORT     — local Dovecot port (default: 143)
    DOVECOT_USER     — local test user (default: testuser)
    DOVECOT_PASSWORD — local test password (default: testpass)
    SOURCE_FOLDER    — Fastmail folder to pull from (default: INBOX)
"""

import imaplib
import os
import sys
import time


FASTMAIL_HOST = "imap.fastmail.com"
FASTMAIL_PORT = 993

FASTMAIL_USER = os.environ.get("FASTMAIL_USER", "")
FASTMAIL_PASSWORD = os.environ.get("FASTMAIL_PASSWORD", "")

DOVECOT_HOST = os.environ.get("DOVECOT_HOST", "localhost")
DOVECOT_PORT = int(os.environ.get("DOVECOT_PORT", "143"))
DOVECOT_USER = os.environ.get("DOVECOT_USER", "testuser")
DOVECOT_PASSWORD = os.environ.get("DOVECOT_PASSWORD", "testpass")

FETCH_COUNT = int(os.environ.get("FASTMAIL_COUNT", "500"))
SOURCE_FOLDER = os.environ.get("SOURCE_FOLDER", "INBOX")


def _ok(tag: str, result) -> bytes:
    status, data = result
    if status != "OK":
        raise RuntimeError(f"{tag} failed: {status} {data}")
    return data


def connect_fastmail() -> imaplib.IMAP4_SSL:
    if not FASTMAIL_USER or not FASTMAIL_PASSWORD:
        sys.exit("Set FASTMAIL_USER and FASTMAIL_PASSWORD environment variables.")
    print(f"Connecting to Fastmail ({FASTMAIL_HOST}:{FASTMAIL_PORT})...")
    imap = imaplib.IMAP4_SSL(FASTMAIL_HOST, FASTMAIL_PORT)
    _ok("login", imap.login(FASTMAIL_USER, FASTMAIL_PASSWORD))
    print("Fastmail: authenticated.")
    return imap


def connect_dovecot() -> imaplib.IMAP4:
    print(f"Connecting to local Dovecot ({DOVECOT_HOST}:{DOVECOT_PORT})...")
    imap = imaplib.IMAP4(DOVECOT_HOST, DOVECOT_PORT)
    _ok("login", imap.login(DOVECOT_USER, DOVECOT_PASSWORD))
    print("Dovecot: authenticated.")
    return imap


def fetch_message_ids(src: imaplib.IMAP4_SSL, folder: str, count: int) -> list[bytes]:
    _ok(f"select {folder}", src.select(f'"{folder}"', readonly=True))
    data = _ok("search", src.search(None, "ALL"))
    all_ids = data[0].split()
    # Take the most recent `count` messages
    ids = all_ids[-count:] if len(all_ids) > count else all_ids
    print(f"Fastmail: {len(all_ids)} messages in {folder}, fetching {len(ids)}.")
    return ids


def fetch_raw(src: imaplib.IMAP4_SSL, msg_id: bytes) -> bytes | None:
    data = _ok("fetch", src.fetch(msg_id, "(RFC822)"))
    for part in data:
        if isinstance(part, tuple):
            return part[1]
    return None


def ensure_folder(dst: imaplib.IMAP4, folder: str) -> None:
    status, _ = dst.select(f'"{folder}"')
    if status != "OK":
        dst.create(f'"{folder}"')
        dst.subscribe(f'"{folder}"')


def append_message(dst: imaplib.IMAP4, folder: str, raw: bytes) -> None:
    dst.append(f'"{folder}"', None, imaplib.Time2Internaldate(time.time()), raw)


def main() -> None:
    src = connect_fastmail()
    dst = connect_dovecot()

    ensure_folder(dst, "INBOX")

    ids = fetch_message_ids(src, SOURCE_FOLDER, FETCH_COUNT)

    success = 0
    skipped = 0
    for i, msg_id in enumerate(ids, 1):
        raw = fetch_raw(src, msg_id)
        if raw is None:
            skipped += 1
            continue
        try:
            append_message(dst, "INBOX", raw)
            success += 1
        except Exception as exc:
            print(f"  [warn] Could not append message {msg_id.decode()}: {exc}")
            skipped += 1

        if i % 50 == 0:
            print(f"  {i}/{len(ids)} processed...")

    src.logout()
    dst.logout()

    print(f"\nDone. {success} messages seeded, {skipped} skipped.")


if __name__ == "__main__":
    main()
