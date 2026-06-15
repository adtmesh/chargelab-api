"""
ChargeLab Failure Diagnosis Dashboard
--------------------------------------
Run with:
    streamlit run scripts/dashboard.py
"""

import io
import os
import csv
import glob
import sys
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Failure code explanations
# ---------------------------------------------------------------------------

FAILURE_EXPLANATIONS = {
    "FAILED_TO_RECEIVE_REMOTE_START_TRANSACTION_RESPONSE": {
        "short": "Charger didn't respond to start command",
        "detail": (
            "ChargeLab sent a remote command to begin charging, but the charger never "
            "sent back a confirmation. This usually means the charger lost its internet "
            "connection (cellular or Wi-Fi) right at the moment the command was sent, or "
            "the charger's internal software was temporarily unresponsive. "
            "The car was never actually told to start charging. "
            "If this happens repeatedly on the same charger, it likely has a weak signal "
            "or a modem that needs a reboot or replacement."
        ),
        "action": "Check the charger's cellular signal strength. Try power-cycling the charger. If it keeps happening, contact ChargeLab with these session IDs.",
    },
    "TIMED_OUT_WAITING_FOR_CHARGE_HANDLER_RESPONSE": {
        "short": "ChargeLab's system timed out internally",
        "detail": (
            "ChargeLab's own servers started processing the charge request but took too "
            "long to complete it and gave up. This is typically a problem on ChargeLab's "
            "side — a temporary server slowdown or internal queue backup — rather than "
            "anything wrong with the charger or the car. "
            "The charger itself may have been perfectly ready to charge."
        ),
        "action": "Report this session ID to ChargeLab support. If it happens more than once in a short window, ask them to check their system logs for that time period.",
    },
    "STOPPED_BEFORE_TX": {
        "short": "Session ended before charging started",
        "detail": (
            "A charging session was created but stopped before the car actually began "
            "receiving power. This can happen for three reasons: (1) someone issued a "
            "stop command via the app or API before the car connected, (2) the driver "
            "plugged in too slowly and the session timed out waiting for a connection, "
            "or (3) the charger ran into an unspecified issue preventing it from starting. "
            "No energy was delivered and the driver was not charged."
        ),
        "action": "Check whether a stop command was issued around the same time. If not, inspect the charger for physical issues and ask ChargeLab for more detail on what prevented startup.",
    },
    "UNKNOWN": {
        "short": "Unknown failure",
        "detail": (
            "ChargeLab reported a failure but did not provide a specific reason code. "
            "This may mean the failure occurred in an unexpected way that the system "
            "didn't categorise, or the failure code is new and not yet documented."
        ),
        "action": "Contact ChargeLab support with this session ID and ask for the raw OCPP error logs.",
    },
}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

BASE_URL = "https://api.chargelab.io/core/v1"
# Streamlit Community Cloud stores secrets in st.secrets;
# fall back to .env for local development.
try:
    API_KEY = st.secrets.get("CHARGELAB_API_KEY") or os.getenv("CHARGELAB_API_KEY")
except Exception:
    API_KEY = os.getenv("CHARGELAB_API_KEY")

st.set_page_config(
    page_title="ChargeLab Diagnostics",
    page_icon="⚡",
    layout="wide",
)

if not API_KEY:
    st.error("CHARGELAB_API_KEY not set. Add it to your .env file.")
    st.stop()

HEADERS = {"Authorization": f"x-auth {API_KEY}"}

# ---------------------------------------------------------------------------
# API helpers (cached so we don't re-fetch on every widget interaction)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)  # cache for 5 minutes
def fetch_chargers() -> dict:
    resp = requests.get(f"{BASE_URL}/chargers", headers=HEADERS,
                        params={"role": "COMPANY", "limit": 1000}, timeout=30)
    resp.raise_for_status()
    return {c["chargerId"]: c["name"] for c in resp.json().get("entities", [])}


