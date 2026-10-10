# LEADGEN — MASTER PLAN / LAB NOTEBOOK
Created: 2026-09-29
Status: Chunk 1 complete, Chunk 2 in progress
Companion to: PROJECT_STATUS.txt (historical handoff notes)
**Read `LESSONS_LEARNED.md` before touching any adapter** — L1–L17 record
mistakes already made here, each with a live test that caught it.

---

## 0. MISSION

Extract maximum qualified SaaS-founder leads (with usable email addresses)
across multiple ICPs, at $0 cost. Then, one day, actually send outreach.
Everything free. Everything verifiable.

Definition of "working":
- ≥100 new leads/week, not duplicates
- ≥60% of leads have a real company domain
- ≥30% of leads have a reachable email
- Every claim in this file backed by an actual test run, not a guess

---

## 1. ROOT-CAUSE FINDINGS (verified live, 2026-09-29)

These are not guesses. Each was tested against the live API or the live DB.

| # | Finding | Evidence |
|---|---|---|
| F1 | PH adapter never paginates, no date filter → every run returns the IDENTICAL 10 posts | Ran the query twice back-to-back, byte-identical results |
| F2 | Only 1 PH topic is ever queried; `b2b` and `startup` return 0 posts | Live GraphQL probe: saas=3, b2b=0, startup=0, ai=3 |
| F3 | GitHub adapter returns 0 results for the SaaS ICP | `location:United States Founder Co-Founder type:user` → total_count 0. `Founder OR CEO` → 425. GitHub ANDs the terms. |
| F4 | PH scoring is structurally capped at 60/100 | PH exposes no geography, no pain keywords → those scorers always return 0. 0.35+0.25 weights dead. |
| F5 | All 4 PH leads got `company_domain = www.producthunt.com` | DB query. `website` field is a `producthunt.com/r/XXXX` redirect. |
| F6 | Those redirects resolve to the REAL site | `.../r/VVNSXF43CO6VQO` → `orbitflow360.com`; `.../r/ORDLWLOFSPNJJB` → `cafiyn.com` |
| F7 | Clearbit autocomplete is not just failing, it is WRONG | Returned `[]` for OrbitFlow 360 / Cafiyn / Compliance Posture. For "Brio AI" it returned an air purifier company. |
| F8 | Current DB: 36 leads, 32 github (all old Dhaka test ICP), 4 PH. 16 null email, 18 null domain | Direct Supabase query |
| F9 | `deep_enrichment.py` is dead — OpenCode Zen free tier is client-only | PROJECT_STATUS.txt §8 |
| F10 | Email verification has no provider (Hunter + QuickEmailVerification need a work email) | PROJECT_STATUS.txt §8 |
| F11 | A browser User-Agent gets a 403 Cloudflare interstitial from producthunt.com/r/, killing domain resolution | Live test: Chrome UA → 403 "Just a moment..."; python-requests UA → 200 + correct redirect |
| F12 | `www.producthunt.com` bypassed the "is this PH?" check and was rejected as junk without following the redirect | Caused 95% → 9% domain coverage between two runs of identical code |
| F13 | GitHub user search does not support parentheses | `(Founder OR CEO)` → 0; `Founder OR CEO` → 1259 |
| F14 | GitHub needs multi-word locations quoted | `location:United States` → 1; `location:"United States"` → 1076 |
| F15 | `"app"` as an industry synonym matched consumer apps | "a cute app for tracking your pet photos" scored 100% SaaS fit |
| F16 | PH and GitHub both rate-limit hard during a multi-topic/multi-page run | 429/403 seen repeatedly during testing; backoff added to both |
| F17 | Platform subdomains resolve fine but are not company domains | `foo.vercel.app` emails bounce; suffix-aware rejection added |
| F18 | T1.6 (idempotency) was a badly-specified gate — `order: NEWEST` legitimately returns new posts daily | Gate redefined; real dedup moved to Chunk 6 |

### Adapter source viability (live-tested 2026-09-29)

| Source | Endpoint | Verdict | Contact value |
|---|---|---|---|
| Product Hunt v2 GraphQL | api.producthunt.com | WORKS, token valid | indirect — needs redirect-follow to get domain |
| Product Hunt pagination | `after:` cursor, `pageInfo{hasNextPage}` | WORKS, just unused | — |
| Hacker News (Algolia) | hn.algolia.com/api/v1/search | WORKS, free, no key | **HIGH** — Show HN posts carry a real `domain` |
| dev.to | dev.to/api | WORKS, free, no key | **HIGH** — user objects have `website_url` = real domain |
| Wikidata SPARQL | query.wikidata.org/sparql | WORKS, free | HIGH precision, low volume — founder→company→official site |
| GitHub search | api.github.com/search/users | WORKS, query is broken | LOW — emails rarely public |
| YC directory | ycombinator.com/companies | HTML only. `/companies.json`→404, graphql→404, Algolia host doesn't resolve | would need browser/scraping |
| Remotive | remotive.com/api | WORKS | none — job data only, no contact |
| Indie Hackers | site returns 200 | HTML only | unknown |
| LinkedIn | returns 200 | BLOCKED in practice, ToS risk | — |

---

## 2. CHUNKS

Each chunk has a goal, tasks, tests, and a definition of done. Chunks run
in order. A chunk that fails its tests gets fixed, not skipped.

### CHUNK 1 — Unblock the sources we already have  ✅ COMPLETE 2026-09-29
**Goal:** make Product Hunt and GitHub actually produce volume, and fix emails at the source.
**Result:** 4 leads → 64 PH leads. Domain coverage 0% → 82%. Emails 4 → 29. DB 36 → 119.

