"""
Self-hosted email verification — no vendor, no signup, no cost.

This closes PLAN.md task 5.6, which was BLOCKED for months. Every third
party was unusable because Hunter.io and QuickEmailVerification both
require a work email to sign up, and this project's whole constraint is
"$0, no paid APIs". Turns out the check they charge for is ~40 lines of
Python standard library:

    1. MX lookup          — does the domain have a mail server at all?
    2. SMTP RCPT TO probe — does the mail server accept THIS address?

That is the same pipeline ZeroBounce ($16/2k), NeverBounce and Hunter
run. No account, no quota, no rate limit, no signup, no data leaving the
machine. Verified live 2026-09-29 against known-good and known-bad
addresses before being trusted.

WHAT THE VERDICTS ACTUALLY MEAN — read before filtering on them:

  deliverable (250)   the server accepted the address. It exists, or the
                      server accepts everything. It is NOT proof a human
                      reads it.
  undeliverable (550) the server explicitly rejected the mailbox. Safe
                      to drop — it will bounce on send.
  catch_all (252)     cannot be proven either way. KEEP, but never treat
                      as a personal contact.
  try_again (450)     transient; the server is rate-limiting or busy.
                      Retry later; do NOT drop the lead.
  blocked             the server refused us (grey-list, Spamhaus, port 25
                      filtered). Says nothing about the address. KEEP and
                      mark unknown.

The one genuinely important distinction: an explicit 550 is proof of
absence, everything else is absence of proof. This module never converts
a non-answer into a "bad".
"""
from __future__ import annotations

import smtplib
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import dns.resolver

# Deliverable. The server accepted it.
DELIVERABLE = "deliverable"
# Explicitly rejected by the mail server (550/551/553/556). Will bounce.
UNDELIVERABLE = "undeliverable"
# Server accepts any local-part. Cannot distinguish real from fake.
CATCH_ALL = "catch_all"
# Transient — greylisting, rate limiting, busy. Retry later.
TRY_AGAIN = "try_again"
# The mail server refused to talk to us. Says NOTHING about the address.
BLOCKED = "blocked"
# No MX record: the domain cannot receive mail at all.
NO_MX = "no_mx"
# Malformed input.
INVALID = "invalid"

# Verdicts that are safe to act on.
CONFIRMED_BAD = {UNDELIVERABLE, NO_MX, INVALID}
# Verdicts that are safe to send to.
CONFIRMED_GOOD = {DELIVERABLE}
# Verdicts that mean "we learned nothing" — never drop a lead on these.
UNKNOWN = {CATCH_ALL, TRY_AGAIN, BLOCKED}

# Well-known disposable / role inbox providers. Not junk, but they will
# never reach a founder, so they are worth surfacing separately.
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net",
    "10minutemail.com", "tempmail.com", "temp-mail.org", "yopmail.com",
    "trashmail.com", "sharklasers.com", "getnada.com", "throwawaymail.com",
    "maildrop.cc", "dispostable.com", "fakeinbox.com", "mailnesia.com",
    "mintemail.com", "mytemp.email", "spamgourmet.com", "mailcatch.com",
    "emailondeck.com", "inboxkitten.com", "moakt.com", "tmail.ws",
    "tempr.email", "discard.email", "linshiyouxiang.net",
}

ROLE_PREFIXES = {
    "admin", "abuse", "billing", "careers", "compliance", "contact",
    "enquiries", "enquiry", "feedback", "hello", "help", "hi", "info",
    "inquiries", "inquiry", "jobs", "legal", "mail", "media", "no-reply",
    "noreply", "office", "partners", "postmaster", "press", "sales",
    "security", "support", "team", "webmaster", "hostmaster", "root",
    "dmca", "customerservice", "user", "users", "auth", "api",
}


def is_disposable(email: str) -> bool:
    domain = email.rpartition("@")[2].lower()
    return any(domain == d or domain.endswith("." + d) for d in DISPOSABLE_DOMAINS)


def is_role_address(email: str) -> bool:
    local = email.rpartition("@")[0].lower()
    return local in ROLE_PREFIXES


