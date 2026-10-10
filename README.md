# Lead Generation System

**Find the right people automatically, score them, and draft outreach that fits.**

Manual lead generation took hours for 8–10 people. This does it in bulk, for
free, and keeps a record of every conversation so the next one is better.

---

## The problem

You need to reach a specific type of customer — say, SaaS founders in 8
countries. Finding them means scrolling GitHub, Hacker News, Dev.to, Product
Hunt, and LinkedIn by hand. Then guessing emails. Then writing outreach that
doesn't land because it doesn't fit who they are.

This turns that into a pipeline.

---

## How it works

```
    saas_icp.json  ← "who I'm looking for"
            ↓
    ┌───────┴────────────────────────┐
    │  source adapters (run in parallel) │
    │  GitHub · GitHub Orgs · Hacker News │
    │  Dev.to · Product Hunt             │
    └───────┬────────────────────────┘
            ↓
      raw candidates
            ↓
    enrichment      ← company size, site, tech stack
            ↓
    scoring         ← how well do they match the ICP?
            ↓
    email verify    ← Hunter.io, drop invalid ones
            ↓
    Supabase CRM    ← every lead, message, and reply
            ↓
    message engine  ← drafts outreach using each lead's full history
            ↓
    send_approved   ← only what you personally approved
```

**Hard rule: nothing is sent without a human approving it.** The system
proposes; you decide.

---

## What makes it useful

**Swappable sources.** Each source is an adapter behind one interface. Adding
LinkedIn means writing one file — nothing else changes.

**A real memory.** Every lead, message, and reply is stored with enough context
that an agent can read one lead's entire history and know what to say next.

**Free-only by design.** GitHub's free tier, Hunter.io's 50 free verifications,
local models. No paid APIs unless there's genuinely no free option.

**Validated ICP.** Nothing runs against an incomplete customer profile. If the
ICP file is missing fields, it refuses to start.

---

## Setup

```bash
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt
copy env.example .env      # then fill in your keys
```

`.env` needs:
- `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` — where leads are stored
- `GITHUB_TOKEN` — raises the rate limit from 10/min to 5,000/hr
- `HUNTER_API_KEY` — email verification (free tier: 50/month)
- `GEMINI_API_KEYS` — used by the message engine
- `GMAIL_ACCOUNTS` — `email:app_password` pairs for sending

---

## Running it

```bash
python main.py                  # full pipeline
python local_runner.py          # run locally, step by step
python email_verify.py          # verify a batch of emails
python send_approved.py         # send only what was approved
python crm.py                   # inspect the CRM
```

The database schema is in `crm-schema.sql`.

---

## What's in here

| File | What it does |
|------|--------------|
| `saas_icp.json` | The real target: SaaS founders, 8 countries |
| `example_icp.json` | Test profile used to prove the mechanics work |
| `icp_schema.py` | Validates the ICP — refuses to run on a bad one |
| `adapters/base.py` | The interface every source implements |
| `adapters/github_adapter.py` | Find people by GitHub activity |
| `adapters/github_orgs_adapter.py` | Find whole companies by their org |
| `adapters/hn_adapter.py` | Hacker News — who posts, who asks |
| `adapters/devto_adapter.py` | Dev.to authors |
| `adapters/producthunt_adapter.py` | Product Hunt launches |
| `enrichment.py` | Fill in company details |
| `scoring.py` | Rank candidates against the ICP |
| `email_scrape.py` / `email_verify.py` | Find and verify emails |
| `crm.py` | Read/write the CRM |
| `message_engine/` | Draft and refine outreach |
| `send_approved.py` | Send approved messages only |
| `PROJECT_STATUS.txt` | Full technical handoff, including what's broken |
| `LESSONS_LEARNED.md` | 35 KB of traps — **read before changing anything** |
| `PLAN.md` | The full build plan |

---

## Status

Working end to end. The main open item: GitHub was the first working source but
it's the wrong shape for finding founders — it surfaces developers. The other
adapters exist to solve that.

`LESSONS_LEARNED.md` is 35 KB of hard-won bugs. Read it first.