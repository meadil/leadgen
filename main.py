"""
Lead generation engine — entry point.

All five stages are wired up now:
  Stage 1  icp_schema.load_icp()
  Stage 2  adapters/*
  Stage 3  enrichment.enrich()
  Stage 4  scoring.score_candidate() + crm.find_duplicate()
  Stage 5  crm.insert_lead()
"""
from __future__ import annotations

import sys

# Force UTF-8 output regardless of the terminal's default codepage
# (fixes garbled em-dashes / special characters on Windows).
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()  # reads .env into environment variables — needed for crm.py's os.getenv() calls

import os

from icp_schema import load_icp
from adapters.github_adapter import GitHubAdapter
from adapters.producthunt_adapter import ProductHuntAdapter
from adapters.hn_adapter import HackerNewsAdapter
from adapters.devto_adapter import DevToAdapter
from adapters.github_orgs_adapter import GitHubOrgsAdapter
from enrichment import enrich
from deep_enrichment import deep_enrich
from scoring import score_candidate
from crm import get_client, upsert_icp_profile, find_duplicate, insert_lead, link_lead_to_icp
from paging import get_checkpoint, save_checkpoint, should_restart

ADAPTERS = [ProductHuntAdapter()]
try:
    ADAPTERS.append(HackerNewsAdapter())
except Exception as e:  # noqa: BLE001 - an adapter must never kill the run
    print(f"Skipping Hacker News adapter: {e}")
try:
    ADAPTERS.append(DevToAdapter())
except Exception as e:  # noqa: BLE001
    print(f"Skipping dev.to adapter: {e}")
try:
    ADAPTERS.append(GitHubOrgsAdapter())
except Exception as e:  # noqa: BLE001
    print(f"Skipping GitHub orgs adapter: {e}")
try:
    ADAPTERS.append(GitHubAdapter())
except Exception as e:  # noqa: BLE001
    print(f"Skipping GitHub adapter: {e}")


def _attach_checkpoint(client, icp_id, adapter, icp_name: str) -> None:
    """
    Let an adapter resume from where it left off (PLAN.md task 6.4).

    Without this, Product Hunt re-read the same newest posts on every
    run, so the pipeline did more work each day for fewer new leads.
    With it, each run goes deeper into the archive.
    """
    if not hasattr(adapter, "_on_checkpoint"):
        return
    position = get_checkpoint(adapter.name, icp_name)
    if should_restart(position):
        if position:
            print(f"  ({adapter.name}: checkpoint stale/exhausted, starting from top)")
        adapter._resume = None
    else:
        adapter._resume = position
        print(f"  ({adapter.name}: resuming from checkpoint)")

    def _save(next_at: dict) -> None:
        try:
            save_checkpoint(adapter.name, icp_name, next_at)
        except Exception as e:  # noqa: BLE001 - never fail a run over bookkeeping
            print(f"  (could not save {adapter.name} checkpoint: {e})")

    adapter._on_checkpoint = _save


def run(icp_path: str, limit_per_adapter: int = 20):
    icp = load_icp(icp_path)
    print(f"Loaded ICP: {icp.icp_name}")

    client = get_client()
    icp_id = upsert_icp_profile(client, icp)

    raw_candidates = []
    for adapter in ADAPTERS:
        print(f"Running adapter: {adapter.name}")
        # Let resumable adapters pick up where they left off.
        _attach_checkpoint(client, icp_id, adapter, icp.icp_name)
        found = adapter.search(icp, limit=limit_per_adapter)
        print(f"  -> {len(found)} raw candidates")
        raw_candidates.extend(found)

    print(f"\nEnriching {len(raw_candidates)} candidates...")
    enriched = [enrich(c) for c in raw_candidates]

    if os.getenv("OPENCODE_API_KEY"):
        print("Running AI deep enrichment via MiMo (slower — one call per lead with a domain)...")
        enriched = [deep_enrich(c, icp) for c in enriched]
    else:
        print("Skipping AI deep enrichment (no OPENCODE_API_KEY set in .env)")

    print("Scoring, filtering, and deduping...")
    written = skipped_score = skipped_dupe = linked_existing = failed_writes = 0

    for candidate in enriched:
        score, breakdown = score_candidate(candidate, icp)
        if score < icp.minimum_score_threshold:
            skipped_score += 1
            continue

        dupe = find_duplicate(client, candidate)
        if dupe:
            # Contact dedup stays global (one row per person), but the
            # person can now count toward MANY ICPs via lead_icps.
            if link_lead_to_icp(client, dupe["id"], icp_id, score, breakdown):
                linked_existing += 1
                print(f"  = {candidate.full_name} (score {score}) -> already in CRM, linked to this ICP")
            else:
                skipped_dupe += 1
            continue

        if insert_lead(client, candidate, icp_id, score, breakdown):
            written += 1
            print(f"  + {candidate.full_name} (score {score}) -> written to CRM")
        else:
            # Counted separately so a run that lost writes is visibly
            # wrong rather than quietly short.
            failed_writes += 1

    print(
        f"\nDone. {written} leads written, {linked_existing} existing linked to this ICP, "
        f"{skipped_score} below threshold, {skipped_dupe} duplicates skipped."
        + (f"  ** {failed_writes} WRITE FAILURES — see errors above. **" if failed_writes else "")
    )


if __name__ == "__main__":
    icp_file = sys.argv[1] if len(sys.argv) > 1 else "example_icp.json"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    run(icp_file, limit_per_adapter=limit)
