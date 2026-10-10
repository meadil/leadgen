"""
Sends only interactions where a human has set approved=true and it hasn't
been sent yet. This is the one review gate before anything goes out —
nothing here sends automatically based on a draft alone.

Uses Gmail via SMTP app passwords, rotating across multiple accounts the
same way gemini_client.py rotates API keys.
"""
from __future__ import annotations

import os
import random
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText

from dotenv import load_dotenv
load_dotenv()

from crm import get_client


def _get_gmail_accounts() -> list[tuple[str, str]]:
    """
    GMAIL_ACCOUNTS in .env as comma-separated email:app_password pairs:
      GMAIL_ACCOUNTS=you1@gmail.com:app_password_1,you2@gmail.com:app_password_2
    Use a Gmail App Password (Google Account -> Security -> App Passwords,
    requires 2FA on) — never your normal account password.
    """
    raw = os.getenv("GMAIL_ACCOUNTS", "")
    accounts = []
    for pair in raw.split(","):
        if ":" in pair:
            email, pw = pair.split(":", 1)
            accounts.append((email.strip(), pw.strip()))
    if not accounts:
        raise RuntimeError("Set GMAIL_ACCOUNTS in .env (email:app_password, comma-separated)")
    return accounts


def _send_via_gmail(to_email: str, subject: str, body: str) -> None:
    sender_email, app_password = random.choice(_get_gmail_accounts())

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = to_email

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender_email, app_password)
        server.sendmail(sender_email, [to_email], msg.as_string())


def send_approved():
    client = get_client()

    # Gate: approved by a human AND still pending. (NOTE: sent_at can't be
    # used as the unsent marker — the column is NOT NULL with a now()
    # default, so it is never null. outcome='pending' is the marker;
    # flipped to 'sent' after delivery.)
    approved = (
        client.table("interactions")
        .select("id, lead_id, content, channel")
        .eq("approved", True)
        .eq("outcome", "pending")
        .execute()
        .data
    )

    print(f"Found {len(approved)} approved, unsent messages")

    for row in approved:
        if row["channel"] != "email":
            print(f"  Skipping interaction {row['id']} — channel '{row['channel']}' not wired up yet")
            continue

        lead = (
            client.table("leads")
            .select("email, full_name")
            .eq("id", row["lead_id"])
            .single()
            .execute()
            .data
        )
        if not lead or not lead.get("email"):
            print(f"  Skipping interaction {row['id']} — no email on file for this lead")
            continue

        _send_via_gmail(lead["email"], subject="Quick question", body=row["content"])
        client.table("interactions").update(
            {"sent_at": datetime.now(timezone.utc).isoformat(), "outcome": "sent"}
        ).eq("id", row["id"]).execute()
        print(f"  Sent to {lead['email']}")


if __name__ == "__main__":
    send_approved()
