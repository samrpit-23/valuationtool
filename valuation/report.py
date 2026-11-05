"""
Report builder - converts a pipeline result into (a) numeric tables used by Excel/CSV and
(b) a structured, analyst-style report (13 sections) used by the on-screen view and the PDF.
Narrative is rule-based and only states what the numbers support; qualitative items that the
database cannot evidence are labelled 'requires further investigation'.
"""
from __future__ import annotations

import io
from datetime import date

import pandas as pd

from . import engine as E
from .engine import ok
from .pipeline import suitable_range

RS = "\u20b9"
NA = "\u2013"


# ----------------------------------------------------------------------------- formatting
def f_num(x, d=1):
    return NA if not ok(x) else f"{x:,.{d}f}"


def f_pct(x, d=1):
    return NA if not ok(x) else f"{x * 100:.{d}f}%"


def f_x(x, d=2):
    return NA if not ok(x) else f"{x:.{d}f}x"


def f_ps(x):
    return NA if not ok(x) else f"{RS}{x:,.2f}"


KIND = {"n": f_num, "pct": f_pct, "x": f_x, "ps": f_ps, "int": lambda v: NA if not ok(v) else f"{v:,.0f}",
        "d2": lambda v: f_num(v, 2), "d3": lambda v: f_num(v, 3)}


def fmt_df(df: pd.DataFrame, kinds) -> pd.DataFrame:
    out = df.copy().astype(object)
    for i, k in enumerate(kinds):
        fn = KIND[k]
        out.iloc[i] = [fn(v) for v in df.iloc[i]]
    return out


# ----------------------------------------------------------------------------- tables
def _rows_from_hist(h, yrs, rows):
    idx = [h["years"].index(y) for y in yrs]
    data = {lab: [h[key][i] for i in idx] for lab, key, _ in rows}
    return pd.DataFrame(data, index=yrs).T, [k for _, _, k in rows]


def history_table(R, n=10):
    h = R["hist"]
    yrs = h["years"][-n:]
    rows = [("Revenue", "revenue", "n"), ("EBITDA", "ebitda", "n"), ("EBITDA margin", "ebitda_margin", "pct"),
            ("EBIT", "ebit", "n"), ("PAT", "pat", "n"), ("EPS (INR)", "eps", "d2"), ("Cash flow from operations", "cfo", "n"),
            ("CapEx", "capex", "n"), ("FCFF", "fcff", "n"), ("ROE", "roe", "pct"), ("ROCE", "roce", "pct"),
            ("Borrowings (debt)", "borrowings", "n")]
    return _rows_from_hist(h, yrs, rows)


def ratio_table(R, n=10):
    h = R["hist"]
    yrs = h["years"][-n:]
    rows = [("EBITDA margin", "ebitda_margin", "pct"), ("EBIT margin", "ebit_margin", "pct"), ("Net profit margin", "net_margin", "pct"),
            ("ROE", "roe", "pct"), ("ROCE", "roce", "pct"), ("ROIC", "roic", "pct"), ("Debt / equity", "de", "d2"),
            ("Net debt", "net_debt", "n"), ("Interest coverage (EBIT / interest)", "interest_cov", "x"),
            ("Operating cash flow", "cfo", "n"), ("Free cash flow (CFO - CapEx)", "fcf", "n"),
            ("Cash conversion (CFO / PAT)", "cash_conv", "d2"), ("CapEx % of revenue", "capex_pct", "pct")]
    return _rows_from_hist(h, yrs, rows)


def fcff_hist_table(R, n=10):
    h = R["hist"]
    yrs = h["years"][-n:]
    rows = [("EBIT", "ebit", "n"), ("Tax rate used", "tax_rate", "pct"), ("NOPAT = EBIT x (1 - t)", "nopat", "n"),
            ("+ D&A", "depreciation", "n"), ("- CapEx", "capex", "n"), ("Operating NWC", "nwc", "n"),
            ("- Change in NWC", "d_nwc", "n"), ("FCFF", "fcff", "n")]
    return _rows_from_hist(h, yrs, rows)


def cagr_df(R):
    ct = R["cagr"]
    names = {"revenue": "Revenue", "ebitda": "EBITDA", "ebit": "EBIT", "pat": "PAT", "eps": "EPS"}
    df = pd.DataFrame({f"{n}Y CAGR": [ct[k][n] for k in names] for n in (3, 5, 10)}, index=list(names.values()))
    return df.T, ["pct"] * len(df.T)


