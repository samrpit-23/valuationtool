"""End-to-end tests: stored company JSON -> CompanyData -> pipeline -> reconciled valuation."""
import copy
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from valuation import engine as E  # noqa: E402
from valuation.data import build_company, mapping_table  # noqa: E402
from valuation.pipeline import compute_all, suggest_inputs, suitable_range  # noqa: E402

YEARS = [f"Mar {y}" for y in range(2016, 2026)]


def tbl(cols, rows):
    return {"columns": ["Item"] + cols, "data": [[k] + v for k, v in rows.items()]}


def make_detail(sector="Industrials", industry="Engineering", with_deep=False):
    n = 10
    rev = [1000 * 1.10 ** i for i in range(n)]
    op = [r * 0.20 for r in rev]
    dep = [r * 0.04 for r in rev]
    pat = [(o - d - 20) * 0.75 for o, d in zip(op, dep)]
    pl = tbl(YEARS + ["TTM"], {
        "Sales": rev + [rev[-1] * 1.04], "Operating Profit": op + [op[-1] * 1.04], "Other Income": [10.0] * 11,
        "Interest": [20.0] * 11, "Depreciation": dep + [dep[-1]], "Profit before tax": [o - d - 20 + 10 for o, d in zip(op, dep)] + [200.0],
        "Tax %": [25.0] * 11, "Net Profit": pat + [pat[-1]], "EPS in Rs": [p / 10 for p in pat] + [pat[-1] / 10],
        "Dividend Payout %": [30.0] * 11})
    bs = tbl(YEARS, {
        "Equity Capital": [100.0] * n, "Reserves": [500 + 150 * i for i in range(n)], "Borrowings": [300.0] * n,
        "Fixed Assets": [600 + 80 * i for i in range(n)], "CWIP": [20.0] * n, "Investments": [50.0] * n,
        "Total Assets": [1500 + 150 * i for i in range(n)]})
    cf = tbl(YEARS, {"Cash from Operating Activity": [p * 1.1 for p in pat], "Cash from Investing Activity": [-120.0] * n,
                     "Cash from Financing Activity": [-30.0] * n})
    rt = tbl(YEARS, {"Debtor Days": [45.0] * n, "Inventory Days": [60.0] * n, "Days Payable": [40.0] * n,
                     "Working Capital Days": [65.0] * n, "ROCE %": [18.0] * n})
    d = {"symbol": "TEST", "name": "Test Industries Ltd", "sector": sector, "industry": industry, "basis": "consolidated",
         "fetched_at": datetime.now(timezone.utc).isoformat(), "about": "A test company.",
         "ratios": {"Current Price": 1000.0, "Market Cap": 10000.0, "Book Value": 140.0, "Dividend Yield": 0.5,
                    "Face Value": 10.0, "Stock P/E": 28.0, "52w High": 1200.0, "52w Low": 800.0},
         "tables": {"profit-loss": pl, "balance-sheet": bs, "cash-flow": cf, "ratios": rt}}
    if with_deep:
        yrs = YEARS
        d["schedules"] = {
            "Other Assets": tbl(yrs, {"Inventories": [200.0] * n, "Trade receivables": [150.0] * n, "Cash Equivalents": [120.0] * n}),
            "Other Liabilities": tbl(yrs, {"Trade Payables": [130.0] * n}),
            "Cash from Investing Activity": tbl(yrs, {"Fixed assets purchased": [-100.0] * n})}
    return d


def inputs_for(c, **over):
    inp = suggest_inputs(c, n=5, g=0.04)
    inp["wacc"].update(rf=0.07, beta=1.0, erp=0.055)
    inp["confirmed_market_inputs"] = True
    for k, v in over.items():
        inp[k] = v
    return inp


