"""
ChargeLab Failure Diagnosis Script
-----------------------------------
Fetches failed sessions from the ChargeLab API, computes per-charger failure
rates, detects mid-session power drops from local CSV exports, and prints a
structured report to the terminal.

Usage:
    python scripts/diagnose_failures.py [--days 30] [--csv data/]
"""

import os
import sys
import csv
import glob
import argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import requests
from dotenv import load_dotenv
from tabulate import tabulate

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

BASE_URL = "https://api.chargelab.io/core/v1"
API_KEY = os.getenv("CHARGELAB_API_KEY")

if not API_KEY:
    sys.exit("ERROR: CHARGELAB_API_KEY not set. Copy .env.example to .env and fill it in.")

HEADERS = {"Authorization": f"x-auth {API_KEY}"}

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def get_all(endpoint: str, params: dict) -> list:
    """Fetch all pages from a paginated endpoint."""
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
    """Return a mapping of chargerId -> charger name."""
    chargers = get_all("chargers", {"role": "COMPANY"})
    return {c["chargerId"]: c["name"] for c in chargers}


def fetch_sessions(since: datetime, statuses: list[str]) -> list:
    """Fetch sessions with given statuses created on or after `since`."""
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    status_filter = ",".join(statuses)
    return get_all("sessions", {
        "role": "COMPANY",
        "filter_in[status]": status_filter,
        "filter_ge[createTime]": since_str,
    })

# ---------------------------------------------------------------------------
# CSV analysis
# ---------------------------------------------------------------------------

