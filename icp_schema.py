"""
Stage 1 — ICP intake.

Loads an ICP definition (a JSON file matching icp-definition-schema.md)
and validates it. Nothing downstream runs on an ICP that fails validation
— that's the whole point of making this a hard contract instead of a
loose dict.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class BusinessModel(str, Enum):
    b2b = "B2B"
    b2c = "B2C"
    b2b2c = "B2B2C"
    marketplace = "Marketplace"


class SeniorityLevel(str, Enum):
    c_level = "c_level"
    vp = "vp"
    director = "director"
    manager = "manager"
    ic = "ic"


class Department(str, Enum):
    sales = "sales"
    marketing = "marketing"
    engineering = "engineering"
    ops = "ops"
    hr = "hr"
    finance = "finance"
    founder = "founder"


class DecisionAuthority(str, Enum):
    decision_maker = "decision_maker"
    influencer = "influencer"
    end_user = "end_user"


class TriggerEvent(str, Enum):
    recently_funded = "recently_funded"
    hiring_for_role = "hiring_for_role"
    leadership_change = "leadership_change"
    product_launch = "product_launch"
    expansion = "expansion"
    tech_migration = "tech_migration"
    none_required = "none_required"


class PainIndicatorSource(str, Enum):
    job_postings = "job_postings"
    website_copy = "website_copy"
    reviews = "reviews"
    social_posts = "social_posts"


class ICPProfile(BaseModel):
    # G. metadata
    icp_name: str

    # A. company firmographics
    industry: str
    sub_industry: Optional[str] = None
    company_size_employees_min: int
    company_size_employees_max: int
    revenue_range: Optional[str] = None
    geography_countries: list[str]
    geography_regions_cities: list[str] = Field(default_factory=list)
    business_model: BusinessModel
    founding_year_min: Optional[int] = None
    funding_stage: Optional[str] = None
    tech_stack_uses: list[str] = Field(default_factory=list)

    # B. person / contact
    job_titles: list[str]
    seniority_level: SeniorityLevel
    department: Department
    decision_authority: DecisionAuthority
    tenure_months_min: Optional[int] = None

    # C. trigger / timing signals
    trigger_events: list[TriggerEvent] = Field(default_factory=list)
    trigger_recency_days_max: Optional[int] = None

    # D. pain / problem-fit signals
    pain_indicators: list[str]
    pain_indicator_sources: list[PainIndicatorSource]

    # E. exclusion criteria
    excluded_industries: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    excluded_geographies: list[str] = Field(default_factory=list)
    already_in_crm: bool = True

    # F. scoring configuration
    field_weights: dict[str, float]
    minimum_score_threshold: int = Field(ge=0, le=100)

    @field_validator("company_size_employees_max")
    @classmethod
    def max_gte_min(cls, v, info):
        min_v = info.data.get("company_size_employees_min")
        if min_v is not None and v < min_v:
            raise ValueError(
                "company_size_employees_max must be >= company_size_employees_min"
            )
        return v


def load_icp(path: str) -> ICPProfile:
    """Load and validate an ICP definition from a JSON file.

    Raises a clear pydantic ValidationError listing every missing or
    malformed field if the ICP doc is incomplete — by design, nothing
    runs on a half-filled-out ICP.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return ICPProfile(**data)
