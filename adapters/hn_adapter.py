"""
Hacker News "Show HN" adapter — free, no API key, no signup.

Why this source: Show HN posts are people publicly announcing a product
they built, and the post carries the product's real URL. That gives a
real company domain, which is the single thing the rest of the pipeline
needs most (a domain is what makes an email guessable). Verified live
2026-09-29: ~61% of the 100 newest Show HN posts resolve to a genuine
company domain.

THE BIG LIMITATION — read before using these leads:
HN exposes only a USERNAME, never a person's real name. The user API
returns {username, karma, about} and nothing else — no name, no
location, no email. So `full_name` here is a handle, not a name. We do
NOT guess or invent a real name for it (see PLAN.md rule 4), because a
fabricated name silently corrupts every downstream merge, email
salutation and CRM search. These leads are still worth having: the
domain is real, the product is real, and the handle is a reliable
cross-source dedup key.
"""
from __future__ import annotations

import html
import os
import re
from urllib.parse import urlparse

import requests

from adapters.base import SourceAdapter
from adapters.producthunt_adapter import JUNK_HOSTS, PLATFORM_HOSTS
from icp_schema import ICPProfile
from models import RawCandidate

ALGOLIA_SEARCH = "https://hn.algolia.com/api/v1/search"
ALGOLIA_BY_DATE = "https://hn.algolia.com/api/v1/search_by_date"
ALGOLIA_USER = "https://hn.algolia.com/api/v1/users/"
GITHUB_USER = "https://api.github.com/users/{}"

# Hosts that are never a person's company. Broader than PH's list because
# HN skews heavily to code dumps and personal blogs.
HN_JUNK_HOSTS = JUNK_HOSTS | {
    # GitHub itself: a repo link is where the code lives, and the
    # org/user API turns it into a real company domain and often a real
    # founder email. Handled specially below rather than rejected.
    "github.com", "gist.github.com", "github.io", "githubusercontent.com",
    "gitlab.com", "codeberg.org", "sourceforge.net", "bitbucket.org",
    "news.ycombinator.com", "stackoverflow.com", "stackexchange.com",
    "youtube.com", "youtu.be", "vimeo.com", "twitch.tv", "discord.com",
    "discord.gg", "mastodon.social", "bsky.app", "t.co", "paypal.me",
    "ko-fi.com", "buymeacoffee.com", "patreon.com", "opencollective.com",
    "twitter.com", "x.com", "linkedin.com", "facebook.com", "instagram.com",
    "reddit.com", "tumblr.com", "pinterest.com", "tiktok.com",
    "arxiv.org", "doi.org", "wikipedia.org", "en.wikipedia.org",
    "docs.google.com", "drive.google.com", "notion.site", "calendly.com",
    "amazon.com", "ebay.com", "etsy.com", "aliexpress.com", "walmart.com",
    "imgur.com", "i.redd.it", "steamcommunity.com", "steampowered.com",
    "chess.com", "lichess.org", "duolingo.com", "leetcode.com",
    "r2.dev",
    # App / plugin storefronts — the product's page, not its domain.
    "apps.apple.com", "play.google.com", "chromewebstore.google.com",
    "addons.mozilla.org", "microsoft.com", "apps.microsoft.com",
    "store.steampowered.com", "itch.io", "crazygames.com",
    "play.it", "poki.com", "kongregate.com",
    "vercel.com", "vercel.app", "netlify.com", "netlify.app",
    "herokuapp.com", "heroku.com", "fly.dev", "fly.io",
    "cloudflare.com", "workers.dev", "pages.dev", "trycloudflare.com",
    "digitalocean.com", "render.com", "onrender.com", "railway.app",
    "railway.com", "supabase.com", "planetscale.com", "neon.tech",
    "upstash.com", "mongodb.com", "redis.com", "planetscale.com",
    "linear.app", "notion.so", "figma.com", "slack.com", "discord.com",
    "github.com", "gitlab.com", "atlassian.com", "salesforce.com",
    "hubspot.com", "zapier.com", "make.com", "n8n.io", "retool.com",
    "posthog.com", "sentry.io", "datadog.com", "segment.com",
    "openai.com", "anthropic.com", "google.com", "microsoft.com",
    "amazon.com", "aws.amazon.com", "cloud.google.com", "stripe.com",
    "twilio.com", "sendgrid.net", "mailchimp.com", "hubspot.com",
    "databricks.com", "snowflake.com", "huggingface.co", "langchain.com",
    "modal.com", "runpod.io", "replicate.com", "together.ai",
    "perplexity.ai", "openrouter.ai", "v0.dev", "lovable.dev",
    "tldraw.com", "excalidraw.com", "excalidraw.com",
}

