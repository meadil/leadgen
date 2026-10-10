"""
Product Hunt adapter — free GraphQL API, requires a developer token
(sign up at api.producthunt.com/v2/oauth/applications, no cost, no card).

Best fit: SaaS ICPs. Product Hunt is literally software launches, so the
company/industry fit here is far stronger than GitHub bios ever were.
Returns the makers (founders/team) of products under a given topic.

Three things this adapter gets right that the first version did not
(see PLAN.md F1/F2/F5):

  1. PAGINATION. Without a cursor, every single run returned the exact
     same top-N posts and every later run was 100% duplicates.
  2. MANY TOPICS. One topic = one page of posts. Nine working topics
     (verified live; note b2b/startup/ecommerce return ZERO posts on PH
     despite looking like obvious slugs).
  3. REAL DOMAINS. The `website` field is a producthunt.com/r/XXXX
     redirect, not the company site. It has to be resolved, or every
     lead ends up with company_domain = www.producthunt.com and no
     email can ever be guessed.
"""
from __future__ import annotations

import os
import re
import time

import requests

from adapters.base import SourceAdapter
from icp_schema import ICPProfile
from models import RawCandidate

PRODUCTHUNT_URL = "https://api.producthunt.com/v2/api/graphql"

# Topics verified live to return posts on 2026-09-29. b2b, b2c, startup,
# ecommerce, marketing-tools, product-management, ai, small-business,
# entrepreneurship, healthtech, edtech, cybersecurity, customer-support,
# collaboration and devtools ALL return 0 — don't add them back without
# re-testing first.
SAAS_TOPICS = [
    "saas",
    "artificial-intelligence",
    "developer-tools",
    "productivity",
    "marketing",
    "fintech",
    "analytics",
    "remote-work",
    "growth-hacking",
    "no-code",
    "web3",
]

# ICP industry -> which PH topics to walk. Broader than a 1:1 map on
# purpose: PH's own topic taxonomy doesn't line up with ICP industries,
# and a single mapping capped the whole source at one page of posts.
INDUSTRY_TO_TOPICS = {
    "saas": ["saas", "developer-tools", "productivity", "analytics", "no-code", "marketing"],
    "software": ["saas", "developer-tools", "no-code", "productivity", "analytics"],
    "b2b": ["saas", "analytics", "marketing", "remote-work"],
    "technology": ["saas", "artificial-intelligence", "developer-tools", "no-code", "web3"],
    "fintech": ["fintech", "saas"],
}

# Both orders are walked: RANKING is the popular-ever list, NEWEST is
# today's launches. NEWEST is what actually produces new leads over time.
ORDERS = ["NEWEST", "RANKING"]

QUERY = """
query($slug: String!, $first: Int!, $order: PostsOrder!, $after: String) {
  posts(topic: $slug, first: $first, order: $order, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        tagline
        description
        website
        url
        createdAt
        makers {
          name
          username
          twitterUsername
          websiteUrl
        }
      }
    }
  }
}
"""

# Hosts that are never a company's own site. Used to reject a resolved
# domain so it never becomes company_domain (and never gets an email
# guessed onto it).
JUNK_HOSTS = {
    "producthunt.com", "news.ycombinator.com",
    "ycombinator.com", "indiehackers.com",
    "reddit.com", "medium.com", "twitter.com", "x.com", "linkedin.com",
    "github.com", "gitlab.com", "facebook.com", "instagram.com",
    "youtube.com", "youtu.be", "t.me", "telegram.me", "wa.me",
    "docs.google.com", "notion.so", "calendly.com",
    "forms.gle", "bit.ly", "tinyurl.com", "t.co", "lnkd.in",
    "linktr.ee", "beacons.ai", "stripe.com",
    "appspot.com", "google.com", "apple.com", "amazon.com", "wikipedia.org",
    "figma.com", "airtable.com", "substack.com", "ghost.io", "hashnode.com",
    "devhunt.com", "betalist.com", "uneed.best", "microlaunch.io",
    "mvp.fast", "lnk.to", "shorturl.at", "soo.gd", "dub.sh",
}

# Hosting / site-builder / storefront platforms. A "domain" on one of
# these is where the site happens to live, not the company's own domain —
# e.g. `pergolapro.vercel.app`, `closeclerk.pages.dev`,
# `kupangster2.gumroad.com`. Emails guessed onto these bounce, so they
# are rejected and left for the company-name lookup to try instead.
PLATFORM_HOSTS = {
    "vercel.app", "pages.dev", "netlify.app", "netlify.com", "herokuapp.com",
    "github.io", "gitlab.io", "surge.sh", "glitch.me", "replit.app",
    "replit.dev", "fly.dev", "onrender.com", "render.com", "railway.app",
    "web.app", "firebaseapp.com", "azurewebsites.net", "cloudfront.net",
    "s3.amazonaws.com", "readthedocs.io", "sharepoint.com", "wixsite.com",
    "weebly.com", "squarespace.com", "square.site", "carrd.co", "bubble.site",
    "godaddysites.com", "webnode.page", "site123.me", "mystrikingly.com",
    "gumroad.com", "lemonsqueezy.com", "podia.com", "kajabi.com", "teachable.com",
    "hotmart.com", "sellkit.com", "samcart.com", "zendesk.com", "crunchbase.com",
    "buy.me", "ko-fi.com", "buymeacoffee.com", "tally.so", "formspree.io",
    "web3mail", "mailchimp.com", "sendgrid.net", "zoho.com",
}

