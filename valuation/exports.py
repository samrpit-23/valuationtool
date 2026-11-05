"""Exports: Excel (with live formulas), PDF research report and CSV bundle."""
from __future__ import annotations

import io
import zipfile
from datetime import date

import pandas as pd

from .engine import ok
from . import report as RP
from .report import RS

# colour code: green = from existing database, blue = manual / analyst input, black = formula or model output
GREEN, BLUE, BLACK = "007A33", "0000FF", "000000"


# ----------------------------------------------------------------------------- CSV
def to_csv_zip(c, inp, R) -> bytes:
    tables = {}
    for name, fn in (("historical_financials", RP.history_table), ("historical_ratios", RP.ratio_table),
                     ("fcff_historical", RP.fcff_hist_table), ("forecast_projection", RP.forecast_table),
                     ("cagr", RP.cagr_df), ("scenarios", RP.scenario_df)):
        t = fn(R)
        if t and t[0] is not None:
            tables[name] = t[0]
    for name, df in (("wacc", RP.wacc_table(R)), ("ev_to_equity_bridge", RP.bridge_table(R)), ("sensitivity_wacc_g", RP.sens_df(R)),
                     ("sensitivity_growth_margin", RP.sens_df(R, "growth_margin")), ("relative_multiples", RP.multiples_df(R)),
                     ("peer_comparison", RP.peer_df(R)), ("final_summary", RP.summary_df(R)), ("data_field_mapping",
                      pd.DataFrame(R["mapping"], columns=["Section", "Required field", "Source", "Status", "Weight"]).set_index("Section"))):
        if df is not None:
            tables[name] = df
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, df in tables.items():
            z.writestr(f"{c.symbol}_{name}.csv", df.to_csv())
    return buf.getvalue()


