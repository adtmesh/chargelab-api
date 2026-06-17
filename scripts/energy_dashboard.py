"""
EV Energy Cost Dashboard
-------------------------
Visualizes CSU electricity bills vs ChargeLab EV charger usage to estimate
the share of the electricity bill attributable to EV charging, and calculates
break-even / target pricing per kWh session.

Run with:
    streamlit run scripts/energy_dashboard.py
"""

import io
import os
import csv
import glob
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
CSU_DIR = DATA_DIR / "CSU utility bills"
BILLS_CSV = DATA_DIR / "csu_bills_summary.csv"
MONTHLY_USAGE_CSV = CSU_DIR / "Monthly_Usage_2025-26.csv"
CHARGELAB_CSV_DIR = DATA_DIR

CMS_SESSION_FEE = 0.30   # $ per session
CMS_TRANSACTION_PCT = 0.029  # 2.9% of revenue

st.set_page_config(
    page_title="EV Energy Cost Dashboard",
    page_icon="⚡",
    layout="wide",
)

# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

@st.cache_data
def load_bills() -> list[dict]:
    """Load extracted CSU bill summary CSV."""
    if not BILLS_CSV.exists():
        return []
    with open(BILLS_CSV, newline="") as f:
        return list(csv.DictReader(f))


@st.cache_data
def load_monthly_usage() -> list[dict]:
    """Load CSU monthly usage CSV (building-level kWh + peak kW)."""
    rows = []
    if not MONTHLY_USAGE_CSV.exists():
        return rows
    with open(MONTHLY_USAGE_CSV, newline="") as f:
        reader = csv.reader(f)
        header = None
        for row in reader:
            if not row or row[0].startswith("Download") or row[0].startswith("ServiceType"):
                continue
            if "Month" in row:
                header = row
                continue
            if header and len(row) >= 6:
                # Forward-fill blank account/meter
                month = row[2].strip()
                if not month:
                    continue
                try:
                    rows.append({
                        "month": month,
                        "days": int(row[3]) if row[3].strip() else 0,
                        "kwh": float(row[4]) if row[4].strip() else 0.0,
                        "actual_kw": float(row[5]) if row[5].strip() else 0.0,
                    })
                except ValueError:
                    continue
    return rows