def peers_snapshot():
    return pd.DataFrame({"Symbol": ["P1", "P2", "P3", "P4"], "Name": list("ABCD"), "Market Cap": [5000, 8000, 12000, 3000],
                         "Sales CAGR 3Y": [8, 10, 12, 6], "OPM %": [15, 18, 20, 12], "ROE": [14, 16, 18, 10], "ROCE": [15, 17, 19, 11],
                         "Stock P/E": [20.0, 25.0, 30.0, None], "P/B": [3.0, 4.0, 5.0, 2.0], "Dividend Yield": [1, 1, 1, 1],
                         "Borrowings Latest": [500, 800, 0, 300], "EBITDA Latest": [500, 700, 900, 250]})


def test_company_build_and_mapping():
    c = build_company(make_detail())
    assert c.years == YEARS and c.basis == "consolidated" and c.category == "general"
    assert c.market["shares"] == pytest.approx(10.0) and c.ttm["revenue"] > 0
    status = {f: s for _, f, _, s, _ in mapping_table(c)}
    assert status["Revenue from operations"] == "Available"
    assert status["Beta"].startswith("Missing") and status["Cash and cash equivalents"].startswith("Missing")
    assert status["Capital expenditure"].startswith("Derived")


def test_full_pipeline_reconciles():
    c = build_company(make_detail())
    inp = inputs_for(c)
    R = compute_all(c, inp, peers_snapshot())
    p = R["primary"]
    assert p is not None, R["errors"]
    # reconcile EV, equity and per-share independently from the displayed components
    ev = sum(f / (1 + R["wacc_used"]) ** (i + 1) for i, f in enumerate(p["fcff"])) + \
        p["fcff"][-1] * (1 + inp["g"]) / (R["wacc_used"] - inp["g"]) / (1 + R["wacc_used"]) ** 5
    assert p["ev"] == pytest.approx(ev)
    b = p["bridge"]
    assert b["equity"] == pytest.approx(p["ev"] - inp["bridge"]["debt"] + inp["bridge"]["cash"])
    assert R["iv"] == pytest.approx(b["equity"] / c.market["shares"])
    assert R["mos"] == pytest.approx((R["iv"] - 1000) / R["iv"] * 100)
    # WACC check by hand: ke = 7% + 1.0 x 5.5% = 12.5 %
    assert R["ke"] == pytest.approx(0.125)
    # scenarios ordered, base equals primary DCF
    sc = R["scenarios"]
    assert sc["Bear"]["per_share"] < sc["Base"]["per_share"] < sc["Bull"]["per_share"]
    assert sc["Base"]["per_share"] == pytest.approx(R["iv"])
    # sensitivity centre cell equals the base value
    s = R["sens"]["wacc_g"]
    assert s["grid"][2][2] == pytest.approx(R["iv"])


def test_wacc_inputs_are_never_silently_assumed():
    c = build_company(make_detail())
    inp = suggest_inputs(c, n=5, g=0.04)       # rf / beta / erp deliberately left empty
    R = compute_all(c, inp, None)
    assert R["wacc"] is None and "missing" in R["errors"]["wacc"].lower()
    assert R["primary"] is None and R["iv"] is None
    assert any("not been marked as verified" in w for w in R["warnings"])


def test_invalid_terminal_growth_blocks_valuation():
    c = build_company(make_detail())
    inp = inputs_for(c)
    inp["g"] = 0.20
    R = compute_all(c, inp, None)
    assert R["primary"] is None and "lower than WACC" in R["errors"]["dcf"]


def test_relative_valuation_and_range():
    c = build_company(make_detail())
    inp = inputs_for(c)
    inp["peers"]["symbols"] = ["P1", "P2", "P3", "P4"]
    R = compute_all(c, inp, peers_snapshot())
    st = R["peer_stats"]["pe"]
    assert st["n"] == 3 and st["median"] == 25.0                 # P4 has no P/E and is excluded
    eps = R["rel_inputs"]["eps"]
    assert R["implied"]["pe"]["price"] == pytest.approx(25.0 * eps)
    ev = R["implied"]["ev_ebitda"]
    mu, ebitda = ev["selected"], R["rel_inputs"]["ebitda"]
    assert ev["price"] == pytest.approx((mu * ebitda - inp["bridge"]["debt"]) / 10.0)   # EV -> equity -> per share
    rng = suitable_range(R, "general")
    assert rng["low"] <= rng["high"] and "DCF - Base" not in rng["methods"]


