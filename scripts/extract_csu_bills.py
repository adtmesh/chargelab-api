"""
CSU Bill Extractor
------------------
Extracts electric charge breakdowns from Colorado Springs Utilities PDF bills
and outputs a CSV summary for pricing analysis.

Usage:
    python scripts/extract_csu_bills.py

Output:
    data/csu_bills_summary.csv
"""

import re
import csv
import glob
from pathlib import Path
from dataclasses import dataclass, fields, asdict

import pdfplumber
import pytesseract
from pdf2image import convert_from_path
from tabulate import tabulate

BILLS_DIR = Path(__file__).parent.parent / "data" / "CSU utility bills" / "CSU_bills_PDF"
OUTPUT_CSV = Path(__file__).parent.parent / "data" / "csu_bills_summary.csv"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class BillRecord:
    filename: str
    statement_date: str
    period_start: str
    period_end: str
    billing_days: int
    rate_type: str

    # Usage
    on_peak_kwh: float
    off_peak_kwh: float
    total_kwh: float
    on_peak_demand_kw: float
    off_peak_demand_kw: float
    xof_excess_demand_kw: float

    # Charges (pre-tax)
    access_charge: float
    demand_charge_on_peak: float
    demand_charge_xof: float
    eca_on_peak: float
    eca_off_peak: float
    minimum_charge: float
    capacity_charge: float

    # Taxes
    city_sales_tax: float
    county_sales_tax: float
    state_sales_tax: float
    pprta_tax: float
    total_taxes: float

    # Totals
    electric_total: float

    # Derived
    cost_per_kwh: float       # electric_total / total_kwh


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dollar(text: str, pattern: str) -> float:
    """Extract a dollar amount following `pattern` in text."""
    m = re.search(pattern + r'.*?\$([0-9,]+\.[0-9]{2})', text)
    if m:
        return float(m.group(1).replace(',', ''))
    return 0.0


def _float(text: str, pattern: str) -> float:
    """Extract a plain number following `pattern` in text."""
    m = re.search(pattern + r'.*?([0-9,]+\.?[0-9]*)', text)
    if m:
        return float(m.group(1).replace(',', ''))
    return 0.0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _extract_text(pdf_path: Path) -> str:
    """Extract text via pdfplumber; fall back to Tesseract OCR for image-only PDFs."""
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    if text.strip():
        return text
    # Image-only PDF — run OCR
    images = convert_from_path(pdf_path, dpi=200)
    return "\n".join(pytesseract.image_to_string(img) for img in images)


