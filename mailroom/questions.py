"""Every question, category, and threshold the triage pipeline uses. Nothing else decides.

This is the one file to review and tune. The guiding rule from the TypeSafe docs
(`concepts/how-to-build-with-system-one`) is that broad questions hide several judgments
behind one answer, so each question here asks exactly one thing. All of them ride in a
single `system_one` request: questions are evaluated in parallel and in isolation, so
asking fourteen costs barely more than asking one, and a question we may not need is
close to free.

Where a decision has an ordered set of outcomes we use a `Score` rather than a `Choice`,
because a Score keeps the ordering and needs no threshold fitted to our data
(`cookbooks/entity_alignment`). Where a label is chosen from a list we read the answer's
own `confidence` and step one level *up* in the taxonomy when the model is unsure, so
every email still comes back with a usable label (`cookbooks/classification_using_confidence`).

Jev is literal and cannot do date arithmetic or counting, so every comparison, count, weight
and threshold lives in Python. Questions here only ever judge what the *text says*.
"""

from typesafe_sdk import Choice, Noul, NoulCriteria

# Pin a versioned id once the thresholds below have been tuned against real mail; an alias
# moves on release, which silently changes the answers our thresholds were fitted to.
MODEL = "jev-latest"

# Confidence floors. These are deliberately not one number: the cost of being wrong differs
# per decision, so a wrong `kind` (which gates the whole pipeline) is treated more harshly
# than an unsure category, which we can absorb by reporting a coarser label.
REVIEW_KIND_CONFIDENCE = 0.60  # below this we refuse to use the `kind` label at all
CONFIDENT_KIND_CONFIDENCE = 0.90  # below this we still label, but we do not trust it enough to auto-act
CONFIDENT_CATEGORY_CONFIDENCE = 0.90  # below this we report the coarse fallback instead of the category

# A category's corroborating Noul is read in BOTH directions, against the two edges of the
# documented 0.30/0.70 band rather than a single midpoint. 0.50 is the *undecided* point of a
# Noul ("a value near 0.5 means the model gives yes and no similar probability"), so a bar there
# would count "no opinion" as agreement:
#
#   >= CATEGORY_CORROBORATION_MIN (0.70) -> decisive agreement: rescue a below-floor category
#   <  CATEGORY_DISAGREEMENT_MAX  (0.30) -> decisive disagreement: downgrade a kept category
#   in between                           -> uncertain, so the model's own answer stands
#
# Measured: a technology supplier selling air purifiers, which no category covers, had the model
# weakly guess `telecom` from the company name — and a corroborator landing at 0.51-0.52 was
# enough to *keep* that wrong label. At a 0.70 bar it falls back to `other` deterministically.
#
# The rescue half exists because `confidence` is a peakedness summary, NOT the winner's
# probability, and it shifts when the option set changes: measured on one real invoice, adding a
# fifth category moved its confidence 0.920 -> 0.880 across the 0.90 floor while the label stayed
# correct on every run. Without a rescue, a taxonomy change silently discards correct answers.
CATEGORY_CORROBORATION_MIN = 0.70
CATEGORY_DISAGREEMENT_MAX = 0.30

# There is no evidence composite and no label/evidence cross-check any more. One existed,
# weighting states_amount_owed / from_billing_entity / has_billing_identifiers and penalising
# is_promotional, and it flagged a confident `invoice` whenever that composite fell below 0.5.
# It was removed because its inputs encoded a narrower reading of `invoice` than this file now
# defines: a bill need not state an amount (the figure may sit behind a link) and need not come
# from the biller (it may arrive from a relay). So it contradicted the very label it guarded.
# Measured on 16 real emails it produced three false-positive reviews and no useful catch.
#
# The four Nouls it consumed are KEPT: they are the decomposed evidence behind `kind`, they are
# reported to the reader, and no decision reads them.

# The coarse fallback. A category we are not confident about is reported as this rather than
# dropped, so the email still gets a bucket.
FALLBACK_CATEGORY = "other"

