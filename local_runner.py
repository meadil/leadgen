"""
Tiny local HTTP server that runs the lead-gen scripts on this Windows
host, called by n8n (running in Docker) over host.docker.internal.

Run this once, leave it running in its own terminal:
  venv\\Scripts\\python.exe local_runner.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback

from flask import Flask, jsonify, request

app = Flask(__name__)
PYTHON = sys.executable
LEADGEN_DIR = os.path.dirname(os.path.abspath(__file__))


def _run(cmd: list[str]) -> dict:
    try:
        # encoding=utf-8 + errors=replace: main.py prints utf-8 (em-dashes
        # from ICP names). Default cp1252 decode crashes with
        # UnicodeDecodeError and yields empty stdout/stderr via HTTP.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=LEADGEN_DIR, encoding="utf-8", errors="replace")
        return {
            "returncode": result.returncode,
            "stdout": (result.stdout or "")[-4000:],
            "stderr": (result.stderr or "")[-4000:],
        }
    except subprocess.TimeoutExpired as e:
        return {
            "returncode": -1,
            "stdout": (e.stdout or "")[-2000:],
            "stderr": f"TIMED OUT after {e.timeout}s",
        }


@app.errorhandler(Exception)
def handle_error(e):
    # Full trace, not just the error text — so we see the real file/line.
    return jsonify({"error": str(e), "trace": traceback.format_exc()}), 200


@app.route("/run-leadgen", methods=["POST"])
def run_leadgen():
    data = request.get_json(silent=True) or {}
    icp_file = data.get("icp_file", "saas_icp.json")
    limit = str(data.get("limit", 100))
    return jsonify(_run([PYTHON, "main.py", icp_file, limit]))


@app.route("/run-draft", methods=["POST"])
def run_draft():
    data = request.get_json(silent=True) or {}
    campaign_id = data.get("campaign_id")
    if not campaign_id:
        return jsonify({"error": "campaign_id missing from request body"}), 200
    status = data.get("status", "new")
    return jsonify(_run([
        PYTHON, "message_engine/main.py",
        "--batch", campaign_id,
        "--status", status,
        "--auto",
    ]))


@app.route("/run-backfill-emails", methods=["POST"])
def run_backfill_emails():
    """
    Chunk 5 — find addresses the company published on its own site.

    Separate from the lead-gen run on purpose: it only ever FILLS A GAP
    (leads with a domain and no email), never overwrites, and it is
    re-runnable, so it suits a daily schedule better than a 3-hourly one.
    A site that publishes an address today is picked up tomorrow.
    """
    data = request.get_json(silent=True) or {}
    limit = str(data.get("limit", 100))
    return jsonify(_run([PYTHON, "backfill_emails.py", limit]))


@app.route("/run-verify-emails", methods=["POST"])
def run_verify_emails():
    """
    Verify stored addresses against their mail servers (self-hosted SMTP).

    Separate from the backfill on purpose: finding an address is cheap,
    checking it costs an SMTP conversation, so this runs less often and
    only re-checks what is unconfirmed. Defaults to guessed addresses
    only; pass {"all": true} to re-check everything.
    """
    data = request.get_json(silent=True) or {}
    limit = str(data.get("limit", 100))
    cmd = [PYTHON, "backfill_verify_emails.py", limit]
    if data.get("all"):
        cmd.append("--all")
    return jsonify(_run(cmd))


@app.route("/run-send", methods=["POST"])
def run_send():
    return jsonify(_run([PYTHON, "send_approved.py"]))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005)
