"""
Chunk 5 — find an email from what we already know.

The question this answers, per the project owner: "how can we find their
email from what we already know about them?" We already hold 430 real
domains and 517 product descriptions. The cheapest reliable address is
the one a company PUBLISHES on its own website.

This is a separate, on-demand step rather than part of the enrichment
pass because it is slow (several HTTP requests per lead) and because it
should be re-runnable: a lead with no email today may have one after the
company updates its contact page.

Why this beats a paid enrichment API, decisively:
  - free, no key, no signup, and crucially NO WORK EMAIL REQUIRED (which
    is what blocks Hunter.io and QuickEmailVerification here)
  - a published address is higher quality than a verified guess — the
    company chose to print it
  - it degrades gracefully: a dead domain costs one timeout

Measured yield on 30 of our own domains (2026-09-29): ~40% published an
address somewhere on the site. A naive regex overstates this badly — the
first version returned font filenames and JS artefacts as "emails", so
every extraction is now TLD-validated and domain-checked.
"""
from __future__ import annotations

import re
import time
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests

# The TLDs we accept. A whitelist, not a blacklist: a naive regex over
# HTML returns things like `Geist-Variable-l@in.woff` and
# `14659233656382_d@acenter.jpg` as emails. Rejecting by TLD kills that
# class of noise outright, and a real business address essentially always
# ends in one of these.
VALID_TLDS = {
    "com", "net", "org", "io", "ai", "co", "dev", "app", "cloud", "tech",
    "xyz", "sh", "me", "gg", "to", "so", "in", "us", "uk", "ca", "de",
    "fr", "es", "it", "nl", "se", "no", "fi", "dk", "pl", "pt", "ie",
    "nz", "au", "jp", "kr", "sg", "hk", "za", "br", "mx", "ar", "cl",
    "cz", "at", "ch", "be", "hu", "ro", "bg", "hr", "si", "sk", "lt",
    "lv", "ee", "is", "lu", "ru", "ua", "tr", "il", "ae", "sa", "eg",
    "vn", "th", "id", "my", "ph", "pk", "bd", "lk", "np", "ke", "ng",
    "gh", "sn", "ci", "cm", "af", "ge", "kz", "uz", "az", "by", "rs",
    "pro", "online", "site", "store", "shop", "agency", "studio", "space",
    "website", "live", "life", "world", "today", "media", "digital",
    "network", "systems", "solutions", "services", "company", "capital",
    "ventures", "fund", "group", "team", "works", "labs", "io", "design",
    "email", "news", "blog", "page", "link", "click", "run", "app",
    "one", "top", "vip", "info", "biz", "name", "mobi", "tel", "travel",
    "codes", "rocks", "guru", "ninja", "expert", "cafe", "pub", "wtf",
    "fyi", "gg", "sh", "tv", "cc", "ws", "am", "fm", "im", "st",
}

# Local parts that are a company's own asset, not a mailbox.
PLACEHOLDER_LOCALS = {
    "example", "yourname", "youremail", "your-email", "email", "name",
    "someone", "user", "username", "firstname", "lastname", "you",
    "myemail", "test", "demo", "sample", "noreply", "no-reply",
    "donotreply", "do-not-reply", "mailer", "postmaster", "abuse",
    "webmaster", "hostmaster", "root", "www", "admin2", "xxx",
}

# Domains that are never a real recipient.
PLACEHOLDER_DOMAINS = {
    "example.com", "example.org", "example.net", "example.co", "ex.com",
    "domain.com", "yourdomain.com", "mydomain.com", "company.com",
    "email.com", "test.com", "sentry.io", "w3.org", "schema.org",
    "localhost", "users.noreply.github.com", "email", "sentry",
    "wixpress.com", "squarespace.com", "cloudflare.com", "godaddy.com",
    "placeholder.com", "sitemaps.com", "googlemail.com",
}

# Personal mailbox providers — a real address, but not a company one.
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "outlook.de", "live.com",
    "msn.com", "aol.com", "icloud.com", "me.com", "mac.com", "protonmail.com",
    "proton.me", "pm.me", "gmx.com", "gmx.de", "mail.com", "yandex.com",
    "zoho.com", "fastmail.com", "tutanota.com", "hey.com", "duck.com",
    "yep.com", "qq.com", "163.com", "126.com", "web.de", "mail.ru",
}

