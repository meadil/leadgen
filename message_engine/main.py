"""
Message engine entry point.

Usage:
  python main.py <lead_id> <campaign_id>
      Draft one message for one lead, show it, then ask before logging it.

  python main.py --batch <campaign_id> [--status new]
      Draft (and confirm one by one) for every lead currently in that status.
      Defaults to status "new" if not given.

Each confirmed message gets written into `interactions`, and the lead's
status is bumped to "contacted" — so the same lead won't get drafted for
again as a "first touch" next time this runs.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))  # import crm.py from project root

from dotenv import load_dotenv
load_dotenv()

from crm import get_client
from context_builder import get_lead, get_interaction_history, get_campaign
from generator import generate_message


def draft_for_lead(client, lead_id: str, campaign_id: str, auto: bool = False):
    lead = get_lead(client, lead_id)
    campaign = get_campaign(client, campaign_id)
    history = get_interaction_history(client, lead_id)

    message = generate_message(lead, campaign, history)

    print(f"\n--- Draft for {lead.get('full_name')} ({lead.get('company_name')}) ---")
    print(message)
    print("---")

    if auto:
        # Non-interactive (n8n) mode: always log unapproved. A human reviews
        # and flips `approved` to true in Supabase before anything sends.
        _log(client, lead_id, campaign, message, approved=False)
        return

    choice = input("Log this as sent? [y/N/edit]: ").strip().lower()
    if choice == "y":
        _log(client, lead_id, campaign, message, approved=True)
    elif choice == "edit":
        edited = input("Paste your edited version:\n")
        _log(client, lead_id, campaign, edited, approved=True)
    else:
        print("Skipped — nothing logged.")


def _log(client, lead_id: str, campaign: dict, content: str, approved: bool):
    client.table("interactions").insert({
        "lead_id": lead_id,
        "campaign_id": campaign["id"],
        "direction": "outbound",
        "channel": campaign["channel"],
        "content": content,
        "outcome": "pending",
        "approved": approved,
    }).execute()
    client.table("leads").update({"status": "contacted"}).eq("id", lead_id).execute()
    print(f"Logged to CRM (approved={approved}).")


def run_batch(client, campaign_id: str, status: str, auto: bool = False):
    leads = client.table("leads").select("id").eq("status", status).execute().data
    print(f"Found {len(leads)} leads with status '{status}'")
    for lead in leads:
        draft_for_lead(client, lead["id"], campaign_id, auto=auto)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("lead_id", nargs="?", help="Lead UUID (omit when using --batch)")
    parser.add_argument("campaign_id", nargs="?", help="Campaign UUID (omit when using --batch)")
    parser.add_argument("--batch", metavar="CAMPAIGN_ID", help="Draft for every lead in --status")
    parser.add_argument("--status", default="new", help="Lead status to batch over (default: new)")
    parser.add_argument("--auto", action="store_true", help="Non-interactive: log every draft unapproved, for automation")
    args = parser.parse_args()

    client = get_client()

    if args.batch:
        run_batch(client, args.batch, args.status, auto=args.auto)
    else:
        if not args.lead_id or not args.campaign_id:
            parser.error("Provide lead_id and campaign_id, or use --batch CAMPAIGN_ID")
        draft_for_lead(client, args.lead_id, args.campaign_id, auto=args.auto)
