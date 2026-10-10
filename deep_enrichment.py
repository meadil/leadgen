"""
Optional Stage 3.5 - AI-powered deep enrichment.

Fetches a lead's company homepage and asks MiMo-V2.6-Flash (via OpenCode
Zen) to actually read it and judge industry fit, pain-point evidence, and
pull any visible team emails - instead of the crude keyword matching in
enrichment.py. Slower (one fetch + one AI call per candidate) and opt-in:
only runs if OPENCODE_API_KEY is set in .env.
"""
from __future__ import annotations

import json
import os
import re

import requests
from bs4 import BeautifulSoup

from icp_schema import ICPProfile
from models import RawCandidate

OPENCODE_ZEN_URL = "https://opencode.ai/zen/v1/chat/completions"
MODEL = "mimo-v2.6-flash-free"


def _fetch_homepage_text(domain: str, max_chars: int = 4000) -> str | None:
    for scheme in ("https://", "http://"):
        try:
            resp = requests.get(
                f"{scheme}{domain}", timeout=10, headers={"User-Agent": "Mozilla/5.0"}
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            text = re.sub(r"\s+", " ", soup.get_text()).strip()
            return text[:max_chars]
        except requests.RequestException:
            continue
    return None


def _ask_mimo(page_text: str, candidate: RawCandidate, icp: ICPProfile) -> dict | None:
    api_key = os.getenv("OPENCODE_API_KEY")
    if not api_key:
        raise RuntimeError("OPENCODE_API_KEY not set in .env")

    prompt = f"""You are qualifying a sales lead against an ICP. Read the company
website text below and answer ONLY with a JSON object, no other text.

ICP industry: {icp.industry}
ICP pain indicators to look for: {icp.pain_indicators}

Company: {candidate.company_name}
Website text: {page_text}

Return exactly this JSON shape:
{{
  "is_target_industry": true/false,
  "industry_evidence": "short quote or phrase from the text, or null",
  "pain_indicators_matched": ["indicators from the ICP list this website supports"],
  "extracted_emails": ["any visible email addresses, empty list if none"],
  "estimated_company_size": "a rough guess like '1-10', '11-50', or null"
}}"""

    try:
        resp = requests.post(
            OPENCODE_ZEN_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": MODEL, "messages": [{"role": "user", "content": prompt}], "temperature": 0},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        body = ""
        if e.response is not None:
            try:
                body = f": {e.response.text[:300]}"
            except Exception:
                pass
        print(f"    (deep enrichment: Zen API call failed for {candidate.full_name} ({e}){body})")
        return None
    raw = resp.json()["choices"][0]["message"]["content"]

    cleaned = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        print(f"    (deep enrichment: couldn't parse model response for {candidate.full_name})")
        return None


def deep_enrich(candidate: RawCandidate, icp: ICPProfile) -> RawCandidate:
    if not candidate.company_domain:
        return candidate

    page_text = _fetch_homepage_text(candidate.company_domain)
    if not page_text:
        return candidate

    result = _ask_mimo(page_text, candidate, icp)
    if not result:
        return candidate

    candidate.source_details["deep_enrichment"] = result

    if not candidate.email and result.get("extracted_emails"):
        candidate.email = result["extracted_emails"][0]
        candidate.source_details["email_status"] = "scraped_from_website"

    return candidate