def forecast_table(R):
    p = R["primary"]
    if not p:
        return None, None
    n = len(p["fcff"])
    cols = [f"Year {i + 1}" for i in range(n)]
    f = R["forecast"]
    if f:
        rows = [("Revenue", f["revenue"], "n"), ("Revenue growth", f["growth"], "pct"), ("EBITDA", f["ebitda"], "n"),
                ("EBITDA margin", f["ebitda_margin"], "pct"), ("EBIT", f["ebit"], "n"), ("Tax on EBIT", f["tax"], "n"),
                ("NOPAT", f["nopat"], "n"), ("D&A", f["dep"], "n"), ("CapEx", f["capex"], "n"),
                ("Change in NWC", f["d_nwc"], "n"), ("FCFF", f["fcff"], "n")]
    else:
        rows = [("FCFF", p["fcff"], "n")]
    rows += [("Discount factor", p["factors"], "d3"), ("Present value of FCFF", p["pv"], "n")]
    return pd.DataFrame({l: v for l, v, _ in rows}, index=cols).T, [k for _, _, k in rows]


def wacc_table(R):
    w = R["wacc"]
    if not w:
        return None
    rows = [("Risk-free rate", w["rf"], "pct"), ("Beta", w["beta"], "d2"), ("Equity risk premium", w["erp"], "pct"),
            ("Additional premium", w["extra_premium"], "pct"), ("Cost of equity = Rf + Beta x ERP", w["ke"], "pct"),
            ("Pre-tax cost of debt", w["kd"], "pct"), ("Tax rate", w["tax"], "pct"),
            ("After-tax cost of debt = Kd x (1 - t)", w["kd_after_tax"], "pct"),
            (f"Market value of equity ({RS} Cr)", w["equity"], "n"), (f"Interest-bearing debt incl. leases ({RS} Cr)", w["debt"], "n"),
            ("Equity weight E/(D+E)", w["we"], "pct"), ("Debt weight D/(D+E)", w["wd"], "pct"),
            ("WACC = We x Ke + Wd x Kd x (1 - t)", w["wacc"], "pct")]
    if ok(R["wacc_used"]) and abs(R["wacc_used"] - w["wacc"]) > 1e-12:
        rows.append(("WACC used (manual override)", R["wacc_used"], "pct"))
    return pd.DataFrame({"Value": [KIND[k](v) for _, v, k in rows]}, index=[l for l, _, _ in rows])


def bridge_table(R):
    p = R["primary"]
    if not p:
        return None
    b = p["bridge"]
    rows = [("PV of projected FCFF", p["pv_sum"]), ("PV of terminal value", p["pv_tv"]), ("Enterprise value", b["ev"]),
            ("Less: total debt", -b["debt"]), ("Less: lease liabilities (not in debt)", -b["leases"]),
            ("Less: minority interest", -b["minority"]), ("Less: preferred equity", -b["preferred"]),
            ("Add: cash & equivalents", b["cash"]), ("Add: non-operating investments", b["nonop"]), ("Equity value", b["equity"]),
            ("Diluted shares (Cr)", b["shares"]), (f"Intrinsic value per share ({RS})", b["per_share"])]
    return pd.DataFrame({"Value": [f_num(v, 2) for _, v in rows]}, index=[l for l, _ in rows])


def scenario_df(R):
    sc = R["scenarios"]
    if not sc or any("error" in v for v in sc.values()):
        return None, None
    rows = [("WACC", "wacc", "pct"), ("Terminal growth", "g", "pct")]
    data = {lab: [sc[k][key] for k in ("Bear", "Base", "Bull")] for lab, key, _ in rows}
    kinds = [k for _, _, k in rows]
    for lab, fkey, kind in (("Final-year revenue", "revenue", "n"), ("Final-year EBITDA", "ebitda", "n"),
                            ("Final-year EBIT", "ebit", "n"), ("Final-year FCFF", "fcff", "n")):
        data[lab] = [sc[k]["forecast"][fkey][-1] for k in ("Bear", "Base", "Bull")]
        kinds.append(kind)
    for lab, key, kind in (("Enterprise value", "ev", "n"), ("Equity value", "equity", "n"), ("Intrinsic value / share", "per_share", "ps"),
                           ("Margin of safety %", "mos", "n"), ("Upside / downside %", "upside", "n")):
        data[lab] = [sc[k].get(key) for k in ("Bear", "Base", "Bull")]
        kinds.append(kind)
    return pd.DataFrame(data, index=["Bear", "Base", "Bull"]).T, kinds


def sens_df(R, which="wacc_g"):
    s = R["sens"].get(which)
    if not s or not s.get("grid"):
        return None
    if which == "wacc_g":
        return pd.DataFrame(s["grid"], index=[f"WACC {w:.2%}" for w in s["waccs"]], columns=[f"g {g:.2%}" for g in s["gs"]])
    if which == "growth_margin":
        return pd.DataFrame(s["grid"], index=[f"Growth {d:+.1%}" for d in s["dg"]], columns=[f"Margin {d:+.1%}" for d in s["dm"]])
    return pd.DataFrame(s["grid"], index=[f"FCFF growth {g:.0%}" for g in s["growths"]], columns=[f"WACC {w:.2%}" for w in s["waccs"]])