# Show HN titles that are pure personal projects, not companies. These
# pass the "has a domain" test but are not a business you can sell to,
# so they waste an email guess. Word-boundary alternation; the entries
# are literal tokens, no nested groups.
NOT_A_COMPANY = re.compile(
    r"\b(?:"
    r"i\s+made|i\s+built|open[\s-]?source|self[\s-]?host|"
    r"my\s+\w+\s*(?:game|toy|project|clone|blog)|"
    r"ask\s+hn|interview|scratchpad|cheat\s?sheet|dotfiles|"
    r"dot\s?files|cover\s?letter|"
    r"javascript\s+(?:interview|quiz)|"
    r"a\s+(?:tiny|simple|minimal|fun|weird|little)\b|"
    r"AMA\b"
    r")",
    re.IGNORECASE,
)

# Words in a title that indicate an actual business product. Used to
# RANK B2B-ness, never to hard-filter — a real founder rarely uses
# these exact words, so a hard filter here would throw away good leads.
B2B_SIGNALS = re.compile(
    r"\b(?:"
    r"saas|b2b|api|apis|platform|analytics|dashboard|crm|erp|"
    r"billing|invoic\w*|payment\w*|checkout|subscription|"
    r"workflow|automation|integrat\w*|webhook\w*|"
    r"monitor\w*|observab\w*|logging|"
    r"helpdesk|support|"
    r"devtool\w*|deploy\w*|hosting|database|postgres|"
    r"crawler|scraper|index\w*|search\s+engine|"
    r"project\s+manag\w*|collaborat\w*|productivity|"
    r"revenue|growth|marketing|"
    r"schedul\w*|calendar|accounting|payroll|"
    r"security|auth\w*|identity|encryption|"
    r"backup|storage|queue|proxy|gateway|"
    r"generator|converter|parser|extractor|"
    r"board|management|teams?|business|company|enterprise|"
    r"autonomous|agent|agents|llm|ai\b|"
    r"server|infrastructure|infra|devops|pipeline|"
    r"notification\w*|tracking|report\w*"
    r")",
    re.IGNORECASE,
)

TITLE_PREFIX = re.compile(r"^\s*show\s+hn:?\s*", re.IGNORECASE)


def _clean_host(url: str) -> str | None:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _is_junk_host(host: str) -> bool:
    if host in HN_JUNK_HOSTS:
        return True
    # Platform subdomains: `someone.github.io` is a page, not a company.
    return any(host == p or host.endswith("." + p) for p in PLATFORM_HOSTS)


# A domain with a numeric/hex segment is almost always a shared host
# (`terminal-typing-svg.vaidikv.workers.dev`, `x.sf.tools`, `x.y.glitch.me`),
# never a company. Cheap structural signal that catches hosts no list
# will ever be complete enough to enumerate (LESSONS_LEARNED L2).
SHARED_HOST_HINT = re.compile(
    r"^(?:[a-z0-9-]{10,}\.)+[a-z0-9-]+\.[a-z]{2,6}$"
)


def _looks_like_shared_host(host: str) -> bool:
    """
    Flag a domain whose registrable label is long and generic, which is
    what a personal project on a shared host looks like.

    Deliberately conservative: a real SaaS domain is short and branded
    (`gemhunter.top`), a personal deploy is long and descriptive
    (`terminal-typing-svg`). This only rejects when the second-level
    label is BOTH long (>18 chars) and contains a hyphen or digit.
    """
    label = host.split(".")[0]
    if len(label) > 18 and ("-" in label or any(c.isdigit() for c in label)):
        return True
    return False


