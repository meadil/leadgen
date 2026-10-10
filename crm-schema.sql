-- ============================================================
-- CRM / Lead database schema for Supabase (Postgres)
-- Run this in the Supabase SQL editor.
-- ============================================================

create extension if not exists pgcrypto; -- provides gen_random_uuid()

-- ------------------------------------------------------------
-- Helper: auto-update `updated_at` on row changes
-- ------------------------------------------------------------
create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

-- ============================================================
-- 1. icp_profiles
-- Stores each ICP definition (from the ICP schema doc) so it
-- can be reused across lead-gen runs and compared later.
-- ============================================================
create table icp_profiles (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  definition jsonb not null,          -- the full ICP JSON object
  min_score_threshold int not null default 70,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create trigger trg_icp_profiles_updated_at
  before update on icp_profiles
  for each row execute function set_updated_at();

-- ============================================================
-- 2. campaigns
-- A "campaign" here means a message pattern/approach being
-- tested (e.g. "pain-point opener v1"), not a batch send.
-- ============================================================
create table campaigns (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  channel text not null check (channel in ('email', 'linkedin_dm', 'twitter_dm', 'other')),
  pattern_type text,                  -- e.g. 'pain_point', 'curiosity', 'direct_ask', 'social_proof'
  template text,                      -- the template/prompt used to generate messages for this pattern
  description text,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

-- ============================================================
-- 3. leads
-- One row per person. Dedup relies on normalized_email
-- (generated column) plus an app-level fuzzy check on
-- company_domain + name for near-duplicates.
-- ============================================================
create table leads (
  id uuid primary key default gen_random_uuid(),
  icp_profile_id uuid references icp_profiles(id) on delete set null,

  -- person
  full_name text,
  first_name text,
  last_name text,
  job_title text,
  seniority_level text,
  department text,
  email text,
  normalized_email text generated always as (lower(trim(email))) stored,
  linkedin_url text,
  github_url text,
  twitter_url text,
  other_social jsonb default '{}'::jsonb,

  -- company
  company_name text,
  company_domain text,
  company_size int,
  industry text,
  geography_country text,
  geography_city text,

  -- lead-gen metadata
  source text,                        -- which adapter found this lead (e.g. 'github', 'theharvester')
  source_details jsonb default '{}'::jsonb,
  trigger_events text[] default '{}',
  pain_indicators_matched text[] default '{}',
  score numeric(5,2),
  score_breakdown jsonb default '{}'::jsonb,  -- per-field score contribution, kept for later analysis

  -- pipeline state
  status text not null default 'new'
    check (status in ('new', 'contacted', 'replied', 'call_booked', 'won', 'lost', 'disqualified')),
  next_action_date date,
  notes text,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  constraint uq_leads_normalized_email unique (normalized_email)
);

create trigger trg_leads_updated_at
  before update on leads
  for each row execute function set_updated_at();

create index idx_leads_status on leads(status);
create index idx_leads_company_domain on leads(company_domain);
create index idx_leads_icp_profile on leads(icp_profile_id);

-- ============================================================
-- 4. interactions
-- Every message, in either direction, tied to a lead and the
-- campaign/pattern that produced it. This is the table your
-- feedback loop reads to figure out what actually works.
-- ============================================================
create table interactions (
  id uuid primary key default gen_random_uuid(),
  lead_id uuid not null references leads(id) on delete cascade,
  campaign_id uuid references campaigns(id) on delete set null,

  direction text not null check (direction in ('outbound', 'inbound')),
  channel text not null check (channel in ('email', 'linkedin_dm', 'twitter_dm', 'other')),
  content text not null,

  sent_at timestamptz not null default now(),
  opened_at timestamptz,
  replied_at timestamptz,

  outcome text default 'pending'
    check (outcome in ('pending', 'ignored', 'soft_no', 'interested', 'call_booked', 'not_interested', 'bounced')),
  sentiment text,                     -- optional: 'positive' / 'neutral' / 'negative', filled in later

  metadata jsonb default '{}'::jsonb, -- personalization tokens, generation params, etc.
  created_at timestamptz not null default now()
);

create index idx_interactions_lead on interactions(lead_id);
create index idx_interactions_campaign on interactions(campaign_id);
create index idx_interactions_outcome on interactions(outcome);

-- ============================================================
-- Note on Row Level Security (RLS):
-- Supabase tables are not RLS-protected by default. If you only
-- ever write to this DB from a backend script using the
-- service_role key, RLS doesn't block you (service_role bypasses
-- it). Only turn RLS on with policies if you plan to query this
-- from a client-side app using the anon key.
-- ============================================================
