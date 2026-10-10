"""
Per-source paging checkpoints.

WHY THIS EXISTS (PLAN.md task 6.4)
----------------------------------
Every adapter started from page 1 on every run. Measured 2026-09-29:
135 Product Hunt leads came from only 103 distinct posts, all published
in the same month — we were re-reading the same window daily and calling
it a run. The pipeline did more work each day for fewer new leads.

A checkpoint stores the last position consumed per (adapter, ICP, feed)
so the next run resumes from there and goes DEEPER. This is what turns a
daily cron into a steadily deepening crawl instead of a treadmill.

Storage: a local JSON file, not a database table.
  - This pipeline runs on ONE Windows host (n8n calls it over HTTP via
    local_runner.py), so there is no second process to share state with.
  - A DB table would mean a migration the operator has to run by hand in
    the Supabase SQL editor before anything works. A file just works.
  - Writes are atomic (temp file + os.replace) so a crash mid-write
    cannot corrupt the checkpoint and send the crawler back to page 1.
  - Stale checkpoints are ignored: each carries a timestamp, and anything
    older than MAX_AGE_DAYS restarts from the top. That self-heals if the
    file is deleted, corrupted, or restored from an old backup.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Optional

# A rolling feed's cursor goes stale: the interesting content scrolls past.
# A static archive (HN, GitHub) never does. 7 days is a reasonable default.
MAX_AGE_DAYS = 7

CHECKPOINT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".source_checkpoints.json"
)


def _load() -> dict[str, dict]:
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        # Missing, corrupt, or unreadable — start clean rather than crash.
        # The crawler is self-healing: a lost checkpoint costs one
        # re-read of the first page, nothing more.
        return {}


def _save(data: dict[str, dict]) -> None:
    """Atomic write, so an interrupted save can't corrupt the file."""
    directory = os.path.dirname(CHECKPOINT_FILE)
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, CHECKPOINT_FILE)
        except Exception:
            # Never leave a stray temp file behind.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        # Bookkeeping must never take down a lead-gen run.
        print(f"  (could not write checkpoint file: {e})")


def _key(adapter: str, icp_name: str) -> str:
    return f"{adapter}::{icp_name}"


def get_checkpoint(adapter: str, icp_name: str) -> Optional[dict]:
    """Saved position for one (adapter, ICP), or None on first run."""
    return _load().get(_key(adapter, icp_name))


def save_checkpoint(adapter: str, icp_name: str, position: dict) -> None:
    """
    Merge a new position into the store.

    Only the `order:topic` entries the adapter reported are replaced —
    entries for feeds it did not touch this run are preserved, so a run
    that stopped early does not erase the position of the feeds it never
    reached.
    """
    data = _load()
    key = _key(adapter, icp_name)
    existing: dict[str, Any] = dict(data.get(key) or {})

    all_done = bool(position.pop("_all_done", None))
    for k, v in position.items():
        if isinstance(v, dict):
            v = dict(v)
            v["saved_at"] = time.time()
        existing[k] = v

    if all_done:
        existing["_all_done"] = {"at": time.time()}
    else:
        existing.pop("_all_done", None)

    existing["_updated_at"] = time.time()
    data[key] = existing
    _save(data)


def clear_checkpoint(adapter: str, icp_name: str) -> None:
    data = _load()
    if data.pop(_key(adapter, icp_name), None) is not None:
        _save(data)


def should_restart(position: Optional[dict], max_age_days: int = MAX_AGE_DAYS) -> bool:
    """
    Should this source start from the top again?

    True when: never run, saved too long ago, or fully exhausted. The
    last case matters for rolling feeds — once every topic/order
    combination has been walked, new content arrives at page 1, so
    resuming deep would only re-read old posts.
    """
    if not position:
        return True
    if position.get("_all_done"):
        return True
    saved = position.get("_updated_at") or position.get("saved_at")
    if not saved:
        return True
    try:
        age_days = (time.time() - float(saved)) / 86400
    except (TypeError, ValueError):
        return True
    return age_days > max_age_days