def _hn_about_to_text(about: str | None) -> str:
    """HN `about` fields are HTML-escaped fragments. Flatten to text."""
    if not about:
        return ""
    text = html.unescape(html.unescape(about))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# People write their address in plain text. Deliberately conservative:
# requires a plausible TLD so it doesn't match "e.g. something@2" prose.
EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}\b"
)
JUNK_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "test.com", "domain.com",
    "email.com", "yourdomain.com", "mydomain.com", "sentry.io", "w3.org",
    "schema.org", "localhost", "users.noreply.github.com",
}
# A gmail/hotmail/outlook address is the person's personal mail, not the
# company's — useful to store, but never a substitute for a company email.
PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "hotmail.co.uk", "outlook.com", "live.com", "msn.com", "aol.com",
    "icloud.com", "me.com", "mac.com", "protonmail.com", "proton.me",
    "gmx.com", "gmx.de", "mail.com", "zoho.com", "yandex.com", "qq.com",
    "163.com", "126.com", "web.de", "fastmail.com", "tutanota.com",
    "hey.com", "duck.com", "pm.me", "yep.com",
}

# A real personal name at the start of an about field: "Alex. I build
# kern...", "Jane Doe, founder of ...". Requires two capitalised words so
# a sentence starting with a capital ("Software Engineer.") can't match.
REAL_NAME_RE = re.compile(
    r"\b([A-Z][a-z]{1,15})\s+([A-Z][a-z]{1,20})\b"
)
# Titles that look like name words but aren't.
NOT_A_NAME = {
    "Software", "Engineer", "Founder", "Ceo", "Cto", "Coo", "Cfo", "Dev",
    "Developer", "Designer", "Product", "Student", "Research", "Phd", "Msc",
    "Bsc", "Working", "Building", "Making", "Hello", "Hi", "Hey", "About",
    "Contact", "Email", "Twitter", "Website", "Blog", "Git", "Github", "I",
    "It", "The", "And", "But", "My", "Our", "We", "Just", "Currently",
    "Previously", "Also", "New", "Open", "Free", "Full", "Senior", "Junior",
    "Lead", "Head", "Chief", "Tech", "Data", "Web", "App", "Ai", "Ml",
    "Ops", "Devops", "Cloud", "Self", "Open", "Source", "Rust", "Go",
    "Python", "Java", "JavaScript", "Type", "Script", "Node", "React",
    "Vue", "Svelte", "Rust", "Linux", "Mac", "Windows", "Android", "Ios",
    "Show", "Tell", "Ask", "Read", "Write", "Learn", "Teach", "Help",
    "Uses", "Like", "Loves", "Enjoys", "Writes", "Reads", "Runs", "Owns",
    "Creates", "Builds", "Ships", "Launches", "Scaled", "Grew", "Raised",
}


