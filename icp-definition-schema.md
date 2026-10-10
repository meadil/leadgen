# ICP Definition Schema

This is the required input contract for the lead generation engine. Nothing runs until every
**required** key below has a value. No intuition, no vibes — if a field isn't filled in, the
engine doesn't know what to search for.

Each field below lists: type, whether it's required, an example, and a **discoverability** tag
(how easy it is to find via free/public sources — this directly determines which source
adapters we can build in Stage 2).

---

## A. Company firmographics

| Key | Type | Required | Example | Discoverability |
|---|---|---|---|---|
| `industry` | string | yes | `"B2B SaaS"` | Easy — company sites, LinkedIn, directories |
| `sub_industry` | string | no | `"DevOps tooling"` | Medium |
| `company_size_employees_min` | integer | yes | `10` | Easy — LinkedIn company pages |
| `company_size_employees_max` | integer | yes | `200` | Easy |
| `revenue_range` | string | no | `"$1M-$10M ARR"` | Hard (rarely public) |
| `geography_countries` | array\<string\> | yes | `["US", "UK", "Canada"]` | Easy |
| `geography_regions_cities` | array\<string\> | no | `["Austin", "Remote"]` | Easy |
| `business_model` | enum | yes | `B2B` / `B2C` / `B2B2C` / `Marketplace` | Easy |
| `founding_year_min` | integer | no | `2015` | Medium — Crunchbase, registries |
| `funding_stage` | enum | no | `bootstrapped` / `seed` / `series_a` / `series_b_plus` / `public` / `unknown` | Medium — Crunchbase free tier |
| `tech_stack_uses` | array\<string\> | no | `["Shopify", "HubSpot"]` | Medium — BuiltWith/Wappalyzer |

## B. Person / contact criteria

| Key | Type | Required | Example | Discoverability |
|---|---|---|---|---|
| `job_titles` | array\<string\> | yes | `["Head of Growth", "VP Marketing"]` | Easy |
| `seniority_level` | enum | yes | `c_level` / `vp` / `director` / `manager` / `ic` | Easy |
| `department` | enum | yes | `sales` / `marketing` / `engineering` / `ops` / `hr` / `finance` / `founder` | Easy |
| `decision_authority` | enum | yes | `decision_maker` / `influencer` / `end_user` | Medium — inferred from title |
| `tenure_months_min` | integer | no | `6` | Medium |

## C. Trigger / timing signals

| Key | Type | Required | Example | Discoverability |
|---|---|---|---|---|
| `trigger_events` | array\<enum\> | no | `["recently_funded", "hiring_for_role"]` | Medium — varies by source |
| `trigger_recency_days_max` | integer | no | `30` | Medium |

Allowed values: `recently_funded`, `hiring_for_role`, `leadership_change`, `product_launch`,
`expansion`, `tech_migration`, `none_required`

## D. Problem-fit / pain signals

| Key | Type | Required | Example | Discoverability |
|---|---|---|---|---|
| `pain_indicators` | array\<string\> | yes | `["no dedicated marketing hire", "manual reporting mentioned in job post"]` | Hard — needs text analysis |
| `pain_indicator_sources` | array\<enum\> | yes | `["job_postings", "website_copy", "reviews", "social_posts"]` | — |

This is the field most people skip, and it's the one that actually separates "matches my ICP"
from "will actually want what I'm selling." Be concrete: not "struggles with X" but a thing you
could point at on a webpage or job listing as evidence.

## E. Exclusion criteria (auto-disqualify)

| Key | Type | Required | Example | Discoverability |
|---|---|---|---|---|
| `excluded_industries` | array\<string\> | no | `["Government", "Non-profit"]` | Easy |
| `excluded_companies` | array\<string\> | no | `["Existing customers", "Competitor X's clients"]` | Easy — your own records |
| `excluded_geographies` | array\<string\> | no | `["Sanctioned regions"]` | Easy |
| `already_in_crm` | boolean | yes | `true` | Easy — checked automatically against CRM |

## F. Scoring configuration (used by Stage 4)

| Key | Type | Required | Example |
|---|---|---|---|
| `field_weights` | object\<string, float\> | yes | `{"industry": 0.2, "job_titles": 0.25, "trigger_events": 0.15, "pain_indicators": 0.25, "geography": 0.15}` |
| `minimum_score_threshold` | integer (0-100) | yes | `70` |

Weights should roughly sum to 1.0 across whichever fields you choose to weight — you don't have
to weight every field, only the ones that actually predict a good lead for this ICP.

## G. Profile metadata

| Key | Type | Required | Example |
|---|---|---|---|
| `icp_name` | string | yes | `"US SaaS marketing leaders — Q4"` |
| `created_at` | datetime | yes (auto-set) | — |

---

## Example: a filled ICP profile

```json
{
  "icp_name": "US SaaS marketing leaders — Q4",
  "industry": "B2B SaaS",
  "sub_industry": "DevOps tooling",
  "company_size_employees_min": 10,
  "company_size_employees_max": 200,
  "geography_countries": ["US", "Canada"],
  "business_model": "B2B",
  "funding_stage": "seed",
  "job_titles": ["Head of Growth", "VP Marketing", "Director of Marketing"],
  "seniority_level": "director",
  "department": "marketing",
  "decision_authority": "decision_maker",
  "trigger_events": ["recently_funded", "hiring_for_role"],
  "trigger_recency_days_max": 45,
  "pain_indicators": ["no dedicated marketing hire", "job post mentions 'wearing many hats'"],
  "pain_indicator_sources": ["job_postings", "website_copy"],
  "excluded_industries": ["Government"],
  "already_in_crm": true,
  "field_weights": {
    "industry": 0.2,
    "job_titles": 0.25,
    "trigger_events": 0.15,
    "pain_indicators": 0.25,
    "geography": 0.15
  },
  "minimum_score_threshold": 70
}
```

Once every required key has a value, this JSON object is what gets passed into Stage 2
(source adapters) as the search filter.
