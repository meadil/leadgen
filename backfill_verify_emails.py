"""
Verify stored email addresses against their mail servers, and record the
verdict on the lead.

This is the step that finally makes the outreach list trustworthy. Until
now every address in the CRM was either unverified or unverifiable
(Hunter.io needed a work email), so 145 addresses were pattern guesses of
unknown quality — and two were already known bad.

    venv\\Scripts\\python.exe backfill_verify_emails.py           # 100
    venv\\Scripts\\python.exe backfill_verify_emails.py 300
    venv\\Scripts\\python.exe backfill_verify_emails.py 300 --dry-run

Design decisions worth knowing:

  * DEFAULT SCOPE IS `guessed_unverified` ONLY. An address a company
    published on its own site is already first-hand evidence; re-probing
    it is lower value than checking a 50/50 pattern guess, and probing is
    not free — it asks a stranger's mail server a question. Pass
    --all to verify everything.

  * ONLY EXPLICIT 550 IS TREATED AS BAD. A catch-all, a greylisted
    server, a timeout, or a server that refuses us all mean "we learned
    nothing", and those leads are kept. A tool that quietly drops leads
    because a mail server was slow is worse than no tool at all.

  * FAIL OPEN. Any error leaves the lead untouched and the run
    continues. A verification pass must never destroy data.
"""
from __future__ import annotations

import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv()

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from supabase import create_client

from email_verify import (
    SmtpVerifier, DELIVERABLE, UNDELIVERABLE, CATCH_ALL, TRY_AGAIN,
    BLOCKED, NO_MX, INVALID, CONFIRMED_BAD, CONFIRMED_GOOD, UNKNOWN,
)

BATCH_WORKERS = 6


def get_client():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not set in .env")
    return create_client(url, key)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    limit = int(args[0]) if args else 100
    dry_run = "--dry-run" in sys.argv
    verify_all = "--all" in sys.argv

    client = get_client()
    verifier = SmtpVerifier(max_workers=BATCH_WORKERS)

    if verify_all:
        # Ordering by a dedicated verified-at column would be cleaner, but
        # it doesn't exist in the schema and adding one means a migration
        # for no real gain — the timestamp is already kept in
        # source_details, which is where every other piece of provenance
        # lives.
        query = (
            client.table("leads")
            .select("id, full_name, email, source, company_domain, source_details")
            .not_.is_("email", None)
        )
    else:
        # Only the addresses that are actually guesses.
        rows_all = (
            client.table("leads")
            .select("id, full_name, email, source, company_domain, source_details")
            .not_.is_("email", None)
            .limit(3000)
            .execute()
            .data
        )
        rows_all = [r for r in rows_all if r.get("email")]
        rows = [
            r for r in rows_all
            if str((r.get("source_details") or {}).get("email_status", ""))
            .startswith("guessed")
        ]
        query = None

    if query is not None:
        rows = query.limit(limit).execute().data
    rows = [r for r in (rows or []) if r.get("email")][:limit]

    scope = "ALL stored addresses" if verify_all else "guessed_unverified only"
    print(f"Verifying {len(rows)} addresses ({scope})"
          f"{'  [DRY RUN]' if dry_run else ''}")
    if not rows:
        print("Nothing to verify.")
        return 0

    t0 = time.time()
    verdicts = Counter()
    updated = 0
    confirmed_bad = []
    by_code = Counter()

    with ThreadPoolExecutor(max_workers=BATCH_WORKERS) as pool:
        futures = {pool.submit(verifier.verify, r["email"]): r for r in rows}
        done = 0
        for fut in as_completed(futures):
            row = futures[fut]
            done += 1
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001
                verdicts["error"] += 1
                print(f"  ! {row['email'][:38]:40} ERROR {type(e).__name__}")
                continue

            v = res.get("verdict")
            verdicts[v] += 1
            by_code[res.get("smtp_code")] += 1

            if v in CONFIRMED_BAD:
                confirmed_bad.append((row["email"], v, res.get("smtp_code")))
                mark = "B"
            elif v in CONFIRMED_GOOD:
                mark = "ok"
            elif v in UNKNOWN:
                mark = "?"
            else:
                mark = "-"

            if done % 10 == 0 or v in CONFIRMED_BAD:
                print(f"  [{done}/{len(rows)}] {mark} {row['email'][:40]:42} "
                      f"{v} code={res.get('smtp_code')} {res.get('ms')}ms")

            if dry_run:
                continue

            details = dict(row.get("source_details") or {})
            details["email_verification"] = {
                "verdict": v,
                "smtp_code": res.get("smtp_code"),
                "mx_host": res.get("mx_host"),
                "reason": res.get("reason", ""),
                "verifier": "self_hosted_smtp",
            }
            details["email_verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            if v in CONFIRMED_GOOD:
                details["email_status"] = "guessed_verified"
                details["email_confidence"] = "verified"
            elif v in CONFIRMED_BAD:
                # Do NOT delete. Record it, so the row is never used for
                # outreach and can be re-checked if the address changes.
                details["email_status"] = f"verified_{v}"
                details["email_confidence"] = "confirmed_bad"
            else:
                # Inconclusive. Say so; never silently mark it good.
                details["email_status"] = "guessed_unverified"
                details["email_confidence"] = "unconfirmed"

            try:
                client.table("leads").update(
                    {"source_details": details}
                ).eq("id", row["id"]).execute()
                updated += 1
            except Exception as e:  # noqa: BLE001
                print(f"  ! db update failed for {row['email'][:30]}: {type(e).__name__}")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.0f}s. {updated} leads updated"
          f"{' (DRY RUN)' if dry_run else ''}.")
    print(f"\nverdicts: {dict(verdicts)}")
    print(f"smtp codes: {dict(by_code)}")
    if confirmed_bad:
        print(f"\nCONFIRMED BAD ({len(confirmed_bad)}) — will bounce, do not send:")
        for email, v, code in confirmed_bad:
            print(f"   {email:44} {v} code={code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