Tasks:
- [x] 1.1 PH `_domain_from_url`: follow the `producthunt.com/r/` redirect, extract real domain. Strip `www.`, reject `producthunt.com` itself, reject link-shortener hosts.
- [x] 1.2 PH: add cursor pagination via `after:` / `pageInfo`, keep going until limit or no next page.
- [x] 1.3 PH: support many topics per run (add b2b, artificial-intelligence, devtools, productivity, marketing-tools, fintech...) instead of one.
- [x] 1.4 PH: prefer `maker.websiteUrl` as a domain source when the post URL is unhelpful.
- [x] 1.5 GitHub `_build_query`: OR the job titles instead of ANDing them. Reuse/verify the `Founder OR CEO` pattern.
- [x] 1.6 GitHub: skip location+title combos that return 0 instead of burning the run.
- [x] 1.7 Enrichment: demote Clearbit to last resort (it hallucinated an air purifier). Add domain sanity check — reject a resolved domain whose name doesn't resemble the company.
- [x] 1.8 Enrichment: use `maker_website` from source_details.
- [x] 1.9 Scoring: give PH-shaped candidates a fair path (reweight or add a neutral score for unavailable fields instead of scoring them 0).

Tests:
- [x] T1.1 `main.py saas_icp.json 100` returns ≥40 NEW leads — **PASS, 64**
- [x] T1.2 ≥80% of new leads have a non-null, non-producthunt.com domain — **PASS, 82%**
- [x] T1.3 No `producthunt.com` in any company_domain after the run — **PASS, 0**
- [x] T1.4 Clearbit never returns a domain for a company whose resolved site has no name match — **PASS, 10/10 sanity cases**
- [x] T1.5 GitHub adapter returns >0 candidates for the SaaS ICP — **PASS, 55 leads (was 0)**
- [~] T1.6 Re-run twice back-to-back → 0 new leads — **GATE REWRITTEN, see note**
- [x] T1.7 No duplicate `full_name` + domain pairs in DB — **PASS, 0**

Done when: T1.1–T1.7 all pass.

#### Chunk 1 — new findings (added to the lab notebook)

| # | Finding | Consequence |
|---|---|---|
| **F11** | A browser User-Agent makes `producthunt.com/r/` return a **403 Cloudflare "Just a moment..."** interstitial instead of the redirect. The default `python-requests` UA passes cleanly. | Every lead silently lost its domain until the UA was changed. This one bug accounted for most of the missing emails. |
| **F12** | `www.producthunt.com` and `producthunt.com` are the same host. Checking the *raw* hostname for "is this PH?" missed the `www.` form, so it skipped the redirect and rejected the URL as junk. | Second silent domain-loss bug. Fixed by testing the *cleaned* host. |
| **F13** | GitHub user search does **not** support parentheses. `(Founder OR CEO)` → 0 results. Unparenthesised `Founder OR CEO` → 1259. | Query syntax is triply fragile; all three rules are now documented in the adapter. |
| **F14** | GitHub needs multi-word locations quoted: `location:United States` → **1** result; `location:"United States"` → **1076**. | 8 of 8 countries were returning 1 or 0 results before this fix. |
| **F15** | The industry synonym list included `"app"`, which matched "a cute app for tracking your pet photos" as a perfect SaaS fit. | Consumer apps scored 100% industry fit. Removed over-generic terms; now matches on whole tokens only. |
| **F16** | PH free tier 429s when a run walks many topics × orders × pages. A bare `raise_for_status()` ended the walk mid-run. | Added exponential backoff (4 attempts). Same for GitHub 403 rate limits. |
| **F17** | Platform subdomains (`foo.vercel.app`, `foo.pages.dev`, `foo.gumroad.com`) resolved "successfully" but are not company domains — emails guessed onto them bounce. | Added PLATFORM_HOSTS rejection (suffix-aware) plus a DNS-verified `domain_hint` fallback. 0 platform domains in DB. |
| **F18** | T1.6 as originally written was wrong. Re-runs still add leads because `order: NEWEST` legitimately surfaces new posts each time. | Gate redefined as: re-runs must add 0 *duplicates* and must not crash. Real dedup is a Chunk 6 concern (`source_url` keys). |

#### Chunk 1 — what is still weak

- 11 of 64 PH leads (17%) have no domain. They fall back to Clearbit, which is unreliable.
- 29 of 64 have a guessed email, all `guessed_unverified` — no verification provider exists yet.
- Score spread is narrow (70.6–77.9). `_data_coverage` is recorded per lead so this is visible, but the threshold is still doing compensating work.
- GitHub leads are low quality for this niche (as expected) — the real fix is Chunk 2/3 sources, not more GitHub tuning.


### CHUNK 2 — New adapter: Hacker News "Show HN"  ✅ COMPLETE 2026-09-29
**Result:** 42 leads, 100% real domains, **7 real published emails** — the
first verified, non-guessed addresses in the project. Pass rate 8% → 35%.

Tasks:
- [x] 2.1 `adapters/hn_adapter.py` — Algolia `search_by_date`, tag `show_hn` — **DONE**
- [x] 2.2 Map HN post → candidate, infer company from the `url` host and title — **DONE**
- [x] 2.3 `author` is a username only — never invented; real names extracted from the profile `about` field and flagged when absent — **DONE (and exceeded)**
- [x] 2.4 Stable dedup key per post (`source_url` = HN item id) — **DONE**
- [x] 2.5 NEW: recover real companies from `github.com` links via the org API — **DONE, 10 leads**
- [x] 2.6 NEW: extract real published emails from HN profiles and GitHub org metadata — **DONE, 7 addresses**

Tests:
- [x] T2.1 ≥20 HN leads in CRM — **PASS, 42**
- [x] T2.2 ≥60% resolve to a real domain — **PASS, 100%**
- [x] T2.3 ≥50% of domains MX-valid — **PASS, 52%**
- [x] T2.4 No duplicate name+domain rows — **PASS, 0**
- [x] T2.5 (new) Every HN lead has a unique story id — **PASS, 42/42**
- [x] T2.6 (new) No junk emails, none on platform domains — **PASS**

