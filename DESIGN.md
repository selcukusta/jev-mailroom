# Design notes

The reasoning behind the thresholds, the measurements they were fitted from, and the machinery
that was removed along the way. [README.md](README.md) is the practical guide; this file is the
record of *why*.

## Method

Three habits this project adopted the hard way, each after getting something wrong.

**Never conclude from a single run.** An LLM-backed classifier carries run-to-run variance, so a
single sample is not a measurement. Two conclusions here were overturned by simply repeating the
call: a review-rate improvement that turned out to be one lucky sample, and a "regression" that a
controlled A/B attributed to a completely different cause.

**Check that an A/B actually reproduces the original configuration.** One comparison was built by
slicing a list of options that had since been reordered, producing a variant that had never
existed. It would have yielded a confident, wrong conclusion. The corrected test changed one
variable and held everything else constant.

**Report what you observed, not what you expected.** Verification runs in this project were
briefed with an expected outcome, and on more than one occasion the expectation was wrong. The
useful runs were the ones that said so plainly rather than adjusting the test to fit.

## Verification history

| Scope | Result |
|---|---|
| 6 synthetic English fixtures (since deleted) | 6/6 exact on `kind` + `category` |
| 9 real emails | 9/9 confirmed by hand |
| 7 further real emails | the two the owner checked were both mis-read; that drove a criteria fix |
| 19 real emails (current set) | every label stable; one case still varies in whether it escalates — see below |

The synthetic fixture set is gone, along with its label file — `fixtures/` is untracked, so when
it was repurposed for real mail the test data was lost. The numbers above are a record of
measurements that were made, not a suite you can re-run.

## How `confidence` actually behaves

Three findings, each measured, each of which changed a design decision.

**`confidence` is not the winner's probability.** On one real email the top option scored `0.907`
while `confidence` came back `0.880`. They are different quantities — confidence summarises how
peaked the distribution is — so both have to be printed when diagnosing, and a threshold fitted
against one is meaningless for the other.

**A confidence threshold is coupled to the option count.** Same email, same state, identical
wording; only the number of categories differed:

| options | chosen | top probability | confidence |
| --- | --- | --- | --- |
| 4 | correct | 0.943 | **0.920** |
| 5 | correct | 0.907 | **0.880** |

The label never changed and was right on every run. Only the certainty fell, by ~0.04, across a
`0.90` floor — silently turning a correct answer into a fallback plus a human review. So **adding
or removing an option invalidates any threshold fitted against the previous set**, and a taxonomy
change has to be followed by re-validating the cases that already passed.

**But the option's name pulls the other way, and it can win.** Renaming a category from `school`
to `education` while simultaneously adding a sixth option produced a *rise*: the same email went
from a mean confidence of `0.873` to `0.968`, stable over five runs, when the added option should
have cost it ~0.04. The option name is part of the prompt, and the new name matched the biller's
own wording far better than the old one. Two competing forces, then — count dilutes, naming
sharpens — and the count and name changed together in that measurement, so the attribution is
inference rather than isolation.

## Why the category corroborators ask about subject matter

Each real category has one corroborating yes/no question, read in **both** directions against the
two edges of the documented 0.30 / 0.70 band: at or above **0.70** a category that fell short of
the confidence floor is **kept** (the rescue); below **0.30** a chosen category is downgraded to
the fallback. The middle band is *undecided*, so the model's own answer stands.

The edges matter, and a midpoint does not. A Noul at ~0.50 means "yes and no are equally likely",
so a single 0.50 bar treats *no opinion* as agreement — and it did. A technology supplier selling
air purifiers, which no category covers, had the model weakly guess `telecom` from the company
name, and a corroborator landing at 0.51–0.52 was enough to **keep** that wrong label. Moving the
bar to the band edge makes it settle on `other` deterministically, and costs nothing: every
legitimate rescue in this mailbox reads 0.73 or higher.

Their wording matters more than it looks. They originally asked *"are the charges for X?"*, which
is only answerable for a bill. A promotion from a bank is still *about* banking while owing
nothing, so a charges-shaped question scores it low — and low means downgrade, which would have
demoted a correct `banking` label to `other`. Rephrased as *"is this email about X?"* the same
email scores `0.99`, and the mechanism works across all six kinds instead of only bills.

## What was removed, and why

Every removal here was justified by one test: **does any decision read it, and does it say
something no other answer already says?**

**The payment-status `Score` and its four Nouls.** It reported a tri-state status. It was
meaningful for exactly one of the six kinds, definitional for a second, and meaningless for the
other four; it asked a third time what other questions already asked; and it had accreted a
confidence floor, an informational-notice split and a `bill_related` gate around it. On eight real
emails it drove zero routing decisions. Removing it, and then the four Nouls it had used, changed
no label and cut input tokens by 11.7%. The consequence is stated plainly in the README: the
pipeline no longer answers *"is this already paid?"* at all.

