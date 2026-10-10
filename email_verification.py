"""
Email verification — self-hosted SMTP probing.

REPLACES the Hunter.io integration that stood here. Hunter was never
usable: it requires a work email to sign up, and this project's one hard
constraint is $0 with no paid APIs. The module silently returned `None`
for months, which meant every stored address stayed `guessed_unverified`
and nobody noticed, because "verification unavailable" and "verification
passed" printed the same way.

The self-hosted verifier in `email_verify.py` needs no account, no key,
no quota and no network round-trip to a third party, so verification is
no longer an optional step gated on a missing credential.

IMPORTANT — the single most important behaviour in this file:
it is called from the ENRICHMENT hot path, once per candidate, so it MUST
be cheap and MUST fail open. If a check is slow, errors, or is disabled,
the pipeline keeps running with an unverified address. It never drops a
lead and never blocks a run.
"""
from __future__ import annotations

import os
from typing import Optional

from email_verify import SmtpVerifier, UNDELIVERABLE, CONFIRMED_BAD

# Off by default. Enrichment runs per candidate across ~500 leads, and an
# SMTP conversation costs ~2.5s; leaving it always-on would multiply a
# pipeline run from minutes to well over an hour. It is enabled for
# deliberate batch verification (backfill_verify_emails.py) and can be
# switched on globally with VERIFY_SMTP=1.
_ENABLED = os.getenv("VERIFY_SMTP", "").strip().lower() in ("1", "true", "yes", "on")

_verifier: Optional[SmtpVerifier] = None


def _get_verifier() -> SmtpVerifier:
    global _verifier
    if _verifier is None:
        _verifier = SmtpVerifier()
    return _verifier


def verify_email(email: str) -> Optional[dict]:
    """
    Verify one address.

    Returns None when verification is disabled, unavailable, or the check
    is inconclusive — which callers treat as "unverified", never as
    "invalid". Only an explicit rejection returns a verdict of
    `undeliverable`.
    """
    if not _ENABLED:
        return None
    try:
        result = _get_verifier().verify(email)
    except Exception:  # noqa: BLE001 - verification must never break a run
        return None

    # Normalise onto the shape the rest of the pipeline already expects
    # (`result` field, as Hunter used to return).
    verdict = result.get("verdict")
    if verdict in CONFIRMED_BAD:
        return {
            "result": "undeliverable",
            "score": 0,
            "smtp_code": result.get("smtp_code"),
            "mx_host": result.get("mx_host"),
            "reason": result.get("reason", ""),
            "verifier": "self_hosted_smtp",
        }
    if verdict == "deliverable":
        return {
            "result": "deliverable",
            "score": 100,
            "smtp_code": result.get("smtp_code"),
            "mx_host": result.get("mx_host"),
            "verifier": "self_hosted_smtp",
        }
    if verdict == "catch_all":
        return {
            "result": "unknown",
            "score": 50,
            "smtp_code": result.get("smtp_code"),
            "mx_host": result.get("mx_host"),
            "reason": "catch-all server, cannot verify",
            "verifier": "self_hosted_smtp",
        }
    # try_again / blocked / no_mx / invalid -> genuinely unknown
    return {
        "result": "unknown",
        "score": None,
        "verdict_raw": verdict,
        "smtp_code": result.get("smtp_code"),
        "mx_host": result.get("mx_host"),
        "reason": result.get("reason", ""),
        "verifier": "self_hosted_smtp",
    }
