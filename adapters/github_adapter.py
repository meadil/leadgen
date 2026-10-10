"""
GitHub source adapter — free, official API, no scraping/ToS risk.

Rate limits: ~10 req/min unauthenticated, 5,000 req/hr with a personal
access token (set GITHUB_TOKEN in .env — no scopes needed for search).

Best fit: ICPs where the target person is a developer, engineer, or
technical lead. It searches GitHub bios/locations, not general
businesspeople, so this won't be useful for every ICP — that's expected,
it's one adapter among several we'll add.
"""
from __future__ import annotations

import os
import time

import requests

from adapters.base import SourceAdapter
from icp_schema import ICPProfile
from models import RawCandidate

GITHUB_SEARCH_URL = "https://api.github.com/search/users"
GITHUB_USER_URL = "https://api.github.com/users/{login}"


class GitHubAdapter(SourceAdapter):
    name = "github"

    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("GITHUB_TOKEN")
        self.headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"

    def _build_query(self, icp: ICPProfile, location: str | None) -> str:
        """
        GitHub's search syntax is narrow, so this is a best-effort
        translation of the ICP into a query, not a full re-implementation
        of every ICP field. Enrichment/scoring downstream does the real
        matching against the rest of the ICP fields.

        Three syntax rules, all verified live 2026-09-29 because getting
        any of them wrong returns total_count=0 and the adapter silently
        contributes nothing:

        1. Titles must be OR'd, never space-joined. Space-separated
           terms are AND'd across the whole indexed profile, so the
           original `Founder Co-Founder` matched nobody.
        2. Parentheses are NOT supported in user search.
           `(Founder OR CEO)` -> 0 results, while the unparenthesised
           `Founder OR CEO` -> 1259.
        3. Multi-word locations must be quoted.
           `location:United States` -> 1 result, `location:"United
           States"` -> 1076.
        """
        parts: list[str] = []

        if location:
            # Quote anything with a space; a single word needs no quotes.
            parts.append(
                f'location:"{location}"' if " " in location else f"location:{location}"
            )

        # A profile can only hold one or two of these words in practice,
        # so capping the list keeps the query from becoming so narrow
        # that it matches nobody.
        titles = [t.replace('"', " ").strip() for t in icp.job_titles[:4]]
        titles = [t for t in titles if t]
        if titles:
            # Unparenthesised — GitHub user search has no grouping.
            parts.append(" OR ".join(titles))

        parts.append("type:user")
        return " ".join(parts)

    def search(self, icp: ICPProfile, limit: int = 50) -> list[RawCandidate]:
        # Search every listed location, not just the first - needed now
        # that an ICP can list several countries at once.
        locations = icp.geography_regions_cities or icp.geography_countries or [None]
        per_location_limit = max(1, limit // len(locations))

        candidates: list[RawCandidate] = []
        for location in locations:
            if len(candidates) >= limit:
                break

            query = self._build_query(icp, location)
            page = 1
            fetched_this_location = 0
            rate_limit_retries = 0

            while fetched_this_location < per_location_limit and len(candidates) < limit:
                if rate_limit_retries > 3:
                    print(f"    (github: still rate limited after 3 retries, skipping '{location}')")
                    break
                try:
                    resp = requests.get(
                        GITHUB_SEARCH_URL,
                        headers=self.headers,
                        params={"q": query, "per_page": min(per_location_limit, 100), "page": page},
                        timeout=15,
                    )
                    resp.raise_for_status()
                except requests.HTTPError:
                    # One bad location/query (403 rate-limit, 422 bad query)
                    # must not kill the whole run — skip to next location.
                    try:
                        reason = resp.json().get("message", resp.text[:200])
                    except Exception:
                        reason = resp.text[:200]
                    if resp.status_code == 403 and "rate limit" in str(reason).lower():
                        # Not a bad query — we're out of quota. Sleeping
                        # here and retrying is better than silently
                        # reporting "no matches", which looks identical
                        # to a query that legitimately matched nobody.
                        wait = 20.0 * (2 ** (page - 1))
                        wait = min(wait, 60.0)
                        print(f"    (github rate limited, sleeping {wait:.0f}s before retrying)")
                        time.sleep(wait)
                        rate_limit_retries += 1
                        continue
                    print(f"    (github search skipped '{query}': HTTP {resp.status_code} - {reason})")
                    break
                items = resp.json().get("items", [])
                if not items:
                    # A query that matches nobody is common (a rare title
                    # in a small country). Say so once and move on rather
                    # than paging an empty result set repeatedly.
                    if page == 1:
                        print(f"    (github: no matches for '{query}')")
                    break

                for item in items:
                    candidate = self._fetch_profile(item["login"])
                    if candidate:
                        candidates.append(candidate)
                        fetched_this_location += 1
                    if fetched_this_location >= per_location_limit or len(candidates) >= limit:
                        break

                page += 1

        return candidates

    def _fetch_profile(self, login: str) -> RawCandidate | None:
        resp = requests.get(
            GITHUB_USER_URL.format(login=login), headers=self.headers, timeout=15
        )
        if resp.status_code != 200:
            # Surface *why* a candidate was skipped instead of failing silently.
            # A wall of 403s here means you're rate-limited — add GITHUB_TOKEN to .env.
            reason = resp.json().get("message", resp.text) if resp.content else resp.reason
            print(f"    (skipped {login}: HTTP {resp.status_code} - {reason})")
            return None
        data = resp.json()

        twitter_url = (
            f"https://twitter.com/{data['twitter_username']}"
            if data.get("twitter_username")
            else None
        )

        return RawCandidate(
            full_name=data.get("name"),
            job_title=None,  # GitHub doesn't expose this; enrichment can infer from bio text
            company_name=data.get("company"),
            email=data.get("email"),  # usually null unless the user made it public
            github_url=data.get("html_url"),
            twitter_url=twitter_url,
            geography_city=data.get("location"),
            source="github",
            source_details={
                "bio": data.get("bio"),
                "login": login,
                # Stable per-person key. Every adapter writes one so dedup
                # has a universal handle; the GitHub adapter had none, so
                # 66/66 of its leads relied on the fallback keys alone.
                "source_url": data.get("html_url"),
            },
        )