def multiples_df(R):
    m, notes = R["multiples"], R["multiple_notes"]
    labels = [("pe", "Trailing P/E", "x"), ("normalized_pe", "Normalised P/E (price / 5Y avg EPS)", "x"), ("forward_pe", "Forward P/E", "x"),
              ("peg", "PEG", "x"), ("pb", "Price / Book", "x"), ("ps", "Price / Sales", "x"), ("p_fcf", "Price / FCF", "x"),
              ("ev_ebitda", "EV / EBITDA (own EV incl. cash)", "x"), ("ev_ebit", "EV / EBIT", "x"), ("ev_sales", "EV / Sales", "x"),
              ("ev_fcff", "EV / FCFF", "x")]
    rows = [(lab, KIND[k](m.get(key)), notes.get(key, "")) for key, lab, k in labels]
    rows.append(("Dividend yield", NA if not ok(m.get("div_yield")) else f"{m['div_yield']:.2f}%", ""))
    rows.append(("Historical P/E & EV/EBITDA", NA, "Price history is not stored in the database."))
    return pd.DataFrame(rows, columns=["Multiple", "Value", "Note"]).set_index("Multiple")


def peer_df(R):
    p = R["peers"]
    if p is None or not len(p):
        return None
    out = p.copy()
    out.columns = ["Symbol", "Company", f"Mkt cap ({RS} Cr)", "Rev growth 3Y CAGR %", "EBITDA margin %", "ROE %", "ROCE %", "P/E",
                   "EV/EBITDA*", "P/B", "Div yield %"]
    return out.set_index("Symbol")


def summary_df(R):
    rows = [(n, f_ps(v)) for n, v in R["final_table"]]
    return pd.DataFrame(rows, columns=["Valuation method", f"Estimated value / share"]).set_index("Valuation method")


# ----------------------------------------------------------------------------- narrative helpers
def _last(h, k, back=0):
    v = [x for x in h[k] if ok(x)]
    return v[-1 - back] if len(v) > back else None


def hist_summary(R):
    v = R["verdict"]
    lines = [f"**Overall direction: {v['verdict']}** (signal score {v['score']:+d}; based on several trends together, not one ratio)."]
    lines += [f"- {t} ({'positive' if s > 0 else 'negative' if s < 0 else 'neutral'})" for t, s in v["signals"]]
    lines += [f"- WARNING: {f}" for f in v["flags"]]
    return "\n".join(lines)


def financial_health(R, c):
    h = R["hist"]
    out = []
    de, ic, debt = _last(h, "de"), _last(h, "interest_cov"), _last(h, "borrowings")
    out.append(f"**Debt position.** Borrowings are {RS}{f_num(debt, 0)} Cr; debt/equity {f_num(de, 2)}; interest coverage {f_x(ic, 1)}. " +
               ("Leverage is low." if ok(de) and de < 0.5 else "Leverage is moderate." if ok(de) and de < 1 else "Leverage is high - review refinancing risk." if ok(de) else "Not assessable."))
    cc = [x for x in h["cash_conv"] if ok(x)][-3:]
    out.append(f"**Cash-flow quality.** Average CFO/PAT over the last {len(cc)} years is {f_num(sum(cc) / len(cc), 2) if cc else NA}. " +
               ("Profits are backed by cash." if cc and sum(cc) / len(cc) >= 0.8 else "Cash conversion is weak - check receivables and inventory." if cc else ""))
    nwc, rev = _last(h, "nwc"), _last(h, "revenue")
    method = R["hist_meta"]["nwc_method"]
    out.append(f"**Working capital.** Operating NWC is {f_pct(nwc / rev) if ok(nwc) and ok(rev) and rev else NA} of revenue "
               f"({'exact schedule data' if method == 'schedule' else 'estimated from working-capital days' if method == 'days' else 'not available'}).")
    out.append(f"**Capital efficiency.** ROCE {f_pct(_last(h, 'roce'))}, ROE {f_pct(_last(h, 'roe'))}, ROIC {f_pct(_last(h, 'roic'))} "
               f"(ROIC basis: {R['hist_meta']['roic_basis']}).")
    out.append(f"**Profitability.** EBITDA margin {f_pct(_last(h, 'ebitda_margin'))}, EBIT margin {f_pct(_last(h, 'ebit_margin'))}, net margin {f_pct(_last(h, 'net_margin'))}.")
    oi, pbt = c.latest("other_income"), c.latest("pbt")
    out.append(f"**Earnings quality.** Other income is {f_pct(oi / pbt) if ok(oi) and ok(pbt) and pbt else NA} of PBT" +
               ("; a high share suggests earnings depend on non-operating items." if ok(oi) and ok(pbt) and pbt and oi / pbt > 0.3 else "."))
    out.append("**Financial risks.** " + ("; ".join(R["verdict"]["flags"]) if R["verdict"]["flags"] else "No multi-signal warning was triggered by the available data."))
    return out