class HackerNewsAdapter(SourceAdapter):
    name = "hackernews"

    def __init__(self, limit_per_page: int = 20, max_pages: int = 8):
        self.limit_per_page = limit_per_page
        self.max_pages = max_pages
        self.session = requests.Session()
        # Do NOT spoof a browser UA here either (see LESSONS_LEARNED L1) —
        # Algolia doesn't need one and it only creates risk.
        self._user_cache: dict[str, dict] = {}
        self._gh_cache: dict[str, dict] = {}
        # Paging checkpoint, injected by main.py (see paging.py).
        self._resume: dict | None = None
        self._on_checkpoint = None
        token = os.getenv("GITHUB_TOKEN")
        self._gh_headers = {"Accept": "application/vnd.github+json"}
        if token:
            # Without a token the org lookup 401s and this whole
            # recovery path silently returns nothing (verified).
            self._gh_headers["Authorization"] = f"Bearer {token}"

    # ------------------------------------------------------------------ #
    def _github_org(self, owner: str) -> dict:
        """
        Resolve a GitHub org/user to a real domain + contact.

        41% of Show HN posts link straight to a GitHub repo (measured
        2026-09-29), and rejecting them threw away real companies. The
        org API gives back the company website and frequently a founder
        email outright: rsync-ai -> rsync.ai, lemma-work -> lemma.work +
        deepak@lemma.work, SupercmoHQ -> getsupercmo.ai +
        founder@getsuprcmo.ai.
        """
        if owner.lower() in self._gh_cache:
            return self._gh_cache[owner.lower()]

        result: dict = {}
        try:
            resp = self.session.get(
                GITHUB_USER.format(owner), headers=self._gh_headers, timeout=12
            )
            if resp.status_code == 200:
                data = resp.json()
                blog = (data.get("blog") or "").strip()
                domain = None
                if blog:
                    # blog is often "https://rsync.ai" but sometimes bare
                    # "rsync.ai" or a full path.
                    candidate = blog if "://" in blog else f"https://{blog}"
                    host = _clean_host(candidate)
                    if host and not _is_junk_host(host) and not _looks_like_shared_host(host):
                        domain = host
                result = {
                    "domain": domain,
                    "name": data.get("name"),
                    "email": (data.get("email") or "").strip().lower() or None,
                    "twitter": (data.get("twitter_username") or None),
                    "blog": blog or None,
                    "type": data.get("type"),
                }
        except requests.RequestException:
            result = {}

        self._gh_cache[owner.lower()] = result
        return result
    # ------------------------------------------------------------------ #
    def search(self, icp: ICPProfile, limit: int = 50) -> list[RawCandidate]:
        """
        Walk Show HN newest-first, keep what looks like a company.

        Newest-first matters: sorted by relevance the same top posts come
        back on every run (exactly the Product Hunt bug, PLAN.md F1).
        search_by_date is what makes this a daily source of new leads.

        RESUMES from a saved page (PLAN.md task 6.4). Show HN holds
        539,339 posts and page 20 is still same-day, so the depth here is
        effectively unlimited. Without a checkpoint every run re-read the
        same newest pages forever — this was the largest remaining volume
        leak after Product Hunt was fixed.
        """
        candidates: list[RawCandidate] = []
        seen_posts: set[str] = set()
        # One person can Show HN several products, and one product can be
        # posted twice. Without this, a single founder enters the CRM
        # three times (verified: "flkdnfsnsf / App2Api" appeared twice
        # from two different posts in the same run).
        seen_people: set[str] = set()
        seen_companies: set[str] = set()

        # The checkpoint is namespaced per feed like every other adapter
        # (`{"show_hn": {"page": n}}`), so read from inside it. Reading
        # `self._resume["page"]` directly silently defaulted to 0 and
        # every run re-read page 1 — the exact no-op this is meant to fix
        # (PLAN.md F52: store what the API actually uses).
        resume_state = (self._resume or {}).get("show_hn") or {}
        start_page = int(resume_state.get("page", 0))
        if start_page:
            print(f"    (hn resuming at page {start_page})")

        exhausted = False
        for page in range(start_page, start_page + self.max_pages):
            if len(candidates) >= limit:
                break
            try:
                resp = self.session.get(
                    ALGOLIA_BY_DATE,
                    params={
                        "tags": "show_hn",
                        "hitsPerPage": self.limit_per_page,
                        "page": page,
                    },
                    timeout=25,
                )
                resp.raise_for_status()
                hits = resp.json().get("hits", [])
            except requests.RequestException as e:
                print(f"    (hn page {page} failed: {e})")
                break

            if not hits:
                break

            for hit in hits:
                if len(candidates) >= limit:
                    break
                cand = self._hit_to_candidate(hit, icp)
                if cand is None:
                    continue

                post_key = cand.source_details["source_url"]
                if post_key in seen_posts:
                    continue
                seen_posts.add(post_key)

                person_key = (cand.source_details.get("hn_username") or "").lower()
                company_key = (cand.company_domain or "").lower()
                if person_key and person_key in seen_people:
                    continue
                if company_key and company_key in seen_companies:
                    continue
                if person_key:
                    seen_people.add(person_key)
                if company_key:
                    seen_companies.add(company_key)

                candidates.append(cand)

        # A short page means Algolia has no more results past this point.
        if page >= start_page and len(hits) < self.limit_per_page:
            exhausted = True

        # Save the position for next time. `page` is where the NEXT run
        # starts — so it is the page we finished on, plus one.
        if self._on_checkpoint:
            next_page = start_page + self.max_pages
            if exhausted:
                next_page = 0  # wrap: HN is a rolling feed, restart at top
            self._on_checkpoint({
                "show_hn": {
                    "page": next_page,
                    "done": exhausted,
                    "exhausted_at_page": page if exhausted else None,
                }
            })

        return candidates

    # ------------------------------------------------------------------ #
    def _hit_to_candidate(self, hit: dict, icp: ICPProfile) -> RawCandidate | None:
        title = (hit.get("title") or "").strip()
        url = hit.get("url")
        author = hit.get("author")
        story_id = hit.get("objectID")

        if not title or not author or not story_id:
            return None

        clean_title = TITLE_PREFIX.sub("", title).strip()

        # No url means a text post (Ask HN, a comment) — not a product.
        if not url:
            return None

        host = _clean_host(url)

        # A GitHub link isn't a dead end — the org API can turn it into a
        # real company domain and a real founder email. 41% of Show HN
        # posts link to a repo, so rejecting them was discarding real
        # companies.
        gh_org: dict = {}
        github_repo: str | None = None
        if host and (host == "github.com" or host.endswith(".github.com")):
            parts = [p for p in urlparse(url).path.split("/") if p]
            if not parts:
                return None
            owner = parts[0]
            repo = parts[1] if len(parts) > 1 else None
            github_repo = f"https://github.com/{owner}" + (f"/{repo}" if repo else "")
            gh_org = self._github_org(owner)
            domain = gh_org.get("domain")
            # No website published on the org page — nothing to work with.
            if not domain or _looks_like_shared_host(domain):
                return None
        else:
            if not host or _is_junk_host(host):
                return None
            if _looks_like_shared_host(host):
                return None
            domain = host

        # Reject obvious personal projects: they have a domain but there is
        # no business behind it to sell to.
        if NOT_A_COMPANY.search(clean_title):
            return None

        # The product's own page is the strongest "bio" signal we get.
        # Feed the title through so scoring sees something meaningful.
        bio = clean_title
        if domain:
            bio = f"{clean_title} ({domain})"

        # Profile is the only person data HN has. It may carry a real
        # name and a real email, which is what makes these leads
        # contactable at all.
        profile = self._profile(author)
        real_name = profile.get("real_name")
        found_email = profile.get("email")
        personal_site = profile.get("personal_site")

        # Prefer a real name; fall back to the handle, flagged so nothing
        # downstream mistakes a handle for a name.
        display_name = real_name or author

        # Contact priority: the org's published address, then the person's.
        # Both were published by the person/company on purpose — these are
        # REAL addresses, not guesses, so enrichment must not overwrite them.
        email = gh_org.get("email") or found_email
        email_kind = (
            "company_org" if gh_org.get("email") else profile.get("email_kind")
        )

        # A company name that is really a status phrase ("Built Algebrix",
        # "Made with AI") is not a company. Prefer the org's real name.
        company_name = gh_org.get("name") or clean_title
        if not company_name or len(company_name) < 3:
            company_name = domain

        return RawCandidate(
            full_name=display_name,
            job_title="Founder",
            company_name=company_name,
            # Product URL first, then the GitHub org's website, then a
            # personal site in the HN profile.
            company_domain=domain or personal_site,
            email=email,
            github_url=github_repo or (
                f"https://github.com/{profile['github']}" if profile.get("github") else None
            ),
            twitter_url=(
                f"https://twitter.com/{profile['twitter']}"
                if profile.get("twitter")
                else (
                    f"https://twitter.com/{gh_org['twitter']}"
                    if gh_org.get("twitter") else None
                )
            ),
            source="hackernews",
            source_details={
                "bio": bio,
                "hn_username": author,
                "name_is_handle": real_name is None,
                "real_name_from_profile": real_name,
                "email_from_profile": email,
                "email_kind": email_kind,
                # Distinguishes a real published address from a guessed
                # one, so Chunk 5 knows where to focus.
                "email_is_real": bool(email),
                "personal_site": personal_site,
                "about_text": profile.get("about_text"),
                "hn_karma": profile.get("karma"),
                "source_url": f"https://news.ycombinator.com/item?id={story_id}",
                "hn_story_id": story_id,
                "hn_points": hit.get("points"),
                "hn_comments": hit.get("num_comments"),
                "hn_posted_at": hit.get("created_at"),
                "product_url": url,
                "via_github": bool(gh_org),
                "b2b_signal": bool(B2B_SIGNALS.search(clean_title)),
            },
        )

    def _profile(self, username: str) -> dict:
        """
        Fetch and parse an HN profile. Cached for the whole run.

        The `about` field is the single most valuable thing this adapter
        has access to, because HN exposes no other person data. People
        routinely publish their real name AND a real email address there
        (verified: "Alex. I build kern... hello@getkern.dev",
        "contact: ... tshepohlala@gmail.com"). That converts HN leads from
        "a username and a domain" into actually contactable people —
        without inventing anything.
        """
        if username in self._user_cache:
            return self._user_cache[username]

        data: dict = {}
        try:
            resp = self.session.get(ALGOLIA_USER + username, timeout=12)
            if resp.status_code == 200:
                data = resp.json() or {}
        except requests.RequestException:
            data = {}

        about_text = _hn_about_to_text(data.get("about"))
        # Keep the raw text too — `_hn_about_to_text` strips URLs, and a
        # personal site URL in the about is itself a domain lead.
        about_raw = html.unescape(html.unescape(data.get("about") or ""))

        email, email_kind = self._extract_email(about_raw)
        real_name = self._extract_real_name(about_text)
        twitter = self._extract_handle(about_raw, "twitter.com")
        github = self._extract_handle(about_raw, "github.com")
        personal_site = self._extract_personal_site(about_raw)

        parsed = {
            "about_text": about_text,
            "about_raw": about_raw,
            "email": email,
            "email_kind": email_kind,   # "company" | "personal" | None
            "real_name": real_name,
            "twitter": twitter,
            "github": github,
            "personal_site": personal_site,
            "karma": data.get("karma"),
        }
        self._user_cache[username] = parsed
        return parsed

    @staticmethod
    def _extract_email(about_raw: str) -> tuple[str | None, str | None]:
        for match in EMAIL_RE.findall(about_raw):
            addr = match.lower()
            domain = addr.split("@", 1)[1]
            if domain in JUNK_EMAIL_DOMAINS:
                continue
            if domain in PERSONAL_EMAIL_DOMAINS:
                return addr, "personal"
            return addr, "company"
        return None, None

    @staticmethod
    def _extract_real_name(about_text: str) -> str | None:
        """
        Find a person's actual name in the profile text.

        Only trusted when it appears at the START of the about field —
        people introduce themselves that way ("Alex. I build kern...").
        Anywhere else in a sentence it's far more likely to be a product
        or company name, and storing that as a person's name would
        corrupt every downstream merge.
        """
        if not about_text:
            return None
        head = about_text[:120]
        m = REAL_NAME_RE.search(head)
        if not m:
            return None
        first, last = m.group(1), m.group(2)
        if first in NOT_A_NAME or last in NOT_A_NAME:
            return None
        return f"{first} {last}"

    @staticmethod
    def _extract_handle(about_raw: str, host: str) -> str | None:
        m = re.search(
            host.replace(".", r"\.") + r"[/@]([A-Za-z0-9_.-]{2,40})", about_raw
        )
        return m.group(1) if m else None

    @staticmethod
    def _extract_personal_site(about_raw: str) -> str | None:
        """First URL in the about that isn't a known social/profile host."""
        for url in re.findall(r"https?://[^\s<>\"']+", about_raw):
            host = _clean_host(url)
            if host and not _is_junk_host(host):
                return host
        return None