# ----------------------------------------------------------------------------- Excel
def to_excel(c, inp, R) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter as L

    wb = Workbook()
    HEAD = PatternFill("solid", fgColor="DCE6F2")

    def put(ws, ref, v, color=BLACK, fmt=None, bold=False):
        cell = ws[ref]
        cell.value = None if (isinstance(v, float) and not ok(v)) else v
        cell.font = Font(color=color, bold=bold)
        if fmt:
            cell.number_format = fmt
        return cell

    def title(ws, text, sub=None):
        ws["A1"] = text
        ws["A1"].font = Font(bold=True, size=13)
        if sub:
            ws["A2"] = sub
            ws["A2"].font = Font(italic=True, color="666666")
        ws.column_dimensions["A"].width = 44

    def header(ws, row, labels, start=1):
        for i, lab in enumerate(labels):
            cell = ws.cell(row=row, column=start + i, value=lab)
            cell.font, cell.fill = Font(bold=True), HEAD
            cell.alignment = Alignment(horizontal="center")

    m = c.market
    n_hist = len(c.years)
    yrs = c.years

    # --- Company Information
    ws = wb.active
    ws.title = "Company Information"
    title(ws, f"Fundamental Valuation & Investment Research - {c.name}", f"Generated {date.today():%d %b %Y}")
    info = [("Company", c.name, GREEN), ("NSE symbol", c.symbol, GREEN), ("Sector", c.sector, GREEN), ("Industry", c.industry, GREEN),
            ("Statement basis", c.basis, GREEN), ("Latest financial year", c.latest_year, GREEN), ("Currency / unit", f"{c.currency} / {c.unit}", GREEN),
            ("Data source", "Screener.in data held in the application's existing database (data/details)", GREEN),
            ("Last updated (UTC)", c.fetched_at[:16].replace("T", " "), GREEN), ("Current share price (INR)", m.get("price"), GREEN),
            ("Market capitalisation (INR Cr)", m.get("mcap"), GREEN), ("Basic shares (Cr) = market cap / price", m.get("shares"), GREEN),
            ("Diluted shares used (Cr)", R["shares"], BLUE), ("Business category", R["category"], BLACK),
            ("Data quality indicator", f"{R['dq']['label']} ({R['dq']['score']}/100) - heuristic, not a probability", BLACK)]
    for i, (k, v, col) in enumerate(info, start=4):
        put(ws, f"A{i}", k, bold=True)
        put(ws, f"B{i}", v, col)
    ws.column_dimensions["B"].width = 60
    r = 4 + len(info) + 1
    put(ws, f"A{r}", "Colour legend", bold=True)
    put(ws, f"A{r + 1}", "Green = value from existing database", GREEN)
    put(ws, f"A{r + 2}", "Blue = manual input / analyst assumption (editable)", BLUE)
    put(ws, f"A{r + 3}", "Black = formula or model output")
    r += 5
    header(ws, r, ["Section", "Required field", "Source", "Status"])
    for i, (sec, fld, src, st, _w) in enumerate(R["mapping"], start=r + 1):
        for j, val in enumerate((sec, fld, src, st)):
            ws.cell(row=i, column=1 + j, value=val)
    for col in "CD":
        ws.column_dimensions[col].width = 42

    # --- Historical Financials (database values + formulas for derived rows)
    ws = wb.create_sheet("Historical Financials")
    title(ws, f"Historical financials ({RS} Cr)", "Green = database; black = formulas")
    header(ws, 3, ["Particulars"] + yrs)
    labels = [("Revenue", "revenue"), ("EBITDA (operating profit, excl. other income)", "operating_profit"), ("Other income", "other_income"),
              ("Depreciation & amortisation", "depreciation"), ("Finance cost (interest)", "interest"), ("Profit before tax", "pbt"),
              ("Tax %", "tax_pct"), ("Profit after tax (attributable)", "pat"), ("EPS (INR)", "eps"), ("Dividend payout %", "payout_pct"),
              ("Equity capital", "equity_capital"), ("Reserves", "reserves"), ("Borrowings", "borrowings"), ("Fixed assets", "fixed_assets"),
              ("CWIP", "cwip"), ("Investments", "investments"), ("Total assets", "total_assets"), ("Cash from operations", "cfo"),
              ("Cash from investing", "cfi"), ("Cash from financing", "cff"), ("Cash & equivalents (live schedule)", "cash"),
              ("Inventory (live schedule)", "inventory"), ("Trade receivables (live schedule)", "receivables"), ("Trade payables (live schedule)", "payables"),
              ("Capex - fixed assets purchased (live schedule)", "capex_gross")]
    rowmap = {}
    for i, (lab, key) in enumerate(labels, start=4):
        rowmap[key] = i
        put(ws, f"A{i}", lab)
        for j, y in enumerate(yrs):
            v = c.s.get(key, [None] * n_hist)[j]
            if ok(v):
                put(ws, f"{L(2 + j)}{i}", v, GREEN, "#,##0.00")
    r0 = 4 + len(labels) + 1
    derived = [("EBIT = EBITDA - D&A", lambda col: f"={col}{rowmap['operating_profit']}-{col}{rowmap['depreciation']}"),
               ("EBITDA margin", lambda col: f"=IFERROR({col}{rowmap['operating_profit']}/{col}{rowmap['revenue']},\"\")"),
               ("Net profit margin", lambda col: f"=IFERROR({col}{rowmap['pat']}/{col}{rowmap['revenue']},\"\")"),
               ("Net worth = equity capital + reserves", lambda col: f"={col}{rowmap['equity_capital']}+{col}{rowmap['reserves']}")]
    for k, (lab, fn) in enumerate(derived):
        put(ws, f"A{r0 + k}", lab, bold=True)
        for j in range(n_hist):
            col = L(2 + j)
            put(ws, f"{col}{r0 + k}", fn(col), BLACK, "0.0%" if "margin" in lab else "#,##0.00")
    ws.freeze_panes = "B4"

    # --- Historical Ratios
    ws = wb.create_sheet("Historical Ratios")
    title(ws, "Historical ratios (model output)", "CAPEX / NWC may be estimates - see warnings in the Final Summary sheet")
    t, kinds = RP.ratio_table(R, n_hist)
    header(ws, 3, ["Metric"] + list(t.columns))
    for i, (lab, row) in enumerate(t.iterrows(), start=4):
        put(ws, f"A{i}", lab)
        for j, v in enumerate(row):
            put(ws, f"{L(2 + j)}{i}", v, BLACK, "0.0%" if kinds[i - 4] == "pct" else "#,##0.00")
    ct, ck = RP.cagr_df(R)
    r = 4 + len(t) + 2
    header(ws, r, ["CAGR"] + list(ct.columns))
    for i, (lab, row) in enumerate(ct.iterrows(), start=r + 1):
        put(ws, f"A{i}", lab)
        for j, v in enumerate(row):
            put(ws, f"{L(2 + j)}{i}", v, BLACK, "0.0%")

    # --- FCFF Calculation (formulas)
    ws = wb.create_sheet("FCFF Calculation")
    h = R["hist"]
    title(ws, f"Historical FCFF = EBIT x (1 - t) + D&A - CapEx - change in NWC ({RS} Cr)",
          f"CapEx method: {R['hist_meta']['capex_method']}; NWC method: {R['hist_meta']['nwc_method'] or 'not available'}")
    header(ws, 3, ["Particulars"] + yrs)
    labs = ["EBIT", "Tax rate", "NOPAT = EBIT x (1 - t)", "D&A", "CapEx", "Operating NWC", "Change in NWC", "FCFF"]
    for i, lab in enumerate(labs, start=4):
        put(ws, f"A{i}", lab, bold=(lab == "FCFF"))
    for j in range(n_hist):
        col, prev = L(2 + j), L(1 + j)
        for r_, key, fmt in ((4, "ebit", "#,##0.0"), (5, "tax_rate", "0.0%"), (7, "depreciation", "#,##0.0"), (8, "capex", "#,##0.0"), (9, "nwc", "#,##0.0")):
            if ok(h[key][j]):
                put(ws, f"{col}{r_}", h[key][j], GREEN, fmt)
        put(ws, f"{col}6", f"=IF(ISNUMBER({col}4),{col}4*(1-{col}5),\"\")", BLACK, "#,##0.0")
        if j > 0:
            put(ws, f"{col}10", f"=IF(AND(ISNUMBER({col}9),ISNUMBER({prev}9)),{col}9-{prev}9,\"\")", BLACK, "#,##0.0")
            put(ws, f"{col}11", f"=IF(AND(ISNUMBER({col}6),ISNUMBER({col}7),ISNUMBER({col}8),ISNUMBER({col}10)),{col}6+{col}7-{col}8-{col}10,\"\")", BLACK, "#,##0.0", True)
    ws.freeze_panes = "B4"

    # --- Forecast Assumptions
    ws = wb.create_sheet("Forecast Assumptions")
    title(ws, "Forecast assumptions (blue = editable)", "Changing these cells re-computes the DCF, WACC link and sensitivity sheets")
    a = R["assump"]
    n = inp["n"]
    mode_b = inp["mode"] == "B" and a is not None
    put(ws, "A3", "Mode (A = FCFF growth, B = operating forecast)")
    put(ws, "B3", inp["mode"], BLUE)
    put(ws, "A4", f"Base-year revenue ({RS} Cr)")
    put(ws, "B4", a.base_revenue if mode_b else None, GREEN, "#,##0.0")
    put(ws, "A5", f"Base-year operating NWC ({RS} Cr)")
    nwc0 = (a.nwc0 if ok(a.nwc0) else a.nwc_pct[0] * a.base_revenue) if mode_b else None
    put(ws, "B5", nwc0, GREEN if mode_b and ok(a.nwc0) else BLUE, "#,##0.0")
    put(ws, "A6", "Terminal growth rate")
    put(ws, "B6", inp["g"], BLUE, "0.00%")
    put(ws, "A7", "Forecast years")
    put(ws, "B7", n, BLUE)
    put(ws, "A8", "Terminal value method (gordon / exit_multiple)")
    put(ws, "B8", inp["tv_method"], BLUE)
    put(ws, "A9", "Exit EV/EBITDA multiple")
    put(ws, "B9", inp.get("exit_multiple"), BLUE, "0.0")
    put(ws, "A10", f"Current FCFF - Mode A ({RS} Cr)")
    put(ws, "B10", inp["modeA"]["fcff0"] if inp["mode"] == "A" else None, BLUE, "#,##0.0")
    header(ws, 12, ["Assumption"] + [f"Year {i + 1}" for i in range(n)])
    arows = [("Revenue growth", "revenue_growth"), ("EBIT margin", "ebit_margin"), ("Tax rate on EBIT", "tax_rate"), ("D&A % of revenue", "dep_pct"),
             ("CapEx % of revenue", "capex_pct"), ("Operating NWC % of revenue", "nwc_pct")]
    src = inp["assump"]
    for i, (lab, key) in enumerate(arows, start=13):
        put(ws, f"A{i}", lab)
        if mode_b:
            for j in range(n):
                put(ws, f"{L(2 + j)}{i}", src[key][j], BLUE, "0.0%")
    put(ws, "A19", "FCFF growth - Mode A")
    if inp["mode"] == "A":
        for j in range(n):
            put(ws, f"{L(2 + j)}19", inp["modeA"]["growths"][j], BLUE, "0.0%")
    put(ws, "A21", "EV to equity bridge items", bold=True)
    bi = inp["bridge"]
    items = [("Total debt", bi["debt"], GREEN), ("Lease liabilities (only if NOT in debt)", bi["leases"], BLUE), ("Minority interest", bi["minority"], BLUE),
             ("Preferred equity", bi["preferred"], BLUE), ("Cash & equivalents", bi["cash"], BLUE), ("Non-operating investments", bi["nonop"], BLUE),
             ("Diluted shares (Cr)", R["shares"], BLUE), ("Current share price (INR)", R["price"], GREEN)]
    for i, (lab, v, col) in enumerate(items, start=22):
        put(ws, f"A{i}", lab)
        put(ws, f"B{i}", v, col, "#,##0.00")

    # --- WACC Calculation (formulas)
    ws = wb.create_sheet("WACC Calculation")
    title(ws, "WACC (CAPM) - FCFF must be discounted at WACC, not at the cost of equity")
    w = inp["wacc"]
    rows = [(3, "Risk-free rate", w["rf"], BLUE, "0.00%"), (4, "Beta", w["beta"], BLUE, "0.00"), (5, "Equity risk premium", w["erp"], BLUE, "0.00%"),
            (6, "Additional premium", w.get("extra") or 0, BLUE, "0.00%"), (8, "Pre-tax cost of debt", w["kd"], BLUE, "0.00%"),
            (9, "Tax rate", w["tax"], BLUE, "0.00%"), (11, f"Market value of equity ({RS} Cr)", m.get("mcap"), GREEN, "#,##0"),
            (12, f"Interest-bearing debt incl. leases ({RS} Cr)", inp["bridge"]["debt"] + inp["bridge"]["leases"], GREEN, "#,##0"),
            (16, "Manual WACC override (optional)", w.get("override"), BLUE, "0.00%")]
    for r_, lab, v, col, fmt in rows:
        put(ws, f"A{r_}", lab)
        put(ws, f"B{r_}", v, col, fmt)
    forms = [(7, "Cost of equity = Rf + Beta x ERP + premium", '=IF(COUNT(B3:B5)<3,"enter inputs",B3+B4*B5+B6)', "0.00%"),
             (10, "After-tax cost of debt = Kd x (1 - t)", "=B8*(1-B9)", "0.00%"),
             (13, "Equity weight E/(D+E)", "=B11/(B11+B12)", "0.0%"), (14, "Debt weight D/(D+E)", "=B12/(B11+B12)", "0.0%"),
             (15, "WACC = We x Ke + Wd x Kd(1-t)", '=IF(ISNUMBER(B7),B13*B7+B14*B10,"enter inputs")', "0.00%"),
             (17, "WACC used in DCF", '=IF(ISNUMBER(B16),B16,B15)', "0.00%")]
    for r_, lab, f, fmt in forms:
        put(ws, f"A{r_}", lab, bold=(r_ in (15, 17)))
        put(ws, f"B{r_}", f, BLACK, fmt, bold=(r_ in (15, 17)))
    ws.column_dimensions["B"].width = 18

    # --- DCF Valuation (formulas)
    ws = wb.create_sheet("DCF Valuation")
    FA, WS = "'Forecast Assumptions'!", "'WACC Calculation'!"
    title(ws, f"DCF valuation - FCFF ({RS} Cr)", "All cells are formulas driven by the assumption sheets")
    header(ws, 3, ["Period (t)"] + list(range(1, n + 1)))
    first, last = L(2), L(1 + n)
    names = {4: "Revenue", 5: "Revenue growth", 6: "EBIT", 7: "D&A", 8: "EBITDA", 9: "EBITDA margin", 10: "Tax on EBIT", 11: "NOPAT",
             12: "CapEx", 13: "Operating NWC", 14: "Change in NWC", 15: "FCFF", 16: "Discount factor", 17: "PV of FCFF"}
    for r_, lab in names.items():
        put(ws, f"A{r_}", lab, bold=(r_ in (15, 17)))
    for j in range(n):
        col, prev = L(2 + j), L(1 + j)
        if mode_b:
            put(ws, f"{col}4", f"={FA}$B$4*(1+{FA}{col}13)" if j == 0 else f"={prev}4*(1+{FA}{col}13)", BLACK, "#,##0.0")
            put(ws, f"{col}5", f"={FA}{col}13", BLACK, "0.0%")
            put(ws, f"{col}6", f"={col}4*{FA}{col}14", BLACK, "#,##0.0")
            put(ws, f"{col}7", f"={col}4*{FA}{col}16", BLACK, "#,##0.0")
            put(ws, f"{col}8", f"={col}6+{col}7", BLACK, "#,##0.0")
            put(ws, f"{col}9", f"={col}8/{col}4", BLACK, "0.0%")
            put(ws, f"{col}10", f"={col}6*{FA}{col}15", BLACK, "#,##0.0")
            put(ws, f"{col}11", f"={col}6-{col}10", BLACK, "#,##0.0")
            put(ws, f"{col}12", f"={col}4*{FA}{col}17", BLACK, "#,##0.0")
            put(ws, f"{col}13", f"={col}4*{FA}{col}18", BLACK, "#,##0.0")
            put(ws, f"{col}14", f"={col}13-{FA}$B$5" if j == 0 else f"={col}13-{prev}13", BLACK, "#,##0.0")
            put(ws, f"{col}15", f"={col}11+{col}7-{col}12-{col}14", BLACK, "#,##0.0", True)
        else:
            put(ws, f"{col}15", f"={FA}$B$10*(1+{FA}{col}19)" if j == 0 else f"={prev}15*(1+{FA}{col}19)", BLACK, "#,##0.0", True)
        put(ws, f"{col}16", f"=1/(1+{WS}$B$17)^{col}3", BLACK, "0.0000")
        put(ws, f"{col}17", f"={col}15*{col}16", BLACK, "#,##0.0", True)
    tvf = (f'=IF({FA}B8="gordon",{last}15*(1+B21)/(B22-B21),{FA}B9*{last}8)' if mode_b else f"={last}15*(1+B21)/(B22-B21)")
    summ = [(19, "Sum of PV of FCFF", f"=SUM({first}17:{last}17)", "#,##0.0"), (20, "Final-year FCFF", f"={last}15", "#,##0.0"),
            (21, "Terminal growth rate", f"={FA}B6", "0.00%"), (22, "WACC", f"={WS}B17", "0.00%"), (23, "Terminal value", tvf, "#,##0.0"),
            (24, "PV of terminal value", f"=B23*{last}16", "#,##0.0"), (25, "Enterprise value", "=B19+B24", "#,##0.0"),
            (26, "Terminal value % of EV", "=B24/B25", "0.0%"), (28, "Less: debt", f"={FA}B22", "#,##0.0"),
            (29, "Less: lease liabilities", f"={FA}B23", "#,##0.0"), (30, "Less: minority interest", f"={FA}B24", "#,##0.0"),
            (31, "Less: preferred equity", f"={FA}B25", "#,##0.0"), (32, "Add: cash & equivalents", f"={FA}B26", "#,##0.0"),
            (33, "Add: non-operating investments", f"={FA}B27", "#,##0.0"), (34, "Equity value", "=B25-B28-B29-B30-B31+B32+B33", "#,##0.0"),
            (35, "Diluted shares (Cr)", f"={FA}B28", "#,##0.000"), (36, "Intrinsic value per share (INR)", "=B34/B35", "#,##0.00"),
            (37, "Current market price (INR)", f"={FA}B29", "#,##0.00"), (38, "Margin of safety %", "=(B36-B37)/B36", "0.0%"),
            (39, "Potential upside %", "=(B36-B37)/B37", "0.0%")]
    for r_, lab, f, fmt in summ:
        put(ws, f"A{r_}", lab, bold=r_ in (25, 34, 36))
        put(ws, f"B{r_}", f, BLACK, fmt, bold=r_ in (25, 34, 36))
    ws.column_dimensions["B"].width = 16
    put(ws, "D19", "Python model value / share (check):")
    put(ws, "G19", R["iv"], BLACK, "#,##0.00")

    # --- Sensitivity (live formulas for the Gordon method)
    ws = wb.create_sheet("Sensitivity Analysis")
    title(ws, f"Intrinsic value per share ({RS}): WACC vs terminal growth", "Live formulas (Gordon growth). Edit the blue axis values.")
    sg = R["sens"].get("wacc_g")
    if sg:
        DV = "'DCF Valuation'!"
        header(ws, 3, ["WACC \\ g"] + [""] * len(sg["gs"]))
        for j, g in enumerate(sg["gs"]):
            put(ws, f"{L(2 + j)}3", g, BLUE, "0.00%", True)
        for i, wv in enumerate(sg["waccs"]):
            rr = 4 + i
            put(ws, f"A{rr}", wv, BLUE, "0.00%", True)
            for j in range(len(sg["gs"])):
                col = L(2 + j)
                if inp["tv_method"] == "gordon":
                    f = (f'=IF($A{rr}<={col}$3,"n/a",(SUMPRODUCT({DV}${first}$15:${last}$15/(1+$A{rr})^{DV}${first}$3:${last}$3)'
                         f'+{DV}${last}$15*(1+{col}$3)/($A{rr}-{col}$3)/(1+$A{rr})^{DV}${last}$3'
                         f'-{DV}$B$28-{DV}$B$29-{DV}$B$30-{DV}$B$31+{DV}$B$32+{DV}$B$33)/{DV}$B$35)')
                    put(ws, f"{col}{rr}", f, BLACK, "#,##0.0")
                else:
                    put(ws, f"{col}{rr}", sg["grid"][i][j], BLACK, "#,##0.0")
        if inp["tv_method"] != "gordon":
            put(ws, f"A{5 + len(sg['waccs'])}", "Exit-multiple method: values are static model output.")
        r = 6 + len(sg["waccs"])
    else:
        put(ws, "A3", "Sensitivity not available for the current inputs.")
        r = 5
    for key, label in (("growth_margin", "Revenue growth shift vs EBIT-margin shift (static model values)"),
                       ("fcff_growth_wacc", "FCFF growth vs WACC (static model values)")):
        df = RP.sens_df(R, key)
        if df is not None:
            put(ws, f"A{r}", label, bold=True)
            header(ws, r + 1, [""] + list(df.columns))
            for i, (idx, row) in enumerate(df.iterrows(), start=r + 2):
                put(ws, f"A{i}", idx)
                for j, v in enumerate(row):
                    put(ws, f"{L(2 + j)}{i}", v, BLACK, "#,##0.0")
            r += len(df) + 4

    # --- Relative Valuation
    ws = wb.create_sheet("Relative Valuation")
    title(ws, "Relative valuation", "Peer multiples are analyst-adjustable (blue)")
    md = RP.multiples_df(R)
    header(ws, 3, ["Multiple", "Value", "Note"])
    for i, (idx, row) in enumerate(md.iterrows(), start=4):
        put(ws, f"A{i}", idx)
        put(ws, f"B{i}", row["Value"])
        put(ws, f"C{i}", row["Note"])
    r = 4 + len(md) + 1
    pt = RP.peer_df(R)
    if pt is not None:
        put(ws, f"A{r}", "Peer comparison (EV/EBITDA* = gross debt basis)", bold=True)
        header(ws, r + 1, ["Symbol"] + list(pt.columns))
        for i, (idx, row) in enumerate(pt.iterrows(), start=r + 2):
            put(ws, f"A{i}", idx)
            for j, v in enumerate(row):
                put(ws, f"{L(2 + j)}{i}", v, GREEN)
        r += len(pt) + 4
    ri = R["rel_inputs"]
    put(ws, f"A{r}", "Implied valuation (formulas)", bold=True)
    header(ws, r + 1, ["Method", "Selected multiple", "Per-share metric / EBITDA", "Implied price (INR)"])
    DVf = inp["bridge"]
    sel = R["implied"]
    rr = r + 2
    if "pe" in sel:
        put(ws, f"A{rr}", "P/E x EPS")
        put(ws, f"B{rr}", sel["pe"]["selected"], BLUE, "0.00")
        put(ws, f"C{rr}", ri["eps"], GREEN, "0.00")
        put(ws, f"D{rr}", f"=B{rr}*C{rr}", BLACK, "#,##0.00")
        rr += 1
    if "pb" in sel:
        put(ws, f"A{rr}", "P/B x BVPS")
        put(ws, f"B{rr}", sel["pb"]["selected"], BLUE, "0.00")
        put(ws, f"C{rr}", ri["bvps"], GREEN, "0.00")
        put(ws, f"D{rr}", f"=B{rr}*C{rr}", BLACK, "#,##0.00")
        rr += 1
    if "ev_ebitda" in sel:
        put(ws, f"A{rr}", "EV/EBITDA -> EV -> equity -> per share (cash not added: peer basis)")
        put(ws, f"B{rr}", sel["ev_ebitda"]["selected"], BLUE, "0.00")
        put(ws, f"C{rr}", ri["ebitda"], GREEN, "#,##0.0")
        put(ws, f"D{rr}", f"=(B{rr}*C{rr}-{FA}B22-{FA}B23-{FA}B24-{FA}B25)/{FA}B28", BLACK, "#,##0.00")

    # --- Scenario Analysis
    ws = wb.create_sheet("Scenario Analysis")
    title(ws, "Bear / Base / Bull scenarios (static model output; edit deltas in the app)")
    sd, sk = RP.scenario_df(R)
    if sd is not None:
        header(ws, 3, ["Metric"] + list(sd.columns))
        for i, (lab, row) in enumerate(sd.iterrows(), start=4):
            put(ws, f"A{i}", lab)
            for j, v in enumerate(row):
                put(ws, f"{L(2 + j)}{i}", v, BLACK, "0.0%" if sk[i - 4] == "pct" else "#,##0.00")
        r = 5 + len(sd)
        put(ws, f"A{r}", "Scenario deltas applied", bold=True)
        header(ws, r + 1, ["Scenario", "Revenue growth", "EBIT margin", "NWC % rev", "WACC", "Terminal g"])
        for i, (k, v) in enumerate(inp["deltas"].items(), start=r + 2):
            put(ws, f"A{i}", k)
            for j, key in enumerate(("growth", "margin", "nwc", "wacc", "g")):
                put(ws, f"{L(2 + j)}{i}", v[key], BLUE, "0.0%")
    else:
        put(ws, "A3", "Scenario analysis not available (needs Advanced mode and valid inputs).")

    # --- Final Summary
    ws = wb.create_sheet("Final Summary")
    title(ws, "Final valuation summary", "Model output - not investment advice")
    header(ws, 3, ["Valuation method", "Estimated value / share (INR)"])
    for i, (nme, v) in enumerate(R["final_table"], start=4):
        put(ws, f"A{i}", nme)
        put(ws, f"B{i}", v, BLACK, "#,##0.00")
    r = 5 + len(R["final_table"])
    rng = RP.suitable_range(R, R["category"])
    put(ws, f"A{r}", "Reasoned range (suitable methods only, not an average)", bold=True)
    put(ws, f"B{r}", f"{RP.f_ps(rng['low'])} - {RP.f_ps(rng['high'])}" if rng else "n/a")
    put(ws, f"A{r + 1}", "Margin of safety %")
    put(ws, f"B{r + 1}", R["mos"], BLACK, "0.0")
    put(ws, f"A{r + 3}", "Warnings and data limitations", bold=True)
    for i, wmsg in enumerate(R["warnings"], start=r + 4):
        put(ws, f"A{i}", wmsg)
    ws.column_dimensions["B"].width = 30
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