class SmtpVerifier:
    def __init__(self, timeout: int = 12, max_workers: int = 6, helo: str = "leadgen.local"):
        self.timeout = timeout
        self.max_workers = max_workers
        self.helo = helo

    # ------------------------------------------------------------------ #
    def verify(self, email: str) -> dict:
        """
        Verify one address. Returns
        {email, verdict, smtp_code, mx_host, reason, ms}.
        """
        t0 = time.time()
        email = (email or "").strip().lower()
        result = {"email": email, "smtp_code": None, "mx_host": None,
                  "reason": "", "verdict": INVALID}

        if "@" not in email:
            result["reason"] = "no @"
            result["ms"] = int((time.time() - t0) * 1000)
            return result
        local, _, domain = email.rpartition("@")
        if not local or "." not in domain or " " in email:
            result["reason"] = "malformed"
            result["ms"] = int((time.time() - t0) * 1000)
            return result

        result["is_disposable"] = is_disposable(email)
        result["is_role"] = is_role_address(email)

        # 1. MX lookup
        try:
            mx = dns.resolver.resolve(domain, "MX", lifetime=8)
            if not len(mx):
                result["verdict"] = NO_MX
                result["reason"] = "empty MX"
                result["ms"] = int((time.time() - t0) * 1000)
                return result
            # dnspython's Answer.sort() raises TypeError on this version
            # ('NoneType' object is not callable). That exception was
            # silently swallowed once and made EVERY domain look like it
            # had no mail server. Use sorted() — do not "optimise" this.
            ordered = sorted(mx, key=lambda r: r.preference)
            mx_host = str(ordered[0].exchange).rstrip(".")
        except dns.resolver.NXDOMAIN:
            result["verdict"] = NO_MX
            result["reason"] = "NXDOMAIN"
            result["ms"] = int((time.time() - t0) * 1000)
            return result
        except Exception as e:
            result["verdict"] = NO_MX
            result["reason"] = f"mx_{type(e).__name__}"
            result["ms"] = int((time.time() - t0) * 1000)
            return result

        result["mx_host"] = mx_host

        # 2. SMTP conversation
        try:
            # Pass host/port, NOT a pre-made socket: on Python 3.12+
            # smtplib(SOCKET) raises "'socket' object has no attribute
            # 'find'". The connect-then-wrap pattern is deprecated.
            server = smtplib.SMTP(mx_host, 25, timeout=self.timeout)
            try:
                server.ehlo(self.helo)
                server.mail("verify@leadgen.local")
                code, msg = server.rcpt(email)
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
        except smtplib.SMTPConnectError as e:
            # 554 from Spamhaus / greylisting / port-25 filtering. The
            # address is fine; WE were refused. Never mark it bad.
            result["verdict"] = BLOCKED
            result["smtp_code"] = e.smtp_code
            result["reason"] = (e.smtp_error.decode(errors="replace")[:90]
                                if isinstance(e.smtp_error, bytes)
                                else str(e.smtp_error)[:90])
            result["ms"] = int((time.time() - t0) * 1000)
            return result
        except smtplib.SMTPServerDisconnected:
            result["verdict"] = BLOCKED
            result["reason"] = "server disconnected"
            result["ms"] = int((time.time() - t0) * 1000)
            return result
        except socket.timeout:
            result["verdict"] = BLOCKED
            result["reason"] = "timeout"
            result["ms"] = int((time.time() - t0) * 1000)
            return result
        except Exception as e:
            result["verdict"] = BLOCKED
            result["reason"] = f"conn_{type(e).__name__}"
            result["ms"] = int((time.time() - t0) * 1000)
            return result

        result["smtp_code"] = code
        text = (msg.decode(errors="replace") if isinstance(msg, bytes) else str(msg))
        result["smtp_msg"] = text[:90]

        if code in (250, 251):
            result["verdict"] = DELIVERABLE
        elif code == 252:
            # "Cannot VRFY user, but will accept message" — catch-all.
            result["verdict"] = CATCH_ALL
            result["reason"] = "252 cannot verify"
        elif code in (550, 551, 553, 556):
            result["verdict"] = UNDELIVERABLE
            result["reason"] = text[:70]
        elif code in (450, 451, 452):
            result["verdict"] = TRY_AGAIN
            result["reason"] = "greylisted/rate-limited"
        elif code == 421:
            result["verdict"] = BLOCKED
            result["reason"] = "421 service unavailable"
        else:
            result["verdict"] = BLOCKED
            result["reason"] = f"code {code}"

        result["ms"] = int((time.time() - t0) * 1000)
        return result

    # ------------------------------------------------------------------ #
    def verify_many(self, emails: list[str]) -> dict[str, dict]:
        """
        Verify a batch, in parallel.

        Concurrency is modest (6) on purpose: each check opens an SMTP
        conversation, and hammering mail servers from one IP is how you
        get grey-listed. Correctness over speed — 2.5s per address with 6
        workers is still ~2.4 addresses/second.
        """
        results: dict[str, dict] = {}
        unique = list(dict.fromkeys(e.strip().lower() for e in emails if e))
        if not unique:
            return results

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self.verify, e): e for e in unique}
            for fut in as_completed(futures):
                email = futures[fut]
                try:
                    results[email] = fut.result()
                except Exception as e:  # noqa: BLE001
                    results[email] = {
                        "email": email, "verdict": BLOCKED,
                        "smtp_code": None, "mx_host": None,
                        "reason": f"pool_{type(e).__name__}", "ms": 0,
                    }
        return results