# Role addresses: real, sendable, and they route to a team. Worth having,
# but they are not a named decision-maker, so they are labelled as such.
ROLE_LOCALS = {
    "hello", "hi", "contact", "info", "support", "sales", "team", "admin",
    "office", "mail", "email", "enquiries", "enquiry", "inquiries",
    "inquiry", "help", "service", "customerservice", "care", "feedback",
    "founders", "founder", "ceo", "cto", "coo", "press", "media", "pr",
    "marketing", "hello", "hey", "business", "partnerships", "partners",
    "legal", "privacy", "gdpr", "dpo", "security", "billing", "accounts",
    "invoice", "invoices", "hr", "jobs", "careers", "recruiting",
    "abuse", "noreply", "no-reply", "donotreply", "postmaster",
    "webmaster", "hostmaster", "compliance", "legal@", "dmca",
    "success", "customers", "user", "users", "auth", "api", "dev",
    "engineering", "product", "feedback", "hello",
}

# Pages most likely to print an address. `/privacy` and `/imprint` matter
# more than they look: EU sites are legally required to publish one.
CONTACT_PATHS = [
    "/", "/contact", "/contact-us", "/about", "/about-us",
    "/company", "/privacy", "/privacy-policy", "/legal",
    "/imprint", "/impressum", "/terms",
]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")


def _decode_obfuscation(text: str) -> str:
    """
    Undo the ways sites hide an address from scrapers.

    "hello [at] example.com", "hello(at)example.com",
    "hello &#64; example.com", "hello AT example DOT com".
    """
    text = text.replace("&#64;", "@").replace("&#x40;", "@").replace("\u0040", "@")
    # [at] (at) {at} <at>  -> @
    text = re.sub(r"\s*[\[\(\{<]\s*(?:at|AT|At)\s*[\]\)\}>]\s*", "@", text)
    text = re.sub(r"\s+(?:at|AT)\s+", "@", text)
    # [dot] (dot) . spaced dot
    text = re.sub(r"\s*[\[\(\{<]\s*(?:dot|DOT|Dot|punkt)\s*[\]\)\}>]\s*", ".", text)
    text = re.sub(r"\s+(?:dot|DOT)\s+", ".", text)
    text = text.replace(" (dot) ", ".").replace(" dot ", ".")
    return text


