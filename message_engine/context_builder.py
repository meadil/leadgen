"""
Pulls everything the message engine needs for one lead: the lead's own
record, and every past interaction ordered oldest to newest. This is the
full "what do we know, what's been said" context handed to Gemini.
"""
from __future__ import annotations

from supabase import Client


def get_lead(client: Client, lead_id: str) -> dict:
    result = client.table("leads").select("*").eq("id", lead_id).single().execute()
    if not result.data:
        raise ValueError(f"No lead found with id {lead_id}")
    return result.data


def get_interaction_history(client: Client, lead_id: str) -> list[dict]:
    result = (
        client.table("interactions")
        .select("*")
        .eq("lead_id", lead_id)
        .order("sent_at", desc=False)
        .execute()
    )
    return result.data or []


def get_campaign(client: Client, campaign_id: str) -> dict:
    result = client.table("campaigns").select("*").eq("id", campaign_id).single().execute()
    if not result.data:
        raise ValueError(f"No campaign found with id {campaign_id}")
    return result.data