def risk_table(R, c, inp):
    h = R["hist"]
    rows = []

    def add(risk, evidence, status):
        rows.append((risk, evidence, status))

    add("Revenue concentration", "Segment / product revenue split is not in the database.", "Requires further investigation")
    add("Customer concentration", "Customer data is not in the database (see annual report).", "Requires further investigation")
    de, ic = _last(h, "de"), _last(h, "interest_cov")
    add("Debt", f"D/E {f_num(de, 2)}, interest coverage {f_x(ic, 1)}.", "Flag" if (ok(de) and de > 1) or (ok(ic) and ic < 3) else "OK" if ok(de) else "Data missing")
    m_now, m_old = _last(h, "ebit_margin"), _last(h, "ebit_margin", 3)
    add("Margin deterioration", f"EBIT margin {f_pct(m_old)} -> {f_pct(m_now)} over 3 years.", "Flag" if ok(m_now) and ok(m_old) and m_now - m_old < -0.01 else "OK" if ok(m_now) else "Data missing")
    cc = [x for x in h["cash_conv"] if ok(x)][-3:]
    add("Working capital / cash conversion", f"CFO/PAT average {f_num(sum(cc) / len(cc), 2) if cc else NA}; NWC method: {R['hist_meta']['nwc_method'] or 'n/a'}.",
        "Flag" if cc and sum(cc) / len(cc) < 0.7 else "OK" if cc else "Data missing")
    cp = _last(h, "capex_pct")
    add("High capital expenditure", f"CapEx {f_pct(cp)} of revenue ({R['hist_meta']['capex_method']}).", "Flag" if ok(cp) and cp > 0.12 else "OK" if ok(cp) else "Data missing")
    oi, pbt = c.latest("other_income"), c.latest("pbt")
    share = oi / pbt if ok(oi) and ok(pbt) and pbt > 0 else None
    add("Earnings quality", f"Other income {f_pct(share)} of PBT; effective tax rate fallbacks used in {len(R['hist_meta']['tax_fallback_years'])} years.",
        "Flag" if ok(share) and share > 0.4 else "OK" if ok(share) else "Data missing")
    add("Industry risks", f"Sector: {c.sector or NA}; industry: {c.industry or NA}. Cyclicality, regulation and competition are not quantified here.", "Requires further investigation")
    p = R["primary"]
    vr = []
    if p and p.get("tv_share") is not None:
        vr.append(f"terminal value is {p['tv_share']:.0%} of EV")
    g = R["sens"].get("wacc_g")
    if g:
        vals = [v for row in g["grid"] for v in row if ok(v)]
        if vals and R["iv"]:
            vr.append(f"value ranges {f_ps(min(vals))} - {f_ps(max(vals))} across the WACC/g grid")
    if R["reverse"]:
        vr.append(f"price implies average growth of {f_pct(R['reverse']['avg_growth_needed'])}")
    flag = bool(p and p.get("tv_share") and p["tv_share"] > 0.75)
    add("Valuation risk", "; ".join(vr) or "DCF not available.", "Flag" if flag else "Review" if vr else "Data missing")
    return pd.DataFrame(rows, columns=["Risk", "Evidence from data", "Status"]).set_index("Risk")


