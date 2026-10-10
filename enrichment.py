"""
Stage 3 — enrichment.

Fills gaps in a RawCandidate before scoring: resolves a company domain
from a company name, guesses + sanity-checks an email address, and pulls
a rough job title out of bio text when the source didn't provide one.

Everything here is free and deterministic. Guessed emails are marked
unverified in source_details — nothing here confirms a real mailbox,
only that the domain can receive mail at all (MX record check).

Order matters: a domain the SOURCE already gave us is trusted, a domain
hinted by a subdomain label is tried next, and a name search is the last
resort. Clearbit autocomplete is last because it confidently returns the
wrong company for small SaaS names — asked about "Brio AI" it returned an
air purifier company (PLAN.md F7).
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

import dns.resolver
import requests

from models import RawCandidate
from email_verification import verify_email

CLEARBIT_AUTOCOMPLETE = "https://autocomplete.clearbit.com/v1/companies/suggest"

TITLE_KEYWORDS = {
    "founder": ["founder", "co-founder", "cofounder"],
    "cto": ["cto", "chief technology officer"],
    "ceo": ["ceo", "chief executive officer"],
    "engineer": ["engineer", "developer", "swe"],
}

# Suffixes worth trying on a hint, cheapest/most likely first.
DOMAIN_SUFFIXES = [".com", ".io", ".ai", ".co", ".app", ".dev", ".net", ".org"]

# Common TLDs of a bare label, for `label` -> `label.com` style guesses.
GENERIC_SUFFIXES = {".com", ".io", ".ai", ".co", ".app", ".dev", ".net", ".org",
                    ".xyz", ".tech", ".sh", ".me", ".gg", ".to", ".so", ".in"}


def guess_company_domain(company_name: str) -> Optional[str]:
    """
    Free lookup, no API key required — but UNRELIABLE for small companies.

    Kept as a last resort only. Verified 2026-09-29: returns [] for
    "OrbitFlow 360", "Cafiyn" and "Compliance Posture" (all real,
    all small SaaS), and returns a wrong-company match for "Brio AI".
    Every result is sanity-checked against the company name by the
    caller before it is trusted.
    """
    try:
        resp = requests.get(CLEARBIT_AUTOCOMPLETE, params={"query": company_name}, timeout=8)
        resp.raise_for_status()
        results = resp.json()
        if results:
            return results[0].get("domain")
    except requests.RequestException:
        pass
    return None


def domain_accepts_mail(domain: str) -> bool:
    """Cheap sanity check: does this domain have MX records at all?
    Filters out obviously dead/typo'd domains, doesn't verify a mailbox."""
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        return len(answers) > 0
    except Exception:
        return False


def _name_tokens(*values: str | None) -> set[str]:
    out: set[str] = set()
    for v in values:
        if not v:
            continue
        for part in re.split(r"[^a-z0-9]+", str(v).lower()):
            if len(part) > 2:
                out.add(part)
    return out


def domain_matches_company(domain: str, company_name: str | None) -> bool:
    """
    Does this domain plausibly belong to this company?

    Guards against a name-search returning a same-initial but different
    company. Only meaningful when we have a company name to compare
    against; with no name, we can't judge, so we don't reject.

    Containment alone is NOT enough. "Brio AI" is a substring of
    "brioairpurifier.com" (verified: Clearbit returns exactly that for
    "Brio AI"), so a token match has to also DOMINATE the domain label —
    the matched part has to account for most of the label, not a corner
    of it.
    """
    if not company_name:
        return True
    label = domain.split(".")[0].lower()
    # The registrable label can be hyphenated or a mashup, so compare
    # compact forms too: "closeclerk" vs "close-clerk".
    compact = re.sub(r"[^a-z0-9]", "", label)
    name_tokens = _name_tokens(company_name)
    if not name_tokens or not compact:
        return True

    def dominant(token: str) -> bool:
        if token not in compact:
            return False
        # An exact label match is always good enough.
        if token == compact:
            return True
        coverage = len(token) / len(compact)
        # Short tokens ("ply" in "getply") are allowed to be a smaller
        # share; long ones must dominate, which is what rejects
        # "brio" inside "brioairpurifier" (0.25 coverage).
        floor = 0.4 if len(token) <= 4 else 0.6
        return coverage >= floor

    if any(dominant(token) for token in name_tokens):
        return True

    # Company name itself may be domain-like: "Compliance Posture" vs
    # complianceposture.ai. Requires the two to be close in length —
    # otherwise "Brio AI" ("brioai") sits inside "brioairpurifier".
    joined = re.sub(r"[^a-z0-9]", "", (company_name or "").lower())
    if joined and len(joined) >= len(compact) * 0.75:
        if joined in compact or compact in joined:
            return True

    return False


def _domain_from_hint(hint: str | None, company_name: str | None) -> str | None:
    """
    Turn a subdomain-label hint into a real domain if one exists.

    `closeclerk` -> tries closeclerk.com / .io / .ai ... and returns the
    first that resolves in DNS. Used when a product was found hosted on
    `closeclerk.pages.dev` and we suspect closeclerk.com is the real one.
    """
    if not hint:
        return None
    hint = re.sub(r"[^a-z0-9-]", "", hint.lower()).strip("-")
    if not hint or "." in hint:
        return None

    candidates = [hint + s for s in DOMAIN_SUFFIXES]
    for cand in candidates:
        # A DNS lookup is the test: a real company domain resolves, a
        # made-up one gets NXDOMAIN.
        try:
            dns.resolver.resolve(cand, "A", lifetime=4)
            if domain_matches_company(cand, company_name):
                return cand
        except Exception:
            continue
    return None


def _ascii_name_parts(full_name: str) -> list[str]:
    """
    Split a name into ASCII first/last parts usable in an address.

    Real names carry characters that cannot appear in an email local
    part — verified failures from live data: "İlhan Neğiş" produced
    `i̇lhan.neğiş@eringen.com` (a combining dot above a capital I, plus
    a non-ASCII ğ). Transliterating crudely would be worse: it silently
    invents an address that will never arrive. So any name part that
    isn't plain ASCII alphabetic is DROPPED, and a name with too few
    usable parts produces no guesses at all.
    """
    if not full_name:
        return []
    words = re.split(r"\s+", full_name.strip())
    parts: list[str] = []
    for w in words:
        # Keep only a-z/A-Z — no digits, no dots, no symbols, no non-ASCII.
        cleaned = re.sub(r"[^A-Za-z]", "", w)
        if len(cleaned) >= 2:
            parts.append(cleaned.lower())
    return parts


def guess_email_permutations(full_name: str, domain: str) -> list[str]:
    """
    Build candidate firstname.lastname@ style addresses.

    Only ever called with a verified real name — the adapters are
    responsible for rejecting handles (see devto_adapter._is_handle),
    because a handle produces addresses that look plausible and bounce:
    `spuriq.engineering@spuriq.ai`, `posting.dude@postingdude.com`.
    """
    parts = _ascii_name_parts(full_name)
    if len(parts) < 2:
        return []
    first, last = parts[0], parts[-1]

    # Skip the whole guess when the "name" is really the domain again —
    # a solo founder on a personal site like hisashi.space gave
    # `hisashi.space@hisashi.space`, which is not a person.
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0].lower())
    if label and label in (first + last, first, last):
        return []

    return [
        f"{first}.{last}@{domain}",
        f"{first}{last}@{domain}",
        f"{first[0]}{last}@{domain}",
        f"{first}@{domain}",
    ]


def infer_title_from_bio(bio: str) -> Optional[str]:
    bio_lower = bio.lower()
    for title, keywords in TITLE_KEYWORDS.items():
        if any(kw in bio_lower for kw in keywords):
            return title
    return None


def _domain_from_website_url(url: str | None) -> str | None:
    """(1.8) A maker's own site, when they gave us one."""
    if not url:
        return None
    try:
        host = urlparse(url if "://" in url else f"https://{url}").hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    if host in ("twitter.com", "x.com", "linkedin.com", "github.com",
                "instagram.com", "facebook.com", "medium.com", "substack.com",
                "producthunt.com", "linktr.ee", "youtube.com", "beacons.ai",
                "gumroad.com", "notion.so", "calendly.com"):
        return None
    return host if "." in host else None


