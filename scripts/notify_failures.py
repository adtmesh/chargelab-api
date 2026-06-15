"""
ChargeLab Failure Notifier
--------------------------
Polls the ChargeLab API for failed charging sessions and sends an email
alert for any new failures not seen in previous runs.

Designed to run hourly via cron:
    0 * * * * /path/to/.venv/bin/python /path/to/scripts/notify_failures.py

State is persisted in data/seen_failures.json so each failure is only
notified once regardless of how many times the script runs.

Setup:
    1. Add to .env:
         NOTIFY_EMAIL_FROM=you@gmail.com
         NOTIFY_EMAIL_TO=you@gmail.com
         NOTIFY_EMAIL_PASSWORD=your_gmail_app_password
    2. Get a Gmail App Password at:
         https://myaccount.google.com/apppasswords
       (requires 2FA to be enabled on your Google account)
"""

import os
import sys
import json
import smtplib
import textwrap
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

BASE_URL = "https://api.chargelab.io/core/v1"
API_KEY = os.getenv("CHARGELAB_API_KEY")
EMAIL_FROM = os.getenv("NOTIFY_EMAIL_FROM")
EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO")
EMAIL_PASSWORD = os.getenv("NOTIFY_EMAIL_PASSWORD")

STATE_FILE = ROOT / "data" / "seen_failures.json"

FAILURE_EXPLANATIONS = {
    "FAILED_TO_RECEIVE_REMOTE_START_TRANSACTION_RESPONSE": (
        "Charger didn't respond to the start command. "
        "Likely a connectivity/modem issue on the charger."
    ),
    "TIMED_OUT_WAITING_FOR_CHARGE_HANDLER_RESPONSE": (
        "ChargeLab's system timed out internally. "
        "This is a server-side issue — no action needed on the charger."
    ),
    "STOPPED_BEFORE_TX": (
        "Session ended before charging started. "
        "Vehicle may have disconnected or the driver cancelled."
    ),
}

HEADERS = {"Authorization": f"x-auth {API_KEY}"}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

missing = [k for k, v in {
    "CHARGELAB_API_KEY": API_KEY,
    "NOTIFY_EMAIL_FROM": EMAIL_FROM,
    "NOTIFY_EMAIL_TO": EMAIL_TO,
    "NOTIFY_EMAIL_PASSWORD": EMAIL_PASSWORD,
}.items() if not v]

if missing:
    sys.exit(f"ERROR: Missing environment variables: {', '.join(missing)}")

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def get_all(endpoint: str, params: dict) -> list:
    results = []
    offset = 0
    limit = 1000
    while True:
        resp = requests.get(
            f"{BASE_URL}/{endpoint}",
            headers=HEADERS,
            params={**params, "offset": offset, "limit": limit},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        entities = data.get("entities", [])
        results.extend(entities)
        if offset + limit >= data.get("totalCount", 0):
            break
        offset += limit
    return results


def fetch_chargers() -> dict:
    chargers = get_all("chargers", {"role": "COMPANY"})
    return {c["chargerId"]: c["name"] for c in chargers}


def fetch_recent_failures(hours: int = 2) -> list:
    """Fetch failed sessions from the last `hours` hours (2h window catches any cron drift)."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return get_all("sessions", {
        "role": "COMPANY",
        "filter_in[status]": "FAILED",
        "filter_ge[createTime]": since,
    })

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_seen() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen: set):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(seen)))

# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def format_email_body(new_failures: list, charger_names: dict) -> str:
    lines = [
        f"{len(new_failures)} new charging session failure(s) detected:\n",
    ]
    for s in new_failures:
        cid = s.get("chargerId", "unknown")
        name = charger_names.get(cid, cid[:8])
        sid = s.get("sessionId", s.get("id", "unknown"))
        code = s.get("failureCode", "UNKNOWN")
        create_time = s.get("createTime", "unknown")
        explanation = FAILURE_EXPLANATIONS.get(code, "Unknown failure — check ChargeLab dashboard.")

        lines.append(f"  Charger     : {name}")
        lines.append(f"  Session ID  : {sid}")
        lines.append(f"  Time        : {create_time}")
        lines.append(f"  Failure code: {code}")
        lines.append(f"  What it means: {explanation}")
        lines.append("")

    lines.append("View the full dashboard for details.")
    return "\n".join(lines)


def send_email(subject: str, body: str):
    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Polling ChargeLab for failures...")

    charger_names = fetch_chargers()
    failures = fetch_recent_failures(hours=2)

    if not failures:
        print("No failures in the last 2 hours.")
        return

    seen = load_seen()
    new_failures = [s for s in failures if s.get("sessionId", s.get("id")) not in seen]

    if not new_failures:
        print(f"{len(failures)} failure(s) found but all already notified.")
        return

    print(f"{len(new_failures)} new failure(s) — sending email to {EMAIL_TO}...")

    subject = f"ChargeLab Alert: {len(new_failures)} charging session failure(s)"
    body = format_email_body(new_failures, charger_names)
    send_email(subject, body)

    # Mark all as seen (including already-seen ones, to keep the set current)
    seen.update(s.get("sessionId", s.get("id")) for s in failures)
    save_seen(seen)

    print("Email sent.")


if __name__ == "__main__":
    main()
