"""mailroom - invoice mail triage PoC on TypeSafe System One (Jev).

A fake IMAP mailbox feeds a listener, which asks Jev one batched call per email and
turns the typed answers into a routing decision in plain Python.
"""

import os
from pathlib import Path

KEY_FILE = Path.home() / ".typesafe.key"


def load_api_key() -> str:
    """Return the TypeSafe API key from the env, else from ~/.typesafe.key.

    Never log or print the result.
    """
    if key := os.environ.get("TYPESAFE_API_KEY"):
        return key.strip()
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    raise RuntimeError(
        f"No API key found. Set TYPESAFE_API_KEY or create {KEY_FILE}"
    )


# Mailbox each module talks to.
IMAP_HOST = "127.0.0.1"
IMAP_PORT = 1143
IMAP_USER = "poc@local"
IMAP_PASSWORD = "poc"