# ----------------------------------------------------------------------------- report sections
def build_report(c, inp, R):
    """Returns a list of sections: {'title', 'blocks': [('p', md) | ('table', df, caption, first_col_label) | ('chart', key)]}."""
    S = []
    m, h, p = c.market, R["hist"], R["primary"]
    price = m.get("price")
    ev_ebitda = R["multiples"].get("ev_ebitda") if R.get("multiples") else None
    sc = R["scenarios"]
    rng = suitable_range(R, R["category"])

    # 1 Executive summary
    rows = [("Company", c.name), ("NSE symbol", c.symbol), ("Sector / industry", f"{c.sector or NA} / {c.industry or NA}"),
            ("Current market price", f_ps(price)), ("Market capitalisation", f"{RS}{f_num(m.get('mcap'), 0)} Cr"),
            ("Current P/E", f_x(R['multiples'].get('pe'))), ("Current EV/EBITDA", f_x(ev_ebitda)),
            ("Estimated intrinsic value (selected case)", f_ps(R["iv"])),
            ("Bear / Base / Bull value", " / ".join(f_ps(sc[k].get("per_share")) if k in sc else NA for k in ("Bear", "Base", "Bull"))),
            ("Margin of safety", f"{f_num(R['mos'])}%" if ok(R["mos"]) else NA),
            ("Potential upside / downside", f"{f_num(R['upside'])}%" if ok(R["upside"]) else NA),
            ("Data basis", f"{c.basis}, FY {c.latest_year}, {c.unit}"), ("Data quality indicator", f"{R['dq']['label']} ({R['dq']['score']}/100)")]
    if R["iv"] is None:
        concl = "No DCF value could be produced with the current inputs: " + (R["errors"].get("dcf") or "") + " See the warnings below."
    else:
        rel = "above" if R["iv"] > price else "below"
        concl = (f"With the selected assumptions the DCF value of {f_ps(R['iv'])} is {rel} the market price of {f_ps(price)} "
                 f"(margin of safety {f_num(R['mos'])}%, upside {f_num(R['upside'])}%). This is a model output that depends entirely on the "
                 "assumptions listed in this report; it is not a statement that the stock is under- or over-valued.")
    S.append({"title": "1. Executive Summary", "blocks": [("table", pd.DataFrame(rows, columns=["Item", "Value"]).set_index("Item"), "", "Item"), ("p", concl)]})

    # 2 Business overview
    about = c.about or "Business model, products and revenue sources are not stored in the database - read the latest annual report for these."
    S.append({"title": "2. Business Overview", "blocks": [
        ("p", f"**{c.name}** ({c.symbol}) operates in the **{c.sector or 'n/a'}** sector, industry **{c.industry or 'n/a'}**."),
        ("p", about), ("p", "Competitive position, moat and customer/revenue concentration cannot be derived from financial statements and require further research.")]})

    # 3 Historical analysis
    t, k = history_table(R)
    t2, k2 = cagr_df(R)
    S.append({"title": "3. Historical Financial Analysis", "blocks": [
        ("table", fmt_df(t, k), f"Amounts in {RS} Cr unless stated; FY ending March", "Particulars"),
        ("table", fmt_df(t2, k2), "Compound annual growth rates", "Metric"),
        ("chart", "growth"), ("chart", "margins"), ("chart", "returns"), ("chart", "cash"), ("chart", "debt"),
        ("p", hist_summary(R))]})

    # 4 Financial health
    S.append({"title": "4. Financial Health Analysis", "blocks": [("p", x) for x in financial_health(R, c)]})

    # 5 DCF working
    blocks = []
    h_idx = max((i for i, x in enumerate(h["fcff"]) if ok(x)), default=None)
    if h_idx is not None:
        y = h["years"][h_idx]
        blocks.append(("p", "\n".join([
            f"**Historical FCFF worked example (FY {y})**", "",
            f"- Step 1: Current EBIT = {RS}{f_num(h['ebit'][h_idx])} Cr (EBITDA {f_num(h['ebitda'][h_idx])} - D&A {f_num(h['depreciation'][h_idx])})",
            f"- Step 2: Tax rate = {f_pct(h['tax_rate'][h_idx])}",
            f"- Step 3: NOPAT = EBIT x (1 - Tax Rate) = {RS}{f_num(h['nopat'][h_idx])} Cr",
            f"- Step 4: Add D&A = {RS}{f_num(h['depreciation'][h_idx])} Cr",
            f"- Step 5: Less CapEx = {RS}{f_num(h['capex'][h_idx])} Cr ({R['hist_meta']['capex_method']})",
            f"- Step 6: Less change in NWC = {RS}{f_num(h['d_nwc'][h_idx])} Cr (NWC {f_num(h['nwc'][h_idx])})",
            f"- Step 7: Historical FCFF = {RS}{f_num(h['fcff'][h_idx])} Cr"])))
    else:
        blocks.append(("p", "Historical FCFF could not be calculated (insufficient capex or working-capital data)."))
    t, k = fcff_hist_table(R)
    blocks.append(("table", fmt_df(t, k), "Historical FCFF build-up", "Particulars"))
    ft, fk = forecast_table(R)
    if ft is not None:
        mode = "Advanced operating forecast (Mode B)" if R["forecast"] else "Simple FCFF growth (Mode A)"
        blocks.append(("p", f"**Forecast mode:** {mode}. Forecast period {len(p['fcff'])} years; cash flows discounted at year-end."))
        blocks.append(("table", fmt_df(ft, fk), f"Projection ({RS} Cr)", "Particulars"))
    else:
        blocks.append(("p", f"DCF projection not available: {R['errors'].get('forecast') or R['errors'].get('dcf')}"))
    S.append({"title": "5. DCF Calculation - Detailed Working", "blocks": blocks})

    # 6 WACC
    wt = wacc_table(R)
    wb = [("table", wt, "CAPM and WACC", "Component")] if wt is not None else [("p", f"WACC could not be computed: {R['errors']['wacc']}")]
    wb.append(("p", "FCFF is discounted at WACC (the blended cost of all capital), never at the cost of equity. Equity-based models (FCFE, DDM, "
                    "residual income) are discounted at the cost of equity instead. Risk-free rate, beta and equity risk premium are analyst "
                    "inputs, not database values" + (" and have been marked as verified by the analyst." if inp.get("confirmed_market_inputs") else " and have NOT been marked as verified.")))
    S.append({"title": "6. WACC Calculation", "blocks": wb})

    # 7 Terminal value
    if p:
        gline = (f"Terminal value = final FCFF x (1 + g) / (WACC - g) = {f_num(p['fcff'][-1])} x (1 + {p['g']:.2%}) / ({p['wacc']:.2%} - {p['g']:.2%}) = {RS}{f_num(p['tv'])} Cr"
                 if p["tv_method"] == "gordon" else f"Terminal value = final EBITDA x exit multiple = {RS}{f_num(p['tv'])} Cr ({p['exit_multiple']}x)")
        tv = ["\n".join([f"- Terminal method used: **{'Gordon growth' if p['tv_method'] == 'gordon' else 'Exit EV/EBITDA multiple'}**",
                         f"- Final forecast FCFF: {RS}{f_num(p['fcff'][-1])} Cr", f"- Terminal growth: {p['g']:.2%}" if p['tv_method'] == 'gordon' else "- Terminal growth: not used",
                         f"- WACC: {p['wacc']:.2%}", f"- {gline}", f"- Discount factor: {p['tv_factor']:.4f}",
                         f"- PV of terminal value: {RS}{f_num(p['pv_tv'])} Cr", f"- Terminal value as % of enterprise value: {f_pct(p['tv_share'], 1)}"])]
        if R["tv_crosscheck"]:
            x = R["tv_crosscheck"]
            tv.append(f"Cross-check only (not blended): the alternative terminal method gives {f_ps(x['per_share'])} per share.")
        tv += [f"WARNING: {w}" for w in p["warnings"] if "Terminal value" in w]
        S.append({"title": "7. Terminal Value Calculation", "blocks": [("p", t) for t in tv]})
    else:
        S.append({"title": "7. Terminal Value Calculation", "blocks": [("p", "Not available - " + str(R["errors"].get("dcf")))]})

    # 8 Bridge
    bt = bridge_table(R)
    S.append({"title": "8. Enterprise Value to Equity Value Bridge", "blocks": [("table", bt, f"{RS} Cr unless stated", "Item")] if bt is not None else [("p", "Not available.")]})

    # 9 Relative
    blocks = [("table", multiples_df(R), "Valuation multiples of the company (trailing)", "Multiple")]
    pt = peer_df(R)
    if pt is not None:
        blocks.append(("table", pt, "Peer comparison (EV/EBITDA* = (market cap + gross debt) / EBITDA; cash is not deducted for peers)", "Symbol"))
        st = R["peer_stats"]
        lines = []
        for key, lab in (("pe", "P/E"), ("ev_ebitda", "EV/EBITDA*"), ("pb", "P/B")):
            if key in st and st[key]:
                s = st[key]
                lines.append(f"- {lab}: mean {s['mean']:.2f}, median {s['median']:.2f}, interquartile range {s['p25']:.2f} - {s['p75']:.2f} (n={s['n']})")
        for key, lab in (("pe", "P/E"), ("ev_ebitda", "EV/EBITDA"), ("pb", "P/B")):
            if key in R["implied"]:
                i = R["implied"][key]
                lines.append(f"- Implied price from {lab} at {i['selected']:.2f}x: {f_ps(i['price'])} (peer range {f_ps(i['low'])} - {f_ps(i['high'])})")
        blocks.append(("p", "\n".join(lines) or "No usable peer multiples."))
        blocks.append(("p", "Differences in growth, margins, ROE and leverage between the company and its peers should be reviewed before applying a peer multiple; the selected multiple is an analyst choice and can be adjusted."))
    else:
        blocks.append(("p", "No peers were selected."))
    S.append({"title": "9. Relative Valuation", "blocks": blocks})

    # 10 Scenarios and sensitivity
    blocks = []
    sd, sk = scenario_df(R)
    if sd is not None:
        blocks.append(("table", fmt_df(sd, sk), "Bear / Base / Bull (independent, user-editable; no probabilities are assumed)", "Metric"))
        d = inp["deltas"]
        blocks.append(("p", "\n".join(f"- {k}: growth {v['growth']:+.1%}, EBIT margin {v['margin']:+.1%}, NWC {v['nwc']:+.1%} of revenue, WACC {v['wacc']:+.1%}, terminal growth {v['g']:+.1%}" for k, v in d.items())))
    sg = sens_df(R)
    if sg is not None:
        blocks.append(("table", sg.map(lambda x: f_num(x, 1)), f"Intrinsic value per share ({RS}): WACC vs terminal growth (centre = base assumptions)", "WACC / g"))
    S.append({"title": "10. Scenario and Sensitivity Analysis", "blocks": blocks or [("p", "Scenario analysis needs the Advanced forecast mode and valid inputs.")]})

    # 11 Risks
    S.append({"title": "11. Risk Analysis", "blocks": [("table", risk_table(R, c, inp), "Only data-supported risks are rated; others are marked for further investigation", "Risk")]})

    # 12 Final summary
    blocks = [("table", summary_df(R), "Per-share values by method", "Valuation method"), ("chart", "football")]
    fit = "\n".join(f"- {k}: {v}" for k, v in R["suitability"].items())
    blocks.append(("p", f"**Method suitability for a {R['category']} company** (methods are not equally appropriate, so they are not averaged):\n{fit}"))
    if rng:
        blocks.append(("p", f"**Reasoned range:** {f_ps(rng['low'])} - {f_ps(rng['high'])}, spanning the most suitable methods only ({', '.join(rng['methods'])}). "
                            "This is the spread of those methods, not an average."))
    ov = [f"- Selected mode: {'Advanced operating forecast' if inp['mode'] == 'B' else 'Simple FCFF growth'}; forecast years {inp['n']}; terminal growth {inp['g']:.2%}; terminal method {inp['tv_method']}."]
    if R["wacc"]:
        ov.append(f"- WACC {R['wacc_used']:.2%} (Ke {R['wacc']['ke']:.2%}); manual override {'yes' if ok(inp['wacc'].get('override')) else 'no'}.")
    ov.append(f"- Diluted shares {f_num(R['shares'], 3)} Cr ({'basic count - diluted not available' if abs((R['shares'] or 0) - (m.get('shares') or 0)) < 1e-9 else 'manual override'}).")
    ov.append(f"- Bridge: debt {f_num(inp['bridge']['debt'])}, leases {f_num(inp['bridge']['leases'])}, minority {f_num(inp['bridge']['minority'])}, cash {f_num(inp['bridge']['cash'])}, non-operating investments {f_num(inp['bridge']['nonop'])} ({RS} Cr).")
    blocks.append(("p", "**Analyst assumptions and manual overrides**\n" + "\n".join(ov)))
    S.append({"title": "12. Final Valuation Summary", "blocks": blocks})

    # 13 Conclusion
    q = []
    if p:
        q.append(f"**What drives the valuation?** Terminal value is {f_pct(p['tv_share'], 0)} of enterprise value; the result is therefore driven mainly by the long-run growth ({p['g']:.2%}), WACC ({p['wacc']:.2%}) and the steady-state FCFF margin.")
    if R["reverse"]:
        rv = R["reverse"]
        q.append(f"**What must be true at today's price?** Holding other assumptions constant, the market price of {f_ps(price)} requires average revenue growth of about "
                 f"{f_pct(rv['avg_growth_needed'])} per year, compared with {f_pct(rv['base_avg_growth'])} in the base assumptions " +
                 ("(more aggressive than assumed)." if rv["delta"] > 0.005 else "(close to or below what is assumed)." if rv["delta"] < 0.005 else "."))
    else:
        q.append("**What must be true at today's price?** A reverse-DCF solution was not available for these inputs.")
    q.append("**Main uncertainties:** " + ("; ".join(R["warnings"][:5]) if R["warnings"] else "none flagged by the automated checks") + ".")
    q.append("**Model output vs advice:** this report is an analytical model built on stated assumptions and third-party data. It is not investment advice or a recommendation to buy, hold or sell.")
    S.append({"title": "13. Conclusion", "blocks": [("p", t) for t in q]})
    return S