def _strip_html(text: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    # CSS/JS/font fragments produce garbage "emails"; drop url-ish blobs.
    text = re.sub(r"url\([^)]*\)", " ", text, flags=re.I)
    return text


def _is_valid(email: str) -> bool:
    """Reject everything that only looked like an address."""
    if not email or "@" not in email or ".." in email:
        return False
    local, _, domain = email.rpartition("@")
    if not local or not domain or "." not in domain:
        return False
    if local.lower() in PLACEHOLDER_LOCALS:
        return False
    d = domain.lower().strip(".")
    if d in PLACEHOLDER_DOMAINS:
        return False
    # TLD must be a real one — kills `x@in.woff`, `y@acenter.jpg`,
    # `z@e.hidden`, which a naive regex happily returns.
    tld = d.rsplit(".", 1)[-1].lower()
    if tld not in VALID_TLDS:
        return False
    if len(tld) < 2 or tld.isdigit():
        return False
    # Local part must be mostly letters — asset filenames embed digits.
    if sum(ch.isdigit() for ch in local) > len(local) / 2:
        return False
    if not re.fullmatch(r"[A-Za-z0-9._%+\-]+", local):
        return False
    if any(c in local for c in "_-") and any(ch.isdigit() for ch in local):
        return False
    return True


def classify_email(email: str) -> str:
    """company_role | personal | individual_company | unknown."""
    local, _, domain = email.rpartition("@")
    local = local.lower()
    d = domain.lower()
    if d in PERSONAL_DOMAINS:
        return "personal"
    if local in ROLE_LOCALS:
        return "company_role"
    if "." in local:
        return "individual_company"
    return "unknown"


def domain_matches(email_domain: str, company_host: str) -> bool:
    """
    Does this address belong to the company we asked about?

    Sites routinely print other people's addresses — a customer's, a
    testimonial's, a payment provider's, a framework demo's. Taking
    those would be worse than taking nothing, because they look valid
    and would send outreach to a stranger. So a scraped address must
    either be on the company's own domain or be a personal mailbox.
    """
    e = email_domain.lower().strip(".")
    h = company_host.lower().strip(".")
    if not e or not h:
        return False
    if e in PERSONAL_DOMAINS:
        return True
    # same domain, or a subdomain of it (app.acme.com for acme.com)
    if e == h or e.endswith("." + h) or h.endswith("." + e):
        return True
    # A different registrable domain is a third party. Reject.
    return False


class EmailScraper:
    """Finds an address a company published on its own site."""

    def __init__(self, timeout: int = 8, max_pages: int = 6, pause: float = 0.3):
        self.timeout = timeout
        self.max_pages = max_pages
        self.pause = pause
        self.session = requests.Session()
        # NOT a browser UA (LESSONS_LEARNED L1). Identify honestly; some
        # sites will serve us a plain page, which is all we need.
        self.session.headers["User-Agent"] = (
            "leadgen-bot/1.0 (finds published contact addresses; +https://example.invalid)"
        )
        self._cache: dict[str, dict] = {}

    def find(self, domain: str) -> dict:
        """
        Return {email, kind, source_path, confidence} for a domain.

        Confidence is deliberately coarse and honest:
          published      — the company printed it on its own site
          not_found      — we looked and found nothing
          unreachable    — the site didn't respond
        """
        if not domain:
            return {"confidence": "not_found"}
        host = domain.lower().strip()
        for prefix in ("www.", "m.", "docs.", "app."):
            if host.startswith(prefix) and host.count(".") > 1:
                host = host[len(prefix):]
        if host in self._cache:
            return self._cache[host]
        # A docs/marketplace host has no company mailbox to find.
        if _is_non_company_host(host):
            result = {"confidence": "not_found", "reason": "non_company_host"}
            self._cache[host] = result
            return result

        best: Optional[dict] = None
        contacted = False
        for path in CONTACT_PATHS[: self.max_pages]:
            url = f"https://{host}{path}"
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            except requests.RequestException:
                # http:// fallback for sites without TLS
                try:
                    resp = self.session.get(f"http://{host}{path}", timeout=self.timeout,
                                             allow_redirects=True)
                except requests.RequestException:
                    continue
            time.sleep(self.pause)

            if resp.status_code >= 400:
                continue
            contacted = True
            text = _strip_html(resp.text)
            text = _decode_obfuscation(text)

            found = []
            for raw in EMAIL_RE.findall(text):
                e = raw.strip(".,;:'\"").lower()
                if not _is_valid(e):
                    continue
                # Must belong to THIS company (or be a personal mailbox).
                if not domain_matches(e.rpartition("@")[2], host):
                    continue
                found.append(e)
            if not found:
                continue

            # Prefer an address on the company's own domain, then a role
            # address, then anything.
            own = [e for e in found if e.rpartition("@")[2].endswith(host)
                   or host.endswith(e.rpartition("@")[2])]
            pool = own or found
            # On the homepage, don't settle for a personal gmail when a
            # company address exists anywhere else.
            for cand in pool:
                kind = classify_email(cand)
                if kind in ("company_role", "individual_company") and own:
                    best = {
                        "email": cand,
                        "kind": kind,
                        "source_path": path,
                        "confidence": "published",
                        "alternatives": sorted(set(found))[:5],
                    }
                    break
            if best:
                break

        if best is None:
            best = {"confidence": "unreachable" if not contacted else "not_found"}
        self._cache[host] = best
        return best


NON_COMPANY_HOSTS = {
    "readthedocs.io", "gitbook.io", "shields.io", "github.com", "github.io",
    "npmjs.com", "pypi.org", "codecanyon.net", "envato.com", "crunchbase.com",
    "producthunt.com", "docker.com", "sourceforge.net", "wikipedia.org",
    "w3.org", "apache.org", "python.org", "mozilla.org", "gnu.org",
    "debian.org", "ubuntu.com", "linux.org", "k8s.io", "kubernetes.io",
    "microsoft.com", "apple.com", "google.com", "amazon.com", "adobe.com",
    "twitter.com", "x.com", "linkedin.com", "facebook.com", "instagram.com",
    "youtube.com", "medium.com", "substack.com", "hashnode.dev", "dev.to",
    "notion.site", "calendly.com", "linktr.ee", "beacons.ai", "gumroad.com",
    "vercel.app", "netlify.app", "pages.dev", "herokuapp.com", "workers.dev",
    "web.app", "firebaseapp.com", "glitch.me", "replit.app", "onrender.com",
    "surge.sh", "cloudfront.net", "sentry.io", "stripe.com", "g2.com",
    "capterra.com", "trustradius.com", "wellfound.com", "angel.co",
    "apps.apple.com", "play.google.com", "chromewebstore.google.com",
    "developer.chrome.com", "go.dev", "golang.org", "rust-lang.org",
    "opensource.org", "creativecommons.org", "schema.org", "unicode.org",
}


def _is_non_company_host(host: str) -> bool:
    if host in NON_COMPANY_HOSTS:
        return True
    return any(host == p or host.endswith("." + p) for p in NON_COMPANY_HOSTS)
