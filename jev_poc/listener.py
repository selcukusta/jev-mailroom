"""Mail listener: poll a local IMAP INBOX and triage unseen messages.

Run with ``uv run python -m jev_poc.listener``::

    --once            one poll pass, then exit
    --interval N      poll every N seconds (default 5)
    --offline         pass live=False to triage (no API calls)
    --user/--password/--host/--port   overrides for the jev_poc defaults

Each triaged result is appended as one JSON object per line to
``<repo>/results.jsonl`` and printed as a compact row.
"""

from __future__ import annotations

import argparse
import imaplib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from jev_poc import IMAP_HOST, IMAP_PASSWORD, IMAP_PORT, IMAP_USER

#: Repository root (this file lives in ``<repo>/jev_poc/``).
REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_PATH = REPO_ROOT / "results.jsonl"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="jev_poc.listener",
        description="Poll a local IMAP INBOX and triage unseen messages.",
    )
    parser.add_argument("--once", action="store_true",
                        help="run a single poll pass and exit")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="seconds between poll passes (default: 5)")
    parser.add_argument("--offline", action="store_true",
                        help="do not call the API (triage live=False)")
    parser.add_argument("--user", default=IMAP_USER)
    parser.add_argument("--password", default=IMAP_PASSWORD)
    parser.add_argument("--host", default=IMAP_HOST)
    parser.add_argument("--port", type=int, default=IMAP_PORT)
    return parser.parse_args(argv)


def _cell(value: Any) -> str:
    """Stringify a result field, using '?' for missing/empty values."""
    if value is None or value == "":
        return "?"
    return str(value)


def _num(value: Any) -> str:
    """Format a confidence value, using '?' when not a number."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:.2f}"
    return "?"


def format_row(result: dict[str, Any], now: datetime | None = None) -> str:
    """Render one compact aligned row from a triage result dict."""
    now = now or datetime.now()
    kind = _cell(result.get("kind"))
    category = _cell(result.get("category"))
    decision = _cell(result.get("decision"))
    subject = _cell(result.get("subject"))
    from_email = _cell(result.get("from_email"))
    conf = (f"{_num(result.get('kind_confidence'))}/"
            f"{_num(result.get('category_confidence'))}")
    kind_category = f"{kind}/{category}"
    return (f"{now:%H:%M:%S}  [{kind_category:<16}] "
            f"{decision:<7} conf {conf}   {subject} <{from_email}>")


def _append_result(result: dict[str, Any]) -> None:
    line = json.dumps(result, ensure_ascii=False)
    with RESULTS_PATH.open("a", encoding="utf-8") as out:
        out.write(line + "\n")


def _extract_rfc822(msg_data: list[Any]) -> bytes | None:
    """Pull the raw message bytes out of an imaplib FETCH response."""
    for part in msg_data:
        if isinstance(part, tuple) and len(part) >= 2:
            return part[1]
    return None


def _connect(args: argparse.Namespace) -> imaplib.IMAP4:
    mail = imaplib.IMAP4(args.host, args.port)
    mail.login(args.user, args.password)
    return mail


def _poll(mail: imaplib.IMAP4, args: argparse.Namespace, offline: bool,
          parse_eml: Any, triage: Any) -> None:
    """Select INBOX, triage every unseen message, mark each as seen."""
    mail.select("INBOX")  # re-select so new mail is noticed
    typ, data = mail.search(None, "UNSEEN")
    if typ != "OK":
        raise imaplib.IMAP4.error(f"SEARCH failed: {typ}")

    for mid in data[0].split():
        typ, msg_data = mail.fetch(mid, "(RFC822)")
        if typ != "OK":
            print(f"listener: FETCH {mid!r} failed: {typ}")
            continue
        raw = _extract_rfc822(msg_data)
        if raw is None:
            print(f"listener: FETCH {mid!r} returned no message")
            continue

        email = parse_eml(raw)
        result = triage(email, live=not offline)
        _append_result(result)
        print(format_row(result))

        try:
            mail.store(mid, "+FLAGS", "\\Seen")
        except imaplib.IMAP4.error as exc:
            print(f"listener: STORE {mid!r} failed: {exc}")


def _run_pass(args: argparse.Namespace, offline: bool,
              parse_eml: Any, triage: Any) -> None:
    mail = _connect(args)
    try:
        _poll(mail, args, offline, parse_eml, triage)
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # Lazy import: triage.py may still be under construction at import time.
    from jev_poc.triage import parse_eml, triage

    if args.once:
        try:
            _run_pass(args, args.offline, parse_eml, triage)
        except KeyboardInterrupt:
            print("\nlistener: stopped.")
            return 130
        except (imaplib.IMAP4.error, OSError) as exc:
            print(f"listener: {exc}")
            return 1
        return 0

    while True:
        try:
            _run_pass(args, args.offline, parse_eml, triage)
        except KeyboardInterrupt:
            print("\nlistener: stopped.")
            return 0
        except (imaplib.IMAP4.error, OSError) as exc:
            print(f"listener: {exc}")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nlistener: stopped.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
