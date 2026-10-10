"""
Stage 4 (scoring half) — combines weighted field matches into one 0-100
score per candidate. Anything below icp.minimum_score_threshold gets
dropped before it ever reaches the CRM. The breakdown is kept and stored
alongside the lead so you can later see which signals actually
correlated with replies.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

from icp_schema import ICPProfile
from models import RawCandidate


def _title_score(candidate: RawCandidate, icp: ICPProfile) -> float:
    if not candidate.job_title:
        return 0.0
    best = max(
        (fuzz.partial_ratio(candidate.job_title.lower(), t.lower()) for t in icp.job_titles),
        default=0,
    )
    return best / 100.0


def _geography_score(candidate: RawCandidate, icp: ICPProfile) -> float:
    location_text = " ".join(
        filter(None, [candidate.geography_city, candidate.geography_country])
    ).lower()
    targets = icp.geography_regions_cities + icp.geography_countries
    if not location_text or not targets:
        return 0.0
    best = max((fuzz.partial_ratio(location_text, t.lower()) for t in targets), default=0)
    return best / 100.0


# Industry synonyms are matched on whole tokens, so each entry has to be
# a word that genuinely implies software/SaaS. "app" and "tool" are
# deliberately absent: they are the most common words in the *worst*
# matches ("a cute app for tracking your pet photos"), and treating them
# as industry signals let a consumer photo app score 100% industry fit.
INDUSTRY_SYNONYMS = {
    "software": ["saas", "software", "platform", "b2b", "b2c", "cloud", "saas",
                 "api", "dashboard", "automation", "workflow", "crm", "erp"],
    "saas": ["saas", "software", "platform", "b2b", "cloud", "api", "dashboard",
             "automation", "workflow", "crm", "analytics", "integration",
             "subscription", "onboarding"],
    "b2b": ["saas", "software", "platform", "b2b", "cloud", "api", "enterprise",
            "workflow", "productivity", "analytics", "crm", "erp", "invoice",
            "billing", "compliance", "operations", "procurement", "sales"],
    "fintech": ["fintech", "finance", "banking", "payments", "billing", "invoice",
                "accounting", "insurance", "crypto", "trading", "payments"],
    "technology": ["saas", "software", "platform", "b2b", "cloud", "ai",
                   "developer", "api", "automation", "open-source", "devops"],
}




def _industry_score(candidate: RawCandidate, icp: ICPProfile) -> float:
    """
    Industry fit, graded rather than pass/fail.

    An explicit category claim is full credit. A broader functional
    signal ("platform", "agent", "dashboard", "deploy") is partial
    credit, because a product described by what it DOES ("Core Metric
    Suite", "Enterprise Postgres for AI") is a legitimate SaaS business
    that no closed keyword list will ever match — but a functional
    keyword is a weaker signal than a category claim, so the two must
    not score the same. Making them equal is how a functional filter
    turns into a rubber stamp (LESSONS_LEARNED L11).
    """
    # Trust a real AI read of the company website over the keyword
    # heuristic, when deep enrichment ran.
    deep = candidate.source_details.get("deep_enrichment")
    if deep is not None:
        return 1.0 if deep.get("is_target_industry") else 0.0

    # Matches against a synonym list, not just the literal word — a bio
    # that says "SaaS" or "B2B platform" should count for an ICP whose
    # industry is "Software", not just a literal "software" mention.
    #
    # The company name matters more than the tagline here: a PH product
    # called "Compliance Posture" with the tagline "B2B compliance
    # tracking" is unambiguously SaaS even though neither string says
    # the word "saas".
    text = " ".join(
        filter(None, [candidate.company_name, candidate.source_details.get("bio")])
    ).lower()
    if not text:
        return 0.0
    terms = INDUSTRY_SYNONYMS.get(icp.industry.lower(), [icp.industry.lower()])
    # Word-boundary match, so "app" doesn't fire on "happy" and "tool"
    # doesn't fire on "toolkit" being irrelevant etc. Short/generic terms
    # still need care, so match on tokens.
    tokens = set(re.findall(r"[a-z0-9]+", text))
    for term in terms:
        if " " in term:
            if term in text:
                return 1.0
        elif term in tokens:
            return 1.0

    # No explicit category claim in the text. If the source adapter has
    # already judged this to be business software from functional
    # language, award partial credit — enough to clear a sane threshold
    # alongside a founder title, not enough to pass alone.
    if candidate.source_details.get("b2b_signal"):
        return 0.6

    return 0.0


def _pain_indicator_score(candidate: RawCandidate, icp: ICPProfile) -> float:
    text = (candidate.source_details.get("bio") or "").lower()
    if not text or not icp.pain_indicators:
        return 0.0
    hits = sum(1 for p in icp.pain_indicators if p.lower() in text)
    return min(hits / len(icp.pain_indicators), 1.0)


def _company_size_score(candidate: RawCandidate, icp: ICPProfile) -> float:
    """
    Fit against the ICP's company-size band, from a SIZE PROXY.

    No free source in this project reports headcount, so where the only
    available signal is GitHub org followers, we estimate a band and
    score against the ICP. This is a proxy and it is noisy — but it is
    what separates a 10-100 person SaaS from Supabase or n8n, which
    otherwise dominate every GitHub-derived source.

    Scored as a soft signal, not a hard gate: a wrong estimate should
    lower a score, not delete a real company.
    """
    followers = candidate.source_details.get("org_followers")
    if followers is None:
        stars = candidate.source_details.get("stars")
        # Stars are a much weaker size signal than org followers; only
        # use them when there is nothing better, at a discount.
        if stars is None:
            return 0.0
        est = _followers_to_employees(None, stars)
    else:
        est = _followers_to_employees(followers, None)

    lo = icp.company_size_employees_min
    hi = icp.company_size_employees_max
    if est is None or lo is None or hi is None:
        return 0.0
    if lo <= est <= hi:
        return 1.0
    if est < lo:
        # Under the floor. Solo/very small companies are still valid
        # founders to reach, so this is penalised, not zeroed.
        return 0.6 if est >= lo / 2 else 0.3
    # Over the ceiling. The further over, the worse — a 200-person
    # company in a 10-100 band is a mild miss; a 5000-person one is not.
    ratio = est / max(hi, 1)
    if ratio <= 2:
        return 0.5
    if ratio <= 6:
        return 0.25
    return 0.0


def _followers_to_employees(followers: int | None, stars: int | None) -> int | None:
    """
    Rough headcount estimate from an open-source presence.

    Calibrated by eye against known cases (2026-09-29):
      omnistrate (~30 staff)  8 followers   -> very small
      documenso (~50)        235 followers
      formbricks (~60)       273 followers
      calcom (~100)        2178 followers
      supabase (~800)     11987 followers
      n8n (~1000+)       15017 followers

    Deliberately conservative and clearly labelled a proxy. Never
    present the result to a human as a real employee count.
    """
    if followers is not None:
        f = max(0, int(followers))
        if f < 30:
            return 8
        if f < 150:
            return 25
        if f < 400:
            return 50
        if f < 1200:
            return 90
        if f < 3000:
            return 160
        if f < 8000:
            return 400
        return 1200
    if stars is not None:
        s = max(0, int(stars))
        if s < 50:
            return 8
        if s < 500:
            return 40
        if s < 5000:
            return 120
        return 400
    return None


FIELD_SCORERS = {
    "job_titles": _title_score,
    "geography": _geography_score,
    "industry": _industry_score,
    "pain_indicators": _pain_indicator_score,
    "company_size": _company_size_score,
}


def _has_data(field_name: str, candidate: RawCandidate, icp: ICPProfile) -> bool:
    """
    Could this field be judged for this candidate at all?

    Scoring a field as 0 when the SOURCE simply never provided the data
    is not a judgement about the lead, it's an artefact of the source.
    Product Hunt exposes no location and no pain-signal copy, so every PH
    lead was silently losing 0.15 + 0.25 = 40% of the available weight
    and could never score above 60/100 (PLAN.md F4). That is why the
    threshold had to be dropped to 50 — it was compensating for a
    scoring bug, not for genuine lead quality.
    """
    if field_name == "geography":
        return bool(candidate.geography_city or candidate.geography_country)
    if field_name == "pain_indicators":
        return bool(candidate.source_details.get("bio"))
    if field_name == "job_titles":
        return bool(candidate.job_title)
    if field_name == "industry":
        return bool(
            candidate.company_name
            or candidate.source_details.get("bio")
            or candidate.source_details.get("deep_enrichment") is not None
        )
    if field_name == "company_size":
        return bool(
            candidate.source_details.get("org_followers") is not None
            or candidate.source_details.get("stars") is not None
        )
    return True


def score_candidate(candidate: RawCandidate, icp: ICPProfile) -> tuple[float, dict]:
    """
    Weighted 0-100 score.

    Fields the source gave us no data for are EXCLUDED from the average
    rather than counted as zero, so a well-matched lead from a sparse
    source isn't punished for the source's gaps. The breakdown records
    `data_coverage` (the share of total weight that could actually be
    judged) so a high score built on one signal is still visible as such.
    """
    breakdown: dict[str, float] = {}
    weighted_sum = 0.0
    judged_weight = 0.0
    total_weight = 0.0

    for field_name, weight in icp.field_weights.items():
        scorer = FIELD_SCORERS.get(field_name)
        if scorer:
            total_weight += weight
            if not _has_data(field_name, candidate, icp):
                # No data from this source — don't score it either way.
                breakdown[field_name] = None
                continue
            field_score = scorer(candidate, icp)
            breakdown[field_name] = round(field_score * 100, 1)
            weighted_sum += field_score * weight
            judged_weight += weight
        else:
            total_weight += weight

    if judged_weight == 0:
        return 0.0, breakdown

    breakdown["_data_coverage"] = (
        round(judged_weight / total_weight, 2) if total_weight else 0.0
    )
    return round((weighted_sum / judged_weight) * 100, 1), breakdown