# ----------------------------------------------------------------------------- PDF
def to_pdf(c, inp, R) -> bytes:
    import matplotlib
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    fdir = matplotlib.get_data_path() + "/fonts/ttf/"
    pdfmetrics.registerFont(TTFont("DejaVu", fdir + "DejaVuSans.ttf"))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", fdir + "DejaVuSans-Bold.ttf"))
    pdfmetrics.registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold", italic="DejaVu", boldItalic="DejaVu-Bold")
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName="DejaVu", fontSize=8.3, leading=11.5)
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName="DejaVu-Bold", fontSize=13, textColor=colors.HexColor("#1f3b63"), spaceBefore=8)
    ttl = ParagraphStyle("t", parent=ss["Title"], fontName="DejaVu-Bold", fontSize=17, textColor=colors.HexColor("#1f3b63"))
    cap = ParagraphStyle("c", parent=body, fontSize=7, textColor=colors.grey)
    cell = ParagraphStyle("cell", parent=body, fontSize=6.5, leading=8)

    def md(text):  # tiny markdown -> reportlab markup
        import re
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        return text.replace("\n", "<br/>")

    story = [Paragraph(f"Fundamental Valuation &amp; Investment Research Report \u2013 {c.name}", ttl),
             Paragraph(f"{c.symbol} | Reporting date {date.today():%d %B %Y} | Financial year {c.latest_year} | {c.basis} statements | {c.unit} | "
                       f"Source: Screener.in data in the application database (updated {c.fetched_at[:10]})", cap), Spacer(1, 0.3 * cm)]
    charts = RP.make_charts(c, R)
    width = A4[0] - 3 * cm
    for sec in RP.build_report(c, inp, R):
        story.append(Paragraph(sec["title"], h1))
        for blk in sec["blocks"]:
            if blk[0] == "p":
                for part in blk[1].split("\n\n"):
                    story.append(Paragraph(md(part), body))
                story.append(Spacer(1, 0.12 * cm))
            elif blk[0] == "table":
                _, df, caption, first = blk
                if df is None:
                    continue
                d = df.reset_index()
                d.columns = [first] + [str(x) for x in d.columns[1:]]
                from xml.sax.saxutils import escape
                data = [[Paragraph(escape(str(x)), cell) for x in d.columns]] + [[Paragraph(escape(str(x)), cell) for x in row] for row in d.values.tolist()]
                ncol = len(d.columns)
                first_w = 4.6 * cm if ncol > 3 else 5.5 * cm
                rest = (width - first_w) / max(ncol - 1, 1)
                t = Table(data, colWidths=[first_w] + [rest] * (ncol - 1), repeatRows=1)
                t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dce6f2")), ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                                       ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
                if caption:
                    story.append(Paragraph(caption, cap))
                story.append(t)
                story.append(Spacer(1, 0.2 * cm))
            elif blk[0] == "chart" and blk[1] in charts:
                story.append(Image(io.BytesIO(charts[blk[1]]), width=width, height=width * (0.32 if blk[1] not in ("growth", "football") else 0.55)))
        if sec["title"].startswith(("5.", "8.")):
            story.append(PageBreak())
    story += [Spacer(1, 0.4 * cm), Paragraph("Disclaimer: this report is generated by a quantitative model from third-party data and the analyst's "
                                           "assumptions. It is not investment advice. Verify all inputs against company filings.", cap)]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("DejaVu", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(1.5 * cm, 0.9 * cm, f"{c.name} \u2013 Valuation & Research")
        canvas.drawRightString(A4[0] - 1.5 * cm, 0.9 * cm, f"Page {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.5 * cm, rightMargin=1.5 * cm, topMargin=1.4 * cm, bottomMargin=1.6 * cm,
                      title=f"Valuation report - {c.name}").build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