def test_peer_multiple_can_be_overridden():
    c = build_company(make_detail())
    inp = inputs_for(c)
    inp["peers"].update(symbols=["P1", "P2", "P3"], pe=18.0)
    R = compute_all(c, inp, peers_snapshot())
    assert R["implied"]["pe"]["selected"] == 18.0 and R["implied"]["pe"]["price"] == pytest.approx(18.0 * R["rel_inputs"]["eps"])


def test_live_schedules_improve_data_and_remove_estimates():
    c = build_company(make_detail(with_deep=True))
    assert c.deep and c.latest("cash") == 120.0
    h, meta = E.derive_history(c.s)
    assert meta["nwc_method"] == "schedule" and meta["capex_method"] == "cash-flow statement"
    assert h["capex"][-1] == 100.0 and h["nwc"][-1] == pytest.approx(200 + 150 - 130)
    inp = inputs_for(c)
    assert inp["bridge"]["cash"] == 120.0
    R = compute_all(c, inp, None)
    assert not any("net-block" in w or "estimated as the change" in w for w in R["warnings"])


def test_financial_company_is_steered_away_from_fcff():
    c = build_company(make_detail(sector="Financial Services", industry="Private Sector Bank"))
    assert c.category == "financial"
    R = compute_all(c, inputs_for(c), None)
    assert "Not appropriate" in R["suitability"]["DCF (FCFF)"]
    assert any("Financial-sector company" in w for w in R["warnings"])
    assert R["other"].get("fcfe") is None and R["other"].get("rim") is not None   # RIM/DDM family used instead
    rng = suitable_range(R, "financial")
    assert rng and all("DCF" not in m for m in rng["methods"])


def test_negative_fcff_and_negative_equity_are_flagged():
    d = make_detail()
    d["tables"]["balance-sheet"]["data"][1][1:] = [-900.0] * 10          # Reserves very negative -> negative equity
    c = build_company(d)
    R = compute_all(c, inputs_for(c), None)
    assert any("equity is negative" in w for w in R["warnings"])
    assert R["other"].get("rim") is None and "positive book value" in R["other"]["rim_err"]


def test_missing_working_capital_data_message():
    d = make_detail()
    d["tables"].pop("ratios")
    c = build_company(d)
    R = compute_all(c, inputs_for(c), None)
    assert any("Insufficient data to calculate working capital changes" in w for w in R["warnings"])


def test_unit_consistency_and_no_stale_mix():
    c = build_company(make_detail())
    assert c.unit == "INR Crore" and c.currency == "INR"
    # shares (Cr) x price (INR) must equal market cap (INR Cr)
    assert c.market["shares"] * c.market["price"] == pytest.approx(c.market["mcap"])


def test_data_quality_is_bounded_and_labelled():
    c = build_company(make_detail())
    R = compute_all(c, inputs_for(c), None)
    assert 0 <= R["dq"]["score"] <= 100 and "not a statistical probability" in R["dq"]["note"]
    c2 = build_company(make_detail(with_deep=True))
    R2 = compute_all(c2, inputs_for(c2), None)
    assert R2["dq"]["score"] > R["dq"]["score"]


def test_too_little_history_is_rejected():
    d = make_detail()
    for k in d["tables"]["profit-loss"]["data"]:
        del k[2:]
    d["tables"]["profit-loss"]["columns"] = d["tables"]["profit-loss"]["columns"][:2]
    with pytest.raises(ValueError):
        build_company(d)