def parse_bill(pdf_path: Path) -> BillRecord:
    text = _extract_text(pdf_path)

    # --- Statement date ---
    m = re.search(r'Statement Date:?\s*(\d{2}/\d{2}/\d{4})', text)
    statement_date = m.group(1) if m else ""

    # --- Service period ---
    m = re.search(r'Service Detail for:\s*(\d{2}/\d{2}/\d{2})\s*[-–]\s*(\d{2}/\d{2}/\d{2})\s*\((\d+)\s*Days\)', text)
    if m:
        period_start = m.group(1)
        period_end   = m.group(2)
        billing_days = int(m.group(3))
    else:
        period_start = period_end = ""
        billing_days = 0

    # --- Rate type ---
    if "Frozen Commercial" in text:
        rate_type = "Electric Frozen Commercial (ETL)"
    else:
        rate_type = "Electric Commercial (ETL)"

    # --- Demand (only present on ETL non-frozen bills) ---
    m = re.search(r'Total On Peak kW billed:\s*([0-9,]+\.?\d*)\s*kW', text)
    on_peak_demand_kw = float(m.group(1).replace(',', '')) if m else 0.0

    m = re.search(r'Total Off Peak kW:\s*([0-9,]+\.?\d*)\s*kW', text)
    off_peak_demand_kw = float(m.group(1).replace(',', '')) if m else 0.0

    m = re.search(r'Excess Off \(XOF\) Peak kW billed:\s*([0-9,]+\.?\d*)\s*kW', text)
    xof_excess_demand_kw = float(m.group(1).replace(',', '')) if m else 0.0

    # --- kWh ---
    # Some bills split ECA into two lines — sum all matches
    on_peak_kwh = sum(
        float(x.replace(',', ''))
        for x in re.findall(r'ECA On-?Peak:\s*([0-9,]+)\s*kWh', text)
    )
    off_peak_kwh = sum(
        float(x.replace(',', ''))
        for x in re.findall(r'ECA Off-?Peak:\s*([0-9,]+)\s*kWh', text)
    )

    # Total kWh — look for explicit total line first, fall back to sum
    m = re.search(r'Kilowatt Hours \(Total\):\s*([0-9,]+)\s*kWh', text)
    if m:
        total_kwh = float(m.group(1).replace(',', ''))
    else:
        # Frozen bills: parse from "Measured Quantity: NNN kWh x 100 Meter Constant: NNN kWh"
        m = re.search(r'Measured Quantity:.*?Meter Constant:\s*([0-9,]+)\s*kWh', text)
        total_kwh = float(m.group(1).replace(',', '')) if m else on_peak_kwh + off_peak_kwh

    # --- Charge line items ---
    # Access Charge
    m = re.search(r'Access Charge:\s*\d+ days x \$[0-9.]+:\s*\$([0-9,]+\.[0-9]{2})', text)
    access_charge = float(m.group(1).replace(',', '')) if m else 0.0

    # Demand Charge On-Peak
    m = re.search(r'Demand Charge: kW/On x \$[0-9.]+/Day:\s*\$([0-9,]+\.[0-9]{2})', text)
    demand_charge_on_peak = float(m.group(1).replace(',', '')) if m else 0.0

    # Demand Charge XOF
    m = re.search(r'Demand Charge: kW/XOF x \$[0-9.]+/Day:\s*\$([0-9,]+\.[0-9]{2})', text)
    demand_charge_xof = float(m.group(1).replace(',', '')) if m else 0.0

    # ECA On-Peak dollar — sum all lines
    eca_on_peak = sum(
        float(x.replace(',', ''))
        for x in re.findall(r'ECA On-?Peak:.*?:\s*\$([0-9,]+\.[0-9]{2})', text)
    )

    # ECA Off-Peak dollar — sum all lines
    eca_off_peak = sum(
        float(x.replace(',', ''))
        for x in re.findall(r'ECA Off-?Peak:.*?:\s*\$([0-9,]+\.[0-9]{2})', text)
    )

    # Minimum Charge (Frozen bills only)
    m = re.search(r'Minimum Charge:.*?:\s*\$([0-9,]+\.[0-9]{2})', text)
    minimum_charge = float(m.group(1).replace(',', '')) if m else 0.0

    # Capacity Charge
    m = re.search(r'Capacity Charge:.*?:\s*\$([0-9,]+\.[0-9]{2})', text)
    capacity_charge = float(m.group(1).replace(',', '')) if m else 0.0

    # Taxes — match lines like "City Sales Tax: 3.07% x $4,834.71: $148.43"
    m = re.search(r'City Sales Tax:.*?:\s*\$([0-9,]+\.[0-9]{2})', text)
    city_sales_tax = float(m.group(1).replace(',', '')) if m else 0.0

    m = re.search(r'County Sales Tax:.*?:\s*\$([0-9,]+\.[0-9]{2})', text)
    county_sales_tax = float(m.group(1).replace(',', '')) if m else 0.0

    m = re.search(r'State Sales Tax:.*?:\s*\$([0-9,]+\.[0-9]{2})', text)
    state_sales_tax = float(m.group(1).replace(',', '')) if m else 0.0

    m = re.search(r'PPRTA Tax:.*?:\s*\$([0-9,]+\.[0-9]{2})', text)
    pprta_tax = float(m.group(1).replace(',', '')) if m else 0.0

    total_taxes = city_sales_tax + county_sales_tax + state_sales_tax + pprta_tax

    # Electric Total — "Total: $X,XXX.XX" immediately after the electric charges block
    # Look for the first Total line that follows "Electric"
    m = re.search(
        r'Electric(?:.*?\n)+?.*?Total:\s*\$([0-9,]+\.[0-9]{2})',
        text, re.DOTALL
    )
    electric_total = float(m.group(1).replace(',', '')) if m else 0.0

    # Fallback: parse "Electric Total Charge ... $X,XXX.XX" from summary line
    if electric_total == 0.0:
        m = re.search(r'Electric Total Charge\s+\$([0-9,]+\.[0-9]{2})', text)
        electric_total = float(m.group(1).replace(',', '')) if m else 0.0

    cost_per_kwh = round(electric_total / total_kwh, 6) if total_kwh > 0 else 0.0

    return BillRecord(
        filename=pdf_path.name,
        statement_date=statement_date,
        period_start=period_start,
        period_end=period_end,
        billing_days=billing_days,
        rate_type=rate_type,
        on_peak_kwh=on_peak_kwh,
        off_peak_kwh=off_peak_kwh,
        total_kwh=total_kwh,
        on_peak_demand_kw=on_peak_demand_kw,
        off_peak_demand_kw=off_peak_demand_kw,
        xof_excess_demand_kw=xof_excess_demand_kw,
        access_charge=access_charge,
        demand_charge_on_peak=demand_charge_on_peak,
        demand_charge_xof=demand_charge_xof,
        eca_on_peak=eca_on_peak,
        eca_off_peak=eca_off_peak,
        minimum_charge=minimum_charge,
        capacity_charge=capacity_charge,
        city_sales_tax=city_sales_tax,
        county_sales_tax=county_sales_tax,
        state_sales_tax=state_sales_tax,
        pprta_tax=pprta_tax,
        total_taxes=total_taxes,
        electric_total=electric_total,
        cost_per_kwh=cost_per_kwh,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    pdf_paths = sorted(glob.glob(str(BILLS_DIR / "*.pdf")))
    if not pdf_paths:
        print(f"No PDFs found in {BILLS_DIR}")
        return

    records = []
    errors = []
    for path in pdf_paths:
        p = Path(path)
        if p.suffix.lower() != '.pdf':
            continue
        try:
            rec = parse_bill(p)
            records.append(rec)
            print(f"  ✓  {p.name}")
        except Exception as e:
            errors.append((p.name, str(e)))
            print(f"  ✗  {p.name}: {e}")

    if not records:
        print("No records extracted.")
        return

    # Sort by period start (works even when statement_date is blank from OCR)
    records.sort(key=lambda r: r.period_start or r.statement_date)

    # --- Print summary table ---
    print("\n" + "=" * 80)
    print("  CSU ELECTRIC BILL SUMMARY")
    print("=" * 80)

    table_rows = []
    for r in records:
        table_rows.append([
            r.statement_date,
            f"{r.period_start} – {r.period_end}",
            f"{r.total_kwh:,.0f}",
            f"{r.on_peak_kwh:,.0f}",
            f"{r.off_peak_kwh:,.0f}",
            f"${r.electric_total:,.2f}",
            f"${r.cost_per_kwh:.4f}",
        ])

    print(tabulate(
        table_rows,
        headers=["Statement", "Period", "Total kWh", "On-Peak kWh", "Off-Peak kWh", "Electric Total", "$/kWh"],
        tablefmt="rounded_outline",
    ))

    # --- Charge breakdown table ---
    print("\n  CHARGE BREAKDOWN\n")
    breakdown_rows = []
    for r in records:
        breakdown_rows.append([
            r.statement_date,
            f"${r.access_charge:,.2f}",
            f"${r.demand_charge_on_peak:,.2f}",
            f"${r.demand_charge_xof:,.2f}",
            f"${r.eca_on_peak:,.2f}",
            f"${r.eca_off_peak:,.2f}",
            f"${r.minimum_charge:,.2f}" if r.minimum_charge else "—",
            f"${r.capacity_charge:,.2f}",
            f"${r.total_taxes:,.2f}",
            f"${r.electric_total:,.2f}",
        ])

    print(tabulate(
        breakdown_rows,
        headers=["Statement", "Access", "Demand On-Pk", "Demand XOF", "ECA On-Pk", "ECA Off-Pk", "Min Charge", "Capacity", "Taxes", "Total"],
        tablefmt="rounded_outline",
    ))

    # --- Averages ---
    n = len(records)
    avg_kwh = sum(r.total_kwh for r in records) / n
    avg_total = sum(r.electric_total for r in records) / n
    avg_cpp = sum(r.cost_per_kwh for r in records) / n

    print(f"\n  Avg monthly kWh     : {avg_kwh:,.0f}")
    print(f"  Avg monthly electric: ${avg_total:,.2f}")
    print(f"  Avg cost per kWh    : ${avg_cpp:.4f}")
    print(f"  Bills analysed      : {n}")

    if errors:
        print(f"\n  Errors ({len(errors)}):")
        for name, err in errors:
            print(f"    {name}: {err}")

    # --- Write CSV ---
    field_names = [f.name for f in fields(BillRecord)]
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=field_names)
        writer.writeheader()
        for r in records:
            writer.writerow(asdict(r))

    print(f"\n  CSV saved to: {OUTPUT_CSV}\n")


if __name__ == "__main__":
    main()