def load_csv_intervals(csv_dir: str) -> list[dict]:
    """Load all 15-min interval rows from every CSV in csv_dir."""
    rows = []
    for path in glob.glob(os.path.join(csv_dir, "*.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    return rows


def detect_zero_energy_intervals(csv_rows: list[dict]) -> dict:
    """
    Detect sessions with a genuine mid-session power drop — a zero-energy
    interval that occurs in the first half of the session AND before the
    charger has delivered enough energy to plausibly have filled the battery.

    Overnight sessions where the battery hits 100% and idles are excluded:
    those zero intervals appear in the second half and after substantial
    energy has already been delivered, so they are expected behaviour.

    Heuristics:
      - Zero interval must be in the first 50% of intervals (by position)
      - Cumulative energy at that point must be < 20 kWh (well below a full
        charge for any common EV on a 12 kW L2 charger)
      - Session must have delivered >0.5 kWh total (ignore trivial sessions)

    Returns {chargerId: [session_id, ...]}
    """
    sessions: dict[str, list] = defaultdict(list)
    charger_by_session: dict[str, str] = {}
    for row in csv_rows:
        sid = row.get("Session ID", "").strip()
        if not sid:
            continue
        sessions[sid].append(row)
        charger_by_session[sid] = row.get("Charger device ID", "").strip()

    issues: dict[str, list[str]] = defaultdict(list)

    for sid, intervals in sessions.items():
        if len(intervals) < 2:
            continue

        try:
            intervals.sort(key=lambda r: r["Interval start date/time (YYYY-MM-DD hh:mm:ss) (UTC)"])
        except KeyError:
            continue

        # Total energy for this session
        try:
            total_energy = float(intervals[0].get("Session energy provided (kWh)", "0") or 0)
        except ValueError:
            continue

        if total_energy < 0.5:
            continue  # too short/trivial to analyse

        midpoint = len(intervals) // 2
        cumulative = 0.0

        # Only examine the first half of intervals
        for i, interval in enumerate(intervals[:-1]):
            try:
                energy = float(interval.get("Interval energy provided (kWh)", "0") or 0)
            except ValueError:
                continue

            if i >= midpoint:
                break  # past the first half — stop, any zeros here are idle

            if energy == 0.0 and cumulative < 20.0:
                cid = charger_by_session[sid]
                if sid not in issues[cid]:
                    issues[cid].append(sid)
                break

            cumulative += energy

    return dict(issues)

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def failure_rate(failures: int, total: int) -> str:
    if total == 0:
        return "N/A"
    return f"{failures / total * 100:.1f}%"


def hour_of_day(iso_str: str) -> int | None:
    """Parse ISO datetime and return the local hour embedded in the string.
    The API returns e.g. '2026-06-13T18:47:18.492177-06:00[America/Denver]'
    — the [Timezone] suffix is non-standard so we strip it before parsing,
    then use the offset that's already in the string as the local time."""
    if not iso_str:
        return None
    try:
        clean = iso_str.split("[")[0]  # strip [America/Denver]
        dt = datetime.fromisoformat(clean)
        return dt.hour  # hour is already in local time per the offset
    except Exception:
        return None


def build_report(days: int, csv_dir: str):
    since = datetime.now(timezone.utc) - timedelta(days=days)

    print(f"\nFetching chargers...")
    charger_names = fetch_chargers()

    print(f"Fetching ALL sessions since {since.strftime('%Y-%m-%d')}...")
    all_sessions = fetch_sessions(since, ["PREPARING", "CHARGING", "SUCCESSFUL", "FAILED"])

    print(f"Fetching FAILED sessions since {since.strftime('%Y-%m-%d')}...")
    failed_sessions = [s for s in all_sessions if s.get("status") == "FAILED"]

    print(f"Loading CSV interval data from {csv_dir}...")
    csv_rows = load_csv_intervals(csv_dir)
    zero_energy = detect_zero_energy_intervals(csv_rows)

    # --- Per-charger counts ---
    total_by_charger: dict[str, int] = defaultdict(int)
    fail_by_charger: dict[str, int] = defaultdict(int)
    fail_hours: dict[str, list[int]] = defaultdict(list)
    fail_initiators: dict[str, list[str]] = defaultdict(list)
    fail_codes: dict[str, list[str]] = defaultdict(list)

    for s in all_sessions:
        cid = s.get("chargerId", "unknown")
        total_by_charger[cid] += 1

    for s in failed_sessions:
        cid = s.get("chargerId", "unknown")
        fail_by_charger[cid] += 1
        h = hour_of_day(s.get("createTime", ""))
        if h is not None:
            fail_hours[cid].append(h)
        via = s.get("startedVia", "UNKNOWN")
        fail_initiators[cid].append(via)
        code = s.get("failureCode", "UNKNOWN")
        fail_codes[cid].append(code)

    all_charger_ids = set(total_by_charger) | set(fail_by_charger)

    # --- Summary table ---
    print("\n" + "=" * 70)
    print(f"  CHARGELAB FAILURE REPORT — Last {days} days")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} local")
    print("=" * 70)

    print(f"\nTotal sessions analysed : {len(all_sessions)}")
    print(f"Failed sessions         : {len(failed_sessions)}")
    print(f"Overall failure rate    : {failure_rate(len(failed_sessions), len(all_sessions))}")

    # Per-charger table
    rows = []
    for cid in sorted(all_charger_ids):
        name = charger_names.get(cid, cid[:8])
        total = total_by_charger[cid]
        fails = fail_by_charger[cid]
        rate = failure_rate(fails, total)
        zero = len(zero_energy.get(cid, []))
        flag = "⚠️ " if fails / max(total, 1) > 0.10 or zero > 0 else "  "
        rows.append([flag + name, total, fails, rate, zero])

    print("\n" + tabulate(
        rows,
        headers=["Charger", "Total Sessions", "Failures", "Failure Rate", "Zero-Energy Intervals"],
        tablefmt="rounded_outline",
    ))

    # Per-charger failure details
    if failed_sessions:
        print("\n--- Failure Details by Charger ---\n")
        for cid in sorted(fail_by_charger):
            name = charger_names.get(cid, cid[:8])
            hours = fail_hours[cid]
            initiators = fail_initiators[cid]

            # Peak failure hour
            if hours:
                from collections import Counter
                peak_hour = Counter(hours).most_common(1)[0][0]
                peak_str = f"{peak_hour:02d}:00–{peak_hour+1:02d}:00 local"
            else:
                peak_str = "N/A"

            initiator_summary = ", ".join(
                f"{k}×{v}" for k, v in
                sorted(
                    defaultdict(int, {i: initiators.count(i) for i in set(initiators)}).items(),
                    key=lambda x: -x[1]
                )
            )

            print(f"  {name}")
            print(f"    Failures        : {fail_by_charger[cid]}")
            print(f"    Peak fail hour  : {peak_str}")
            print(f"    Initiated via   : {initiator_summary}")
            codes = fail_codes[cid]
            from collections import Counter
            code_summary = ", ".join(f"{k} ×{v}" for k, v in Counter(codes).most_common())
            if codes:
                print(f"    Failure codes   : {code_summary}")
            ze = zero_energy.get(cid, [])
            if ze:
                print(f"    ⚠️  Mid-session power drops in {len(ze)} session(s) (see CSV)")
            print()

    # Zero-energy detail
    if zero_energy:
        print("--- Mid-Session Zero-Energy Intervals (from CSV) ---\n")
        print("  These sessions had intervals with 0 kWh mid-session,")
        print("  suggesting the charger stopped delivering power without ending the session.\n")
        for cid, sids in zero_energy.items():
            name = charger_names.get(cid, cid[:8])
            for sid in sids:
                print(f"  {name}  Session: {sid}")
        print()

    print("=" * 70)
    print("  Tip: For deeper diagnosis, ask ChargeLab for expanded failureCodes")
    print("  and per-interval telemetry via the API.")
    print("=" * 70 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChargeLab failure diagnosis")
    parser.add_argument("--days", type=int, default=30, help="Look-back window in days (default: 30)")
    parser.add_argument("--csv", type=str, default="data", help="Directory containing CSV exports (default: data/)")
    args = parser.parse_args()

    build_report(days=args.days, csv_dir=args.csv)
