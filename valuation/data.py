"""
Data layer - reads the company JSON the existing scraper already saves (data/details/<SYMBOL>.json)
and normalises it for the valuation engine. It never invents values: anything the source does not
contain is left as None and listed in the field-mapping report.

All amounts are INR Crore (Screener's reporting unit), EPS / price in INR.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .engine import classify_business, ok

YEAR_RE = re.compile(r"^[A-Z][a-z]{2} \d{4}$")

PL = {"revenue": ("Sales", "Revenue"), "operating_profit": ("Operating Profit", "Financing Profit"),
      "other_income": ("Other Income",), "interest": ("Interest",), "depreciation": ("Depreciation",),
      "pbt": ("Profit before tax",), "tax_pct": ("Tax %",), "pat": ("Net Profit",), "eps": ("EPS in Rs",),
      "payout_pct": ("Dividend Payout %",)}
BS = {"equity_capital": ("Equity Capital",), "reserves": ("Reserves",), "borrowings": ("Borrowings", "Borrowing"),
      "other_liabilities": ("Other Liabilities",), "total_liabilities": ("Total Liabilities",),
      "fixed_assets": ("Fixed Assets",), "cwip": ("CWIP",), "investments": ("Investments",),
      "other_assets": ("Other Assets",), "total_assets": ("Total Assets",)}
CF = {"cfo": ("Cash from Operating Activity",), "cfi": ("Cash from Investing Activity",),
      "cff": ("Cash from Financing Activity",), "net_cash_flow": ("Net Cash Flow",)}
RT = {"debtor_days": ("Debtor Days",), "inventory_days": ("Inventory Days",), "payable_days": ("Days Payable",),
      "wc_days": ("Working Capital Days",), "ccc": ("Cash Conversion Cycle",), "roce_reported": ("ROCE %",)}

# (parent schedule, canonical key, accepted row labels - matched case-insensitively, 'contains')
DEEP = [
    ("Other Assets", "inventory", ("inventories", "inventory")),
    ("Other Assets", "receivables", ("trade receivables", "receivables")),
    ("Other Assets", "cash", ("cash equivalents", "cash and bank", "cash")),
    ("Other Assets", "other_op_assets", ("other asset items",)),
    ("Other Liabilities", "payables", ("trade payables", "payables")),
    ("Other Liabilities", "other_op_liabilities", ("other liability items", "advance from customers")),
    ("Borrowings", "lease_liabilities", ("lease liabilities",)),
    ("Borrowings", "long_term_borrowings", ("long term borrowings",)),
    ("Borrowings", "short_term_borrowings", ("short term borrowings",)),
    ("Borrowings", "preference_capital", ("preference capital",)),
    ("Cash from Investing Activity", "capex_gross", ("fixed assets purchased",)),
    ("Cash from Operating Activity", "wc_change_cf", ("working capital changes",)),
]


def _rows(table):
    if not table:
        return {}
    cols = table["columns"][1:]
    return {r[0]: dict(zip(cols, r[1:])) for r in table["data"]}


def _find(rows, labels, fuzzy=False):
    for lab in labels:
        if lab in rows:
            return rows[lab]
    if fuzzy:
        for lab in labels:
            for k, v in rows.items():
                if lab.lower() in k.lower():
                    return v
    return None


@dataclass
class CompanyData:
    symbol: str
    name: str
    sector: str
    industry: str
    basis: str
    fetched_at: str
    about: str
    years: list
    s: dict                      # canonical series aligned with `years` (+ 'years')
    ttm: dict
    market: dict
    deep: bool
    notes: list = field(default_factory=list)
    category: str = "general"
    currency: str = "INR"
    unit: str = "INR Crore"

    @property
    def latest_year(self):
        return self.years[-1] if self.years else None

    def latest(self, key):
        for x in reversed(self.s.get(key, [])):
            if ok(x):
                return x
        return None

    @property
    def age_days(self):
        try:
            ts = datetime.fromisoformat(self.fetched_at)
            return (datetime.now(timezone.utc) - ts).total_seconds() / 86400
        except Exception:
            return None


def build_company(detail: dict) -> CompanyData:
    tables = detail.get("tables", {})
    pl = tables.get("profit-loss")
    if not pl:
        raise ValueError("No annual profit & loss data is available for this company in the existing database.")
    years = [c for c in pl["columns"][1:] if YEAR_RE.match(str(c))]
    if len(years) < 2:
        raise ValueError("Fewer than two annual periods are available - a valuation would not be meaningful.")

    s = {"years": years}
    notes = []

    def load(table_key, mapping, fuzzy=False):
        rows = _rows(tables.get(table_key))
        for key, labels in mapping.items():
            row = _find(rows, labels, fuzzy)
            s[key] = [row.get(y) if row else None for y in years]

    load("profit-loss", PL)
    load("balance-sheet", BS)
    load("cash-flow", CF)
    load("ratios", RT)

    pl_rows = _rows(pl)
    ttm = {}
    for key in ("revenue", "operating_profit", "pat", "eps"):
        row = _find(pl_rows, PL[key])
        if row and ok(row.get("TTM")):
            ttm[key] = row["TTM"]

    deep = False
    for parent, key, labels in DEEP:
        sched = (detail.get("schedules") or {}).get(parent)
        s[key] = [None] * len(years)
        if not sched:
            continue
        rows = _rows(sched)
        vals = None
        for lab in labels:
            hits = [v for k, v in rows.items() if lab in k.lower()]
            if hits:
                vals = hits
                break
        if vals:
            deep = True
            s[key] = [sum(v.get(y) or 0 for v in vals) if any(ok(v.get(y)) for v in vals) else None for y in years]

    r = detail.get("ratios", {})
    price, mcap = r.get("Current Price"), r.get("Market Cap")
    shares = mcap / price if ok(price) and ok(mcap) and price > 0 else None
    fv = r.get("Face Value")
    eq_last = next((x for x in reversed(s["equity_capital"]) if ok(x)), None)
    shares_chk = eq_last / fv if ok(eq_last) and ok(fv) and fv > 0 else None
    if ok(shares) and ok(shares_chk) and abs(shares - shares_chk) / shares > 0.05:
        notes.append(f"Share count from market cap / price ({shares:.2f} Cr) differs from equity capital / face value "
                     f"({shares_chk:.2f} Cr) by more than 5 % - check for splits, bonus issues or partly-paid shares.")
    market = {"price": price, "mcap": mcap, "shares": shares, "shares_from_capital": shares_chk, "face_value": fv,
              "bvps": r.get("Book Value"), "div_yield": r.get("Dividend Yield"), "high52": r.get("52w High"),
              "low52": r.get("52w Low"), "pe_reported": r.get("Stock P/E"), "roce_reported": r.get("ROCE"),
              "roe_reported": r.get("ROE")}

    basis = detail.get("basis", "unknown")
    if basis == "unknown":
        notes.append("Consolidated / standalone basis was not recorded for this company (scraped before this feature). "
                     "Re-run the scraper to record it, or use 'Load live' in the module.")
    return CompanyData(symbol=detail["symbol"], name=detail.get("name", detail["symbol"]), sector=detail.get("sector", ""),
                       industry=detail.get("industry", ""), basis=basis, fetched_at=detail.get("fetched_at", ""),
                       about=detail.get("about", ""), years=years, s=s, ttm=ttm, market=market, deep=deep, notes=notes,
                       category=classify_business(detail.get("sector"), detail.get("industry")))


def dps_latest(c: CompanyData):
    """Dividend per share = latest EPS x payout %, falling back to yield x price. Returns (value, source)."""
    eps, pay = c.latest("eps"), c.latest("payout_pct")
    if ok(eps) and ok(pay):
        return eps * pay / 100, "EPS x dividend payout % (latest FY)"
    m = c.market
    if ok(m.get("div_yield")) and ok(m.get("price")):
        return m["div_yield"] / 100 * m["price"], "dividend yield x price"
    return None, "not available"


def _has(c, key):
    return any(ok(x) for x in c.s.get(key, []))


def mapping_table(c: CompanyData):
    """Required field -> where it comes from -> status. Used for the 'missing data' report and quality score."""
    def st(key, derived_from=None, deep=False, manual=False):
        if _has(c, key):
            return "Available (live schedule)" if deep else "Available"
        if derived_from:
            return f"Derived: {derived_from}"
        return "Missing - manual input" if manual else "Missing"

    m = c.market
    rows = [
        ("P&L", "Revenue from operations", "Sales / Revenue", st("revenue"), 3),
        ("P&L", "Other income", "Other Income", st("other_income"), 1),
        ("P&L", "EBITDA", "Operating Profit (excludes other income)", st("operating_profit"), 3),
        ("P&L", "Depreciation & amortisation", "Depreciation", st("depreciation"), 2),
        ("P&L", "EBIT", "Operating Profit - Depreciation", "Derived: EBITDA - D&A" if _has(c, "operating_profit") and _has(c, "depreciation") else "Missing", 3),
        ("P&L", "Finance costs", "Interest", st("interest"), 2),
        ("P&L", "Profit before tax", "Profit before tax", st("pbt"), 1),
        ("P&L", "Tax expense", "PBT x Tax %", "Derived: PBT x Tax %" if _has(c, "pbt") and _has(c, "tax_pct") else "Missing", 1),
        ("P&L", "Profit after tax", "Net Profit (attributable)", st("pat"), 3),
        ("P&L", "EPS", "EPS in Rs", st("eps"), 2),
        ("P&L", "Weighted average shares", "Net Profit / EPS", "Derived: PAT / EPS" if _has(c, "pat") and _has(c, "eps") else "Missing", 1),
        ("Balance sheet", "Total assets", "Total Assets", st("total_assets"), 1),
        ("Balance sheet", "Shareholders' equity", "Equity Capital + Reserves", st("reserves"), 3),
        ("Balance sheet", "Total borrowings", "Borrowings", st("borrowings"), 3),
        ("Balance sheet", "Current / long-term borrowings split", "Borrowings schedule", st("long_term_borrowings", deep=True, manual=True), 0.5),
        ("Balance sheet", "Cash and cash equivalents", "Other Assets schedule", st("cash", deep=True, manual=True), 3),
        ("Balance sheet", "Investments", "Investments (total, not split)", st("investments"), 1),
        ("Balance sheet", "Inventory", "Other Assets schedule", st("inventory", "Inventory days x revenue (estimate)" if _has(c, "inventory_days") else None, True, True), 1),
        ("Balance sheet", "Trade receivables", "Other Assets schedule", st("receivables", "Debtor days x revenue (estimate)" if _has(c, "debtor_days") else None, True, True), 1),
        ("Balance sheet", "Trade payables", "Other Liabilities schedule", st("payables", "Payable days x revenue (estimate)" if _has(c, "payable_days") else None, True, True), 1),
        ("Balance sheet", "Other current assets / liabilities", "Schedules", st("other_op_assets", None, True, True), 0.5),
        ("Balance sheet", "Lease liabilities", "Borrowings schedule", st("lease_liabilities", None, True, True), 1),
        ("Balance sheet", "Minority interest", "-", "Missing - manual input", 1),
        ("Cash flow", "Cash flow from operations", "Cash from Operating Activity", st("cfo"), 3),
        ("Cash flow", "Capital expenditure", "Fixed assets purchased",
         st("capex_gross", "net-block method: change in (fixed assets + CWIP) + depreciation" if _has(c, "fixed_assets") else None, True, True), 3),
        ("Cash flow", "Purchase of PPE / intangibles (split)", "Fixed assets purchased", st("capex_gross", None, True, True), 0.5),
        ("Cash flow", "Change in working capital", "Working-capital days", st("wc_change_cf", "from working-capital days x revenue (estimate)" if (_has(c, "wc_days") or _has(c, "debtor_days")) else None, True, True), 2),
        ("Cash flow", "Cash flow from investing / financing", "Cash flow statement", st("cfi"), 1),
        ("Market", "Current share price", "Current Price", "Available" if ok(m.get("price")) else "Missing", 3),
        ("Market", "Market capitalisation", "Market Cap", "Available" if ok(m.get("mcap")) else "Missing", 3),
        ("Market", "Total shares outstanding", "Market cap / price", "Derived: market cap / price" if ok(m.get("shares")) else "Missing", 3),
        ("Market", "Diluted shares outstanding", "-", "Missing - manual input (basic shares used)", 1),
        ("Market", "Beta", "-", "Missing - manual input", 3),
        ("Market", "Dividend per share", "EPS x payout %", "Derived: EPS x payout %" if ok(dps_latest(c)[0]) else "Missing", 1),
        ("Market", "Historical P/E and EV/EBITDA", "-", "Missing (price history is not stored)", 0.5),
    ]
    return rows


def quality_items(c: CompanyData, manual_keys=()):
    """Convert the mapping table to {name: (status, weight)} for engine.data_quality."""
    items = {}
    for _sec, field_, _src, status, w in mapping_table(c):
        if w <= 0:
            continue
        if field_ in manual_keys:
            kind = "manual"
        elif status.startswith("Available"):
            kind = "reported"
        elif status.startswith("Derived"):
            kind = "derived"
        else:
            kind = "missing"
        items[field_] = (kind, w)
    return items