@st.cache_data(ttl=300)
def fetch_all_sessions(since_iso: str) -> list:
    results, offset = [], 0
    while True:
        resp = requests.get(f"{BASE_URL}/sessions", headers=HEADERS, params={
            "role": "COMPANY",
            "filter_ge[createTime]": since_iso,
            "offset": offset,
            "limit": 1000,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        entities = data.get("entities", [])
        results.extend(entities)
        if offset + 1000 >= data.get("totalCount", 0):
            break
        offset += 1000
    return results


@st.cache_data(ttl=300)
def fetch_charger_live() -> list:
    resp = requests.get(f"{BASE_URL}/chargers", headers=HEADERS,
                        params={"role": "COMPANY", "limit": 1000}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("entities", [])

# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _collect_csv_rows(file_like) -> list[dict]:
    """Read all rows from a single CSV file-like object."""
    return list(csv.DictReader(file_like))


def _analyse_rows(all_rows: list[dict]) -> dict:
    """
    Core analysis: given a flat list of interval rows from one or more CSVs,
    return {chargerId: [detail_dict, ...]} for genuine mid-session power drops.
    """
    rows_by_session: dict[str, list] = defaultdict(list)
    charger_by_session: dict[str, str] = {}

    for row in all_rows:
        sid = row.get("Session ID", "").strip()
        if not sid:
            continue
        rows_by_session[sid].append(row)
        charger_by_session[sid] = row.get("Charger device ID", "").strip()

    issues: dict[str, list[dict]] = defaultdict(list)

    for sid, intervals in rows_by_session.items():
        if len(intervals) < 2:
            continue
        try:
            intervals.sort(key=lambda r: r["Interval start date/time (YYYY-MM-DD hh:mm:ss) (UTC)"])
        except KeyError:
            continue

        first = intervals[0]
        try:
            total_energy = float(first.get("Session energy provided (kWh)", "0") or 0)
            session_duration_min = float(first.get("Session duration (min)", "0") or 0)
            session_idle_min = float(first.get("Session idle duration (min)", "0") or 0)
            session_peak_kw = float(first.get("Session peak power (kW)", "0") or 0)
        except ValueError:
            continue

        if total_energy < 0.5:
            continue

        midpoint = len(intervals) // 2
        cumulative = 0.0

        for i, interval in enumerate(intervals[:-1]):
            try:
                energy = float(interval.get("Interval energy provided (kWh)", "0") or 0)
                interval_idle = float(interval.get("Interval idle duration (min)", "0") or 0)
                interval_peak = float(interval.get("Rolling 15-minute peak power (kW)", "0") or 0)
            except ValueError:
                continue

            if i >= midpoint:
                break

            if energy == 0.0 and cumulative < 20.0:
                cid = charger_by_session[sid]
                detail = {
                    "_cid": cid,
                    "Session ID": sid,
                    "Session Start (local)": first.get("Session start date/time (YYYY-MM-DD hh:mm:ss) (local)", "").strip(),
                    "Session End (local)": first.get("Session end date/time (YYYY-MM-DD hh:mm:ss) (local)", "").strip(),
                    "Duration (min)": f"{session_duration_min:.0f}",
                    "Total Energy (kWh)": f"{total_energy:.2f}",
                    "Energy Before Dropout (kWh)": f"{cumulative:.2f}",
                    "% Delivered Before Dropout": f"{cumulative / max(total_energy, 0.001) * 100:.0f}%",
                    "Dropout at Interval #": f"{i + 1} of {len(intervals)}",
                    "Dropout Time (local)": interval.get("Interval start date/time (YYYY-MM-DD hh:mm:ss) (local)", "").strip(),
                    "Session Peak Power (kW)": f"{session_peak_kw:.1f}",
                    "Idle Before Dropout (min)": f"{interval_idle:.1f}",
                    "Peak kW in Dropout Interval": f"{interval_peak:.1f}",
                    "Session Idle Total (min)": f"{session_idle_min:.1f}",
                }
                issues[cid].append(detail)
                break

            cumulative += energy

    return dict(issues)


@st.cache_data
def load_zero_energy_from_disk(csv_dir: str) -> dict:
    """Load and analyse all CSVs found in csv_dir."""
    all_rows: list[dict] = []
    for path in glob.glob(os.path.join(csv_dir, "*.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            all_rows.extend(_collect_csv_rows(f))
    return _analyse_rows(all_rows)


def load_zero_energy_from_uploads(uploaded_files) -> dict:
    """Load and analyse CSVs uploaded via st.file_uploader."""
    all_rows: list[dict] = []
    for f in uploaded_files:
        text = io.StringIO(f.read().decode("utf-8"))
        all_rows.extend(_collect_csv_rows(text))
    return _analyse_rows(all_rows)

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_local_hour(iso_str: str) -> int | None:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.split("[")[0]).hour
    except Exception:
        return None


def parse_dt(iso_str: str) -> datetime | None:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.split("[")[0])
    except Exception:
        return None

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("⚡ ChargeLab Diagnostics")
st.caption("Pikes Peak Plaza MFH · Colorado Springs, CO")

# Sidebar controls
with st.sidebar:
    st.header("Settings")
    days = st.slider("Look-back window (days)", 7, 90, 30)
    if st.button("🔄 Refresh data"):
        st.cache_data.clear()
    st.divider()
    st.subheader("CSV Export")
    st.caption(
        "Upload a ChargeLab interval export to analyse mid-session power drops. "
        "If nothing is uploaded, the dashboard reads from the local `data/` folder."
    )
    uploaded_csvs = st.file_uploader(
        "Drag & drop ChargeLab CSV export(s)",
        type="csv",
        accept_multiple_files=True,
        help="Download from ChargeLab dashboard → Reports → Session export with 15-min intervals",
    )
    st.divider()
    st.caption(f"Data cached for 5 min. Last load: {datetime.now().strftime('%H:%M:%S')}")

since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------------------------------------------------------------------------
# Live charger status
# ---------------------------------------------------------------------------

st.subheader("Live Charger Status")

with st.spinner("Fetching live charger status..."):
    chargers_live = fetch_charger_live()

status_cols = st.columns(len(chargers_live))
STATUS_COLOR = {
    "AVAILABLE": "🟢",
    "SESSION": "🔵",
    "UNAVAILABLE": "🔴",
    "UNKNOWN": "⚫",
}

for col, charger in zip(status_cols, sorted(chargers_live, key=lambda c: c["name"])):
    port = charger.get("ports", [{}])[0]
    status = port.get("status", "UNKNOWN")
    icon = STATUS_COLOR.get(status, "⚫")
    maintenance = " 🔧" if charger.get("maintenanceFlag") else ""
    with col:
        st.metric(
            label=f"{icon} {charger['name']}{maintenance}",
            value=status,
            delta=f"{port.get('maxPowerKilowatts', '?')} kW max",
            delta_color="off",
        )

st.divider()

# ---------------------------------------------------------------------------
# Fetch sessions
# ---------------------------------------------------------------------------

with st.spinner(f"Fetching sessions for the last {days} days..."):
    all_sessions = fetch_all_sessions(since)
    charger_names = fetch_chargers()
    if uploaded_csvs:
        zero_energy = load_zero_energy_from_uploads(uploaded_csvs)
    else:
        zero_energy = load_zero_energy_from_disk(str(ROOT / "data"))

failed = [s for s in all_sessions if s.get("status") == "FAILED"]
total_count = len(all_sessions)
fail_count = len(failed)

# ---------------------------------------------------------------------------
# Top-level metrics
# ---------------------------------------------------------------------------

st.subheader(f"Session Summary — Last {days} Days")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Total Sessions", total_count)
m2.metric("Failed Sessions", fail_count, delta=f"{fail_count/max(total_count,1)*100:.1f}% rate",
          delta_color="inverse")
m3.metric("Successful Sessions", sum(1 for s in all_sessions if s.get("status") == "SUCCESSFUL"))
m4.metric("Chargers with Zero-Energy Intervals", len(zero_energy))

st.divider()

# ---------------------------------------------------------------------------
# Per-charger failure table
# ---------------------------------------------------------------------------

st.subheader("Per-Charger Breakdown")

total_by = defaultdict(int)
fail_by = defaultdict(int)
codes_by = defaultdict(list)
hours_by = defaultdict(list)

for s in all_sessions:
    total_by[s["chargerId"]] += 1
for s in failed:
    cid = s["chargerId"]
    fail_by[cid] += 1
    codes_by[cid].append(s.get("failureCode", "UNKNOWN"))
    h = parse_local_hour(s.get("createTime", ""))
    if h is not None:
        hours_by[cid].append(h)

table_rows = []
for cid in sorted(set(total_by) | set(fail_by), key=lambda c: charger_names.get(c, c)):
    name = charger_names.get(cid, cid[:8])
    total = total_by[cid]
    fails = fail_by[cid]
    rate = fails / max(total, 1) * 100
    zero = len(zero_energy.get(cid, []))  # list of detail dicts
    peak_h = Counter(hours_by[cid]).most_common(1)[0][0] if hours_by[cid] else None
    peak_str = f"{peak_h:02d}:00–{peak_h+1:02d}:00" if peak_h is not None else "—"
    top_code = Counter(codes_by[cid]).most_common(1)[0][0] if codes_by[cid] else "—"
    flag = "⚠️" if rate > 10 or zero > 0 else "✅"
    table_rows.append({
        "": flag,
        "Charger": name,
        "Total Sessions": total,
        "Failures": fails,
        "Failure Rate": f"{rate:.1f}%",
        "Peak Fail Hour (local)": peak_str,
        "Top Failure Code": top_code,
        "Zero-Energy Intervals": zero,
    })

st.dataframe(table_rows, use_container_width=True, hide_index=True)

st.divider()

# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

col_left, col_right = st.columns(2)

# Failure rate bar chart
with col_left:
    st.subheader("Failure Rate by Charger")
    names = [r["Charger"] for r in table_rows]
    rates = [float(r["Failure Rate"].replace("%", "")) for r in table_rows]
    colors = ["#ef4444" if r > 10 else "#22c55e" for r in rates]
    fig = go.Figure(go.Bar(x=names, y=rates, marker_color=colors, text=[f"{r:.1f}%" for r in rates],
                           textposition="outside"))
    fig.update_layout(yaxis_title="Failure Rate (%)", yaxis_range=[0, max(rates or [0]) * 1.3 + 5],
                      margin=dict(t=20), height=300, showlegend=False)
    st.plotly_chart(fig, use_container_width=True)

# Failures by hour of day
with col_right:
    st.subheader("Failures by Hour of Day (local)")
    all_hours = [h for hours in hours_by.values() for h in hours]
    if all_hours:
        hour_counts = Counter(all_hours)
        hours_range = list(range(24))
        counts = [hour_counts.get(h, 0) for h in hours_range]
        fig2 = go.Figure(go.Bar(
            x=[f"{h:02d}:00" for h in hours_range],
            y=counts,
            marker_color="#f97316",
        ))
        fig2.update_layout(xaxis_title="Hour", yaxis_title="Failures",
                           margin=dict(t=20), height=300)
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("No failures with timestamp data in this window.")

st.divider()

# ---------------------------------------------------------------------------
# Failed session log
# ---------------------------------------------------------------------------

st.subheader("Failed Session Log")

if not failed:
    st.success(f"No failed sessions in the last {days} days.")
else:
    failed_sorted = sorted(failed, key=lambda s: s.get("createTime", ""), reverse=True)

    # Group by charger so explanations are shown per-charger
    by_charger: dict[str, list] = defaultdict(list)
    for s in failed_sorted:
        by_charger[charger_names.get(s["chargerId"], s["chargerId"][:8])].append(s)

    for charger_name, sessions in sorted(by_charger.items()):
        st.markdown(f"#### {charger_name}")

        # Show the explanation once per unique failure code on this charger
        codes_seen = set()
        for s in sessions:
            code = s.get("failureCode") or "UNKNOWN"
            if code not in codes_seen:
                codes_seen.add(code)
                info = FAILURE_EXPLANATIONS.get(code, FAILURE_EXPLANATIONS["UNKNOWN"])
                with st.expander(f"ℹ️ What does **{info['short']}** mean?", expanded=True):
                    st.markdown(f"**What happened:** {info['detail']}")
                    st.markdown(f"**What to do:** {info['action']}")

        # Session table for this charger
        log_rows = []
        for s in sessions:
            dt = parse_dt(s.get("createTime", ""))
            code = s.get("failureCode") or "UNKNOWN"
            info = FAILURE_EXPLANATIONS.get(code, FAILURE_EXPLANATIONS["UNKNOWN"])
            log_rows.append({
                "Time (local)": dt.strftime("%Y-%m-%d %H:%M") if dt else "—",
                "Started Via": s.get("startedVia", "—"),
                "Failure": info["short"],
                "Raw Code": code,
                "Session ID": s["sessionId"],
            })
        st.dataframe(log_rows, use_container_width=True, hide_index=True)
        st.divider()

st.divider()

# ---------------------------------------------------------------------------
# Zero-energy interval detail
# ---------------------------------------------------------------------------

st.subheader("Mid-Session Zero-Energy Intervals (from CSV exports)")
st.caption(
    "Sessions where the charger stopped delivering power in the first half of the session "
    "before the battery was plausibly full. Overnight idle (battery full, car still plugged in) "
    "is excluded."
)

if not zero_energy:
    st.info("No CSV data found in data/ — download a report from the ChargeLab dashboard and place it in the data/ folder.")
else:
    for cid, details in sorted(zero_energy.items(), key=lambda x: charger_names.get(x[0], x[0])):
        charger_label = charger_names.get(cid, cid[:8])
        st.markdown(f"#### {charger_label} — {len(details)} session(s) with mid-session dropout")

        # Header row
        h = st.columns([2, 1.6, 1.6, 0.9, 1.1, 1.1, 1.1, 1.0, 1.0, 2.5])
        for col, label in zip(h, [
            "Session ID", "Start (local)", "End (local)", "Duration\n(min)",
            "Total\nEnergy (kWh)", "Energy Before\nDropout (kWh)", "% Delivered\nBefore Dropout",
            "Dropout\nInterval #", "Peak kW at\nDropout", "Interpretation",
        ]):
            col.markdown(f"<small><b>{label}</b></small>", unsafe_allow_html=True)

        st.markdown("<hr style='margin:4px 0'>", unsafe_allow_html=True)

        for d in details:
            pct_str = d.get("% Delivered Before Dropout", "0%")
            pct = int(pct_str.replace("%", "") or 0)
            idle = float(d.get("Idle Before Dropout (min)", "0") or 0)
            peak_dropout = float(d.get("Peak kW in Dropout Interval", "0") or 0)

            hints = []
            if pct < 10:
                hints.append("⚡ Dropped out very early (<10% delivered) — not a full-battery idle.")
            if idle > 5:
                hints.append(f"💤 Car was already idle {idle:.0f} min before dropout — vehicle may have paused charging.")
            if peak_dropout == 0.0:
                hints.append("📡 Charger stopped completely (0 kW) — not a throttle, a full stop.")
            if not hints:
                hints.append("ℹ️ No strong signal — review interval timing manually.")

            cols = st.columns([2, 1.6, 1.6, 0.9, 1.1, 1.1, 1.1, 1.0, 1.0, 2.5])
            cols[0].caption(d.get("Session ID", "")[:18] + "…")
            cols[1].caption(d.get("Session Start (local)", "—"))
            cols[2].caption(d.get("Session End (local)", "—"))
            cols[3].caption(d.get("Duration (min)", "—"))
            cols[4].caption(d.get("Total Energy (kWh)", "—"))
            cols[5].caption(d.get("Energy Before Dropout (kWh)", "—"))
            cols[6].caption(pct_str)
            cols[7].caption(d.get("Dropout at Interval #", "—"))
            cols[8].caption(d.get("Peak kW in Dropout Interval", "—"))
            cols[9].caption("  \n".join(hints))

            st.markdown("<hr style='margin:2px 0; opacity:0.2'>", unsafe_allow_html=True)

        st.divider()