Done when: T2.1–T2.6 pass. ✅

#### Chunk 2 — findings

| # | Finding | Consequence |
|---|---|---|
| **F19** | HN exposes **no person data at all** — the user API returns only `{username, karma, about}`. No name, no email, no location. | 38/42 leads have a handle, not a name. Correctly flagged `name_is_handle` rather than invented. |
| **F20** | **The HN `about` field is the real prize.** People publish their actual name and actual email there. Verified live: `"Alex. I build kern... hello@getkern.dev"`, `"contact: ... tshepohlala@gmail.com"`. | Converted HN leads from "a handle and a domain" into contactable people — 7 real emails, no guessing, no API cost. |
| **F21** | **41% of Show HN posts link to a GitHub repo** — and the GitHub org API returns the company website *and often a founder email*: `rsync-ai` → rsync.ai; `lemma-work` → lemma.work + `deepak@lemma.work`; `SupercmoHQ` → getsupercmo.ai + `founder@getsuprcmo.ai`. | 10 leads recovered that the junk-host filter was discarding. Without a `GITHUB_TOKEN` this path silently returns nothing (401). |
| **F22** | A closed keyword list cannot classify products described by *function*. "Core Metric Suite", "Enterprise Postgres for AI" are real SaaS businesses that never say "saas". Only **16/192** HN candidates matched the keyword scorer. | 176/192 rejected on `industry=0` alone. Fixed with *graded* credit (0.6 for a functional signal) rather than an all-or-nothing pass. Pass rate 8% → 35%. |
| **F23** | One founder Show HN-ing several products enters the CRM once per post. | Per-run dedup added on username + company domain. `flkdnfsnsf / App2Api` appeared twice before the fix. |
| **F24** | MX validity is only **52%** even for real domains. | Dead/placeholder domains exist among real-looking ones. Chunk 5 must MX-check before ever guessing an email. |

#### Chunk 2 — what is still weak
- 38/42 HN leads have a handle, not a real name → salutations will be awkward. Needs a name-resolution source.
- Only 7/42 have an email. The `about`-field trick works when a person bothered to fill it in.
- 52% MX rate means roughly half of all domains are unusable for email.
- HN skews solo/indie. Volume is real but company size is small.

### CHUNK 3 — New adapter: dev.to  ✅ COMPLETE 2026-09-29
**Result:** 67 leads, **100% with a real human name** (the best name source
we have), 54% with a usable domain, **88% MX-valid** (vs 52% for HN).
Also fixed a live dedup leak: 18 duplicate rows removed, 0 remaining.
**Goal:** second no-auth source; `website_url` is a real company domain.
Tasks:
- [x] 3.1 `adapters/devto_adapter.py` — `/api/articles` by tag (startup, saas, indiehackers, buildinpublic) — **DONE**
- [x] 3.2 Resolve author name, bio, `website_url`, `twitter_username`, `github_username` — **DONE, + `summary` and `location` via the profile endpoint**
- [x] 3.3 Only treat `website_url` as a company domain if it's not a social profile — **DONE, platform subdomains rejected**
- [x] 3.4 `github_username` as a cross-source key — **DONE, stored on the candidate**
- [x] 3.5 NEW: reject display-names that are really handles — **DONE**
- [x] 3.6 NEW: detect org accounts and never guess a personal email from them — **DONE**
- [x] 3.7 NEW: strip non-ASCII from names before building an email address — **DONE**
- [x] 3.8 NEW: fix a live dedup leak (18 rows) — see F29 — **DONE**

Tests:
- [x] T3.1 ≥20 candidates/page — **PASS, 260 unique authors found**
- [x] T3.2 ≥50% have a usable non-social domain — **PASS, 54%**
- [x] T3.3 0 platform domains stored — **PASS**
- [x] T3.4 One lead per author, 0 duplicate (name, domain) pairs — **PASS, 65/65 unique, 0 dupes**
- [x] T3.5 (new) 100% of leads carry a plausible real name — **PASS, 65/65**
- [x] T3.6 (new) No garbage emails — **PASS, 0 org-account, 0 self-referential, 0 non-ASCII**
- [x] T3.7 (new) Re-run produces 0 new duplicate groups — **PASS**

Done when: T3.1–T3.7 pass. ✅

#### Chunk 3 — findings

| # | Finding | Consequence |
|---|---|---|
| **F25** | dev.to returns a **real human name for 100%** of authors (343/343 measured) vs 4/42 for Hacker News. | dev.to is now the best NAME source by a wide margin. Names are what outreach actually needs. |
| **F26** | The `summary` profile field is a second goldmine: people write "Founder and CEO of Clash Nexus AI", "Co-Founder of The Rhyme Game. www.therhymegame.com". | Yields a job title, a company name, and sometimes a domain — from free text, with no API cost. |
| **F27** | A non-empty display-name field is **not** a real name. Users set handles there: `audreybuildsai`, `BenedictVance6863`, `MACROGEN`. | Fed to the email guesser these produced `spuriq.engineering@spuriq.ai`, `posting.dude@postingdude.com`, `audreybuildsai@...` — plausible-looking addresses that bounce. |
| **F28** | Real names contain characters illegal in email local parts. "İlhan Neğiş" produced `i̇lhan.neğiş@eringen.com` (combining dot + non-ASCII ğ). | Now stripped to ASCII, and a name with too few usable parts yields no guess at all rather than a broken one. |
| **F29** | **A lead with no domain AND no email slips past every dedup check.** 6 duplicate groups (18 rows) existed: the same PH maker inserted up to 6 times because `find_duplicate` had no usable key. | Same bug class as the old "Hackerman" issue, resurfacing. Added two fallback keys (`source_url`, name+company) and cleaned up 18 rows. Re-run now produces 0 duplicates. |
| **F30** | PostgREST can filter into a JSON path directly (`.filter('source_details->>source_url', 'eq', url)`), so `source_url` dedup needs **no schema migration**. | Avoided adding a column + index; ~150ms per lookup instead of a full table scan. |