**The label/evidence cross-check and its composite.** A weighted composite of three Nouls, minus a
promotional penalty, flagged any confident `invoice` whose score fell below `0.50` — on the theory
that a label disagreeing with independent evidence is a free uncertainty signal. It was removed
because it encoded a *narrower* reading of `invoice` than the criteria: a bill may state no amount,
and may arrive from a relay rather than the biller. It therefore contradicted the very label it
guarded, and on the 16 real emails then present it produced three false-positive reviews — every
time it fired. The
four Nouls it consumed are kept, because they decompose a label that still ships.

**A vacuous downgrade that manufactured a review.** When the model's chosen category was already
the fallback, the code still "climbed one level" — to where it already was — and emitted a review
reason reading `other at 0.77 -> other`. A rule whose action changes nothing should not escalate.

**A `kind` criterion that argued with itself.** The `invoice` definition once required the email to
*state an amount owed*, while the `account_statement` definition covered documents viewable
elsewhere. An invoice notification that points to a link therefore landed as `account_statement`,
even though its subject plainly says "invoice". The definition now judges **what the document is**,
not whether this particular email repeats the figures. This is the same confusion — "a billing
document" versus "a demand for payment" — that caused the composite bug above; it has surfaced
three times and is the single most productive thing to look for when a label looks wrong.

## Known limitations

**The model reads the envelope, not the document.** Jev is text-only. Attachment parts are
correctly skipped, which also means their contents are never read, and real invoice notifications
put the invoice in a PDF. A covering email can be classified at `1.00` confidence while the
document itself is never seen — **high confidence on thin evidence is the most dangerous output
this system produces**, so treat a perfect score on an attachment-only email as a sign the model
had little to go on rather than a sign it was right.

This is also the main remaining source of *category* ambiguity, and the most valuable thing left to
build. When the document is unreadable the category question has only a sender name and a subject
to work from: a supplier whose name contains a technology word can plausibly be guessed as
`telecom` even though it sells air purifiers, and the guess then has to be overruled by the
fallback. Extract the attachment text and the line items carry the answer directly — no guess, no
override, no escalation. Attachments are still out of scope, deliberately, but this is why the
limitation matters more than it looks.

**Classification is English-first.** The upstream documentation states other languages are
"handled but not equally well". A few Turkish examples were added for the signals that measurably
under-read, but expect lower accuracy outside English.

**Number and currency formats are Anglo.** `7.800,00 TRY` against `7,800.00`, and the model is
documented as weak on numeric representation. Any future extraction has to normalise in code with
a locale-aware pattern.

**A relaying address is not evidence about the biller.** Invoice mail often arrives from a
document-delivery service while the biller is named only in the sender's display name and the
subject. The `category` question therefore tells the model to rely on those and to discount a
generic domain. Verify that holds for the senders in your own mail.

**Quoted-history stripping is English-only.** The reply markers are English; localized variants are
not recognised, though the `>` quote prefix and `____` separators are universal. A localized reply
quote can therefore survive into the text being judged, and quoted history can flip a verdict.

**A threshold is a threshold, not a proof.** Any bar with a case sitting on it will flap between
identical runs. Two were observed here, on two different thresholds, and neither was caused by the
change under test — a controlled A/B varying only `criteria.examples` reproduced the flapping in
both arms:

- **Category confidence at 0.86–0.91**, straddling the `0.90` floor. Resolved not by moving the
  floor but by renaming the category (`school` → `education`), which lifted it to a stable
  0.96–0.98. The option *name* mattered more than the threshold.
- **A corroborating question at 0.39–0.52**, straddling what was then a `0.50` bar. That one
  toggled the category label (`other` ↔ `telecom`) *and* the decision. Moving the bar off the
  midpoint to the `0.70` band edge fixed the **label** — it now settles on `other` on every run,
  where before it sometimes landed on `telecom` and stayed there. The **decision** still varies,
  because the model's raw guess itself toggles: when it names a specific category and we overrule
  it, that overrule is a review; when it says `other` outright, there is nothing to overrule. That
  residual is the model's uncertainty, not the logic flapping.

So: repeat any measurement, and when a case sits on a bar, treat the *escalation* as the correct
outcome rather than trying to make it deterministic. That is why the rescue mechanism consults an
independent judgment instead of letting one number decide alone.

**Some labels are only as good as the input.** A perfectly confident label over a thin input is
still thin: an attachment-only invoice can be classified `1.00` while the invoice itself is never
read.
