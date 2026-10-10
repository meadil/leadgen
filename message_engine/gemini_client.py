"""
Thin wrapper around Google's Gemini API, with support for rotating across
multiple API keys — useful since you have several Google accounts, each
with its own free-tier quota.
"""
from __future__ import annotations

import os
import random

import requests

GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
# gemini-2.5-flash is retired for new projects (404) — 3.8-flash is the
# current latest per Google docs (Sep 2026).
DEFAULT_MODEL = "gemini-3.8-flash"


def _get_api_keys() -> list[str]:
    """
    Reads one or more comma-separated keys from GEMINI_API_KEYS in .env:
      GEMINI_API_KEYS=key_from_account_1,key_from_account_2,key_from_account_3
    """
    raw = os.getenv("GEMINI_API_KEYS", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise RuntimeError("Set GEMINI_API_KEYS in .env (comma-separated if you have several)")
    return keys


def generate(prompt: str, model: str = DEFAULT_MODEL) -> str:
    # Fallback chain: 3.8-flash 503s under load — lite models stay up.
    models = [model]
    for fallback in ("gemini-3.5-flash-lite", "gemini-flash-lite-latest"):
        if fallback not in models:
            models.append(fallback)

    last_err = None
    for attempt_model in models:
        key = random.choice(_get_api_keys())  # spreads load across accounts over many calls
        url = GEMINI_URL_TEMPLATE.format(model=attempt_model)

        # AQ.* auth keys (all new AI Studio keys) must go via x-goog-api-key
        # header — ?key= query param is rejected for some accounts.
        try:
            resp = requests.post(
                url,
                headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=30,
            )
        except requests.RequestException as e:
            # Timeout / connection drop — try next model before giving up.
            last_err = f"Gemini network error on {attempt_model}: {e}"
            continue
        if resp.status_code in (429, 500, 503):
            last_err = f"Gemini {resp.status_code} on {attempt_model}: {resp.text[:200]}"
            continue
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            # Surface Google's error body (403/404 messages matter here)
            raise RuntimeError(f"Gemini {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()

    raise RuntimeError(last_err or "Gemini: all models unavailable")