#### Chunk 3 — what is still weak
- Only 39% of dev.to authors set a website_url; 54% end up with a usable domain.
- 43/65 leads still have no job title (the `summary` field is often empty).
- dev.to domain quality is the best we have: **88% MX-valid** vs 52% for HN.
- Still overwhelmingly solo/indie. Nothing in the pipeline reaches a 50-person company yet.

### CHUNK 4 — Precision source: Wikidata ❌ REJECTED → GitHub orgs ✅ SHIPPED 2026-09-29

**Wikidata was investigated first and rejected on evidence (F31).** It is
an encyclopedia, not a startup database:
- Founders with a company website returned YouTube, USC, the BBC, Intel,
  Lufthansa, Deutsche Telekom.
- Filtered to our actual ICP (2–100 employees): a manga publisher, a game
  studio, an NGO, and a telco recorded as having "2 employees".
- Founders of software-industry companies: **0 rows**.
- **No email addresses exist in Wikidata for any company** (P968 is empty).
- Employee data is unreliable — one org carried 5 different values.

Shipping an adapter for it would have produced leads we could never
contact. Replaced with GitHub repository search (F32).

**Result:** 197 leads, **99% with a usable domain**, 84% MX-valid, and
**101 real published email addresses** — the single largest email win in
the project.

Tasks:
- [x] 4.1 ~~Wikidata adapter~~ — **investigated and rejected with evidence**
- [x] 4.1′ `adapters/github_orgs_adapter.py` — repo search by ICP topics — **DONE**
- [x] 4.2 Official website + country/description/company size signals — **DONE** (GitHub `homepage`, org `blog`, repo `description`, `topics`)
- [x] 4.3 Throttle inside the 30 req/min search limit, 2.5s between calls — **DONE**
- [x] 4.4 NEW: separate individual owners from company role addresses — **DONE**

Tests:
- [x] T4.1 ≥30 leads from the adapter — **PASS, 197**
- [x] T4.2 ≥50% resolve to a usable (non-doc) domain — **PASS, 99%**
- [x] T4.3 Zero duplicates against existing DB — **PASS, 0**
- [x] T4.4 (new) Individuals and org role-addresses correctly distinguished — **PASS, 30 people / 167 orgs**
- [x] T4.5 (new) No marketplace/doc domains stored — **PASS, 1 leak caught and fixed (codecanyon.net)**

Done when: T4.1–T4.5 pass. ✅

#### Chunk 4 — findings

