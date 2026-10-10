"""
dev.to adapter — free public API, no key, no signup.

Why this source: it is the only one found so far that gives a REAL PERSON
NAME for ~100% of authors (verified 2026-09-29: 343/343 unique authors
had a real name, vs 4/42 from Hacker News). A name is what outreach
actually needs — a handle produces an unusable salutation.

What dev.to gives per author:
  name, username, website_url, github_username, twitter_username  (article list)
  summary, location, joined_at                                   (profile endpoint)

`summary` is the most valuable field: people write "Founder and CEO of
Clash Nexus AI", "Co-Founder of The Rhyme Game. www.therhymegame.com".
That yields a job title, a company name, and sometimes a domain, none of
which any other free source provides.

Only 39% of authors set a website_url (32% usable), so this source is
better at NAMES and WORDS than at domains. It complements the others
rather than replacing them.
"""
from __future__ import annotations

import os
import re
import time
from urllib.parse import urlparse

import requests

from adapters.base import SourceAdapter
from adapters.hn_adapter import HN_JUNK_HOSTS, _looks_like_shared_host
from adapters.producthunt_adapter import JUNK_HOSTS, PLATFORM_HOSTS
from icp_schema import ICPProfile
from models import RawCandidate

ARTICLES = "https://dev.to/api/articles"
PROFILE = "https://dev.to/api/users/by_username"

# Tags that skew toward people building a business rather than people
# posting tutorials. Verified live: all of these return 200 with volume.
DEVTO_TAGS = [
    "startup", "saas", "indiehackers", "buildinpublic",
    "b2b", "entrepreneurship",
]

INDUSTRY_TO_TAGS = {
    "saas": ["saas", "indiehackers", "buildinpublic", "startup", "b2b"],
    "software": ["saas", "indiehackers", "startup", "buildinpublic"],
    "b2b": ["b2b", "saas", "startup", "indiehackers", "buildinpublic"],
    "technology": ["saas", "startup", "buildinpublic", "indiehackers", "b2b"],
    "fintech": ["fintech", "saas", "startup", "startup"],
}

# A URL written directly in the summary is often the company site, and
# it's the only domain source for authors who left website_url blank.
# Verified real hit: "Co-Founder of The Rhyme Game. www.therhymegame.com"
SUMMARY_URL_RE = re.compile(
    r"(?:https?://|www\.)([a-z0-9][a-z0-9.-]*\.[a-z]{2,24})\b",
    re.IGNORECASE,
)

# Titles, in priority order. The first match becomes job_title — which
# directly feeds the title scorer, so getting it right is worth the effort.
TITLE_PATTERNS = [
    (r"\bco-?founder\s*(?:&|and|,)\s*(?:co-?founder|ceo|cto|coo|cfo)\b", "Co-Founder"),
    (r"\bco-?founder\b", "Co-Founder"),
    (r"\bfounder\s*(?:&|and)\s*(?:ceo|cto)\b", "Founder"),
    (r"\b(?:ceo|chief executive officer)\b", "CEO"),
    (r"\b(?:cto|chief technology officer)\b", "CTO"),
    (r"\b(?:coo|chief operating officer)\b", "COO"),
    (r"\b(?:cfo|chief financial officer)\b", "CFO"),
    (r"\bfounder\b", "Founder"),
    (r"\b(?:indie\s*hacker|solo\s*(?:developer|founder|dev))\b", "Founder"),
    (r"\b(?:maker|creator|entrepreneur|builder)\b", "Founder"),
    (r"\b(?:owner|proprietor)\b", "Owner"),
    (r"\b(?:product\s+manager|product lead)\b", "Product Manager"),
    (r"\b(?:engineer|developer|dev)\b", "Engineer"),
    (r"\b(?:designer|design lead)\b", "Designer"),
    (r"\b(?:marketer|growth|growth lead)\b", "Marketer"),
]
TITLE_RE = [(re.compile(p, re.IGNORECASE), t) for p, t in TITLE_PATTERNS]