# Subdomain labels that describe the hosting, not the company. Stripped
# so `app.venuetappos.com` resolves to the domain you'd actually email.
GENERIC_SUBDOMAINS = (
    "www.", "app.", "go.", "get.", "use.", "try.", "www2.", "en.",
    "dashboard.", "portal.", "api.", "docs.", "help.", "support.",
    "blog.", "mail.", "web.", "home.", "shop.", "buy.", "hello.",
)


class ProductHuntAdapter(SourceAdapter):
    name = "producthunt"

    def __init__(self, token: str | None = None):
        self.token = token or os.getenv("PRODUCTHUNT_TOKEN")
        if not self.token:
            raise RuntimeError("PRODUCTHUNT_TOKEN not set in .env")
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        # One session for GraphQL + one for redirect resolution; the PH
        # redirect host is different from the API host.
        self.api = requests.Session()
        self.web = requests.Session()
        # Do NOT spoof a browser User-Agent here. producthunt.com sits
        # behind Cloudflare, and a Chrome UA makes /r/ links return a
        # 403 "Just a moment..." interstitial instead of the redirect —
        # which silently kills domain resolution for every lead.
        # The default python-requests UA is passed straight through.
        self.web.headers["User-Agent"] = "leadgen-bot/1.0 (+producthunt-redirect-resolver)"
        # url -> resolved domain, so a domain shared by several posts is
        # only fetched once per run.
        self._domain_cache: dict[str, tuple[str | None, str | None]] = {}
        # Paging checkpoint, injected by main.py so the adapter stays
        # decoupled from Supabase. `_resume` is the position to start
        # from, `_on_checkpoint` is called with the position to persist.
        self._resume: dict | None = None
        self._on_checkpoint = None

    # ------------------------------------------------------------------ #
    # search
    # ------------------------------------------------------------------ #
    def search(self, icp: ICPProfile, limit: int = 50) -> list[RawCandidate]:
        """
        Walk topics newest-first, RESUMING from the last saved cursor.

        Without the resume, every run re-read the same newest posts and
        returned the same candidates forever (PLAN.md F1/F18). With it,
        successive runs go progressively deeper into the archive, which
        is where the volume actually is — 6+ pages of pure 2026-09
        content were verified to exist on 2026-09-29, and we had read
        only the first sliver of it.
        """
        topics = INDUSTRY_TO_TOPICS.get(
            icp.industry.lower(), INDUSTRY_TO_TOPICS["technology"]
        )
        per_page = min(20, max(5, limit // max(1, len(topics))))

        start_at = self._resume or {}
        remaining = [
            (order, topic)
            for order in ORDERS
            for topic in topics
            if not start_at.get(f"{order}:{topic}", {}).get("done")
        ]

        if self._resume is not None and not remaining:
            # Every combination was exhausted by earlier runs. A rolling
            # feed brings new posts daily, so start over rather than
            # returning nothing.
            print("  (ph: all topic/order combos exhausted, restarting from newest)")
            start_at = {}
            remaining = [(o, t) for o in ORDERS for t in topics]

        candidates: list[RawCandidate] = []
        seen_posts: set[str] = set()
        next_at: dict[str, dict] = {}
        exhausted_all = True

        for order, topic in remaining:
            if len(candidates) >= limit:
                exhausted_all = False
                break
            combo = f"{order}:{topic}"
            saved = start_at.get(combo) or {}
            state: dict = {"page": int(saved.get("page", 0)),
                           "cursor": saved.get("cursor")}

            stopped_early = False
            last_info: dict = {}
            for post, page_info in self._walk_topic(
                topic, order, per_page,
                start_page=state["page"], start_cursor=state["cursor"],
                state=state,
            ):
                if len(candidates) >= limit:
                    # Stopped mid-stream: `state` already holds exactly
                    # where the next page begins, cursor included.
                    next_at[combo] = {"page": state["page"],
                                      "cursor": state["cursor"], "done": False}
                    stopped_early = True
                    exhausted_all = False
                    break

                last_info = page_info
                key = post.get("id") or post.get("url")
                if key and key in seen_posts:
                    continue
                if key:
                    seen_posts.add(key)

                made = self._post_to_candidates(post)
                if made:
                    candidates.extend(made)

            if not stopped_early:
                has_next = bool(last_info.get("hasNextPage"))
                next_at[combo] = {"page": state["page"],
                                  "cursor": state["cursor"],
                                  "done": not has_next}
                if has_next:
                    exhausted_all = False

        if self._on_checkpoint and next_at:
            if exhausted_all:
                next_at["_all_done"] = {"at": time.time()}
            else:
                next_at.pop("_all_done", None)
            for v in next_at.values():
                v["saved_at"] = time.time()
            self._on_checkpoint(next_at)

        return candidates[:limit]

    def _walk_topic(self, topic: str, order: str, per_page: int,
                    start_page: int = 0, start_cursor: str | None = None,
                    state: dict | None = None):
        """
        Yield (post, pageInfo) across pages, updating `state` in place.

        `state` is how the caller learns where to resume. It is a dict
        the generator mutates, because Product Hunt's GraphQL API paginates
        ONLY by `after:` cursor — there is no page offset. A checkpoint
        that stored a page number without its cursor resumed at page 1
        and re-read the same posts forever, which is the exact bug this
        whole mechanism exists to fix (PLAN.md F1).
        """
        if state is None:
            state = {}
        cursor: str | None = start_cursor
        page = start_page
        state["page"] = page
        state["cursor"] = cursor

        for _ in range(40):  # generous cap; this is the volume source
            payload = self._graphql({
                "slug": topic,
                "first": min(per_page, 20),
                "order": order,
                "after": cursor,
            })
            if payload is None:
                return

            data = payload.get("data") or {}
            posts = data.get("posts")
            if not posts:
                return

            page_info = posts.get("pageInfo") or {}
            for edge in posts.get("edges", []):
                yield edge["node"], page_info

            if not page_info.get("hasNextPage"):
                return
            new_cursor = page_info.get("endCursor")
            # A cursor that doesn't advance means the feed isn't really
            # paginating — stop rather than loop forever.
            if not new_cursor or new_cursor == cursor:
                return
            cursor = new_cursor
            page += 1
            # Record the position of the NEXT page before fetching it, so
            # an interrupted run resumes exactly here.
            state["page"] = page
            state["cursor"] = cursor

    def _graphql(self, variables: dict) -> dict | None:
        """
        One GraphQL call with backoff on rate limit.

        PH's free tier 429s easily once a run walks many topics × orders
        × pages, and a bare raise_for_status() would silently end the
        walk mid-run. Back off and retry instead of losing the run.
        """
        delay = 2.0
        for attempt in range(4):
            try:
                resp = self.api.post(
                    PRODUCTHUNT_URL,
                    headers=self.headers,
                    json={"query": QUERY, "variables": variables},
                    timeout=25,
                )
                if resp.status_code == 429:
                    wait = delay * (2 ** attempt)
                    print(f"    (ph rate limited, backing off {wait:.0f}s)")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("errors"):
                    print(f"    (ph error: {str(payload['errors'])[:120]})")
                    return None
                return payload
            except requests.RequestException as e:
                print(f"    (ph request failed: {e})")
                return None
        print("    (ph gave up after 4 rate-limited attempts)")
        return None

    # ------------------------------------------------------------------ #
    # post -> candidates
    # ------------------------------------------------------------------ #
    def _post_to_candidates(self, post: dict) -> list[RawCandidate]:
        makers = post.get("makers") or []
        if not makers:
            return []

        post_domain, post_hint = self._domain_from_url(post.get("website"))
        # Kept generic as "bio" so enrichment.py's title-inference and
        # scoring.py's industry/pain matching work the same regardless
        # of which adapter produced the candidate.
        bio_text = f"{post.get('tagline', '')}. {post.get('description', '')}".strip()

        out: list[RawCandidate] = []
        for maker in makers:
            name = maker.get("name")
            if not name:
                continue

            # 1.4 — a maker's own site is often a better company signal
            # than the PH redirect, so try it first.
            maker_domain, maker_hint = self._domain_from_url(maker.get("websiteUrl"))
            domain = maker_domain or post_domain
            hint = maker_hint or post_hint

            out.append(RawCandidate(
                full_name=name,
                # Makers of a PH launch are founders/team by definition —
                # the API exposes no job titles, so default to Founder
                # (enrichment/scoring can refine from bio text).
                job_title="Founder",
                company_name=post.get("name"),
                company_domain=domain,
                twitter_url=(
                    f"https://twitter.com/{maker['twitterUsername']}"
                    if maker.get("twitterUsername") else None
                ),
                source="producthunt",
                source_details={
                    "bio": bio_text,
                    "maker_website": maker.get("websiteUrl"),
                    "producthunt_username": maker.get("username"),
                    # Stable per-post key. Dedupe on this, never on name
                    # alone — a post's makers reappear every run.
                    "source_url": post.get("url") or f"ph://{post.get('id')}",
                    "ph_post_id": post.get("id"),
                    "ph_posted_at": post.get("createdAt"),
                    "ph_topic_seen": post.get("tagline"),
                    "domain_from": "maker" if maker_domain else ("post" if post_domain else None),
                    # When the site turned out to be a platform subdomain
                    # (`closeclerk.pages.dev`), the subdomain label is
                    # still a strong guess at the real domain
                    # (`closeclerk.com`). Enrichment tries it before
                    # falling back to a company-name search.
                    "domain_hint": hint,
                },
            ))
        return out

    # ------------------------------------------------------------------ #
    # domain resolution
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clean_host(url: str | None) -> str | None:
        if not url:
            return None
        host = url.strip()
        for prefix in ("https://", "http://"):
            if host.lower().startswith(prefix):
                host = host[len(prefix):]
        host = host.split("/")[0].split("?")[0].split("#")[0]
        host = host.split("@")[-1].split(":")[0].lower().strip()
        # Strip a generic subdomain prefix — `app.venuetappos.com` is
        # VenueTap POS, not "app". Only strips when something is left.
        for prefix in GENERIC_SUBDOMAINS:
            if host.startswith(prefix) and host.count(".") > 1:
                host = host[len(prefix):]
                break
        if not host or "." not in host:
            return None
        if host in JUNK_HOSTS:
            return None
        # Reject anything hosted on a platform — `foo.vercel.app` is not
        # Foo Software's domain. Checks every suffix, not just the exact
        # host, because platform domains appear as subdomains:
        # `closeclerk.pages.dev` is Pages, not CloseClerk.
        for platform in PLATFORM_HOSTS:
            if host == platform or host.endswith("." + platform):
                return None
        return host

    def _domain_from_url(self, url: str | None) -> tuple[str | None, str | None]:
        """
        Turn any URL into a usable company domain.

        Returns (domain, hint):
          domain — a real company domain, or None
          hint   — a best guess at one, when we only resolved to a
                   platform subdomain (`closeclerk.pages.dev` →
                   hint `closeclerk.com`). Never used as the domain
                   itself, because emails guessed onto it bounce.
        """
        if not url:
            return None, None
        if url in self._domain_cache:
            return self._domain_cache[url]

        # Test the CLEANED host, not the raw one. `www.producthunt.com`
        # and `producthunt.com` are the same junk host, and checking the
        # raw hostname made the www form skip the redirect entirely —
        # silently costing every lead its domain.
        cleaned = self._clean_host(url)
        if cleaned:
            result = (cleaned, None)
            self._domain_cache[url] = result
            return result

        # producthunt.com (or an unrecognised shortener) — follow it.
        resolved_host: str | None = None
        try:
            # HEAD is enough: the redirect chain is in the response
            # headers, no body needed, and it's a fraction of the bandwidth.
            resp = self.web.head(url, allow_redirects=True, timeout=12)
            if resp.status_code >= 400:
                # Some hosts reject HEAD outright — fall back to a GET.
                resp = self.web.get(url, allow_redirects=True, timeout=12, stream=True)
            resolved_host = self._raw_host(resp.url)
            resp.close()
        except requests.RequestException:
            resolved_host = None

        result = (None, None)
        if resolved_host:
            real = self._clean_host(resolved_host)
            if real:
                result = (real, None)
            else:
                # Resolved to a platform subdomain — salvage the label.
                hint = self._label_hint(resolved_host)
                if hint:
                    result = (None, hint)

        self._domain_cache[url] = result
        return result

    @staticmethod
    def _raw_host(url: str) -> str | None:
        """Hostname of a URL, lowercased, no cleaning/judging applied."""
        if not url:
            return None
        host = url.strip()
        for prefix in ("https://", "http://"):
            if host.lower().startswith(prefix):
                host = host[len(prefix):]
        host = host.split("/")[0].split("?")[0].split("#")[0]
        host = host.split("@")[-1].split(":")[0].lower().strip()
        return host or None

    @staticmethod
    def _label_hint(host: str) -> str | None:
        """
        From `closeclerk.pages.dev` return `closeclerk` — the subdomain
        label a company chose for itself, which is usually its name.
        Multi-label platform subdomains (`kupangster2.gumroad.com`)
        are used as-is.
        """
        for platform in PLATFORM_HOSTS:
            if host.endswith("." + platform):
                label = host[: -(len(platform) + 1)]
                if not label:
                    return None
                # A registrable-looking label, not a nested one.
                first = label.split(".")[0]
                if "." in label and label != first:
                    # e.g. `a.b.vercel.app` — use only the leading label
                    return first if first and len(first) > 1 else None
                return label if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", label) else None
        return None
