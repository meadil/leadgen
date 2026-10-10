"""
Shared data shapes passed between pipeline stages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RawCandidate:
    """
    What a Stage 2 source adapter returns. Deliberately thin — Stage 3
    (enrichment) fills in the gaps before Stage 4 scores it.
    """
    full_name: Optional[str] = None
    job_title: Optional[str] = None
    company_name: Optional[str] = None
    company_domain: Optional[str] = None
    email: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    twitter_url: Optional[str] = None
    geography_country: Optional[str] = None
    geography_city: Optional[str] = None

    source: str = ""                      # which adapter produced this, e.g. "github"
    source_details: dict = field(default_factory=dict)  # raw payload, kept for debugging/audit