@st.cache_data
def load_chargelab_sessions(csv_dir: str) -> list[dict]:
    """Load ChargeLab session CSV exports, dedup by Session ID."""
    seen = set()
    sessions = []
    for path in glob.glob(os.path.join(csv_dir, "*.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sid = row.get("Session ID", "").strip()
                if not sid or sid in seen:
                    continue
                seen.add(sid)
                sessions.append(row)
    return sessions


def load_chargelab_from_uploads(uploaded_files) -> list[dict]:
    seen = set()
    sessions = []
    for f in uploaded_files:
        text = io.StringIO(f.read().decode("utf-8"))
        for row in csv.DictReader(text):
            sid = row.get("Session ID", "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            sessions.append(row)
    return sessions


def parse_month(s: str) -> str | None:
    """Parse 'MMM YYYY' → 'YYYY-MM' for sorting."""
    try:
        return datetime.strptime(s.strip(), "%b %Y").strftime("%Y-%m")
    except ValueError:
        return None


def session_month(row: dict) -> str | None:
    """Extract YYYY-MM from a ChargeLab session row."""
    ts = row.get("Session start date/time (YYYY-MM-DD hh:mm:ss) (local)", "").strip()
    if not ts:
        return None
    try:
        return datetime.strptime(ts[:7], "%Y-%m").strftime("%Y-%m")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Derived data
# ---------------------------------------------------------------------------

def build_monthly(bills, monthly_usage, sessions) -> list[dict]:
    """
    Join bill data, building usage, and EV session kWh by month.
    Returns a list of dicts sorted by month.
    """
    # Bills keyed by period_start month (YYYY-MM)
    bill_by_month: dict[str, dict] = {}
    for b in bills:
        ps = b.get("period_start", "")
        if not ps:
            continue
        try:
            key = datetime.strptime(ps, "%m/%d/%y").strftime("%Y-%m")
        except ValueError:
            continue
        bill_by_month[key] = b

    # Building usage keyed by YYYY-MM
    usage_by_month: dict[str, dict] = {}
    for u in monthly_usage:
        key = parse_month(u["month"])
        if key:
            usage_by_month[key] = u

    # EV kWh per month
    ev_kwh_by_month: dict[str, float] = defaultdict(float)
    ev_sessions_by_month: dict[str, int] = defaultdict(int)
    for s in sessions:
        m = session_month(s)
        if not m:
            continue
        try:
            kwh = float(s.get("Session energy provided (kWh)", "0") or 0)
        except ValueError:
            kwh = 0.0
        ev_kwh_by_month[m] += kwh
        ev_sessions_by_month[m] += 1

    all_months = sorted(set(bill_by_month) | set(usage_by_month) | set(ev_kwh_by_month))

    result = []
    for m in all_months:
        b = bill_by_month.get(m, {})
        u = usage_by_month.get(m, {})
        ev_kwh = ev_kwh_by_month.get(m, 0.0)
        sessions_count = ev_sessions_by_month.get(m, 0)

        building_kwh = u.get("kwh", 0.0) or float(b.get("total_kwh", 0) or 0)
        peak_kw = u.get("actual_kw", 0.0)
        electric_total = float(b.get("electric_total", 0) or 0)
        cost_per_kwh = float(b.get("cost_per_kwh", 0) or 0)

        ev_share_pct = (ev_kwh / building_kwh * 100) if building_kwh > 0 else 0.0
        ev_cost = ev_kwh * cost_per_kwh if cost_per_kwh > 0 else 0.0

        result.append({
            "month": m,
            "label": datetime.strptime(m, "%Y-%m").strftime("%b %Y"),
            "building_kwh": building_kwh,
            "ev_kwh": ev_kwh,
            "other_kwh": max(building_kwh - ev_kwh, 0),
            "ev_share_pct": ev_share_pct,
            "peak_kw": peak_kw,
            "electric_total": electric_total,
            "cost_per_kwh": cost_per_kwh,
            "ev_cost": ev_cost,
            "sessions": sessions_count,
            "ev_cost_per_session": ev_cost / sessions_count if sessions_count > 0 else 0.0,
        })

    return result


# ---------------------------------------------------------------------------
# Pricing calculator
# ---------------------------------------------------------------------------

def breakeven_price(electricity_cost_per_kwh: float, avg_kwh_per_session: float) -> float:
    """Price per kWh at which revenue exactly covers electricity + CMS fees."""
    if avg_kwh_per_session <= 0:
        return 0.0
    cost = electricity_cost_per_kwh * avg_kwh_per_session + CMS_SESSION_FEE
    return cost / (avg_kwh_per_session * (1 - CMS_TRANSACTION_PCT))


def margin_at_price(price_per_kwh: float, electricity_cost_per_kwh: float, avg_kwh_per_session: float) -> float:
    """Net margin per session at a given price."""
    revenue = price_per_kwh * avg_kwh_per_session
    net_revenue = revenue * (1 - CMS_TRANSACTION_PCT)
    electricity_cost = electricity_cost_per_kwh * avg_kwh_per_session
    return net_revenue - electricity_cost - CMS_SESSION_FEE


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

st.title("⚡ EV Energy Cost Dashboard")
st.caption("Plaza at Pikes Peak · Colorado Springs Utilities · ChargeLab")

# Sidebar
with st.sidebar:
    st.header("Settings")
    st.subheader("ChargeLab CSV Export")
    st.caption("Upload session exports to populate EV kWh data. Falls back to local data/ folder if nothing uploaded.")
    uploaded_csvs = st.file_uploader(
        "Drag & drop ChargeLab CSV(s)",
        type="csv",
        accept_multiple_files=True,
    )
    st.divider()
    st.subheader("CMS Fees")
    session_fee = st.number_input("Per-session fee ($)", value=CMS_SESSION_FEE, step=0.01, format="%.2f")
    transaction_pct = st.number_input("Transaction fee (%)", value=CMS_TRANSACTION_PCT * 100, step=0.1, format="%.1f") / 100
    st.divider()
    if st.button("🔄 Refresh"):
        st.cache_data.clear()
        st.rerun()

# Load data
bills = load_bills()
monthly_usage = load_monthly_usage()
sessions = load_chargelab_from_uploads(uploaded_csvs) if uploaded_csvs else load_chargelab_sessions(str(CHARGELAB_CSV_DIR))
monthly = build_monthly(bills, monthly_usage, sessions)

if not monthly:
    st.warning("No data found. Run `python scripts/extract_csu_bills.py` first and ensure CSU utility bills are in `data/CSU utility bills/`.")
    st.stop()

months_with_data = [m for m in monthly if m["building_kwh"] > 0]
labels = [m["label"] for m in months_with_data]

# ---------------------------------------------------------------------------
# Top metrics
# ---------------------------------------------------------------------------

avg_cost_per_kwh = sum(m["cost_per_kwh"] for m in months_with_data if m["cost_per_kwh"] > 0) / max(sum(1 for m in months_with_data if m["cost_per_kwh"] > 0), 1)
total_ev_kwh = sum(m["ev_kwh"] for m in monthly)
total_ev_cost = sum(m["ev_cost"] for m in monthly)
total_sessions = sum(m["sessions"] for m in monthly)
avg_ev_share = sum(m["ev_share_pct"] for m in months_with_data if m["ev_kwh"] > 0) / max(sum(1 for m in months_with_data if m["ev_kwh"] > 0), 1)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Avg $/kWh (building)", f"${avg_cost_per_kwh:.4f}")
c2.metric("Total EV kWh", f"{total_ev_kwh:,.0f}")
c3.metric("Est. EV electricity cost", f"${total_ev_cost:,.2f}")
c4.metric("EV share of building load", f"{avg_ev_share:.1f}%")
c5.metric("Total sessions", f"{total_sessions:,}")

st.divider()

# ---------------------------------------------------------------------------
# Section 1: Monthly kWh — building vs EV
# ---------------------------------------------------------------------------

st.subheader("Monthly Energy: Building vs EV Chargers")

fig = make_subplots(specs=[[{"secondary_y": True}]])

fig.add_trace(go.Bar(
    name="Other building load",
    x=labels,
    y=[m["other_kwh"] for m in months_with_data],
    marker_color="#94a3b8",
), secondary_y=False)

fig.add_trace(go.Bar(
    name="EV charger load",
    x=labels,
    y=[m["ev_kwh"] for m in months_with_data],
    marker_color="#22c55e",
), secondary_y=False)

fig.add_trace(go.Scatter(
    name="EV share %",
    x=labels,
    y=[m["ev_share_pct"] for m in months_with_data],
    mode="lines+markers",
    line=dict(color="#f59e0b", width=2),
    marker=dict(size=6),
), secondary_y=True)

fig.update_layout(
    barmode="stack",
    height=400,
    legend=dict(orientation="h", yanchor="bottom", y=1.02),
    margin=dict(t=40),
)
fig.update_yaxes(title_text="kWh", secondary_y=False)
fig.update_yaxes(title_text="EV share (%)", secondary_y=True, range=[0, 30])

st.plotly_chart(fig, use_container_width=True)

# ---------------------------------------------------------------------------
# Section 2: Cost attribution
# ---------------------------------------------------------------------------

st.subheader("Estimated EV Electricity Cost Attribution")
st.caption("EV cost = EV kWh × effective $/kWh for that billing period. Does not yet include proportional demand charge.")

col1, col2 = st.columns(2)

with col1:
    fig2 = go.Figure()
    fig2.add_trace(go.Bar(
        name="EV electricity cost",
        x=labels,
        y=[m["ev_cost"] for m in months_with_data],
        marker_color="#22c55e",
        text=[f"${m['ev_cost']:.0f}" for m in months_with_data],
        textposition="outside",
    ))
    fig2.update_layout(
        title="Monthly EV Electricity Cost ($)",
        height=350,
        margin=dict(t=50, b=20),
        yaxis_title="$",
        showlegend=False,
    )
    st.plotly_chart(fig2, use_container_width=True)

with col2:
    fig3 = go.Figure()
    fig3.add_trace(go.Scatter(
        name="$/kWh",
        x=labels,
        y=[m["cost_per_kwh"] for m in months_with_data],
        mode="lines+markers",
        line=dict(color="#6366f1", width=2),
        fill="tozeroy",
        fillcolor="rgba(99,102,241,0.1)",
    ))
    fig3.update_layout(
        title="Effective $/kWh from CSU Bill",
        height=350,
        margin=dict(t=50, b=20),
        yaxis_title="$/kWh",
        yaxis_tickformat="$.4f",
        showlegend=False,
    )
    st.plotly_chart(fig3, use_container_width=True)

# ---------------------------------------------------------------------------
# Section 3: Demand monitor
# ---------------------------------------------------------------------------

st.subheader("Building Peak Demand (kW)")
st.caption("Actual measured peak demand from CSU meter. EV charger contribution is estimated from ChargeLab session peak power.")

months_with_demand = [m for m in months_with_data if m["peak_kw"] > 0]
if months_with_demand:
    fig4 = go.Figure()
    fig4.add_trace(go.Scatter(
        name="Building peak demand (kW)",
        x=[m["label"] for m in months_with_demand],
        y=[m["peak_kw"] for m in months_with_demand],
        mode="lines+markers",
        line=dict(color="#ef4444", width=2),
        marker=dict(size=7),
    ))
    fig4.update_layout(
        height=320,
        margin=dict(t=20, b=20),
        yaxis_title="kW",
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig4, use_container_width=True)
else:
    st.info("No demand data available from CSU monthly usage file.")

# ---------------------------------------------------------------------------
# Section 4: Monthly detail table
# ---------------------------------------------------------------------------

st.subheader("Monthly Summary Table")

table_rows = []
for m in months_with_data:
    table_rows.append({
        "Month": m["label"],
        "Building kWh": f"{m['building_kwh']:,.0f}",
        "EV kWh": f"{m['ev_kwh']:,.1f}",
        "EV Share": f"{m['ev_share_pct']:.1f}%",
        "Sessions": m["sessions"],
        "Avg kWh/session": f"{m['ev_kwh']/m['sessions']:.1f}" if m["sessions"] > 0 else "—",
        "$/kWh (bill)": f"${m['cost_per_kwh']:.4f}" if m["cost_per_kwh"] > 0 else "—",
        "Est. EV Cost": f"${m['ev_cost']:.2f}" if m["ev_cost"] > 0 else "—",
        "EV Cost/Session": f"${m['ev_cost_per_session']:.2f}" if m["ev_cost_per_session"] > 0 else "—",
        "Peak kW": f"{m['peak_kw']:.1f}" if m["peak_kw"] > 0 else "—",
    })

st.dataframe(table_rows, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Section 5: Pricing calculator
# ---------------------------------------------------------------------------

st.divider()
st.subheader("💰 Pricing Calculator")
st.caption(f"CMS fees: ${session_fee:.2f}/session + {transaction_pct*100:.1f}% of revenue")

# Use most recent month with both EV kWh and bill data for defaults
recent = next(
    (m for m in reversed(months_with_data) if m["ev_kwh"] > 0 and m["cost_per_kwh"] > 0),
    None
)
default_kwh_session = round(recent["ev_kwh"] / recent["sessions"], 1) if recent and recent["sessions"] > 0 else 20.0
default_cpp = recent["cost_per_kwh"] if recent else avg_cost_per_kwh

col_a, col_b, col_c = st.columns(3)

with col_a:
    st.markdown("**Your costs**")
    calc_cpp = st.number_input("Electricity cost ($/kWh)", value=round(default_cpp, 4), step=0.001, format="%.4f",
                                help="From the CSU bill — varies by month")
    calc_kwh = st.number_input("Avg kWh per session", value=default_kwh_session, step=0.5, format="%.1f",
                                help="From ChargeLab data for the selected period")

be_price = (calc_cpp * calc_kwh + session_fee) / (calc_kwh * (1 - transaction_pct))

with col_b:
    st.markdown("**Break-even & pricing**")
    target_margin = st.slider("Target margin per session ($)", 0.0, 5.0, 1.0, step=0.25)
    target_price = (calc_cpp * calc_kwh + session_fee + target_margin) / (calc_kwh * (1 - transaction_pct))

with col_c:
    st.markdown("**Results**")
    st.metric("Break-even price", f"${be_price:.3f}/kWh",
              help="Price at which revenue exactly covers electricity + CMS fees")
    st.metric("Price for target margin", f"${target_price:.3f}/kWh",
              delta=f"+${target_margin:.2f}/session margin")

    electricity_cost_session = calc_cpp * calc_kwh
    cms_cost_session = session_fee + target_price * calc_kwh * transaction_pct
    net_per_session = target_price * calc_kwh * (1 - transaction_pct) - electricity_cost_session - session_fee

    st.caption(f"At ${target_price:.3f}/kWh for {calc_kwh:.0f} kWh session:")
    st.caption(f"  Revenue: ${target_price * calc_kwh:.2f}")
    st.caption(f"  Electricity: −${electricity_cost_session:.2f}")
    st.caption(f"  CMS fees: −${cms_cost_session:.2f}")
    st.caption(f"  Net: **${net_per_session:.2f}/session**")

# Price sensitivity chart
st.markdown("**Price sensitivity**")
prices = [round(be_price * 0.5 + i * be_price * 0.05, 3) for i in range(21)]
margins = [margin_at_price(p, calc_cpp, calc_kwh) for p in prices]

fig5 = go.Figure()
fig5.add_trace(go.Scatter(
    x=prices, y=margins,
    mode="lines",
    line=dict(color="#6366f1", width=2),
    fill="tozeroy",
    fillcolor="rgba(99,102,241,0.1)",
))
fig5.add_hline(y=0, line_dash="dash", line_color="#ef4444", annotation_text="Break-even")
fig5.add_vline(x=be_price, line_dash="dot", line_color="#94a3b8", annotation_text=f"BE: ${be_price:.3f}")
fig5.add_vline(x=target_price, line_dash="dot", line_color="#22c55e", annotation_text=f"Target: ${target_price:.3f}")
fig5.update_layout(
    height=280,
    margin=dict(t=20, b=20),
    xaxis_title="Price ($/kWh)",
    yaxis_title="Net margin per session ($)",
    showlegend=False,
)
st.plotly_chart(fig5, use_container_width=True)