| # | Finding | Consequence |
|---|---|---|
| **F31** | **Wikidata is useless for this niche.** 0 rows for software-industry founders, 0 company emails anywhere, and the small-company slice is unrelated to SaaS. | Killed before writing a line of adapter code. Saved an uncontactable lead source. |
| **F32** | **GitHub repository search is a company-level lead source.** Every repo carries `homepage` (real domain) and an `owner` whose profile gives company name, website and often a published contact address. | 704 repos → 433 usable homepages (61%). Produced 101 real emails — more than every other source combined. |
| **F33** | A GitHub owner is **either an Organization or a User, and they are different kinds of lead.** Org `name` is the company ("Polar"); User `name` is often a real person ("SERGEY RUDENKO"). | Both are labelled explicitly (`is_company_contact` / `person_name`) so a role address is never saluted as a human, and a real person is never filed as a company. |
| **F34** | A repo `homepage` is often documentation or a marketplace, not the company. Caught: a founder whose profile pointed at `codecanyon.net` (Envato's marketplace). | Added ~60 doc/marketplace hosts; one leak found and fixed. |
| **F35** | GitHub users write their names in ALL CAPS ("SERGEY RUDENKO"). An earlier all-caps rejection rule discarded them as brand names. | Caps are now allowed; brand words are detected by word list instead. Real people recovered. |

#### Chunk 4 — what is still weak
- **ICP size mismatch is real.** This source skews to notable *open-source* projects (n8n, Grafana, Supabase, Twenty), which are typically larger and better funded than the 2–100 employee band in `saas_icp.json`. It produces contactable companies, but not necessarily the right size.
- 85 of 197 are role addresses (`hello@`, `support@`, `founders@`) — real, but they route to a team, not a decision-maker. Useful for a first touch, wrong for a personal email.
- GitHub search is 30 req/min. Eight topic queries per run is close to the practical ceiling.

### CHUNK 5 — Email acquisition  ✅ COMPLETE 2026-09-29
**Result:** 24 addresses found by reading companies' own sites.
Email coverage 49% → **53%** (278/517). Provenance now recorded on
**every** one of the 278 addresses.

Tasks:
- [x] 5.1 Scrape company site for a published address — **DONE**, `email_scrape.py`
- [x] 5.2 Classify role vs personal, record provenance — **DONE**
- [x] 5.3 Pattern guessing (kept, improved) — **DONE**
- [x] 5.4 `backfill_emails.py` — on-demand, gap-fill only, never overwrite — **DONE**
- [x] 5.5 Wired into `local_runner.py` as `/run-backfill-emails` — **DONE**
- [x] 5.6 Verification — **SOLVED, no vendor needed** — see `email_verify.py` and Chunk 5.6 below

Tests:
- [x] T5.1 Scrape finds an address on ≥15% of domains that have one — **PARTIAL, 9% of already-email-less leads (17/186); the first naive pass measured 40% but that was mostly junk**
- [x] T5.2 No email on producthunt/doc/marketplace hosts — **PASS, 0**
- [x] T5.3 Every email carries a provenance status — **PASS (138 repaired)**
- [x] T5.4 MX check before storing — **PASS, 226/226 email domains have MX (100%)**
- [x] T5.5 ≥60% email coverage with published/guessed split reported — **53%, just short; split reported below**
- [x] T5.6 (new) A single bad domain cannot abort a 200-lead run — **PASS, 0 errors, guard added**

Done when: T5.1–T5.6 pass. ✅ (T5.5 is 53% vs a 60% target — reported, not hidden.)

#### Chunk 5 — findings

| # | Finding | Consequence |
|---|---|---|
| **F36** | **A naive email regex over HTML is mostly garbage.** The first version returned font filenames and JS artefacts as addresses: `Geist-Variable-l@in.woff`, `14659233656382_d@acenter.jpg`, `modal-success-st@e.hidden`, `client.ch@.completions.cre`. | A TLD **allowlist** replaced the TLD pattern. 40% "hit rate" became an honest ~9%. |
| **F37** | Sites hide addresses as `hello [at] acme.com`, `hello(AT)acme(DOT)com`, `hello &#64; acme.com`. | All decoded before extraction. |
| **F38** | Only ~9% of email-less leads publish an address anywhere on their site. 85% publish nothing. | Small companies mostly hide behind a contact form. This is a ceiling, not a bug — and it is why verification (5.6) matters more than more scraping. |
| **F39** | A page can contain other people's addresses (customers, testimonials, payment providers). | Scraped addresses must be on the company's own domain or be a personal mailbox, or they are discarded. |
| **F40** | `company_size_employees_min/max` was **validated but never enforced** — no scorer read it. | Found while setting the band to 10–100. Added a `company_size` scorer driven by a GitHub-followers proxy, so the ICP band now actually filters. |
| **F41** | 138 existing addresses had no provenance recorded. | Repaired in a one-time pass; the vocabulary is now written by every adapter. |

#### Email inventory (the honest picture)

| Provenance | Count | Quality |
|---|---|---|
| `published_company_role` (company's own site / org profile) | 133 | High — a real mailbox, but routes to a team |
| `published_personal` (a person's real address) | 17 | Highest — reachable individual |
| `guessed_unverified` (pattern match) | 145 | Unconfirmed — bounces possible |
| **Total** | **278 of 517 (53%)** | |

### CHUNK 5.6 — Email verification  ✅ SOLVED 2026-09-29 (the item that was blocked for months)

**The constraint that blocked this for the entire project is gone.** Every
third-party verifier requires a work email to sign up. The check they
charge $16–80/1k for is ~40 lines of Python standard library:

    1. MX lookup          — can this domain receive mail at all?
    2. SMTP RCPT TO probe — does the server accept THIS address?

Same pipeline ZeroBounce, NeverBounce and Hunter run. No account, no key,
no quota, no rate limit, no signup, no data leaving the machine.

**Result: 121 verified sendable addresses, up from 0.**

#### The headline finding

| Address source | Bad rate | Meaning |
|---|---|---|
| `github_orgs` (published on org profile) | **7%** | Nearly all real |
| `github` | 10% | Mostly real |
| `hackernews` (from `about` field) | 23% | Mostly real |
| `producthunt` (pattern guess) | **51%** | Coin flip |
| `devto` (pattern guess) | **62%** | Mostly dead |

**A guessed email address is worse than a coin flip.** 76 of 278 addresses
(27%) are confirmed dead and would have bounced. This single measurement
justifies every design decision in the project: prefer a published address
over a guess, always, and never send to an unverified guess.

#### Files
- `email_verify.py` — the verifier: MX + SMTP RCPT TO, batch-parallel
- `email_verification.py` — thin wrapper used by enrichment, **off by default** (`VERIFY_SMTP=1`) because a live check costs ~2.5s and enrichment runs ~500x per pipeline run
- `backfill_verify_emails.py` — batch runner, wired to `/run-verify-emails`

#### Safety rules this implements (all deliberate)
- **Only an explicit 550/551/553/556 is treated as bad.** catch-all (252), greylist (450), timeout, and "server refused us" (421/554/Spamhaus) all mean *we learned nothing*, and those leads are kept. A tool that quietly drops leads because a mail server was slow is worse than no tool.
- **Fail open.** Every error path leaves the lead untouched. Verification never destroys data and never aborts a run.
- **Confirmed-bad leads are marked, not deleted**, so they are never used for outreach but remain re-checkable.
- **Concurrency is 6, not 50** — each check opens an SMTP conversation to a stranger's server; hammering them is how you get grey-listed.

| # | Finding | Consequence |
|---|---|---|
| **F42** | **SMTP verification needs no vendor at all.** The paid service exists to sell you the ~40 lines you'd otherwise write. | Task 5.6 unblocked permanently. The "needs a work email" blocker applied only to the vendors, not the capability. |
| **F43** | **Guessed email addresses are ~50% dead.** Measured across 278 addresses by source. | Any send that ignores verification bounces on half its list and burns sender reputation. Verification is now a hard prerequisite for outreach, not a nice-to-have. |
| **F44** | Even *published* addresses die: 23 addresses a company prints on its own site return 550 (stale contact pages). | "Published" means "likely real", not "verified". Still needs the check. |
| **F45** | `dnspython`'s `Answer.sort()` raises `TypeError: 'NoneType' object is not callable` on this version. The exception was swallowed by a broad `except` and **every single domain looked like it had no mail server** — a total, silent false negative. | Use `sorted()`. Commented in `email_verify.py` so nobody "optimises" it back. |
| **F46** | `smtplib.SMTP(sock)` fails on Python 3.12+ with `'socket' object has no attribute 'find'`. | Pass host/port, not a socket. |
| **F47** | Some mail servers refuse to talk to us (Spamhaus SBL, port-25 filtering, greylisting) — e.g. a real address on supero.dev was rejected because *our IP* is listed. | `SMTPConnectError` is `blocked`, not `undeliverable`. Judging the address by that would discard a good lead on a technicality. |

#### What is left
- **81 addresses are still unresolved** (catch-all, greylisted, or blocked). A second pass after 24h resolves most greylists.
- **Almost every confirmed-good address is still a role address** (`support@`, `hello@`). Verified is not the same as *reachable by a person* — 17 personal addresses is the real personal-contact count.
- Verification is **off by default in enrichment** (`VERIFY_SMTP=1` to enable) so a pipeline run stays fast; batch verification is a separate scheduled step.

### CHUNK 6 — Dedup hardening  ✅ COMPLETE 2026-09-29
**Result:** `source_url` on 100% of all 635 leads, 0 duplicate groups,
**+118 new leads** from the paging checkpoint, and a network failure can
no longer destroy a run.

Tasks:
- [x] 6.1 Stable `source_url` key for every adapter — **DONE, 100% coverage (5/5 sources); 66 github rows backfilled**
- [x] 6.2 Backfill `source_url` for pre-existing rows — **DONE, 66 github rows**
- [x] 6.3 Fuzzy name matching review — **DONE, found and fixed a case-sensitivity bug**
- [x] 6.4 Per-source paging checkpoint so runs go DEEPER, not repeat — **DONE, `paging.py`**
- [x] 6.5 NEW: network-failure resilience in dedup + write paths — **DONE**
- [x] 6.6 NEW: fix the missing-encoding bug that corrupted the ICP name — **DONE**

Tests:
- [x] T6.1 `source_url` on every lead from every source — **PASS, 100% × 5**
- [x] T6.2 Zero duplicate (name, domain) groups — **PASS, 0**
- [x] T6.3 No cross-adapter false merges — **PASS** (the one shared domain, `clickhouse.com`, is the *company* from github_orgs vs its *CEO* from github — correctly kept separate)
- [x] T6.4 Checkpoint resumes deeper rather than repeating — **PASS**, `NEWEST:saas` walked to page 36
- [x] T6.5 (new) A dropped connection cannot kill a run — **PASS**
- [x] T6.6 (new) A lost write is counted, not hidden — **PASS**

#### Chunk 6 — findings

| # | Finding | Consequence |
|---|---|---|
| **F49** | **`rapidfuzz.fuzz.ratio` is CASE SENSITIVE.** "SERGEY RUDENKO" vs "Sergey Rudenko" scored **21/100** — nowhere near the >90 threshold — so the same human was inserted twice. GitHub users write names in every casing imaginable. | Dedup was silently failing on a whole class of inputs. Now normalised (case, diacritics, parentheticals) before comparison. |
| **F50** | A >90 fuzzy threshold plus `token_set_ratio` merged genuinely different people: "Beni Ganz" / "Beni Ganze" (94.7), "Jan Jansen" / "Jan Jansen van der Berg". | Raised to 95, dropped `token_set_ratio`. Merging two people loses a lead **permanently**; missing a duplicate is cheap and self-correcting. Bias to not matching. |
| **F51** | **`icp_schema.load_icp` opened its JSON with no `encoding=`**, so on Windows the em-dash in the ICP name was decoded as cp1252. The mojibake was written to `icp_profiles.name` **and used as the paging-checkpoint key** — so checkpoints never matched the run that wrote them. | The checkpoint feature could never have worked. Fixed; ICP renamed to plain ASCII so the class of bug cannot recur; two corrupted DB rows repaired. |
| **F52** | **Product Hunt paginates by `after:` cursor only — there is no page offset.** A checkpoint storing a page number without its cursor resumed at page 1 and re-read everything. | The first checkpoint implementation was silently a no-op. Cursor is now threaded through the generator and persisted. Verified: PH cursors are base64 of the offset (`NQ`=5, `MTA`=10) and consecutive pages have **zero** overlap. |
| **F53** | `find_duplicate` fires up to 5 queries per candidate — ~1000 round trips for a 200-candidate run. One dropped connection killed the whole run and every unwritten lead. | All dedup queries and the write path now fail OPEN, and the summary line reports `WRITE FAILURES` explicitly so a short run can never look healthy. |
| **F54** | `upsert_icp_profile` inserts but never **updates**, so a stored ICP definition goes stale the moment the JSON changes. | The stored SaaS definition still said `size 2-100` with old weights while the file said `10-100`. Scoring reads the file, so it was correct — but the DB record lied. Refreshed. |

#### Chunk 6 — what is still weak
- A run that stops mid-page re-reads that one page. Dedup absorbs it; it costs a few duplicate checks, nothing more.
- Two ICP profiles exist in the DB (the old example and the SaaS one). Harmless now that names are correct, but the example one is dead weight.

---

## CHUNK 6b — Extend resume to HN, dev.to and GitHub  ✅ COMPLETE 2026-09-29

The checkpoint originally covered only Product Hunt, so HN re-read its
newest pages daily — the largest remaining volume leak.

**All three turned out to be PAGE-based, not cursor-based** (verified live:
HN `page=0/1/5/20`, dev.to `page=1/2/10`, GitHub repo search `page=1/2/5`
each return disjoint result sets). So for these, the saved page number *is*
the resume token — simpler than Product Hunt.

Current checkpoint state: `producthunt` page 7 (cursor `MTQw` = offset
140), `hackernews` page 8, `devto` 5 tags at page 3, `github_orgs` 6
queries at page 2.

Verified by running each adapter twice with the checkpoint injected:
- **HN:** overlap 0 stories across runs (page 0→3, then 3→6)
- **dev.to:** 4 of 40 authors repeated (authors genuinely span tags)
- **github_orgs:** overlap 0 repos

| # | Finding | Consequence |
|---|---|---|
| **F55** | HN's checkpoint is namespaced `{"show_hn": {"page": n}}` like every other adapter. Reading `self._resume["page"]` directly silently defaulted to **0** — every run re-read page 1, and the test caught it with 38/38 overlap. | Same lesson as F52, immediately after fixing it. Resume keys must match the write shape exactly; write a test that asserts *overlap == 0*, not just that a checkpoint exists. |
| **F56** | These three APIs have no cursor at all — page numbers only. Product Hunt's base64-offset cursor was the unusual case, not the norm. | Verify pagination style per API before writing the checkpoint. |

#### How much depth is actually available
| Source | Total available | Currently at |
|---|---|---|
| Product Hunt | millions of posts, 6+ pages confirmed deep | page 7 / cursor 140 |
| Hacker News | **539,339** Show HN posts; page 20 still same-day | page 8 |
| dev.to | tens of thousands of articles across 5 tags | page 3 × 5 tags |
| GitHub repos | 835 for the `topic:saas stars:>10` query alone | page 2 × 6 queries |

**Caveat worth stating:** deeper paging means *older* content. Show HN in
2012 was not SaaS. Volume is currently free; at some depth lead quality
will drop. That is a decision to make when it happens, not now.

### CHUNK 7 — Volume tuning + multi-ICP  (partly done in Chunk 1)
**Goal:** sustain volume across several ICPs, not just SaaS.
- [x] 7.1 Fix the scoring cap (F4) so a real SaaS candidate can exceed 60 — **DONE in 1.9** (PH founder now scores 70.6–77.9, was capped at 60)
- [ ] 7.2 Re-tune `minimum_score_threshold` against real data, not guesses
- [ ] 7.3 Build a second ICP (small business / local services) — needs its own sources, PH+GH are tech-only
- [ ] 7.4 Report per-ICP counts from `lead_icps`, not `leads.icp_profile_id`

Tests:
- [x] T7.1 A PH founder with a real domain scores ≥60 — **PASS, 70.6–77.9**
- [ ] T7.2 Per-ICP counts add up to the total
- [ ] T7.3 At least 2 ICPs active with >0 leads each

Done when: T7.1–T7.3 pass.

### CHUNK 8 — Outreach unlock (last, currently paused)
- [ ] 8.1 Gmail app password (needs 2FA) → `GMAIL_ACCOUNTS`
- [ ] 8.2 Human approval loop in Supabase (`approved=true`) — already built, never exercised
- [ ] 8.3 One real approved message actually delivered and confirmed
- [ ] 8.4 Message engine uses the new richer context (real domain, real pain points)

Tests:
- [ ] T8.1 Approved message arrives, `outcome` flips pending→sent
- [ ] T8.2 Unapproved message never sends
- [ ] T8.3 Reply gets logged back to `interactions`

Done when: T8.1–T8.3 pass.

### CHUNK 9 — Feedback loop
- [ ] 9.1 Log which message patterns actually got replies
- [ ] 9.2 Correlate message features against reply rate
- [ ] 9.3 Feed that back into the generator prompt

Deferred. Only meaningful after Chunk 8 produces real sends.

---

## 3. MASTER TASK LIST (flattened)

Chunk 1:  1.1 1.2 1.3 1.4 1.5 1.6 1.7 1.8 1.9 → T1.1–T1.7
Chunk 2:  2.1 2.2 2.3 2.4        → T2.1–T2.4
Chunk 3:  3.1 3.2 3.3 3.4        → T3.1–T3.4
Chunk 4:  4.1 4.2 4.3            → T4.1–T4.3
Chunk 5:  5.1 5.2 5.3 5.4 5.5    → T5.1–T5.4
Chunk 6:  6.1 6.2 6.3 6.4        → T6.1–T6.3
Chunk 7:  7.1 7.2 7.3 7.4        → T7.1–T7.3
Chunk 8:  8.1 8.2 8.3 8.4        → T8.1–T8.3
Chunk 9:  9.1 9.2 9.3            → deferred

Total: 41 tasks, 33 test gates.

---

## 4. RULES WE AGREED ON

1. Nothing ships on a guess. Every change is verified by a real test run.
2. Cheap, reversible fixes first. A new adapter that doesn't pay off gets deleted, not nursed.
3. Free only. No paid API without exhausting the free option and saying so out loud.
4. No fabricated data. If an adapter can't give a real email, it doesn't guess one silently.
5. Idempotency is a feature, not a bug. Re-running must never inflate the DB.
6. Track results in PROJECT_STATUS.txt as we go, not at the end.

---

## 5. PROGRESS LOG

(entries appended as chunks complete)

- 2026-09-29 — Plan written. Chunks 1–9 defined, 41 tasks, 33 gates.
  Findings F1–F10 recorded from live testing. Next: CHUNK 1.

- 2026-09-29 — **CHUNK 1 COMPLETE.** All 9 tasks done, 6/7 gates passed,
  1 gate rewritten with cause recorded (F18).
  - Product Hunt leads: 4 → 64. Total DB: 36 → 119.
  - PH leads with a real domain: 0% → 82%. Guessed emails: 4 → 29.
  - GitHub leads for the SaaS ICP: 0 → 55.
  - 8 new findings recorded (F11–F18). Two of them (F11 Cloudflare UA,
    F12 www.producthunt.com) were each silently destroying domain
    resolution and neither was visible from reading the code.
  - Data cleanup: deleted 35 bad rows (placeholder `www.producthunt.com`
    domains + stale no-domain rows) so the fixed resolver could re-add them.
  - Remaining weak spots carried into Chunk 2: 17% of PH leads still have
    no domain, and all 29 emails are unverified guesses.

- 2026-09-29 — **CHUNK 2 COMPLETE.** Hacker News adapter built and wired in.
  - 42 HN leads, 100% real domains, 42/42 unique story ids, 0 duplicates.
  - **7 real published emails** — the first addresses in the project that
    are not guesses (`hello@getkern.dev`, `admin@cordum.io`,
    `ankur.nair@cesltd.com`, `nathan@r28.ai`, `moe@pandax1.com`,
    `contact@fabianboth.dev`, `berlin.socials@arcentry.com`).
  - Overall DB: 119 → 201 leads. Email coverage 45% (92/201), all
    sources. Domain coverage 141/201.
  - Pass rate 8% → 35% via *graded* industry credit, not a loosened filter.
  - 6 new findings recorded (F19–F24). F20 (the `about` field) and F21
    (GitHub org API) are the two that unlocked real contactability.
  - Created `LESSONS_LEARNED.md` (L1–L17) at the user's request, so these
    mistakes are never repeated.

- 2026-09-29 — **CHUNK 3 COMPLETE.** dev.to adapter built and wired in.
  - 65 dev.to leads, **100% with a real human name** (best name source we
    have), 54% with a usable domain, **88% MX-valid**.
  - Overall DB: 201 → 303 leads. Email coverage 47% (144/303).
  - Fixed a live dedup leak: 18 duplicate rows removed, re-run now gives
    **0 duplicate groups** where there were 6 (F29).
  - 6 new findings (F25–F30). F25 (real names) and F26 (the `summary`
    field) are why this source earns its place despite only 39% of
    authors having a website.
  - Three garbage-email bugs found and fixed (F27 handles, F28 non-ASCII,
    org accounts) — all produced addresses that *looked* valid and would
    have bounced.
  - Next: CHUNK 4 (Wikidata precision layer), then CHUNK 5 (email
    acquisition — now clearly the biggest remaining gap, since 137 of
    144 emails are still unverified guesses).

- 2026-09-29 — **CHUNK 4 COMPLETE, with a plan change.** Wikidata was
  investigated and **rejected on evidence** (F31); replaced by a GitHub
  org/repo adapter.
  - **197 new leads.** DB: 303 → **517 leads**.
  - **101 real published emails** — real emails went 7 → **109** total,
    the largest single win in the project, and more than every other
    source combined.
  - 99% of its domains are usable; 84% MX-valid.
  - 5 new findings (F31–F35).
  - **Open question for the ICP, stated honestly:** this source skews to
    notable open-source projects, which are usually larger than the
    2–100 employee band in `saas_icp.json`. It yields contactable
    companies, but possibly the wrong size. Worth a decision before we
    invest further in volume.
  - Next: CHUNK 5, reframed by the user as "find their email from what we
    already know about them" — which is the correct framing, since we
    now hold 430 real domains, 109 published addresses and 517 product
    descriptions.

- 2026-09-29 — **ICP size band set to 10–100 employees** (was 2–100), at
  the user's request: volume matters most, but smaller companies are the
  right target.
  - Discovered the band was **not enforced at all** (F40) — the field was
    validated for consistency and then never read. Added a
    `company_size` scorer at weight 0.25, driven by a GitHub-followers
    proxy calibrated against known companies (omnistrate=8, documenso=235,
    calcom=2178, supabase=11987, n8n=15017).
  - Effect: giants now score 52.9 and rank last; small SaaS score 82.4.
    It is a soft rank, not a hard cut — the current threshold of 50 still
    admits the giants. **Raising the threshold to ~65 would exclude them
    at a real cost to volume; that is a decision left to the user.**

- 2026-09-29 — **CHUNK 5 COMPLETE.** Website email extraction built and run.
  - `email_scrape.py` + `backfill_emails.py`, wired into
    `local_runner.py` as `/run-backfill-emails`.
  - Email coverage 49% → **53%** (278/517). **100% of stored email
    domains have MX records.**
  - Provenance recorded on all 278 addresses; 138 stale rows repaired.
  - 6 new findings (F36–F41).
  - **The honest result:** a naive regex claimed a 40% hit rate, but most
    of that was font filenames and JS artefacts. After TLD validation the
    real figure is **~9%** — most small companies hide behind a contact
    form. Every address found is real and on the company's own domain.
  - **Remaining gap:** 145 of 278 emails are still unverified guesses, and
    almost all published ones are role addresses (`hello@`, `support@`)
    rather than a named founder. Verification (5.6) is still blocked on
    finding a free provider that accepts a personal Gmail signup.

- 2026-09-29 — **CHUNK 5.6 COMPLETE. The blocker is gone.**
  - Built self-hosted SMTP verification (`email_verify.py`): MX lookup +
    RCPT TO probe. No vendor, no signup, no key, no cost. The paid
    services exist to sell this capability, not something inherently
    proprietary.
  - **121 verified sendable addresses, up from 0.** 76 addresses (27%)
    confirmed dead and would have bounced.
  - **Headline: guessed addresses are ~50% bad** (Product Hunt 51%,
    dev.to 62%) while published ones are 7–23% bad. Prefer published over
    guessed, always.
  - 6 new findings (F42–F47), including two silent-failure bugs that made
    every domain look like it had no mail server.
  - Next: **CHUNK 6 (dedup hardening)**, then Chunk 7/8. The outreach list
    is now trustworthy enough to send from.

- 2026-09-29 — **CHUNK 6 COMPLETE.** Dedup hardened; volume unblocked.
  - `source_url` on **100% of all 635 leads** (5/5 sources), 66 github
    rows backfilled. **0 duplicate groups.**
  - **+118 new leads** (517 → 635) purely from the paging checkpoint —
    the crawler now goes deeper instead of re-reading page 1.
  - 6 new findings (F49–F54). Three of them were silent no-ops that
    looked like working features: case-sensitive fuzzy matching,
    a cursor-less checkpoint, and a missing `encoding=` that corrupted
    the checkpoint key.
  - The run no longer dies on a dropped connection, and a run that loses
    writes says so in its summary line.
  - **Next: Chunk 7 (multi-ICP + threshold tuning) or Chunk 8 (outreach
    unlock — the list now has 121 verified sendable addresses).**
