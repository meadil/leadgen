"""
GitHub org/repo adapter — company-level source, free, uses GITHUB_TOKEN.

WHY THIS REPLACED WIKIDATA (Chunk 4 as originally planned)
---------------------------------------------------------
Wikidata was investigated first and rejected on evidence, not opinion
(PLAN.md F31): querying founders with a company website returned
YouTube, USC, the BBC, Intel, Lufthansa and Deutsche Telekom. Filtered to
our actual ICP (2-100 employees) the results were a manga publisher, a
game studio, an NGO and a telco with "2 employees". A query for founders
of software-industry companies returned **0 rows**, and Wikidata carries
**no email addresses at all** (P968 is empty for every company). It is an
encyclopedia, not a startup database. Shipping an adapter for it would
have produced leads we could never contact.

GitHub repository search turns out to serve the same goal far better:
every repo carries a `homepage` (the company's real domain) and an
`owner` whose profile gives the company name, website and often a
contact email. Measured 2026-09-29: 704 repos across 10 ICP topics →
**433 with a usable homepage (61%)**, 275 of them org-owned.

IMPORTANT — these are COMPANIES, not named people.
A GitHub org is not a person with a first and last name. `name` on an
org profile is usually the company name ("PostHog"), and `email` is a
role address (hello@, contact@, founder@). That is recorded honestly in
`is_company_contact` rather than dressed up as a named founder, because
a fake name corrupts every salutation and merge downstream.
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

REPO_SEARCH = "https://api.github.com/search/repositories"
USER_API = "https://api.github.com/users/{}"

# Topics that skew toward an actual commercial software product.
# Every one of these was verified live to return results on 2026-09-29.
# Adding a topic here is cheap and reversible — but re-test it first, the
# same lesson as Product Hunt (F2).
TOPIC_QUERIES = {
    "saas": [
        "topic:saas stars:>10",
        "topic:producthunt stars:>20",
        "topic:analytics stars:>40",
        "topic:automation stars:>50",
        "topic:crm stars:>20",
        "topic:marketing stars:>40",
        "topic:payments stars:>30",
        "topic:no-code stars:>30",
    ],
    "software": [
        "topic:saas stars:>10",
        "topic:devtools stars:>100",
        "topic:analytics stars:>40",
        "topic:automation stars:>50",
    ],
    "b2b": [
        "topic:crm stars:>20",
        "topic:saas stars:>10",
        "topic:erp stars:>20",
        "topic:workflow stars:>30",
    ],
    "technology": [
        "topic:ai stars:>500",
        "topic:devtools stars:>100",
        "topic:saas stars:>10",
    ],
}

# A starred OSS library is not a sales prospect. These read like real
# commercial products and are the ones worth messaging.
B2B_SIGNALS = re.compile(
    r"\b(?:"
    r"platform|saas|enterprise|workspace|copilot|assistant|agent|"
    r"automation|integrat\w*|api|analytics|dashboard|crm|erp|"
    r"alternative to|billing|invoic\w*|payment\w*|subscription|"
    r"all-?in-?one|turn-?key|out-?of-?the-?box|"
    r"for teams|for business|team|teams|company|companies|"
    r"self-?hosted|deploy|hosting|infrastructure|observab\w*|"
    r"security|auth\w*|identity|compliance|"
    r"productivity|collaborat\w*|project management|"
    r"customer|support|helpdesk|"
    r"generate|generator|builder|converter|parser|"
    r"marketing|sales|growth|email|"
    r"database|data|storage|search|"
    r"free and open source alternative|"
    r"open source alternative|"
    r"business|enterprise-grade|production-?ready"
    r")",
    re.IGNORECASE,
)

# Hosts that are never a company's commercial site. A GitHub repo's
# homepage is often docs, a mirror, or a wiki.
DOC_HOSTS = {
    "readthedocs.io", "readthedocs.org", "gitbook.io", "gitbook.com",
    "shields.io", "travis-ci.org", "travis-ci.com", "codecov.io",
    "coveralls.io", "npmjs.com", "npmjs.org", "pypi.org", "pypi.io",
    "rubygems.org", "packagist.org", "crates.io", "docker.com",
    "hub.docker.com", "dockerhub.com", "sourceforge.net", "codeberg.org",
    "launchpad.net", "sf.net", "releasemonitor.org", "jitpack.io",
    "www.npmjs.com", "opensource.org", "opensource.guide", "w3.org",
    "creativecommons.org", "spdx.org", "wikipedia.org", "en.wikipedia.org",
    "stackexchange.com", "stackoverflow.com", "discord.com", "discord.gg",
    "slack.com", "gitter.im", "t.me", "twitter.com", "x.com", "linkedin.com",
    "facebook.com", "instagram.com", "youtube.com", "medium.com",
    "substack.com", "hashnode.dev", "dev.to", "notion.site", "notion.so",
    "arxiv.org", "doi.org", "doi.org", "google.com", "microsoft.com",
    "apple.com", "amazon.com", "cloud.google.com", "azure.com",
    "apache.org", "apache.org", "python.org", "python.org", "mozilla.org",
    "kernel.org", "gnu.org", "debian.org", "ubuntu.com", "linux.org",
    "k8s.io", "kubernetes.io", "cncf.io", "ieee.org", "acm.org", "dl.acm.org",
    # Marketplaces and shared platforms — a repo's `homepage` pointing here
    # is a listing, not the author's own company. Verified: a founder's
    # profile listed codecanyon.net, which is Envato's marketplace.
    "codecanyon.net", "codecanyon.com", "envato.com", "themeforest.net",
    "creativemarket.com", "gumroad.com", "udemy.com", "teachable.com",
    "kajabi.com", "hotmart.com", "sellkit.com", "etsy.com", "fiverr.com",
    "upwork.com", "freelancer.com", "contra.com", "toptal.com",
    "producthunt.com", "betalist.com", "uneed.best", "alternativeto.net",
    "sourceforge.net", "capterra.com", "g2.com", "trustradius.com",
    "crunchbase.com", "wellfound.com", "angel.co", "pitchbook.com",
}


def _is_person_name(name: str | None) -> bool:
    """
    Does this look like a human's name rather than a company?

    Needed because a GitHub owner is EITHER an Organization (name is the
    company) or a User (name is often a real person). Both appear in the
    same search results and they must be recorded differently: a User
    owner is a genuine contact, not a "role address".

    ALL-CAPS is allowed on purpose: GitHub users very often write their
    name in capitals ("SERGEY RUDENKO"), and an earlier version rejected
    those as brand names, which threw away real people. Caps are
    disambiguated by the word list below instead.
    """
    if not name:
        return False
    name = name.strip()
    if " " not in name:
        return False
    if any(ch.isdigit() for ch in name):
        return False
    words = []
    for word in name.split():
        cleaned = word.strip(".,'-")
        if not cleaned:
            continue
        if not any(c.isalpha() for c in cleaned):
            return False
        words.append(cleaned.lower())
    if not words:
        return False
    # A "name" made of brand words is a company, not a person.
    if any(w in COMPANY_WORDS for w in words):
        return False
    return True


# Words that give away a company/organisation name. Checked against every
# word of a candidate name, so "SERGEY RUDENKO" passes but "OPEN SOURCE
# LABS" and "POLAR" do not.
COMPANY_WORDS = {
    "software", "labs", "lab", "tech", "technologies", "technology",
    "systems", "solutions", "digital", "media", "works", "group",
    "collective", "ventures", "capital", "partners", "agency", "official",
    "hq", "watch", "news", "blog", "open", "source", "opensource", "sourcecode",
    "inc", "llc", "ltd", "gmbh", "corp", "corporation", "co", "company",
    "studio", "studios", "agency", "io", "ai", "dev", "developers",
    "squad", "team", "foundation", "institute", "university", "college",
    "project", "projects", "tools", "toolkit", "platform", "platforms",
    "cloud", "data", "net", "networks", "web", "app", "apps",
    "official", "community", "org", "foundation", "guild", "collective",
    "free", "open", "the", "my", "our", "your",
}


def _clean_host(url: str | None) -> str | None:
    if not url:
        return None
    raw = url.strip()
    u = raw if "://" in raw else f"https://{raw}"
    try:
        host = (urlparse(u).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    for prefix in ("www.", "m.", "docs.", "git.", "app."):
        if host.startswith(prefix) and host.count(".") > 1:
            host = host[len(prefix):]
            break
    return host or None


def _is_junk_host(host: str) -> bool:
    if host in DOC_HOSTS or host in HN_JUNK_HOSTS or host in JUNK_HOSTS:
        return True
    return any(host == p or host.endswith("." + p) for p in PLATFORM_HOSTS)


class GitHubOrgsAdapter(SourceAdapter):
    name = "github_orgs"

    def __init__(self, min_stars: int = 10, max_queries: int = 8):
        """
        `min_stars` is a floor on interest, not a quality guarantee —
        it just removes the long tail of repos nobody has looked at.
        `max_queries` caps the search API calls; GitHub allows 30/min for
        search and exhausting it mid-run silently truncates the whole
        adapter (F16).
        """
        self.min_stars = min_stars
        self.max_queries = max_queries
        self.session = requests.Session()
        self.headers = {"Accept": "application/vnd.github+json"}
        token = os.getenv("GITHUB_TOKEN")
        if token:
            # Without a token this adapter returns nothing at all — the
            # org lookup 401s and search is throttled to 10/min.
            self.headers["Authorization"] = f"Bearer {token}"
        else:
            print("  (github_orgs: no GITHUB_TOKEN, results will be poor)")
        self._owner_cache: dict[str, dict] = {}
        # Paging checkpoint, injected by main.py (see paging.py).
        self._resume: dict | None = None
        self._on_checkpoint = None

    # ------------------------------------------------------------------ #
    def search(self, icp: ICPProfile, limit: int = 50) -> list[RawCandidate]:
        queries = TOPIC_QUERIES.get(icp.industry.lower(), TOPIC_QUERIES["saas"])

        # One repo -> one company. Several repos under the same owner are
        # the same lead (L21).
        repos: dict[str, dict] = {}
        used = 0
        resume = self._resume or {}
        next_at: dict[str, dict] = {}

        for q in queries:
            if used >= self.max_queries or len(repos) >= limit * 3:
                break
            used += 1
            # Resume per query: GitHub repo search is page-based (verified
            # — pages 1/2/5 return disjoint repo id sets), so the saved
            # page number IS the resume token here.
            start_page = int((resume.get(f"q:{q}") or {}).get("page", 1))
            if start_page > 1:
                print(f"    (github_orgs '{q}' resuming at page {start_page})")
            for repo in self._search_repos(q, page=start_page):
                repos.setdefault(repo["full_name"], repo)
            # One page per query per run keeps us inside the 30 req/min
            # search budget while still going deeper every day.
            next_at[f"q:{q}"] = {"page": start_page + 1, "done": False}
            print(f"  (github_orgs '{q}': {len(repos)} repos so far)")
            # Search API is 30 req/min. 2.5s between calls keeps a long
            # run comfortably inside it.
            time.sleep(2.5)

        if self._on_checkpoint and next_at:
            self._on_checkpoint(next_at)

        candidates: list[RawCandidate] = []
        seen_owners: set[str] = set()

        for repo in repos.values():
            if len(candidates) >= limit:
                break

            domain = _clean_host(repo.get("homepage"))
            if not domain or _is_junk_host(domain) or _looks_like_shared_host(domain):
                continue

            owner = repo.get("owner")
            if not owner or owner.lower() in seen_owners:
                continue
            seen_owners.add(owner.lower())

            profile = self._owner_profile(owner)
            # Prefer the org's own published website over the repo's.
            org_domain = _clean_host(profile.get("blog"))
            if org_domain and not _is_junk_host(org_domain) and not _looks_like_shared_host(org_domain):
                domain = org_domain

            company_name = profile.get("name") or owner
            desc = repo.get("description") or profile.get("bio") or ""

            # A User owner is often a real individual; an Organization is
            # not. Both appear in the same results and they must be
            # recorded differently, so a salutation never addresses a
            # company as a person and a real person's name isn't lost.
            owner_is_org = (
                profile.get("type") or repo.get("owner_type") or ""
            ).lower() == "organization"
            person_name = None
            if not owner_is_org and _is_person_name(profile.get("name")):
                person_name = profile["name"].strip()

            if person_name:
                # An individual's site is their personal site, not a
                # company — don't invent a company name from the domain.
                display_name = person_name
                company_name = None
            else:
                display_name = company_name = (
                    profile.get("name") or owner or domain.split(".")[0]
                )

            # The org's published contact address, if any. These are real
            # role addresses people chose to publish — e.g. PostHog,
            # Supabase, Twenty all list one on their org profile.
            email = profile.get("email")

            topics = repo.get("topics") or []
            bio_parts = [desc]
            if topics:
                bio_parts.append(" ".join(topics))
            if profile.get("bio"):
                bio_parts.append(profile["bio"])
            bio = ". ".join(p for p in bio_parts if p)[:900]

            candidates.append(RawCandidate(
                full_name=display_name,
                job_title="Founder",
                company_name=company_name,
                company_domain=domain,
                email=email,
                twitter_url=(
                    f"https://twitter.com/{profile['twitter']}"
                    if profile.get("twitter") else None
                ),
                source="github_orgs",
                source_details={
                    "bio": bio,
                    "github_org": owner,
                    "github_repo": repo.get("full_name"),
                    "github_url": repo.get("html_url"),
                    "repo_description": desc,
                    "topics": topics[:12],
                    "stars": repo.get("stars"),
                    "repo_homepage": repo.get("homepage"),
                    "org_blog": profile.get("blog"),
                    "org_location": profile.get("location"),
                    # Size proxy. GitHub org followers track company
                    # scale well enough to separate a 10-100 person
                    # SaaS from a giant: measured 2026-09-29 —
                    # omnistrate=8, documenso=235, formbricks=273,
                    # calcom=2178, supabase=11987, n8n=15017. It is a
                    # proxy, NOT headcount, and it is stored raw so a
                    # human can judge the call.
                    "org_followers": profile.get("followers"),
                    "org_public_repos": profile.get("public_repos"),
                    "size_proxy": profile.get("followers"),
                    # An Organization contact is a ROLE address
                    # (hello@, support@, founders@), not a person. A User
                    # owner is a real individual. Recorded separately so
                    # neither is ever mistaken for the other.
                    "is_company_contact": not person_name,
                    "person_name": person_name,
                    "owner_is_organization": owner_is_org,
                    "email_is_real": bool(email),
                    "email_kind": (
                        ("company_org" if owner_is_org else "personal")
                        if email else None
                    ),
                    # Provenance, in the same vocabulary the website
                    # scraper uses, so every address in the CRM can be
                    # sorted by how much we actually trust it.
                    "email_status": (
                        (f"published_{'company_role' if owner_is_org else 'personal'}")
                        if email else None
                    ),
                    "email_source": "github_org_profile" if email else None,
                    "email_confidence": "published" if email else None,
                    "name_is_company": not person_name,
                    "b2b_signal": bool(B2B_SIGNALS.search(bio)),
                    "source_url": f"github://{repo['full_name']}",
                },
            ))

        return candidates

    # ------------------------------------------------------------------ #
    def _search_repos(self, query: str, page: int = 1) -> list[dict]:
        out: list[dict] = []
        try:
            resp = self.session.get(
                REPO_SEARCH,
                headers=self.headers,
                params={"q": query, "per_page": 100, "sort": "updated", "page": page},
                timeout=30,
            )
            if resp.status_code != 200:
                reason = ""
                try:
                    reason = resp.json().get("message", "")[:120]
                except Exception:
                    reason = resp.text[:120]
                print(f"    (github_orgs '{query}' failed: HTTP {resp.status_code} {reason})")
                return out
            for item in resp.json().get("items", []):
                out.append({
                    "full_name": item.get("full_name"),
                    "description": item.get("description"),
                    "homepage": (item.get("homepage") or "").strip(),
                    "stars": item.get("stargazers_count"),
                    "owner": (item.get("owner") or {}).get("login"),
                    "owner_type": (item.get("owner") or {}).get("type"),
                    "topics": item.get("topics") or [],
                    "html_url": item.get("html_url"),
                })
        except requests.RequestException as e:
            print(f"    (github_orgs '{query}' error: {e})")
        return out

    def _owner_profile(self, owner: str) -> dict:
        if owner.lower() in self._owner_cache:
            return self._owner_cache[owner.lower()]
        data: dict = {}
        try:
            resp = self.session.get(
                USER_API.format(owner), headers=self.headers, timeout=15
            )
            if resp.status_code == 200:
                j = resp.json()
                data = {
                    "name": (j.get("name") or "").strip() or None,
                    "blog": (j.get("blog") or "").strip() or None,
                    "email": (j.get("email") or "").strip().lower() or None,
                    "twitter": j.get("twitter_username"),
                    "location": j.get("location"),
                    "bio": j.get("bio"),
                    "type": j.get("type"),
                    "followers": j.get("followers"),
                    "public_repos": j.get("public_repos"),
                }
        except requests.RequestException:
            data = {}
        self._owner_cache[owner.lower()] = data
        return data