# "Founder at X", "CEO of X", "building X", "Maker of X" — the company
# name is what we want out of these.
COMPANY_PATTERNS = [
    r"\b(?:co-?founder|founder|ceo|cto|coo|cfo)\s+(?:of|at)\s+([A-Z][\w&.'-]*(?:\s+[A-Z0-9][\w&.'-]*){0,3})",
    r"\b(?:maker|creator|builder)\s+of\s+([A-Z][\w&.'-]*(?:\s+[A-Z0-9][\w&.'-]*){0,3})",
    r"\bbuilding\s+([A-Z][\w&.'-]*(?:\s+[A-Z0-9][\w&.'-]*){0,3})",
    r"\bwe\s+build\s+([A-Z][\w&.'-]*(?:\s+[A-Z0-9][\w&.'-]*){0,3})",
    r"\bmy\s+(?:startup|saas|company|product|app)\s+([A-Z][\w&.'-]*(?:\s+[A-Z0-9][\w&.'-]*){0,3})",
]
COMPANY_RE = [re.compile(p) for p in COMPANY_PATTERNS]

# Generic phrases that look like company names but aren't.
BAD_COMPANY = {
    "The", "A", "An", "It", "This", "That", "We", "I", "My", "Our",
    "Hacktoberfest", "Dev", "Open", "Source", "Product", "Company",
    "Start", "Startup", "Saas", "Software", "AI", "Web", "The Web",
    "Free", "New", "Best", "Top", "How", "Why", "What", "When",
    "Google", "Github", "GitHub", "Twitter", "LinkedIn", "Notion",
}


def _is_handle(name: str, username: str) -> bool:
    """
    Is this a real person's name, or just their handle pasted into the
    display-name field?

    This matters more than it looks. dev.to lets a user set any display
    name, and plenty set their handle ("audreybuildsai",
    "BenedictVance6863", "MACROGEN"). Feeding those to the email guesser
    produced real garbage: `spuriq.engineering@spuriq.ai`,
    `posting.dude@postingdude.com`, `nikoloz.(@axrisi)@vexrail.com`.

    A real name has: a space, no digits, no underscores, and alphabetic
    words that aren't obviously a brand.
    """
    name = (name or "").strip()
    if not name or name.lower() == (username or "").lower():
        return True
    if " " not in name:
        return True
    if any(ch.isdigit() for ch in name):
        return True
    if "_" in name or "-" in name:
        return True
    # One capitalised chunk only ("MACROGEN", "Product Watch" is fine
    # though) — require every word to look like a name word.
    for word in name.split():
        cleaned = word.strip(".,'-")
        if not cleaned:
            continue
        if not any(c.isalpha() for c in cleaned):
            return True
        if cleaned.isupper() and len(cleaned) > 2:
            return True
    return False


def _company_from_display_name(name: str) -> str | None:
    """
    "SpurIQ Engineering", "Posting Dude", "Product Watch" — org accounts
    masquerading as people. Their display name IS the company, so flag it
    rather than treating "Posting Dude" as a human being.
    """
    org_words = {
        "engineering", "labs", "team", "studio", "studios", "software",
        "solutions", "systems", "tech", "technologies", "digital", "media",
        "works", "group", "collective", "ventures", "capital", "partners",
        "agency", "official", "hq", "watch", "news", "blog", "guy", "dude",
        "gals", "inc", "llc", "ltd", "io", "ai", "dev", "squad",
    }
    words = [w.strip(".,'-").lower() for w in (name or "").split()]
    if not words:
        return None
    if len(words) >= 2 and words[-1] in org_words:
        return " ".join(w.capitalize() for w in words)
    return None


