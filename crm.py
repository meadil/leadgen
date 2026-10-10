"""
Everything that talks to Supabase lives here — dedup checks (Stage 4)
and writing qualified leads (Stage 5).
"""
from __future__ import annotations

import os
import re
from typing import Optional

from rapidfuzz import fuzz
from supabase import Client, create_client

from icp_schema import ICPProfile
from models import RawCandidate


def get_client() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not set in .env")
    return create_client(url, key)


def upsert_icp_profile(client: Client, icp: ICPProfile) -> str:
    """Insert the ICP if it's new, or return its id if it already exists by name."""
    existing = client.table("icp_profiles").select("id").eq("name", icp.icp_name).execute()
    if existing.data:
        return existing.data[0]["id"]

    inserted = client.table("icp_profiles").insert({
        "name": icp.icp_name,
        "definition": icp.model_dump(mode="json"),
        "min_score_threshold": icp.minimum_score_threshold,
    }).execute()
    return inserted.data[0]["id"]


def _norm_name(name: str | None) -> str:
    """
    Normalise a person's name for fuzzy comparison.

    This was the biggest dedup bug in the project and it was invisible:
    `rapidfuzz.fuzz.ratio` is CASE SENSITIVE, so "SERGEY RUDENKO" and
    "Sergey Rudenko" scored 21/100 — nowhere near the >90 threshold —
    and the same human was inserted twice. GitHub users in particular
    write their names in every casing imaginable.

    Also handles two more real-world variants seen in our own data:
      - diacritics:  "İlhan Neğiş"      -> "ilhan negis"
      - parenthetical: "Vlad (Volodymyr Shiyan)" -> keeps both forms
    """
    if not name:
        return ""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("ß", "ss").replace("ø", "o").replace("đ", "d")
    s = re.sub(r"[^a-z0-9\s]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _name_forms(name: str | None) -> list[str]:
    """
    Comparison forms for one name: the whole thing, plus the part inside
    parentheses. "Vlad (Volodymyr Shiyan)" should match "Volodymyr
    Shiyan" — a real dev.to display name that scored 82 and was missed.
    """
    base = _norm_name(name)
    if not base:
        return []
    forms = {base}
    m = re.search(r"\(([^)]+)\)", str(name or ""))
    if m:
        inner = _norm_name(m.group(1))
        if inner:
            forms.add(inner)
    return sorted(forms)


def _same_person(a: str | None, b: str | None) -> bool:
    """
    Do these two name strings refer to the same human?

    The threshold is deliberately strict (95, not 90) and
    `token_set_ratio` is NOT used. That was tuned against the actual
    risk: merging two different people into one row loses a lead
    PERMANENTLY and silently, whereas missing a duplicate only leaves an
    extra row that a later, stronger key can catch.

    Measured on real pairs at threshold 90:
      "Beni Ganz" vs "Beni Ganze"  -> 94.7, matched  (different people)
      "Jan Jansen" vs "Jan Jansen van der Berg" -> matched (arguably the
        same person, but a Dutch name variant is not proof)
    At 95 with plain ratio, the real same-person pairs below still match
    and the first stops matching.
    """
    fa, fb = _name_forms(a), _name_forms(b)
    if not fa or not fb:
        return False
    for x in fa:
        for y in fb:
            if fuzz.ratio(x, y) > 95:
                return True
    return False


def _safe_query(fn, label: str) -> Optional[list]:
    """
    Run one dedup query, surviving a network failure.

    `find_duplicate` fires up to 5 queries per candidate, so a single
    200-candidate run is ~1000 round trips to Supabase. One dropped
    connection anywhere in that used to kill the entire run and lose
    every lead not yet written (hit live 2026-09-29).

    A failed check degrades to "no match found" rather than raising. That
    is deliberately fail-OPEN: the worst case is one duplicate row, which
    the next run's dedup or a cleanup pass removes. Failing closed — or
    crashing — would mean losing a run's worth of real leads.
    """
    try:
        result = fn()
        return result.data
    except Exception as e:  # noqa: BLE001 - any transport/API failure
        print(f"    (dedup check '{label}' unavailable: {type(e).__name__})")
        return None


def find_duplicate(client: Client, candidate: RawCandidate) -> Optional[dict]:
    """
    Match against existing leads, cheapest/most reliable key first.

    The order matters because a candidate can have NONE of the good keys:
    a Product Hunt maker with no resolvable domain arrives with no email
    and no GitHub URL, and the fuzzy name+domain check then can't run
    either because there's no domain. That case used to re-insert on
    every single run (the "Hackerman" bug) and is why the fallbacks below
    exist — verified live: 6 duplicate groups had no domain AND no email
    and slipped straight through every check.

    Keys, in order:
      1. email          — exact, matches the DB's unique constraint
      2. github_url     — stable per person
      3. source_url     — the source record (PH post, HN item, dev.to author)
      4. name + domain  — fuzzy, catches the same person on two sources
      5. name + company  — last resort when there is no domain at all

    Network failures inside any single check are swallowed (see
    _safe_query) so a flaky connection costs a possible duplicate rather
    than the whole run.
    """
    if candidate.email:
        rows = _safe_query(
            lambda: client.table("leads").select("id, full_name").eq(
                "normalized_email", candidate.email.strip().lower()
            ).execute(),
            "email",
        )
        if rows:
            return rows[0]

    if candidate.github_url:
        rows = _safe_query(
            lambda: client.table("leads").select("id, full_name").eq(
                "github_url", candidate.github_url
            ).execute(),
            "github_url",
        )
        if rows:
            return rows[0]

    # Stable per-record key. Every adapter writes this into
    # source_details.source_url (a Product Hunt post, a Hacker News item,
    # a dev.to author). PostgREST can filter straight into the JSON path,
    # so this is an indexed-style lookup rather than a table scan.
    # (Verified: `.filter('source_details->>source_url', 'eq', url)` works
    # over the REST API, so no schema migration is required.)
    source_url = (candidate.source_details or {}).get("source_url")
    if source_url:
        rows = _safe_query(
            lambda: client.table("leads").select("id, full_name").filter(
                "source_details->>source_url", "eq", source_url
            ).execute(),
            "source_url",
        )
        if rows:
            return rows[0]

    if candidate.full_name and candidate.company_domain:
        rows = _safe_query(
            lambda: client.table("leads").select("id, full_name").eq(
                "company_domain", candidate.company_domain
            ).execute(),
            "name+domain",
        )
        for row in rows or []:
            if _same_person(row.get("full_name"), candidate.full_name):
                return row

    # Last resort: no domain, so match on name + company name instead.
    # This is the key that stops the same PH maker being inserted 6 times
    # because their product had no resolvable website.
    if candidate.full_name and candidate.company_name:
        rows = _safe_query(
            lambda: client.table("leads").select("id, full_name, company_name").eq(
                "company_name", candidate.company_name
            ).execute(),
            "name+company",
        )
        for row in rows or []:
            if _same_person(row.get("full_name"), candidate.full_name):
                return row

    return None


def is_linked_to_icp(client: Client, lead_id: str, icp_id: str) -> bool:
    """True if this lead already counts toward this ICP (lead_icps row exists)."""
    try:
        rows = (
            client.table("lead_icps")
            .select("lead_id")
            .eq("lead_id", lead_id)
            .eq("icp_profile_id", icp_id)
            .execute()
        )
    except Exception as e:  # noqa: BLE001
        # Unknown, not "no". The junction row is bookkeeping, not the
        # lead — failing to check it must not discard a real lead.
        print(f"    (lead_icps check unavailable: {type(e).__name__})")
        return False
    return bool(rows.data)


def link_lead_to_icp(
    client: Client, lead_id: str, icp_id: str, score: float, breakdown: dict
) -> bool:
    """Tag an existing lead row as matching an ICP. Returns True if new."""
    if is_linked_to_icp(client, lead_id, icp_id):
        return False
    try:
        client.table("lead_icps").insert({
            "lead_id": lead_id,
            "icp_profile_id": icp_id,
            "score": score,
            "score_breakdown": breakdown,
        }).execute()
    except Exception as e:  # noqa: BLE001
        # The lead itself exists; only the per-ICP tag is missing. A
        # duplicated junction row later is a far smaller problem than
        # losing the lead.
        print(f"    (could not link lead to ICP: {type(e).__name__})")
        return False
    return True


def insert_lead(
    client: Client, candidate: RawCandidate, icp_id: str, score: float, breakdown: dict
) -> Optional[str]:
    """
    Write one qualified lead. Returns its id, or None if the write failed.

    Returns None rather than raising so one failed insert costs one lead
    instead of the run. The lead is still returned as "not written" so
    main.py can count it separately — silently dropping writes would make
    the run look successful while losing data.
    """
    try:
        inserted = client.table("leads").insert({
            "icp_profile_id": icp_id,
            "full_name": candidate.full_name,
            "job_title": candidate.job_title,
            "email": candidate.email,
            "linkedin_url": candidate.linkedin_url,
            "github_url": candidate.github_url,
            "twitter_url": candidate.twitter_url,
            "company_name": candidate.company_name,
            "company_domain": candidate.company_domain,
            "geography_country": candidate.geography_country,
            "geography_city": candidate.geography_city,
            "source": candidate.source,
            "source_details": candidate.source_details,
            "score": score,
            "score_breakdown": breakdown,
            "status": "new",
        }).execute()
    except Exception as e:  # noqa: BLE001
        print(f"  ! insert FAILED for {str(candidate.full_name)[:28]}: "
              f"{type(e).__name__}: {str(e)[:70]}")
        return None

    if not inserted.data:
        print(f"  ! insert returned no row for {str(candidate.full_name)[:28]}")
        return None

    lead_id = inserted.data[0]["id"]
    # Per-ICP tracking: the lead row is global (contact dedup), but every
    # ICP it matches gets its own lead_icps row with its own score.
    link_lead_to_icp(client, lead_id, icp_id, score, breakdown)
    return lead_id
