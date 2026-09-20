"""One-email triage: parse an .eml, ask Jev once, decide in plain Python.

Pipeline: `parse_eml` -> `build_state` -> one `system_one` call with every question
from `questions.py` -> `decide`, which applies all thresholds/evidence rules and
returns a flat, JSON-serializable result. Jev never does arithmetic, dates, or
counting; Python does.
"""

from __future__ import annotations

import email
import html as _html
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Any

from jev_poc import load_api_key
from jev_poc.questions import (
    CATEGORIES,
    CATEGORY_CORROBORATION_MIN,
    CATEGORY_DISAGREEMENT_MAX,
    CATEGORY_NOULS,
    CONFIDENT_CATEGORY_CONFIDENCE,
    MODEL,
    REVIEW_KIND_CONFIDENCE,
    questions,
)

# Cap on body chars: docs note accuracy falls as the state grows with irrelevant
# content, so the body stays hygiene, not raw MIME.
MAX_BODY_CHARS = 12000


@dataclass(frozen=True)
class EmailRecord:
    message_id: str
    subject: str
    from_display: str
    from_email: str
    date: str
    body: str


_QUOTE_PATTERNS = [
    re.compile(r"^\s*On .+ wrote:\s*$", re.MULTILINE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^\s*>{1,}\s", re.MULTILINE),
    re.compile(r"^\s*_{10,}\s*$", re.MULTILINE),
]
_FROM_REPLY_PATTERN = re.compile(r"^\s*From:\s.+$", re.MULTILINE)
_SIGNATURE_PATTERN = re.compile(r"^\s*--\s*$", re.MULTILINE)


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping <style>/<script> bodies."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.chunks.append(data)


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _decode_part(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, UnicodeError):
            return payload.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _strip_html(text: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return _html.unescape(text)
    return _html.unescape("".join(parser.chunks))


def _extract_body(msg: Message) -> str:
    plain: str | None = None
    html_part: str | None = None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        if disposition.startswith("attachment"):
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = _decode_part(part)
        elif ctype == "text/html" and html_part is None:
            html_part = _decode_part(part)
    if plain is not None:
        return plain
    if html_part is not None:
        return _strip_html(html_part)
    return ""


def _first_quote_index(text: str) -> int:
    """Return the offset of the earliest quoted-reply marker, or len(text)."""
    earliest = len(text)
    for pattern in _QUOTE_PATTERNS:
        match = pattern.search(text)
        if match:
            earliest = min(earliest, match.start())
    # A bare "From:" line only signals a prior thread once the body has started.
    blank = text.find("\n\n")
    if blank != -1:
        match = _FROM_REPLY_PATTERN.search(text, blank)
        if match:
            earliest = min(earliest, match.start())
    return earliest


def _strip_history(text: str) -> str:
    """Drop quoted prior thread, so the same content is not judged twice."""
    text = text[: _first_quote_index(text)]
    # Signatures only count near the end; mid-body "--" lines are usually content.
    for match in _SIGNATURE_PATTERN.finditer(text):
        if match.start() >= len(text) * 2 / 3:
            text = text[: match.start()]
            break
    return text


def _normalize(text: str) -> str:
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_eml(raw: bytes) -> EmailRecord:
    """Parse raw .eml bytes into a clean record; never raises."""
    try:
        msg = email.message_from_bytes(raw)
    except Exception:
        return EmailRecord("", "", "", "", "", "")

    try:
        message_id = _decode_header(msg.get("Message-ID")).strip()
    except Exception:
        message_id = ""
    try:
        subject = re.sub(r"\s+", " ", _decode_header(msg.get("Subject"))).strip()
    except Exception:
        subject = ""
    try:
        display, address = parseaddr(_decode_header(msg.get("From")))
    except Exception:
        display, address = "", ""
    from_display = display or address
    try:
        date = msg.get("Date") or ""
    except Exception:
        date = ""

    try:
        body = _normalize(_strip_history(_extract_body(msg)))
    except Exception:
        body = ""
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[...truncated]"

    return EmailRecord(message_id, subject, from_display, address, date, body)


def build_state(email: EmailRecord) -> dict:
    """State object for one API call; questions address values via backticked paths."""
    _, _, domain = email.from_email.partition("@")
    return {
        "email": {
            "subject": email.subject,
            "from": {
                "display_name": email.from_display,
                "email": email.from_email,
                "domain": domain.lower() if domain else "",
            },
            "date": email.date,
            "body": email.body,
        }
    }


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _summarize_answers(answers: dict) -> dict:
    """Collapse typed answers into plain JSON types: noul->float, choice->str, score->float."""
    summary: dict = {}
    for qid, answer in answers.items():
        if hasattr(answer, "noul"):
            summary[qid] = float(answer.noul)
        elif hasattr(answer, "choice"):
            summary[qid] = str(answer.choice)
        elif hasattr(answer, "score"):
            summary[qid] = float(answer.score)
    return summary


def decide(
    answers: dict,
    model: str,
    request_id: str | None,
    usage: Any,
    email: EmailRecord | None = None,
) -> dict:
    """Turn typed answers into the flat triage result. Pure: no network, no I/O."""
    review_reasons: list[str] = []

    # --- kind: the invoice/not-invoice call ---------------------------------
    kind_choice = str(answers["kind"].choice)
    kind_conf = float(answers["kind"].confidence)
    is_bill = kind_choice == "invoice"
    # A receipt is about a bill too, so its category/payment questions are real; a
    # marketing email is not, so theirs are speculative and must not route it.
    bill_related = kind_choice in ("invoice", "payment_confirmation")
    if kind_conf < REVIEW_KIND_CONFIDENCE:
        review_reasons.append("kind label below review floor")

    # --- category: confident label, else rescue or climb one level up --------
    cat_choice = str(answers["category"].choice)
    cat_conf = float(answers["category"].confidence)
    if cat_conf >= CONFIDENT_CATEGORY_CONFIDENCE and cat_choice in CATEGORIES:
        category, category_level = cat_choice, "category"
    else:
        # Below the floor is not the same as wrong: the confidence summary shifts with
        # the option count, so an independent question is allowed to carry the label.
        noul_id = CATEGORY_NOULS.get(cat_choice)
        corroborated = (
            noul_id is not None
            and noul_id in answers
            and float(answers[noul_id].noul) >= CATEGORY_CORROBORATION_MIN
        )
        if corroborated:
            category, category_level = cat_choice, "category"
        else:
            # Nothing to climb: the model already chose the fallback, so "climbing one level"
            # is a no-op and there is no disagreement to escalate. Reviewing here would be
            # reviewing an action that changed nothing. A low-confidence *specific* category
            # still falls back and reviews, which is what this floor is for.
            already_fallback = cat_choice == "other"
            category, category_level = "other", "other"
            if bill_related and not already_fallback:
                review_reasons.append(
                    f"category below confidence floor: {cat_choice} at {cat_conf:.2f} -> other"
                )
    if category_level == "category" and (noul_id := CATEGORY_NOULS.get(category)):
        noul_value = float(answers[noul_id].noul)
        if noul_value < CATEGORY_DISAGREEMENT_MAX:
            category = "other"
            category_level = "other"
            if bill_related:
                review_reasons.append(
                    f"category/evidence disagreement: {cat_choice} but {noul_id}={noul_value:.2f}"
                )

    decision = "review" if review_reasons else "auto"
    record = email or EmailRecord("", "", "", "", "", "")

    return {
        "message_id": record.message_id,
        "subject": record.subject,
        "from_email": record.from_email,
        "date": record.date,
        "is_bill": bool(is_bill),
        "kind": kind_choice,
        "kind_confidence": float(kind_conf),
        "category": category,
        "category_confidence": float(cat_conf),
        "category_level": category_level,
        "decision": decision,
        "review_reasons": review_reasons,
        "answers": _summarize_answers(answers),
        "model": str(model),
        "request_id": request_id or None,
        "input_tokens": _int_or_zero(getattr(usage, "input_tokens", 0)),
        "output_tokens": _int_or_zero(getattr(usage, "output_tokens", 0)),
    }


def triage(email: EmailRecord, *, live: bool = True) -> dict:
    """Triage one email. `live=False` returns a review stub with no network call."""
    if not live:
        # Offline shape is built without touching the SDK, so a listener without a
        # key can still run end to end.
        return {
            "message_id": email.message_id,
            "subject": email.subject,
            "from_email": email.from_email,
            "date": email.date,
            "is_bill": False,
            "kind": "other",
            "kind_confidence": 0.0,
            "category": "other",
            "category_confidence": 0.0,
            "category_level": "other",
            "decision": "review",
            "review_reasons": ["offline mode: not evaluated"],
            "answers": {},
            "model": "offline",
            "request_id": None,
            "input_tokens": 0,
            "output_tokens": 0,
        }

    from typesafe_sdk import TypeSafeClient, TypeSafeError

    try:
        with TypeSafeClient(api_key=load_api_key()) as client:
            response = client.system_one(
                state=build_state(email),
                questions=questions(),
                model=MODEL,
            )
    except TypeSafeError as exc:
        raise RuntimeError(f"TypeSafe call failed: {exc}") from exc

    return decide(
        response.answers,
        response.model,
        getattr(response, "request_id", None) or None,
        response.usage,
        email,
    )