# Adding a category means adding an entry here and a corroborating Noul below. The keys are
# ours: the model never sees them, so a key can be renamed without changing an answer.
CATEGORIES = {
    "education": {
        "what": "Charges from a school, university, kindergarten, or other education provider, "
        "billed by that institution",
        "not_for": "A telephone operator, an electricity supplier, or a general retailer — "
        "even when the item bought is for a student",
        "examples": [
            "tuition fee",
            "course or semester fee",
            "school bus fee",
            "campus housing",
            "student registration fee",
        ],
    },
    "electricity": {
        "what": "Charges for electric power supplied to a home or business, billed by the "
        "electricity supplier",
        "not_for": "Water, gas, internet, or telephone service; a shop selling electrical "
        "equipment or solar hardware",
        "examples": [
            "consumption in kWh",
            "meter reading",
            "distribution or transmission charge",
            "supply charge",
            "standing charge for power",
        ],
    },
    "telecom": {
        "what": "Charges for mobile, cellular, or fixed telephone service, billed by the "
        "telephone operator",
        "not_for": "An electricity or water utility; a retailer selling a phone handset",
        "examples": [
            "monthly line rental",
            "call or SMS charges",
            "data plan or data usage",
            "subscription line",
            "airtime or tariff fee",
        ],
    },
    "banking": {
        "what": "Charges from a bank, card issuer, or other financial institution: account or "
        "card fees, interest, commissions, or instalments — the institution is billing for its "
        "own service",
        "not_for": "An insurance premium, a utility bill, or a payment to a shop — even when the "
        "money moves through a bank",
        "examples": [
            "hesap işletim ücreti",
            "kart aidatı",
            "account maintenance fee",
            "card annual fee",
            "faiz ve komisyon",
        ],
    },
    "airline": {
        "what": "Charges from an airline or travel carrier: tickets, seat or baggage fees, "
        "change fees, or loyalty-programme charges — the carrier is billing for its own service",
        "not_for": "A travel agency, a hotel, or a general retailer; a price comparison site",
        "examples": [
            "flight ticket",
            "baggage fee",
            "seat selection fee",
            "uçuş bileti",
            "kabin bagajı ücreti",
        ],
    },
    "other": {
        "what": "A bill for a service that is none of the categories above, billed by the "
        "provider of that service",
        "not_for": "Charges for education, electric power, telephone service, banking, or air "
        "travel",
        "examples": [
            "a water bill",
            "an insurance premium",
            "a software subscription invoice",
        ],
    },
}

# One corroborating Noul per specific category, so the Choice label and an independent
# judgment can be compared in code. A disagreement between the two is a free signal that the
# email deserves a human look.
CATEGORY_NOULS = {
    "education": "about_education",
    "electricity": "about_electricity",
    "telecom": "about_telecom",
    "banking": "about_banking",
    "airline": "about_airline",
}


