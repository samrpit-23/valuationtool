"""
Streamlit UI for the Valuation & Research module.
Presentation only: inputs are collected here, every calculation lives in valuation/engine.py + pipeline.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from . import exports as X
from . import report as RP
from .data import build_company
from .engine import ok
from .pipeline import compute_all, suggest_inputs, suitable_range

RS = "\u20b9"
NAMES = ["Overview", "Historical", "DCF", "WACC", "Relative", "Scenarios", "Sensitivity", "Other models", "Report", "Risk"]
TAB_LABELS = ["📊 Valuation Overview", "📈 Historical Financial Analysis", "🧮 DCF Valuation", "⚖️ WACC Calculator", "🔁 Relative Valuation",
              "🎭 Scenario Analysis", "🎯 Sensitivity Analysis", "🧩 Other Models", "📄 Detailed Valuation Report", "⚠️ Investment Risk Analysis"]


# ----------------------------------------------------------------------------- data access
@st.cache_data(show_spinner=False)
def _load_detail(path: str, mtime: float) -> dict:
    return json.loads(Path(path).read_text())


@st.cache_data(ttl=3600, show_spinner=False)
def _live(symbol: str, prefer: str, deep: bool) -> dict:
    from scraper import fetch_company

    return fetch_company(symbol, prefer=prefer, deep=deep)


@st.cache_data(show_spinner=False)
def _charts(key: str, _c, _R):
    return RP.make_charts(_c, _R)


# ----------------------------------------------------------------------------- small widgets
def pct_in(label, default, key, step=0.25, help=None, placeholder="enter %"):
    v = st.number_input(label, value=None if default is None else round(default * 100, 4), step=step, format="%.2f",
                        key=key, help=help, placeholder=placeholder)
    return None if v is None else v / 100


def num_in(label, default, key, step=1.0, fmt="%.2f", help=None, minv=None):
    return st.number_input(label, value=None if default is None else float(default), step=step, format=fmt, key=key, help=help,
                           min_value=minv, placeholder="enter value")


def line_bar(df, kind="line", pct=False, title=""):
    df = df.copy()
    order = list(df.index)
    long = df.reset_index(names="Year").melt("Year", var_name="Series", value_name="Value").dropna()
    fmt = ".0%" if pct else ",.1f"
    base = alt.Chart(long, title=title)
    x = alt.X("Year:N", sort=order, title=None, axis=alt.Axis(labelAngle=-45))
    y = alt.Y("Value:Q", title=None, axis=alt.Axis(format=".0%" if pct else ",.0f"))
    tip = ["Year", "Series", alt.Tooltip("Value:Q", format=fmt)]
    if kind == "bar":
        ch = base.mark_bar().encode(x=x, y=y, xOffset="Series:N", color="Series:N", tooltip=tip)
    else:
        ch = base.mark_line(point=True).encode(x=x, y=y, color="Series:N", tooltip=tip)
    st.altair_chart(ch.properties(height=250), width="stretch")


def wide(h, keys, last=10):
    yrs = h["years"][-last:]
    return pd.DataFrame({lab: h[k][-last:] for lab, k in keys.items()}, index=yrs)


def show_table(df, caption=None, first=None):
    if df is None:
        return
    if caption:
        st.caption(caption)
    st.dataframe(df, width="stretch")


def peer_candidates(snap, c):
    if snap is None or not len(snap):
        return []
    same_ind = snap[(snap["Industry"] == c.industry) & (snap["Symbol"] != c.symbol)] if c.industry else snap.iloc[0:0]
    pool = same_ind if len(same_ind) >= 3 else snap[(snap["Sector"] == c.sector) & (snap["Symbol"] != c.symbol)]
    return pool.sort_values("Market Cap", ascending=False)["Symbol"].tolist()


# ----------------------------------------------------------------------------- main entry point
def render(snap: pd.DataFrame, detail_dir: Path):
    st.markdown("### 📑 Valuation & Research")
    st.caption(f"Fundamental valuation workstation. All amounts in {RS} Crore, per-share values in {RS}. "
               "Model output, not investment advice.")

    # ---------------- company selection
    c1, c2, c3 = st.columns([2, 1.3, 1.5])
    q = c1.text_input("Search company name or NSE symbol", key="val_q")
    sectors = ["All"] + sorted(s for s in snap["Sector"].dropna().unique() if s)
    sector = c2.selectbox("Sector", sectors, key="val_sector")
    f = snap if sector == "All" else snap[snap["Sector"] == sector]
    inds = ["All"] + sorted(s for s in f["Industry"].dropna().unique() if s)
    industry = c3.selectbox("Industry", inds, key="val_ind")
    if industry != "All":
        f = f[f["Industry"] == industry]
    if q:
        f = f[f["Symbol"].str.contains(q, case=False, na=False) | f["Name"].str.contains(q, case=False, na=False)]
    if f.empty:
        st.info("No company matches these filters.")
        return
    labels = {r.Symbol: f"{r.Symbol} \u2013 {r.Name}" for r in f.itertuples()}
    sym = st.selectbox(f"Company ({len(f)} match)", list(labels), format_func=labels.get, key="val_company")

    # ---------------- statement basis / data source
    path = Path(detail_dir) / f"{sym}.json"
    detail = _load_detail(str(path), path.stat().st_mtime) if path.exists() else None
    with st.expander("Data source: statement basis and live schedules", expanded=False):
        stored_basis = detail.get("basis", "unknown") if detail else "n/a"
        mode_src = st.radio("Use", [f"Stored database ({stored_basis})", "Live from Screener (consolidated)", "Live from Screener (standalone)"],
                            key=f"src_{sym}", horizontal=False)
        deep = st.checkbox("Also load detailed schedules (cash, receivables, inventory, payables, leases, actual CapEx) - best effort, slower",
                           key=f"deep_{sym}")
        if not mode_src.startswith("Stored") or deep:
            if st.button("Fetch live data", key=f"fetch_{sym}"):
                prefer = "standalone" if "standalone" in mode_src else "consolidated"
                try:
                    with st.spinner("Fetching from Screener (this can take 10-30 seconds)..."):
                        st.session_state[f"live_{sym}"] = _live(sym, prefer, deep)
                except Exception as e:
                    st.error(f"Live fetch failed: {e}")
            if f"live_{sym}" in st.session_state:
                detail = st.session_state[f"live_{sym}"]
                st.success(f"Using live data ({detail.get('basis')}); detailed schedules: {'yes' if detail.get('schedules') else 'no'}.")
            elif not mode_src.startswith("Stored"):
                st.info("Press 'Fetch live data' to switch; until then the stored data is used.")
        else:
            st.session_state.pop(f"live_{sym}", None)
    if detail is None:
        st.error("No stored data for this company. Fetch it live above or run the scraper.")
        return
    try:
        c = build_company(detail)
    except ValueError as e:
        st.error(str(e))
        return

    # ---------------- valuation settings
    with st.expander("⚙️ Valuation settings, capital structure and EV bridge inputs", expanded=True):
        s1, s2, s3, s4 = st.columns(4)
        mode_lbl = s1.radio("Forecast mode", ["B - Advanced operating forecast", "A - Simple FCFF growth"], key=f"mode_{sym}")
        n = s2.slider("Forecast years", 5, 10, 5, key=f"n_{sym}")
        g = pct_in("Terminal growth rate (%)", 0.04, f"g_{sym}", step=0.25, help="Must be lower than WACC.")
        tvm_lbl = s4.radio("Terminal value method", ["Gordon growth", "Exit EV/EBITDA multiple"], key=f"tv_{sym}")
        exit_mult = None
        if tvm_lbl.startswith("Exit"):
            exit_mult = num_in("Exit EV/EBITDA multiple (x)", None, f"exit_{sym}", step=0.5, minv=0.1,
                               help="Used instead of Gordon growth. The two methods are never blended.")
        midcycle = st.checkbox("Cyclical company: use mid-cycle (10-year average) EBIT margin as the starting point", key=f"mid_{sym}")
        inp = suggest_inputs(c, n=n, g=g if ok(g) else 0.04, midcycle=midcycle)
        inp["mode"] = "B" if mode_lbl.startswith("B") else "A"
        inp["tv_method"] = "gordon" if tvm_lbl.startswith("Gordon") else "exit_multiple"
        inp["exit_multiple"] = exit_mult
        if not ok(g):
            st.warning("Enter a terminal growth rate.")
            g = 0.04
        inp["g"] = g

        st.markdown("**Capital structure and EV-to-equity bridge** (database value or default shown; edit any item)")
        b1, b2, b3 = st.columns(3)
        bk = f"{sym}_{'live' if f'live_{sym}' in st.session_state else 'db'}"   # stable across n / mid-cycle changes
        kp = f"{bk}_{n}_{int(midcycle)}"                                                   # only for per-year tables
        br = inp["bridge"]
        br["debt"] = b1.number_input(f"Total borrowings ({RS} Cr) - database", value=float(br["debt"]), min_value=0.0, key=f"{bk}_debt")
        br["leases"] = b2.number_input(f"Lease liabilities not inside borrowings ({RS} Cr)", value=float(br["leases"]), min_value=0.0, key=f"{bk}_lease",
                                       help="Screener's Borrowings normally already include Ind AS 116 leases. Leave 0 unless you know they are separate.")
        br["minority"] = b3.number_input(f"Minority interest ({RS} Cr) - manual", value=0.0, min_value=0.0, key=f"{bk}_mi")
        b4, b5, b6 = st.columns(3)
        br["preferred"] = b4.number_input(f"Preferred equity ({RS} Cr)", value=0.0, min_value=0.0, key=f"{bk}_pref")
        br["cash"] = b5.number_input(f"Cash & equivalents ({RS} Cr) - " + ("live schedule" if c.deep else "manual, not in stored data"),
                                     value=float(br["cash"]), min_value=0.0, key=f"{bk}_cash")
        br["nonop"] = b6.number_input(f"Non-operating investments ({RS} Cr)", value=0.0, min_value=0.0, key=f"{bk}_nonop",
                                      help=f"Balance-sheet investments are {RS}{c.latest('investments') or 0:,.0f} Cr in total; enter only the portion that is non-operating.")
        basic = c.market.get("shares")
        inp["shares"] = st.number_input("Fully diluted shares outstanding (Cr) - default is basic shares (market cap / price); enter diluted count if known",
                                        value=float(basic) if ok(basic) else None, min_value=0.0001, format="%.4f", key=f"{bk}_shares")

    # ---------------- header
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Statements", f"{c.basis}")
    h2.metric("Financial year", c.latest_year)
    h3.metric("Currency / unit", f"{c.currency} / Cr")
    age = c.age_days
    h4.metric("Last updated", c.fetched_at[:10], delta=f"{age:.0f} days old" if age is not None else None, delta_color="off")
    if c.category != "general":
        st.warning("This is a financial-sector company: FCFF and EV/EBITDA are not appropriate. Use P/B, Residual Income and DDM (Other Models tab).")

    tabs = dict(zip(NAMES, st.tabs(TAB_LABELS)))
    a0 = inp["assump"]

    # ================= PASS 1: inputs (code order != visual order) =================
    with tabs["WACC"]:
        st.markdown("**Inputs** - risk-free rate, beta and equity risk premium are never assumed silently; enter them or use the clearly labelled placeholders.")
        wk = f"{bk}_w"

        def _fill():
            st.session_state["val_rf"], st.session_state[f"{wk}_beta"], st.session_state["val_erp"] = 7.0, 1.0, 5.5

        w1, w2, w3 = st.columns(3)
        with w1:
            rf = pct_in("Risk-free rate (%)", None, "val_rf", help="e.g. current 10-year government bond yield. Kept when you switch company.")
        with w2:
            beta = num_in("Stock beta", None, f"{wk}_beta", step=0.05, minv=0.0, help="Not in the database - enter from your data vendor.")
        with w3:
            erp = pct_in("Equity risk premium (%)", None, "val_erp", help="Market-wide input; kept when you switch company.")
        st.button("Fill ILLUSTRATIVE placeholders (7 % / 1.0 / 5.5 % - not market data)", on_click=_fill, key=f"{wk}_fillbtn")
        w4, w5, w6, w7 = st.columns(4)
        with w4:
            kd = pct_in("Pre-tax cost of debt (%)", inp["wacc"]["kd"], f"{wk}_kd", help="Default = interest / average borrowings (derived from financials).")
        with w5:
            tax = pct_in("Corporate tax rate (%)", inp["wacc"]["tax"], f"{wk}_tax", help="Default = 3-year average effective rate (bounded 15-35 %).")
        with w6:
            extra = pct_in("Additional premium (%)", 0.0, f"{wk}_extra", help="e.g. size or company-specific premium")
        with w7:
            override = pct_in("Manual WACC override (%)", None, f"{wk}_ovr", placeholder="optional")
        inp["wacc"].update(rf=rf, beta=beta, erp=erp, kd=kd, tax=tax, extra=extra or 0.0, override=override)
        inp["confirmed_market_inputs"] = st.checkbox("I have verified the risk-free rate, beta and equity risk premium against current market data", key=f"{wk}_conf")

    with tabs["DCF"]:
        a_key = f"{kp}_{inp['mode']}_assump"   # resets when the number of years changes
        if inp["mode"] == "B":
            st.markdown("**Advanced operating forecast** - one value per forecast year (editable). Starting values are suggestions from the company's own history "
                        "(growth fades toward terminal growth); history is not assumed to continue.")
            df = pd.DataFrame({
                "Revenue growth %": [x * 100 for x in a0["revenue_growth"]], "EBIT margin %": [x * 100 for x in a0["ebit_margin"]],
                "Tax rate %": [x * 100 for x in a0["tax_rate"]], "D&A % of revenue": [x * 100 for x in a0["dep_pct"]],
                "CapEx % of revenue": [x * 100 for x in a0["capex_pct"]], "NWC % of revenue": [x * 100 for x in a0["nwc_pct"]]},
                index=[f"Year {i + 1}" for i in range(n)]).T
            ed = st.data_editor(df, key=a_key, width="stretch",
                                column_config={col: st.column_config.NumberColumn(format="%.2f") for col in df.columns})
            keys = ["revenue_growth", "ebit_margin", "tax_rate", "dep_pct", "capex_pct", "nwc_pct"]
            for k, (_, row) in zip(keys, ed.iterrows()):
                a0[k] = [None if pd.isna(v) else v / 100 for v in row.tolist()]
            st.caption(f"Base-year revenue {RS}{a0['base_revenue']:,.1f} Cr (FY {c.latest_year}); operating NWC at base year: "
                       f"{('%.1f' % a0['nwc0']) if ok(a0['nwc0']) else 'n/a (uses Year-1 NWC % x base revenue)'}. " + " ".join(inp["notes"]))
        else:
            st.markdown("**Simple FCFF growth method** - enter the current FCFF and the expected growth for each year.")
            fc0 = num_in(f"Current FCFF ({RS} Cr)", inp["modeA"]["fcff0"], f"{bk}_fcff0", step=10.0,
                         help="Default = average of the last three years' historical FCFF (when positive). Blank means history could not provide it.")
            dfa = pd.DataFrame({"FCFF growth %": [x * 100 for x in inp["modeA"]["growths"]]}, index=[f"Year {i + 1}" for i in range(n)]).T
            eda = st.data_editor(dfa, key=a_key, width="stretch")
            inp["modeA"]["fcff0"] = fc0
            inp["modeA"]["growths"] = [None if pd.isna(v) else v / 100 for v in eda.iloc[0].tolist()]

    with tabs["Scenarios"]:
        if inp["mode"] == "B":
            st.markdown("**Scenario adjustments** (percentage points added to the base-case assumptions; all editable). No probabilities are assumed.")
            d0 = pd.DataFrame({k: [v[x] * 100 for x in ("growth", "margin", "nwc", "wacc", "g")] for k, v in inp["deltas"].items()},
                              index=["Revenue growth (pp)", "EBIT margin (pp)", "NWC % of revenue (pp)", "WACC (pp)", "Terminal growth (pp)"])
            ed = st.data_editor(d0, key=f"{bk}_deltas", width="stretch",
                                column_config={c_: st.column_config.NumberColumn(format="%.2f") for c_ in d0.columns})
            for k in inp["deltas"]:
                col = ed[k].tolist()
                inp["deltas"][k] = dict(zip(("growth", "margin", "nwc", "wacc", "g"), [(0.0 if pd.isna(v) else v / 100) for v in col]))
        else:
            st.info("Scenario analysis needs the Advanced operating forecast (Mode B).")

    with tabs["Sensitivity"]:
        custom = st.checkbox("Use custom sensitivity ranges", key=f"{bk}_cust")
        if custom:
            x1, x2, x3, y1, y2, y3 = st.columns(6)
            wl = x1.number_input("WACC from %", value=9.0, step=0.5, key=f"{bk}_wl")
            wh = x2.number_input("WACC to %", value=13.0, step=0.5, key=f"{bk}_wh")
            ws_ = x3.number_input("WACC step %", value=1.0, min_value=0.1, step=0.1, key=f"{bk}_ws")
            gl = y1.number_input("g from %", value=3.0, step=0.5, key=f"{bk}_gl")
            gh = y2.number_input("g to %", value=5.0, step=0.5, key=f"{bk}_gh")
            gs_ = y3.number_input("g step %", value=0.5, min_value=0.1, step=0.1, key=f"{bk}_gs")
            inp["sens"]["waccs"] = [(wl + i * ws_) / 100 for i in range(int(round((wh - wl) / ws_)) + 1)][:15]
            inp["sens"]["gs"] = [(gl + i * gs_) / 100 for i in range(int(round((gh - gl) / gs_)) + 1)][:15]

    with tabs["Relative"]:
        cands = peer_candidates(snap, c)
        name_of = dict(zip(snap["Symbol"], snap["Name"]))
        default_peers = cands[:8]
        picked = st.multiselect("Comparable companies (same industry/sector; add or remove)", options=sorted(set(cands) | set(default_peers)),
                                default=default_peers, format_func=lambda s: f"{s} \u2013 {name_of.get(s, '')}", key=f"{bk}_peers")
        inp["peers"]["symbols"] = picked
        p1, p2, p3 = st.columns(3)
        pe_sel = num_in("Selected P/E multiple (blank = peer median)", None, f"{bk}_pe_sel", step=0.5, minv=0.1)
        ev_sel = num_in("Selected EV/EBITDA multiple (blank = peer median)", None, f"{bk}_ev_sel", step=0.5, minv=0.1)
        pb_sel = num_in("Selected P/B multiple (blank = peer median)", None, f"{bk}_pb_sel", step=0.1, minv=0.1)
        inp["peers"].update(pe=pe_sel, ev_ebitda=ev_sel, pb=pb_sel)

    with tabs["Other models"]:
        o = inp["other"]
        st.markdown("**Model inputs** (Ke-based models use the cost of equity from the WACC tab)")
        m1, m2, m3 = st.columns(3)
        o["ddm_g"] = pct_in("Stable / dividend growth rate (%)", o["ddm_g"], f"{bk}_ddmg") or 0.0
        o["ddm_g1"] = pct_in("High-growth-stage dividend growth (%)", o["ddm_g1"], f"{bk}_ddmg1") or 0.0
        o["ddm_years"] = m3.number_input("High-growth years", 1, 15, 5, key=f"{bk}_ddmy")
        m4, m5, m6 = st.columns(3)
        with m4:
            o["payout"] = pct_in("Dividend payout ratio (%)", o["payout"], f"{bk}_pay", help="Used by the residual income model.")
        with m5:
            o["rim_roe_start"] = pct_in("Starting ROE (%) - fades toward Ke", o["rim_roe_start"], f"{bk}_roe")
        with m6:
            o["other_income"] = num_in(f"Other income held flat in FCFE ({RS} Cr)", o["other_income"], f"{bk}_oi", step=5.0, help="Set 0 to exclude.")
        st.markdown("**Asset-based valuation: manual adjustments to book value** (positive = asset worth more than book)")
        n1, n2, n3 = st.columns(3)
        o["nav_adj"] = {"Fixed assets revaluation": n1.number_input(f"Fixed assets revaluation ({RS} Cr)", value=0.0, key=f"{bk}_nav1"),
                        "Investments to market value": n2.number_input(f"Investments to market ({RS} Cr)", value=0.0, key=f"{bk}_nav2"),
                        "Other adjustments": n3.number_input(f"Other ({RS} Cr)", value=0.0, key=f"{bk}_nav3")}
        st.markdown("**Sum-of-the-parts** (segment data is not in the database - enter it manually)")
        seg0 = pd.DataFrame({"name": pd.Series(dtype=str), "revenue": pd.Series(dtype=float), "ebitda": pd.Series(dtype=float),
                             "ebit": pd.Series(dtype=float), "method": pd.Series(dtype=str), "multiple": pd.Series(dtype=float)})
        seg = st.data_editor(seg0, num_rows="dynamic", key=f"{bk}_sotp", width="stretch",
                             column_config={"method": st.column_config.SelectboxColumn(options=["EV/EBITDA", "EV/EBIT", "EV/Sales"], required=True)})
        o["segments"] = [{k: (None if pd.isna(v) else v) for k, v in r.items()} for r in seg.to_dict("records") if r.get("name")]
        q1, q2 = st.columns(2)
        o["corp_cost"] = q1.number_input(f"Unallocated corporate costs ({RS} Cr)", value=0.0, min_value=0.0, key=f"{bk}_cc")
        o["corp_multiple"] = q2.number_input("Multiple applied to corporate costs (x)", value=8.0, min_value=0.0, key=f"{bk}_cm")

    with tabs["Overview"]:
        r1, r2 = st.columns(2)
        inp["ret"]["years"] = int(r1.number_input("Holding period (years) for return analysis", 1, 15, 3, key=f"{bk}_hold"))
        inp["ret"]["future_value"] = num_in(f"Estimated future share price after the holding period ({RS})", None, f"{bk}_fv", step=10.0, minv=0.01)

    # ================= COMPUTE =================
    R = compute_all(c, inp, snap)
    price = R["price"]

    # ================= PASS 2: outputs =================
    with tabs["Overview"]:
        m = c.market
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Market price", RP.f_ps(price))
        k2.metric("Intrinsic value (selected case)", RP.f_ps(R["iv"]))
        k3.metric("Margin of safety", f"{R['mos']:.1f}%" if ok(R["mos"]) else "\u2013")
        k4.metric("Potential upside / downside", f"{R['upside']:.1f}%" if ok(R["upside"]) else "\u2013")
        k5, k6, k7, k8 = st.columns(4)
        k5.metric("Market cap", f"{RS}{m.get('mcap') or 0:,.0f} Cr")
        k6.metric("P/E (trailing)", RP.f_x(R["multiples"].get("pe")))
        k7.metric("EV/EBITDA", RP.f_x(R["multiples"].get("ev_ebitda")))
        k8.metric("WACC used", f"{R['wacc_used']:.2%}" if ok(R["wacc_used"]) else "\u2013")
        if R["errors"].get("dcf") and R["iv"] is None:
            st.error(f"DCF value not available: {R['errors']['dcf']}")
        if inp["mode"] == "B" and R["scenarios"]:
            cols = st.columns(3)
            for col, k in zip(cols, ("Bear", "Base", "Bull")):
                s = R["scenarios"].get(k, {})
                col.metric(f"{k} case", RP.f_ps(s.get("per_share")), delta=f"{s['mos']:.1f}% margin of safety" if ok(s.get("mos")) else None, delta_color="off")
        rng = suitable_range(R, R["category"])
        if rng:
            st.info(f"**Reasoned range from the methods suited to this business:** {RP.f_ps(rng['low'])} \u2013 {RP.f_ps(rng['high'])} "
                    f"({', '.join(rng['methods'])}). Methods are not averaged blindly.")
        st.markdown(f"**Data quality indicator: {R['dq']['label']} ({R['dq']['score']}/100)** \u2013 {R['dq']['note']}")
        if R["return"]:
            st.markdown(f"**Expected annualised return** to the entered future price: price CAGR {R['return']['price_cagr']:.1%} + dividend yield "
                        f"{R['return']['dividend_yield']:.1%} = about {R['return']['total']:.1%} per year.")
        if ok(m.get("low52")) and ok(m.get("high52")):
            st.caption(f"52-week price range {RP.f_ps(m['low52'])} \u2013 {RP.f_ps(m['high52'])} (historical P/E range is not stored).")
        st.markdown("**Method suitability for this business type**")
        st.dataframe(pd.DataFrame({"Suitability": R["suitability"]}), width="stretch")
        st.dataframe(RP.summary_df(R), width="stretch")
        if R["warnings"]:
            with st.expander(f"⚠️ Warnings and data limitations ({len(R['warnings'])})", expanded=True):
                for wmsg in R["warnings"]:
                    st.markdown(f"- {wmsg}")
        with st.expander("Field mapping: required fields vs existing database"):
            st.dataframe(pd.DataFrame(R["mapping"], columns=["Section", "Required field", "Source in database", "Status", "Weight"]).drop(columns="Weight"),
                         width="stretch", hide_index=True)

    with tabs["Historical"]:
        h = R["hist"]
        v = R["verdict"]
        st.markdown(RP.hist_summary(R))
        ht, hk = RP.history_table(R)
        show_table(RP.fmt_df(ht, hk), f"{RS} Cr unless stated")
        ct, ck = RP.cagr_df(R)
        show_table(RP.fmt_df(ct, ck), "Compound annual growth rates")
        rt, rk = RP.ratio_table(R)
        show_table(RP.fmt_df(rt, rk), "Historical ratios")
        ft, fk = RP.fcff_hist_table(R)
        show_table(RP.fmt_df(ft, fk), f"Historical FCFF = EBIT x (1 - t) + D&A - CapEx - change in NWC (CapEx: {R['hist_meta']['capex_method']}; NWC: {R['hist_meta']['nwc_method'] or 'n/a'})")
        g1, g2 = st.columns(2)
        with g1:
            line_bar(wide(h, {"Revenue": "revenue"}), "bar", title="Revenue")
            line_bar(wide(h, {"PAT": "pat"}), "bar", title="Profit after tax")
            line_bar(wide(h, {"EBITDA margin": "ebitda_margin", "EBIT margin": "ebit_margin", "Net margin": "net_margin"}), "line", True, "Margin trend")
            line_bar(wide(h, {"CFO": "cfo", "PAT": "pat"}), "bar", title="Operating cash flow vs PAT")
            line_bar(wide(h, {"Borrowings": "borrowings", "Net debt": "net_debt"}), "bar", title="Debt and net debt")
        with g2:
            line_bar(wide(h, {"EBITDA": "ebitda"}), "bar", title="EBITDA")
            line_bar(wide(h, {"EPS": "eps"}), "bar", title="EPS")
            line_bar(wide(h, {"ROE": "roe", "ROCE": "roce", "ROIC": "roic"}), "line", True, "Returns on capital")
            line_bar(wide(h, {"FCFF": "fcff", "FCF (CFO - CapEx)": "fcf"}), "bar", title="Free cash flow trend")

    with tabs["DCF"]:
        if R["errors"].get("forecast"):
            st.error(R["errors"]["forecast"])
        p = R["primary"]
        if p:
            st.caption(f"Discount rate = WACC ({p['wacc']:.2%}); FCFF is never discounted at the cost of equity. Terminal method: "
                       f"{'Gordon growth' if p['tv_method'] == 'gordon' else 'Exit EV/EBITDA multiple'} (not blended).")
            ft, fk = RP.forecast_table(R)
            show_table(RP.fmt_df(ft, fk), f"Projected FCFF and present values ({RS} Cr)")
            d1, d2, d3, d4 = st.columns(4)
            d1.metric("PV of forecast FCFF", f"{RS}{p['pv_sum']:,.0f} Cr")
            d2.metric("PV of terminal value", f"{RS}{p['pv_tv']:,.0f} Cr")
            d3.metric("Enterprise value", f"{RS}{p['ev']:,.0f} Cr")
            d4.metric("Terminal value % of EV", f"{p['tv_share']:.0%}" if p["tv_share"] is not None else "\u2013")
            st.markdown("**Enterprise value to equity value bridge**")
            st.dataframe(RP.bridge_table(R), width="stretch")
            for wmsg in p["warnings"]:
                st.warning(wmsg)
            if R["tv_crosscheck"]:
                x = R["tv_crosscheck"]
                st.caption(f"Cross-check with the other terminal method: {RP.f_ps(x['per_share'])} per share (informational; not combined).")
            if "implied_exit_multiple" in p:
                st.caption(f"Gordon terminal value implies an exit EV/EBITDA of {p['implied_exit_multiple']:.1f}x.")
        else:
            st.error(f"DCF not available: {R['errors'].get('dcf')}")
        with st.expander("Historical FCFF working (step by step)"):
            st.markdown(RP.build_report(c, inp, R)[4]["blocks"][0][1])

    with tabs["WACC"]:
        wt = RP.wacc_table(R)
        if wt is not None:
            st.dataframe(wt, width="stretch")
        else:
            st.error(R["errors"]["wacc"])
        st.info("FCFF must be discounted using WACC, not the cost of equity. Equity cash flows (FCFE, dividends, residual income) use the cost of equity. "
                "Weights use market value of equity and interest-bearing debt (leases included when entered).")

    with tabs["Relative"]:
        st.markdown("**Company multiples (trailing)**")
        st.dataframe(RP.multiples_df(R), width="stretch")
        pt = RP.peer_df(R)
        if pt is not None and len(pt):
            st.markdown("**Comparable-company table**")
            st.dataframe(pt, width="stretch")
            st.caption("EV/EBITDA* for peers = (market cap + gross borrowings) / EBITDA; cash is not deducted because it is not stored for peers. "
                       f"Target on the same basis: {RP.f_x(R['target_ev_ebitda_peer_basis'])}.")
            rows = []
            for key, lab in (("pe", "P/E"), ("ev_ebitda", "EV/EBITDA*"), ("pb", "P/B")):
                s = R["peer_stats"].get(key)
                i = R["implied"].get(key)
                if s:
                    rows.append({"Multiple": lab, "n": s["n"], "Mean": s["mean"], "Median": s["median"], "P25": s["p25"], "P75": s["p75"],
                                 "Selected": i["selected"], "Implied price": i["price"], "Range low": i["low"], "Range high": i["high"]})
            if rows:
                st.dataframe(pd.DataFrame(rows).set_index("Multiple").style.format("{:,.2f}", na_rep="n/a"), width="stretch")
                st.caption("Implied price = multiple x per-share metric. For EV/EBITDA the implied EV is converted to equity (less debt/claims, cash not added on the peer basis) "
                           "and then to a per-share value. Negative or missing earnings are excluded from peer statistics.")
        else:
            st.info("Select peers to see the comparison. If peer EV/EBITDA is blank, run the scrape workflow once to add the new columns.")

    with tabs["Scenarios"]:
        sd, sk = RP.scenario_df(R)
        if sd is not None:
            st.dataframe(RP.fmt_df(sd, sk), width="stretch")
            vals = pd.DataFrame({"Scenario": ["Bear", "Base", "Bull"], "Value per share": [R["scenarios"][k]["per_share"] for k in ("Bear", "Base", "Bull")]})
            ch = alt.Chart(vals).mark_bar().encode(x=alt.X("Scenario:N", sort=["Bear", "Base", "Bull"]), y="Value per share:Q", tooltip=["Scenario", alt.Tooltip("Value per share:Q", format=",.1f")])
            if ok(price):
                ch = ch + alt.Chart(pd.DataFrame({"y": [price]})).mark_rule(color="red", strokeDash=[4, 4]).encode(y="y:Q")
            st.altair_chart(ch.properties(height=230), width="stretch")
            st.caption("The red line is the current market price. The bull case is not assumed to be more likely than the base case.")
        elif inp["mode"] == "B":
            for k, s in R["scenarios"].items():
                if "error" in s:
                    st.error(f"{k}: {s['error']}")

    with tabs["Sensitivity"]:
        sg = RP.sens_df(R)
        if sg is not None:
            st.markdown(f"**Intrinsic value per share ({RS}): WACC vs terminal growth** (highlighted cell = base assumptions)")
            sw = R["sens"]["wacc_g"]
            bi = min(range(len(sw["waccs"])), key=lambda i: abs(sw["waccs"][i] - R["wacc_used"]))
            bj = min(range(len(sw["gs"])), key=lambda j: abs(sw["gs"][j] - inp["g"]))
            st.dataframe(sg.style.format("{:,.1f}", na_rep="n/a").apply(lambda d: pd.DataFrame(
                [["background-color:#ffe08a;font-weight:bold" if (i == bi and j == bj) else "" for j in range(d.shape[1])] for i in range(d.shape[0])],
                index=d.index, columns=d.columns), axis=None), width="stretch")
            st.caption("Cells show n/a where WACC is not above terminal growth.")
        else:
            st.info("Sensitivity needs a valid forecast and WACC.")
        for key, lab in (("growth_margin", "Revenue growth shift vs EBIT-margin shift"), ("fcff_growth_wacc", "FCFF growth vs WACC")):
            df = RP.sens_df(R, key)
            if df is not None:
                st.markdown(f"**{lab}** ({RS} per share)")
                st.dataframe(df.style.format("{:,.1f}", na_rep="n/a"), width="stretch")

    with tabs["Other models"]:
        o = R["other"]
        st.dataframe(pd.DataFrame({"Suitability": R["suitability"]}), width="stretch")
        if o.get("note"):
            st.info(o["note"])
        if o.get("fcfe"):
            st.markdown(f"**FCFE valuation** (discounted at cost of equity {R['ke']:.2%}; result is equity value, no net-debt deduction): "
                        f"**{RP.f_ps(o['fcfe']['per_share'])}** per share, equity {RS}{o['fcfe']['equity']:,.0f} Cr")
        elif o.get("fcfe_err"):
            st.caption(f"FCFE: {o['fcfe_err']}")
        if o.get("ddm1"):
            st.markdown(f"**Dividend discount model:** single-stage {RP.f_ps(o['ddm1']['value'])} (D1 = {o['ddm1']['d1']:.2f}); "
                        f"two-stage {RP.f_ps(o['ddm2']['value']) if o.get('ddm2') else 'n/a'}. DPS source: {R['rel_inputs']['dps_src']}.")
        for k in ("ddm1_err", "ddm2_err"):
            if o.get(k):
                st.caption(f"DDM: {o[k]}")
        if o.get("rim"):
            st.markdown(f"**Residual income model:** equity {RS}{o['rim']['equity']:,.0f} Cr = {RP.f_ps(o['rim']['per_share'])} per share.")
        elif o.get("rim_err"):
            st.caption(f"Residual income: {o['rim_err']}")
        if o.get("nav"):
            nv = o["nav"]
            st.markdown(f"**Asset-based:** book net worth {RS}{nv['nav']:,.0f} Cr (BVPS {RP.f_ps(nv['bvps'])}); adjusted NAV {RS}{nv['adjusted_nav']:,.0f} Cr "
                        f"(adjusted BVPS {RP.f_ps(nv['adjusted_bvps'])}).")
        if o.get("sotp"):
            st.markdown(f"**SOTP:** EV {RS}{o['sotp']['ev']:,.0f} Cr \u2192 {RP.f_ps(o['sotp']['per_share'])} per share.")
            st.dataframe(pd.DataFrame(o["sotp"]["segments"]), width="stretch", hide_index=True)
        elif o.get("sotp_err"):
            st.error(o["sotp_err"])

    with tabs["Report"]:
        st.markdown(f"## Fundamental Valuation & Investment Research Report \u2013 {c.name}")
        key = f"{sym}|{c.fetched_at}|{R['iv']}|{R['wacc_used']}|{[round(v, 1) if ok(v) else 0 for _, v in R['final_table']]}"
        charts = _charts(key, c, R)
        for sec in RP.build_report(c, inp, R):
            st.markdown(f"### {sec['title']}")
            for blk in sec["blocks"]:
                if blk[0] == "p":
                    st.markdown(blk[1])
                elif blk[0] == "table" and blk[1] is not None:
                    if blk[2]:
                        st.caption(blk[2])
                    st.dataframe(blk[1], width="stretch")
                elif blk[0] == "chart" and blk[1] in charts:
                    st.image(charts[blk[1]], width="stretch")
        st.divider()
        st.markdown("#### Export")
        if st.button("Prepare PDF, Excel and CSV exports", key=f"{bk}_prep"):
            with st.spinner("Building files..."):
                try:
                    st.session_state[f"exp_{sym}"] = {"pdf": X.to_pdf(c, inp, R), "xlsx": X.to_excel(c, inp, R), "csv": X.to_csv_zip(c, inp, R)}
                except Exception as e:  # keep the page alive on export problems
                    st.error(f"Export failed: {e}")
        ex = st.session_state.get(f"exp_{sym}")
        if ex:
            e1, e2, e3 = st.columns(3)
            e1.download_button("⬇️ PDF report", ex["pdf"], f"{sym}_valuation_report.pdf", "application/pdf")
            e2.download_button("⬇️ Excel model (with formulas)", ex["xlsx"], f"{sym}_valuation_model.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            e3.download_button("⬇️ CSV tables (zip)", ex["csv"], f"{sym}_calculation_tables.zip", "application/zip")
            st.caption("Exports reflect the inputs at the moment you pressed the button; press it again after changing assumptions.")

    with tabs["Risk"]:
        st.dataframe(RP.risk_table(R, c, inp), width="stretch")
        if R["verdict"]["flags"]:
            for fl in R["verdict"]["flags"]:
                st.warning(fl)
        if R["reverse"]:
            rv = R["reverse"]
            st.markdown(f"**Reverse DCF:** at today's price of {RP.f_ps(price)}, with all other assumptions unchanged, the market is pricing in average revenue growth of about "
                        f"**{rv['avg_growth_needed']:.1%}** per year versus {rv['base_avg_growth']:.1%} in your base assumptions.")
        st.caption("Only risks that the available data can evidence are rated; the rest are marked 'Requires further investigation'. "
                   "This analysis is a model output, not investment advice.")