# ----------------------------------------------------------------------------- charts (matplotlib, also used in PDF)
def make_charts(c, R):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    h = R["hist"]
    yrs = h["years"][-10:]
    sl = slice(-len(yrs), None)

    def vals(k):
        return [x if ok(x) else float("nan") for x in h[k][sl]]

    def save(fig):
        buf = io.BytesIO()
        fig.tight_layout()
        fig.savefig(buf, format="png", dpi=130)
        plt.close(fig)
        return buf.getvalue()

    short = [y.replace("Mar ", "'")[-3:] if y.startswith("Mar") else y for y in yrs]
    charts = {}
    fig, ax = plt.subplots(2, 2, figsize=(9, 5))
    for a, (k, t) in zip(ax.flat, (("revenue", "Revenue"), ("ebitda", "EBITDA"), ("pat", "PAT"), ("eps", "EPS (INR)"))):
        a.bar(short, vals(k), color="#2f6fb3")
        a.set_title(t, fontsize=9)
        a.tick_params(labelsize=7)
    charts["growth"] = save(fig)

    fig, ax = plt.subplots(figsize=(8, 3))
    for k, lab in (("ebitda_margin", "EBITDA margin"), ("ebit_margin", "EBIT margin"), ("net_margin", "Net margin")):
        ax.plot(short, [v * 100 for v in vals(k)], marker="o", label=lab)
    ax.set_ylabel("%")
    ax.set_title("Margin trend", fontsize=9)
    ax.legend(fontsize=7)
    ax.tick_params(labelsize=7)
    charts["margins"] = save(fig)

    fig, ax = plt.subplots(figsize=(8, 3))
    for k, lab in (("roe", "ROE"), ("roce", "ROCE"), ("roic", "ROIC")):
        ax.plot(short, [v * 100 for v in vals(k)], marker="o", label=lab)
    ax.set_ylabel("%")
    ax.set_title("ROE, ROCE and ROIC", fontsize=9)
    ax.legend(fontsize=7)
    ax.tick_params(labelsize=7)
    charts["returns"] = save(fig)

    fig, ax = plt.subplots(1, 2, figsize=(9, 3))
    w = 0.4
    xs = range(len(short))
    ax[0].bar([x - w / 2 for x in xs], vals("cfo"), w, label="CFO", color="#2f6fb3")
    ax[0].bar([x + w / 2 for x in xs], vals("pat"), w, label="PAT", color="#e08e0b")
    ax[0].set_xticks(list(xs))
    ax[0].set_xticklabels(short, fontsize=7)
    ax[0].set_title("Operating cash flow vs PAT", fontsize=9)
    ax[0].legend(fontsize=7)
    ax[1].bar(short, vals("fcff"), color="#3a9d5d", label="FCFF")
    ax[1].plot(short, vals("fcf"), color="#444", marker="o", label="FCF (CFO - CapEx)")
    ax[1].set_title("Free cash flow trend", fontsize=9)
    ax[1].legend(fontsize=7)
    ax[1].tick_params(labelsize=7)
    charts["cash"] = save(fig)

    fig, ax = plt.subplots(figsize=(8, 3))
    ax.bar(short, vals("borrowings"), color="#b34a4a", label="Borrowings")
    nd = vals("net_debt")
    if any(v == v for v in nd):
        ax.plot(short, nd, color="#222", marker="o", label="Net debt")
    ax.set_title("Debt trend" + ("" if any(v == v for v in nd) else " (net debt needs cash data)"), fontsize=9)
    ax.legend(fontsize=7)
    ax.tick_params(labelsize=7)
    charts["debt"] = save(fig)

    # football field
    items = []
    for n, v in R["final_table"]:
        if ok(v):
            items.append((n, v, v))
    imp = R["implied"]
    for key, lab in (("pe", "P/E peers (IQR)"), ("ev_ebitda", "EV/EBITDA peers (IQR)"), ("pb", "P/B peers (IQR)")):
        if key in imp and ok(imp[key]["low"]) and ok(imp[key]["high"]):
            items.append((lab, min(imp[key]["low"], imp[key]["high"]), max(imp[key]["low"], imp[key]["high"])))
    sg = sens_df(R)
    if sg is not None:
        vv = [v for v in sg.values.flatten() if ok(v)]
        if vv:
            items.append(("DCF WACC/g grid", min(vv), max(vv)))
    if items:
        fig, ax = plt.subplots(figsize=(8, 0.45 * len(items) + 1))
        for i, (lab, lo, hi) in enumerate(items):
            ax.barh(i, max(hi - lo, (hi or 1) * 0.004), left=lo, color="#2f6fb3")
        ax.set_yticks(range(len(items)))
        ax.set_yticklabels([x[0] for x in items], fontsize=7)
        if ok(R["price"]):
            ax.axvline(R["price"], color="red", ls="--", label=f"Market price {R['price']:,.0f}")
            ax.legend(fontsize=7)
        ax.set_title("Valuation range by method (INR per share)", fontsize=9)
        ax.tick_params(labelsize=7)
        charts["football"] = save(fig)
    return charts