def questions() -> dict:
    """The whole battery for one email, asked in a single request.

    Every question is independent of every other: none may read another's answer. Anything
    that needs two answers combined is combined in Python, in `triage.decide`.
    """
    return {
        # --- 1. what the sender is doing to the reader -------------------------------------------------
        "kind": Choice(
            instructions={
                "question": "What is this email doing to the reader?",
                "inspect": "`email.subject` and `email.body`",
                "focus": "Judge the sender's action on the reader: asking for money, confirming "
                "money already received, or neither. Judge only the action, not the topic, and "
                "do not treat a payment method being offered as a demand for payment.",
            },
            criteria={
                "invoice": {
                    "what": "A bill the reader has been issued — an invoice, a bill, or a "
                    "statement of charges. Judge by what the document IS, not by whether this "
                    "email repeats the figures: an email announcing an invoice that must be "
                    "opened through a link or attachment is still an invoice email",
                    "not_for": "A receipt, which confirms money already paid; a promotional "
                    "offer that merely displays prices; a notice about a periodic account "
                    "statement rather than a bill; a recurring newsletter or bulletin",
                    "examples": [
                        "Your invoice is attached",
                        "Your e-Arşiv Faturanız is ready — click to view",
                        "e-Arşiv Faturanızı görüntülemek için tıklayınız",
                        "Payment due by 12 March",
                        "New charges on your account",
                    ],
                },
                "payment_confirmation": {
                    "what": "A confirmation that a payment has already been made or received",
                    "not_for": "A demand for payment, even when it states an amount",
                    "examples": [
                        "Thank you for your payment",
                        "Payment received",
                        "Please find your receipt",
                        "We have debited your account",
                    ],
                },
                "account_statement": {
                    "what": "A notice that a periodic account statement is available — a "
                    "credit-card or bank account summary of activity, as opposed to a bill for "
                    "goods or services",
                    "not_for": "An announcement of an invoice, even one that must be opened "
                    "elsewhere; a confirmation that a payment was made",
                    "examples": [
                        "Your statement is ready — view it in the app",
                        "Hesap özetinizi uygulamadan görüntüleyebilirsiniz",
                        "Ağustos 2026 TLcard Hesap Özeti",
                        "Your card statement is available",
                    ],
                },
                "promotion": {
                    "what": "A marketing message whose purpose is to sell or promote: a discount, "
                    "an offer, a campaign, or an advertisement",
                    "not_for": "A bill for a service already provided; an informational "
                    "newsletter that carries no offer",
                    "examples": [
                        "50% off this weekend",
                        "Upgrade now and save",
                        "Passo Dükkan'da alışveriş keyfin başlasın!",
                    ],
                },
                "newsletter": {
                    "what": "A recurring bulletin or informational digest — news, updates, tips, "
                    "articles, or announcements, sent on a regular schedule. It pushes no offer "
                    "and states no amount owed",
                    "not_for": "A one-off advertising campaign, which is a promotion; a bill or a "
                    "payment confirmation",
                    "examples": [
                        "This month's news and updates",
                        "Our quarterly bulletin",
                        "Bültenimizin Eylül sayısı yayında",
                    ],
                },
                "other": {
                    "what": "Anything else that is none of the above: personal mail, shipping "
                    "notices, account-access changes, notifications with no billing content",
                    "not_for": "A bill, a payment confirmation, a statement notice, a promotion, "
                    "or a newsletter — each of those has its own option above",
                    "examples": [
                        "Your parcel has shipped",
                        "Your password was changed",
                        "Your appointment is confirmed",
                    ],
                },
            },
        ),
        # --- 1a. independent evidence for the label above ---------------------------------------------
        "states_amount_owed": Noul(
            instructions={
                "question": "Does the email state a specific amount as money owed by the reader?",
                "inspect": "`email.body`",
                "focus": "An amount presented as money the reader must pay. An amount quoted as "
                "information, as a saving, or as a payment already made does not count.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "A figure is presented as the money now owed",
                    "examples": [
                        "Total due: EUR 214.90",
                        "Amount payable 89.00",
                        "Ödenecek Tutar: 7.800,00 TRY",
                        "Belge Tutarı: 7.800,00 TRY",
                    ],
                },
                false={
                    "what": "No amount is presented as money owed",
                    "not_for": "A price in an advertisement, or an amount already paid",
                    "examples": ["Save EUR 40", "You paid EUR 89.00 last month"],
                },
            ),
        ),
        "from_billing_entity": Noul(
            instructions={
                "question": "Is the sender a biller sending out its own charges to the reader?",
                "compare": ["`email.from.display_name`", "`email.from.email`", "`email.body`"],
                "focus": "A commercial provider billing for its own goods or services. A private "
                "individual, a newsletter, or a marketing list is not a biller.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "The sender presents itself as the provider issuing the charges",
                    "examples": [
                        "City Power <billing@citypower.example>",
                        "Northside University <fees@northside.example>",
                    ],
                },
                false={
                    "what": "The sender is not a provider billing for its own services",
                    "not_for": "A private person, a promotion, or a third party discussing "
                    "someone else's charges",
                    "examples": [
                        "Weekly Deals <deals@shop.example>",
                        "A friend forwarding a bill",
                    ],
                },
            ),
        ),
        "has_billing_identifiers": Noul(
            instructions={
                "question": "Does the email carry billing identifiers that tie it to a specific account or charge?",
                "inspect": "`email.body`",
                "focus": "Look for an invoice or reference number, a customer or account number, "
                "a stated billing period, or payment terms. An advertisement has none of these.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "At least one billing identifier is present",
                    "examples": [
                        "Invoice no. 2026-44817",
                        "Account 55-881-2",
                        "Billing period: February 2026",
                        "Payable within 30 days",
                    ],
                },
                false={
                    "what": "No identifier ties the email to a specific account or charge",
                    "examples": ["A newsletter with no reference number"],
                },
            ),
        ),
        "is_promotional": Noul(
            instructions={
                "question": "Is the email an advertisement or promotional offer rather than a bill?",
                "inspect": "`email.subject` and `email.body`",
                "focus": "The email's purpose is to sell something, and the figures it shows are "
                "prices or savings rather than money owed.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "The email aims to promote or sell, and asks for no payment owed",
                    "examples": [
                        "Limited-time offer, 50% off",
                        "Upgrade now and save",
                        "New season prices",
                    ],
                },
                false={
                    "what": "The email is not promoting a product or offer",
                    "not_for": "A bill that happens to mention a product, or a receipt",
                    "examples": ["Your monthly statement is ready"],
                },
            ),
        ),
        # --- 2. which service the money is for ---------------------------------------------------------
        "category": Choice(
            instructions={
                "question": "What service is this bill charging for?",
                "compare": ["`email.from.display_name`", "`email.subject`", "`email.body`"],
                "focus": "Judge the service being charged for, not the payment status. The biller "
                "is the organisation named in `email.from.display_name` and in `email.subject` — "
                "rely on those, since they name the service outright. The sending address and "
                "domain often belong to a document-delivery or notification relay rather than to "
                "the biller itself, so a generic or unfamiliar domain is NOT evidence against a "
                "service that the display name or the subject names directly. Line items, "
                "metering units, and account details in the body corroborate the service.",
            },
            criteria={name: spec for name, spec in CATEGORIES.items()},
        ),
        # --- 2a. one corroborating Noul per category ---------------------------------------------------
        # These ask about SUBJECT MATTER, not about charges. A promotion or a newsletter is still
        # "about" education or banking even though nothing is owed, so a charges-shaped question is
        # unanswerable for anything that is not a bill — and it would wrongly downgrade a correct
        # category on a promotional email. Each asks one property and works for every `kind`.
        "about_education": Noul(
            instructions={
                "question": "Is this email about education?",
                "compare": ["`email.from.display_name`", "`email.subject`", "`email.body`"],
                "focus": "Judge the subject matter only: is the sender a school, university, or "
                "other education provider, or does the email concern education or its services? "
                "Do not judge whether money is owed.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "The sender or the subject is an education provider, or education itself",
                    "examples": [
                        "Riverside University Billing Office",
                        "Tuition for the spring semester",
                        "Course registration fee",
                        "An enrolment campaign from a college",
                    ],
                },
                false={
                    "what": "The email is about something other than education",
                    "not_for": "A phone or power bill that merely belongs to a student",
                    "examples": ["A mobile line rental for a student's phone"],
                },
            ),
        ),
        "about_electricity": Noul(
            instructions={
                "question": "Is this email about electric power?",
                "compare": ["`email.from.display_name`", "`email.subject`", "`email.body`"],
                "focus": "Judge the subject matter only: is the sender an electricity supplier, or "
                "does the email concern electric power supplied to a premises? Do not judge "
                "whether money is owed.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "The sender or the subject is electric power or its supplier",
                    "examples": [
                        "CK Boğaziçi Elektrik",
                        "Consumption 312 kWh",
                        "Meter reading 44 218",
                        "A supply or distribution charge",
                    ],
                },
                false={
                    "what": "The email is about something other than electric power",
                    "not_for": "Telephone or mobile service, even from the same holding company",
                    "examples": ["A monthly data plan"],
                },
            ),
        ),
        "about_telecom": Noul(
            instructions={
                "question": "Is this email about telephone service?",
                "compare": ["`email.from.display_name`", "`email.subject`", "`email.body`"],
                "focus": "Judge the subject matter only: is the sender a telephone operator, or "
                "does the email concern mobile, cellular, or fixed-line service? Do not judge "
                "whether money is owed.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "The sender or the subject is telephone service or its operator",
                    "examples": [
                        "Monthly line rental",
                        "Call and data charges",
                        "A mobile subscription",
                    ],
                },
                false={
                    "what": "The email is about something other than telephone service",
                    "not_for": "An electricity supplier, or a shop selling handsets",
                    "examples": ["Electricity consumption 312 kWh"],
                },
            ),
        ),
        "about_banking": Noul(
            instructions={
                "question": "Is this email about banking or a payment card?",
                "compare": ["`email.from.display_name`", "`email.subject`", "`email.body`"],
                "focus": "Judge the subject matter only: is the sender a bank, card issuer, or "
                "other financial institution, or does the email concern its accounts, cards, or "
                "services? A marketing or informational email from a bank about its own product "
                "is still about banking. Do not judge whether money is owed.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "The sender or the subject is a bank, a card, or a financial institution",
                    "examples": [
                        "Yapı Kredi",
                        "A Garanti BBVA card bonus campaign",
                        "Your card statement",
                        "hesap işletim ücreti",
                    ],
                },
                false={
                    "what": "The email is about something other than banking",
                    "not_for": "A utility or telephone bill that merely happens to be paid through a bank",
                    "examples": ["An electricity bill paid by direct debit"],
                },
            ),
        ),
        "about_airline": Noul(
            instructions={
                "question": "Is this email about air travel?",
                "compare": ["`email.from.display_name`", "`email.subject`", "`email.body`"],
                "focus": "Judge the subject matter only: is the sender an airline or travel "
                "carrier, or does the email concern flights, tickets, or a carrier's loyalty "
                "programme? A fare promotion from an airline is still about air travel. Do not "
                "judge whether money is owed.",
            },
            criteria=NoulCriteria(
                true={
                    "what": "The sender or the subject is an airline or travel carrier",
                    "examples": [
                        "Turkish Airlines Miles&Smiles",
                        "Pegasus",
                        "A flight or baggage fee",
                        "uçuş bileti",
                    ],
                },
                false={
                    "what": "The email is about something other than air travel",
                    "not_for": "A hotel, a travel agency, or a general retailer",
                    "examples": ["A hotel booking confirmation"],
                },
            ),
        ),
    }


