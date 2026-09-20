# jev-mailroom

A mailbox triage proof-of-concept. A fake IMAP server feeds a listener, the listener asks **Jev**
one batched call per email, and plain Python turns the answers into labels that a dashboard
displays.

**It only reads and labels mail.** Nothing here sends, replies, pays, or moves money.

```
fixtures/*.eml → mailbox/ (Maildir) → IMAP :1143 → listener → Jev (1 call, 11 questions)
                                                                ↓
                                        decisions in Python → results.jsonl → dashboard :8000
```

## Why a decision engine rather than an LLM

Jev is not a text generator. You send it content plus a set of typed questions, and it returns
typed answers with probabilities. Two properties shape this whole design:

- **Questions in one request are answered independently and in parallel.** Eleven questions cost
  about the same as one, so the design asks many narrow questions in a single call and combines
  them in Python, instead of writing one clever prompt.
- **It cannot do arithmetic, compare dates, count, or write prose.** Every comparison, threshold
  and weight therefore lives in Python.

A useful consequence: asking a question you might not need is nearly free, so over-asking is cheap
and the filtering happens in code.

## Quick start

```bash
# API key from TYPESAFE_API_KEY, or from ~/.typesafe.key
./jev.sh start
open http://localhost:8000/dashboard.html
```

| Command | Description |
|---|---|
| `./jev.sh start` | start the stack; waits for the IMAP port to answer before starting the listener |
| `./jev.sh stop` | stop everything |
| `./jev.sh restart` | stop, then start |
| `./jev.sh status` | pid, state, port and record count per process |
| `./jev.sh logs [stub\|listener\|dashboard]` | follow the logs |
| `./jev.sh import` | push new `fixtures/*.eml` into the running stub — no restart |

Working on the pipeline itself:

| Command | Description |
|---|---|
| `uv run python -m jev_poc.questions` | self-check the question battery and its thresholds |
| `uv run python -m jev_poc.listener --once --offline` | one poll pass without calling the API |

## What it labels

Two independent axes, so "a promotion about banking" is expressible as one combination.

**`kind` — what the email is**

`invoice` · `payment_confirmation` · `account_statement` · `promotion` · `newsletter` · `other`

**`category` — what it is about**

`education` · `electricity` · `telecom` · `banking` · `airline` · `other`

A bank's mail can arrive as `account_statement/banking`, `invoice/banking`, `newsletter/banking` or
`promotion/banking` — four different things to act on.

## The questions

Eleven of them, all sent in one call. Each asks exactly one thing, because a broad question hides
several judgments behind a single answer.

| # | Question | What it decides |
|---|---|---|
| 1 | `kind` | what the email is doing |
| 2 | `states_amount_owed` | a figure is presented as money owed |
| 3 | `from_billing_entity` | the sender is a biller, not a person or a marketer |
| 4 | `has_billing_identifiers` | invoice or account number, billing period, terms |
| 5 | `is_promotional` | it is an offer with prices rather than a bill |
| 6 | `category` | which service the email is about |
| 7–11 | `about_education`, `about_electricity`, `about_telecom`, `about_banking`, `about_airline` | one corroborating question per category |

Questions 2–5 are reported for the reader; no decision reads them. Questions 7–11 are what the
category rules actually use.

## Decision rules

All thresholds live in `questions.py`. Every email costs exactly one API call.

| Stage | Rule |
|---|---|
| `kind` | confidence below `0.60` → review; otherwise take the answer |
| `category` | confidence ≥ `0.90` → take it. Below the floor, that category's corroborating question decides: **≥ `0.70`** → keep the category, otherwise fall back to `other` |
| category cross-check | a kept category whose corroborating question reads **< `0.30`** → downgrade to `other`. Between `0.30` and `0.70` that question is undecided, so the model's own answer stands rather than being overruled |
| bills only | the category rules apply only when `kind` is `invoice` or `payment_confirmation`. For a promotion, newsletter or statement the question is speculative and must not create a review |

An email whose chosen category is already `other` is not reviewed for "falling back" to `other` —
nothing changed, so there is nothing to escalate.

## Dashboard

`dashboard.html` is a single file with no build step and no dependencies. Serve the repo root and
open it; it polls `results.jsonl` every 2 seconds.

- leads with the auto-routed / needs-review split
- lists the review queue above the table, with each item's reason in full
- **click any bar in the breakdowns to filter the table** — one filter per axis, the two combine,
  click again to clear; the summary and the breakdowns stay global so the numbers remain the
  mailbox's, not the filter's
- expand any row to see its raw answers

It must be **served, not opened as a file** — it fetches `results.jsonl` relatively, so `file://`
will not work. `./jev.sh` serves the repo root for you; by hand:

```bash
uv run python -m http.server 8000     # from the repo root
```

## Configuration

| Variable | Purpose |
|---|---|
| `TYPESAFE_API_KEY` | API key; falls back to `~/.typesafe.key` |
| `TYPESAFE_DEFAULT_MODEL` | override the model |
| `TYPESAFE_BASE_URL` | override the API endpoint |
| `TYPESAFE_LOG_LEVEL` | `debug`, `info`, `warning`, `error`, `off` |

`MODEL` in `questions.py` is `jev-latest`, which currently resolves to `jev-1.13.0`. That alias
moves when a new version ships, so once you have tuned the thresholds against your own mail, pin
the versioned id instead. Every result records the model that produced it.

Cost, measured on real mail: **~5.7k input tokens per email, about $0.00024 each** — roughly
$0.24 per 1000 emails. Output tokens are free.

## Adding your own mail

1. Save mail as `.eml`, one file per message. Gmail Takeout's `.mbox` and Outlook's `.msg` are not
   supported.
2. Put the files in `fixtures/`.
3. Run `./jev.sh import` — they are picked up live, with no restart, and import is idempotent by
   `Message-ID`. Use `./jev.sh restart` instead if you want a clean slate.

Or point any IMAP client straight at the stub: `127.0.0.1:1143`, user `poc@local`, password `poc`.

Both `fixtures/` and `mailbox/` are gitignored, so your mail is never committed.

## Gotchas

- Both ports bind to `127.0.0.1` only, and the stub's IMAP is **plaintext** — it is a test double,
  not a mail server.
- The listener marks messages `\Seen` as it processes them, so it only ever triages *new* mail.
  Deleting `results.jsonl` without also clearing `mailbox/` leaves the two out of sync.
- Results carry no triage timestamp; recency comes from the email's `Date` header.
- **Email content is untrusted input.** Jev does not treat its state as hostile, so a crafted email
  can move an answer. Treat the output as a triage signal, not an authority.

## Not built

- **Attachments are not read.** Jev is text-only, so an invoice that lives in a PDF attachment is
  judged by its covering email.
- **Amount and due-date extraction.** The upstream docs sketch the approach
  (`pre_parsed_value_extraction`, `date_extraction`).
- **A pre-filter** to settle trivial cases without spending an API call.
- **Locale-independent quoted-history stripping.** The reply markers are English-only, so a
  localized reply quote can survive into the text being judged.

## Design notes

Why the thresholds are set where they are, the measurements they were fitted from, and the
machinery that was removed along the way: [DESIGN.md](DESIGN.md).
