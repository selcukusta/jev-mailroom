"""A real local IMAP server backed by a Maildir, plus fixture import.

`uv run python -m jev_poc.imap_stub` stands up a genuine pymap IMAP server on
``IMAP_HOST:IMAP_PORT`` serving ``<repo>/mailbox``. Dropping ``*.eml`` files into
``<repo>/fixtures`` (or calling :func:`import_fixtures`) makes mail "arrive".

The server is real pymap (the same package the listener talks to), configured
programmatically with a single Maildir account. Account records are written to
``<repo>/mailbox/pymap-etc-{passwd,shadow}`` using pymap's own file readers so
the on-disk format always matches what the server expects.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from email import message_from_bytes
from pathlib import Path

from jev_poc import IMAP_HOST, IMAP_PASSWORD, IMAP_PORT, IMAP_USER

#: Repository root (this file lives in ``<repo>/jev_poc/``).
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAILDIR = REPO_ROOT / "mailbox"
DEFAULT_FIXTURES = REPO_ROOT / "fixtures"

_MAILDIR_SUBDIRS = ("cur", "new", "tmp")


def _message_id(raw: bytes) -> str | None:
    """Return the normalized ``Message-ID`` header, or None if absent."""
    try:
        msg = message_from_bytes(raw)
    except Exception:
        return None
    value = msg.get("Message-ID")
    return value.strip() if value else None


def _existing_message_ids(maildir: Path) -> set[str]:
    """Scan every message already in the Maildir for its ``Message-ID``."""
    ids: set[str] = set()
    for sub in ("new", "cur"):
        directory = maildir / sub
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            try:
                raw = entry.read_bytes()
            except OSError:
                continue
            mid = _message_id(raw)
            if mid:
                ids.add(mid)
    return ids


def _unique_target(new_dir: Path, seq: int) -> Path:
    """Return a free, conventionally-shaped Maildir filename in ``new/``."""
    host = socket.gethostname().replace("/", "_").replace(":", "_")
    host = host or "localhost"
    n = seq
    while True:
        candidate = new_dir / f"{time.time_ns()}.{os.getpid()}_{n}.{host}"
        if not candidate.exists():
            return candidate
        n += 1


def import_fixtures(src: Path, maildir: Path) -> int:
    """Import every ``*.eml`` in *src* into ``<maildir>/new/``.

    Returns the number of newly imported messages. Idempotent for messages that
    carry a ``Message-ID``: a message already present in the Maildir (new or
    cur) is skipped. Missing/empty *src* is not an error and returns 0.
    """
    src = Path(src)
    maildir = Path(maildir)
    if not src.is_dir():
        return 0
    emls = sorted(src.glob("*.eml"))
    if not emls:
        return 0

    new_dir = maildir / "new"
    new_dir.mkdir(parents=True, exist_ok=True)
    seen = _existing_message_ids(maildir)

    imported = 0
    for seq, eml in enumerate(emls):
        try:
            raw = eml.read_bytes()
        except OSError:
            continue
        mid = _message_id(raw)
        if mid and mid in seen:
            continue
        _unique_target(new_dir, seq).write_bytes(raw)
        if mid:
            seen.add(mid)
        imported += 1
    return imported


def _write_account(maildir: Path) -> None:
    """Write the single Maildir account the server authenticates against."""
    # pymap's own file readers guarantee the passwd/shadow layout matches.
    from pymap.backend.maildir.users import PasswordsFile, UsersFile
    from pymap.config import IMAPConfig
    from pysasl.prep import saslprep

    base_dir = str(maildir)
    # home_dir "." means the account's maildir is base_dir itself.
    users = UsersFile(base_dir)
    users.set(UsersFile.build_record(IMAP_USER, "."))
    users.file_write()

    digest = IMAPConfig._get_hash_context(None).hash(saslprep(IMAP_PASSWORD))
    passwords = PasswordsFile(base_dir)
    passwords.set(PasswordsFile.build_record(IMAP_USER, digest))
    passwords.file_write()


def _count_messages(maildir: Path) -> int:
    total = 0
    for sub in ("new", "cur"):
        directory = maildir / sub
        if directory.is_dir():
            total += sum(1 for entry in directory.iterdir() if entry.is_file())
    return total


def main() -> None:
    """CLI entrypoint: import fixtures, then serve IMAP until Ctrl-C."""
    maildir = DEFAULT_MAILDIR
    for sub in _MAILDIR_SUBDIRS:
        (maildir / sub).mkdir(parents=True, exist_ok=True)

    imported = import_fixtures(DEFAULT_FIXTURES, maildir)
    print(f"Imported {imported} new fixture(s); "
          f"{_count_messages(maildir)} message(s) in Maildir.")
    _write_account(maildir)

    print(f"IMAP stub listening on {IMAP_HOST}:{IMAP_PORT} as {IMAP_USER}")
    sys.stdout.flush()

    # Drive the real pymap server. `--no-tls` keeps it plaintext on localhost.
    sys.argv = [
        "pymap",
        "--host", IMAP_HOST,
        "--port", str(IMAP_PORT),
        "--no-tls",
        "maildir", str(maildir),
    ]
    try:
        from pymap.main import main as pymap_main
        pymap_main()
    except KeyboardInterrupt:
        print("\nIMAP stub stopped.")


if __name__ == "__main__":
    main()
