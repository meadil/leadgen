"""
Chunk 5 — backfill published email addresses into the CRM.

Run on demand, not as part of the main pipeline:

    venv\\Scripts\\python.exe backfill_emails.py            # 50 leads
    venv\\Scripts\\python.exe backfill_emails.py 200        # 200 leads
    venv\\Scripts\\python.exe backfill_emails.py 200 --dry-run

This is deliberately separate from main.py. It is slow (up to 6 HTTP
requests per lead) and it is re-runnable: a company that publishes an
address today can be picked up tomorrow. It also only ever FILLS A GAP —
it never overwrites an address the pipeline already found.

Provenance is recorded, never blurred. A scraped address is stored as
`published_*`, which is a different quality tier from a pattern guess:

    published_company_role  - the company printed it (hello@, support@)
    published_personal      - a real personal address on their own site
    published_individual    - name@owndomain, printed on their site
    guessed_unverified      - firstname.lastname@ pattern, unconfirmed
    guessed_verified        - pattern guess, confirmed by a verifier
"""
from __future__ import annotations

import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv()

import dns.resolver
from supabase import create_client

from email_scrape import EmailScraper, classify_email

MAX_PAGES = 6


def get_client():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not set in .env")
    return create_client(url, key)


def mx_ok(domain: str) -> bool:
    """No point storing an address on a domain that cannot receive mail."""
    try:
        return len(dns.resolver.resolve(domain, "MX", lifetime=5)) > 0
    except Exception:
        return False


PERSONAL_SUFFIXES = (
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "proton.me", "protonmail.com", "fastmail.com", "aol.com", "live.com",
    "me.com", "gmx.com", "yandex.com", "zoho.com",
)


def _process(client, row, scraper, dry_run, stats) -> None:
    """Handle one lead. Sets stats['_last_filled'] = True on success."""
    result = scraper.find(row["company_domain"])
    conf = result.get("confidence")

    if not result.get("email"):
        key = conf if conf in ("not_found", "unreachable") else "other"
        stats[key] = stats.get(key, 0) + 1
        return

    email = result["email"]
    email_domain = email.rpartition("@")[2]
    # A personal address needs no MX check; a company one must accept mail.
    if not email_domain.endswith(PERSONAL_SUFFIXES) and not mx_ok(email_domain):
        stats["no_mx"] = stats.get("no_mx", 0) + 1
        return

    status = f"published_{classify_email(email)}"
    print(f"  + {str(row['full_name'])[:24]:26} {email[:38]:40} {status:28} {result['source_path']}")

    if not dry_run:
        details = dict(row.get("source_details") or {})
        details["email_status"] = status
        details["email_source"] = "company_website"
        details["email_source_path"] = result.get("source_path")
        details["email_confidence"] = "published"
        details["email_alternatives"] = result.get("alternatives") or []
        client.table("leads").update(
            {"email": email, "source_details": details}
        ).eq("id", row["id"]).execute()

    stats["_last_filled"] = True


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    limit = int(args[0]) if args else 50
    dry_run = "--dry-run" in sys.argv

    client = get_client()

    # Only leads that have a domain and NO address. A gap, never an
    # overwrite.
    rows = (
        client.table("leads")
        .select("id, full_name, company_domain, email, source, source_details")
        .is_("email", None)
        .not_.is_("company_domain", None)
        .limit(limit)
        .execute()
        .data
    )
    # supabase `.is_(None)` also needs the null check on domain; filter in python
    rows = [r for r in rows if r.get("company_domain")]
    print(f"Leads with a domain and no email: {len(rows)} (of {limit} requested)")
    if not rows:
        print("Nothing to do.")
        return 0

    scraper = EmailScraper(max_pages=MAX_PAGES)
    filled = 0
    stats = {"published": 0, "not_found": 0, "unreachable": 0, "no_mx": 0, "other": 0, "error": 0}

    for i, row in enumerate(rows, 1):
        # One bad domain must never end a 200-lead backfill. Some hosts
        # raise non-requests exceptions (socket errors, encoding faults),
        # so the guard is deliberately broad and reports what it skipped.
        try:
            _process(client, row, scraper, dry_run, stats)
            if stats.get("_last_filled"):
                filled += 1
                stats["published"] = stats.get("published", 0) + 1
            stats.pop("_last_filled", None)
        except Exception as e:  # noqa: BLE001
            stats["error"] = stats.get("error", 0) + 1
            print(f"  ! {str(row.get('full_name'))[:24]:26} {row.get('company_domain')[:28]:30} "
                  f"ERROR {type(e).__name__}: {str(e)[:60]}")
        if i % 10 == 0:
            print(f"  [{i}/{len(rows)}] filled={filled} {stats}")

    print(f"\nDone. {filled} addresses filled{' (DRY RUN)' if dry_run else ''}.")
    print(f"  {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