def enrich(candidate: RawCandidate) -> RawCandidate:
    if not candidate.job_title:
        bio = candidate.source_details.get("bio") or ""
        inferred = infer_title_from_bio(bio)
        if inferred:
            candidate.job_title = inferred

    # (1.8) A maker's personal/company site, if the source captured one.
    if not candidate.company_domain:
        maker_domain = _domain_from_website_url(
            candidate.source_details.get("maker_website")
        )
        if maker_domain and domain_matches_company(
            maker_domain, candidate.company_name
        ):
            candidate.company_domain = maker_domain
            candidate.source_details["domain_from"] = "maker_website"

    # (1.7a) Platform-subdomain hint: a product found at
    # `closeclerk.pages.dev` almost certainly owns `closeclerk.com`.
    # DNS-verified, so unlike Clearbit this can't invent a company.
    if not candidate.company_domain:
        hinted = _domain_from_hint(
            candidate.source_details.get("domain_hint"), candidate.company_name
        )
        if hinted:
            candidate.company_domain = hinted
            candidate.source_details["domain_from"] = "subdomain_hint"

    # (1.7) Last resort: a name search, sanity-checked before it's trusted.
    if candidate.company_name and not candidate.company_domain:
        domain = guess_company_domain(candidate.company_name)
        # Clearbit will happily return a different company with a similar
        # name, so never accept its answer unchecked.
        if domain and domain_matches_company(domain, candidate.company_name):
            candidate.company_domain = domain
            candidate.source_details["domain_from"] = "clearbit"
        elif domain:
            candidate.source_details["clearbit_rejected"] = domain

    if not candidate.email and candidate.full_name and candidate.company_domain:
        # Never invent a personal address from something that isn't a
        # person: an org account's name ("SpurIQ Engineering"), or a
        # company-level role contact whose "name" is the company itself
        # ("Polar", "Frappe"). Guessing produced spuriq.engineering@spuriq.ai.
        is_person = not (
            candidate.source_details.get("is_org_account")
            or candidate.source_details.get("is_company_contact")
        )
        if is_person:
            if domain_accepts_mail(candidate.company_domain):
                guesses = guess_email_permutations(candidate.full_name, candidate.company_domain)
                if guesses:
                    best_guess = guesses[0]
                    result = verify_email(best_guess)
                    candidate.email = best_guess
                    candidate.source_details["email_alternatives"] = guesses[1:]
                    if result:
                        candidate.source_details["email_verification"] = result
                        # Hunter's `result` field: deliverable / risky / undeliverable / unknown
                        verdict = result.get("result", "unknown")
                        candidate.source_details["email_status"] = (
                            "guessed_verified" if verdict == "deliverable" else f"guessed_{verdict}"
                        )
                    else:
                        candidate.source_details["email_status"] = "guessed_unverified"

    return candidate