def _clean_host(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = (urlparse(url if "://" in url else f"https://{url}").hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    for prefix in ("www.", "m.", "en."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host or None


def _is_junk_host(host: str) -> bool:
    if host in HN_JUNK_HOSTS or host in JUNK_HOSTS:
        return True
    return any(host == p or host.endswith("." + p) for p in PLATFORM_HOSTS)


class DevToAdapter(SourceAdapter):
    name = "devto"

    def __init__(self, profiles_per_run: int = 40):
        """
        `profiles_per_run` caps the extra profile calls.

        The article list gives a real name, website and socials for
        everyone. The profile endpoint adds `summary` (the job title and
        company goldmine) and `location`, but it is one request per
        author and it is noticeably slow, so it is spent only on authors
        who already look like a fit.
        """
        self.profiles_per_run = profiles_per_run
        self.session = requests.Session()
        # No browser UA — see LESSONS_LEARNED L1. dev.to doesn't need one.
        self._profile_cache: dict[str, dict] = {}
        # Paging checkpoint, injected by main.py (see paging.py).
        self._resume: dict | None = None
        self._on_checkpoint = None
        self._pages_per_tag = 2

    # ------------------------------------------------------------------ #
    def search(self, icp: ICPProfile, limit: int = 50) -> list[RawCandidate]:
        tags = INDUSTRY_TO_TAGS.get(icp.industry.lower(), DEVTO_TAGS)
        per_tag = max(10, min(100, (limit * 2) // max(1, len(tags))))

        # Collect unique authors across tags first — one author posts many
        # articles and must only become one lead.
        authors: dict[str, dict] = {}
        resume = self._resume or {}
        next_at: dict[str, dict] = {}

        for tag in tags:
            if len(authors) >= limit * 3:
                break
            start_page = int((resume.get(f"tag:{tag}") or {}).get("page", 1))
            if start_page > 1:
                print(f"    (devto '{tag}' resuming at page {start_page})")

            pages_read = 0
            exhausted = False
            for offset in range(self._pages_per_tag):
                page = start_page + offset
                articles = self._articles(tag, per_tag, page=page)
                if not articles:
                    exhausted = True
                    break
                pages_read += 1
                for article in articles:
                    user = article.get("user") or {}
                    uname = user.get("username")
                    if not uname or uname in authors:
                        continue
                    display = (user.get("name") or "").strip()
                    # A real name is the whole point of this source.
                    # Without one there's no reason to prefer dev.to over
                    # anywhere else — and guessing an email from a handle
                    # produces garbage addresses, so those are dropped.
                    if not display or _is_handle(display, uname):
                        continue
                    authors.setdefault(uname, {
                        "user": user,
                        "articles": [article],
                    })
                if len(authors) >= limit * 3:
                    break

            # dev.to's article API is a rolling feed — older posts stay
            # available, so once exhausted there is nothing left to read
            # and we mark the tag done rather than re-reading it forever.
            if exhausted or (pages_read < self._pages_per_tag and not authors):
                next_at[f"tag:{tag}"] = {"page": 1, "done": True}
            else:
                next_at[f"tag:{tag}"] = {
                    "page": start_page + max(pages_read, 1),
                    "done": False,
                }
            print(f"  (devto '{tag}': {len(authors)} unique authors so far)")

        candidates: list[RawCandidate] = []
        budget = self.profiles_per_run

        for uname, entry in authors.items():
            if len(candidates) >= limit:
                break
            user = entry["user"]

            # Decide who deserves a profile call, using only free data.
            summary_guess = ""
            website = (user.get("website_url") or "").strip()
            if budget > 0 and (website or user.get("github_username")):
                budget -= 1
                profile = self._profile(uname)
                summary_guess = (profile.get("summary") or "").strip()
                location = (profile.get("location") or "").strip()
            else:
                location = ""

            text = summary_guess
            job_title = self._extract_title(text)
            display = (user.get("name") or uname).strip()
            company_name = self._extract_company(text)
            # An org account's display name IS its company name. These are
            # not people, so enrichment must not guess a person's email
            # from them — that produced `spuriq.engineering@spuriq.ai`
            # and `posting.dude@postingdude.com`.
            org_name = _company_from_display_name(display)
            is_org = org_name is not None
            if org_name:
                company_name = org_name
                if not job_title:
                    job_title = "Founder"

            # Domain priority: explicit website_url, then a URL written
            # into the summary, then the company name via DNS-guarded
            # lookup in enrichment.
            domain = None
            domain_from = None
            host = _clean_host(website)
            if host and not _is_junk_host(host) and not _looks_like_shared_host(host):
                domain = host
                domain_from = "website_url"
            else:
                m = SUMMARY_URL_RE.search(text)
                if m:
                    host = _clean_host(m.group(1))
                    if host and not _is_junk_host(host) and not _looks_like_shared_host(host):
                        domain = host
                        domain_from = "summary_url"

            # The company name is the fallback when there's no domain.
            if not company_name and domain:
                company_name = domain.split(".")[0].title()

            # A bio built from the summary + what they write about. The
            # article titles are genuine product/industry language, which
            # is exactly what the industry scorer needs.
            article_bits = " ".join(
                (a.get("title") or "") for a in entry["articles"][:5]
            )
            desc_bits = " ".join(
                (a.get("description") or "") for a in entry["articles"][:5]
            )
            bio = ". ".join(filter(None, [text, article_bits, desc_bits]))[:900]

            candidates.append(RawCandidate(
                full_name=(user.get("name") or uname).strip(),
                job_title=job_title,
                company_name=company_name,
                company_domain=domain,
                github_url=(
                    f"https://github.com/{user['github_username']}"
                    if user.get("github_username") else None
                ),
                twitter_url=(
                    f"https://twitter.com/{user['twitter_username']}"
                    if user.get("twitter_username") else None
                ),
                geography_city=location or None,
                source="devto",
                source_details={
                    "bio": bio,
                    "devto_username": uname,
                    "devto_profile": f"https://dev.to/{uname}",
                    "summary": text,
                    "devto_location": location or None,
                    "github_username": user.get("github_username"),
                    "twitter_username": user.get("twitter_username"),
                    "domain_from": domain_from,
                    # Not a person — enrichment must not guess a personal
                    # email from an org account's name.
                    "is_org_account": is_org,
                    # Stable key: the author, not the article. A dev.to
                    # author who posts weekly is ONE lead (L21).
                    "source_url": f"https://dev.to/{uname}",
                    "article_urls": [a.get("url") for a in entry["articles"][:3] if a.get("url")],
                    "article_count": len(entry["articles"]),
                },
            ))

        if self._on_checkpoint and next_at:
            self._on_checkpoint(next_at)

        return candidates[:limit]

    # ------------------------------------------------------------------ #
    def _articles(self, tag: str, per_page: int, page: int = 1) -> list[dict]:
        try:
            resp = self.session.get(
                ARTICLES,
                params={"tag": tag, "per_page": min(per_page, 100), "page": page},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json() or []
        except (requests.RequestException, ValueError) as e:
            print(f"    (devto '{tag}' page {page} failed: {e})")
            return []

    def _profile(self, username: str) -> dict:
        if username in self._profile_cache:
            return self._profile_cache[username]
        data: dict = {}
        for attempt in range(2):
            try:
                resp = self.session.get(PROFILE, params={"url": username}, timeout=20)
                if resp.status_code == 200:
                    data = resp.json() or {}
                    break
                if resp.status_code in (429, 500, 502, 503):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            except requests.RequestException:
                time.sleep(1.0)
        self._profile_cache[username] = data
        return data

    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_title(text: str) -> str | None:
        for pattern, title in TITLE_RE:
            if pattern.search(text or ""):
                return title
        return None

    @staticmethod
    def _extract_company(text: str) -> str | None:
        if not text:
            return None
        for pattern in COMPANY_RE:
            m = pattern.search(text)
            if not m:
                continue
            name = (m.group(1) or "").strip(" .,-—–")
            if len(name) < 2:
                continue
            first = name.split()[0]
            if first in BAD_COMPANY or name.upper() == first.upper():
                continue
            # A company name shouldn't be a bare number or symbol.
            if not any(c.isalnum() for c in name):
                continue
            return name
        return None