def demo() -> None:
    """Self-check the battery's own invariants. Run: uv run python -m mailroom.questions"""
    battery = questions()

    expected = {
        "kind",
        "states_amount_owed",
        "from_billing_entity",
        "has_billing_identifiers",
        "is_promotional",
        "category",
        "about_education",
        "about_electricity",
        "about_telecom",
        "about_banking",
        "about_airline",
    }
    assert set(battery) == expected, f"battery drifted: {set(battery) ^ expected}"

    # The corroborating Nouls must exist, or the category rescue and downgrade silently no-op.
    for category, noul_id in CATEGORY_NOULS.items():
        assert category in CATEGORIES, f"{category} is not a category"
        assert noul_id in battery, f"{noul_id} is missing from the battery"

    # The fallback has to be a real label, not a magic string.
    assert FALLBACK_CATEGORY in CATEGORIES
    # No question returns a Score any more: the payment-state judgment was removed because it was
    # meaningful for only one of the six kinds and had accreted three special cases around it.
    # This guards against a scored status being quietly reintroduced.
    assert not any(getattr(q, "type", None) == "score" for q in battery.values()), \
        "a scored question came back; payment state is meant to be gone"

    print(f"ok: {len(battery)} questions, {len(CATEGORIES)} categories, "
          f"thresholds kind>={REVIEW_KIND_CONFIDENCE}/{CONFIDENT_KIND_CONFIDENCE} "
          f"cat>={CONFIDENT_CATEGORY_CONFIDENCE} "
          f"cat_corrob>={CATEGORY_CORROBORATION_MIN}")


if __name__ == "__main__":
    demo()
