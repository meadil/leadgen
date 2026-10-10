"""
Builds the actual prompt for one lead and calls Gemini. First-touch vs
follow-up is decided purely by whether interaction history exists for
this lead — no separate flag needed, matching how the CRM already
tracks state.
"""
from __future__ import annotations

from gemini_client import generate


def _format_history(history: list[dict]) -> str:
    if not history:
        return "(no messages sent yet — this is the first outreach)"

    lines = []
    for msg in history:
        speaker = "You" if msg["direction"] == "outbound" else "Them"
        lines.append(f"[{msg['sent_at']}] {speaker}: {msg['content']}")
    return "\n".join(lines)


def build_prompt(lead: dict, campaign: dict, history: list[dict]) -> str:
    is_first_touch = len(history) == 0

    instruction = (
        "Write a short, genuinely personalized first outreach message."
        if is_first_touch
        else "Write a short, natural follow-up message that continues this conversation "
        "appropriately given what's already been said — don't repeat the opener."
    )

    return f"""You are writing a {campaign['channel']} message for a B2B sales outreach effort.
Approach/pattern to use: {campaign.get('pattern_type', 'general')}
Template guidance: {campaign.get('template', 'none provided — use your judgment')}

Lead:
  Name: {lead.get('full_name')}
  Title: {lead.get('job_title')}
  Company: {lead.get('company_name')}
  Industry: {lead.get('industry')}

Conversation so far:
{_format_history(history)}

{instruction}
Keep it under 100 words, no generic filler, no "I hope this email finds you well."
Output ONLY the message text, nothing else."""


def generate_message(lead: dict, campaign: dict, history: list[dict]) -> str:
    prompt = build_prompt(lead, campaign, history)
    return generate(prompt)
