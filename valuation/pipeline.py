"""
Pipeline - turns (CompanyData + user inputs) into a complete valuation result dict.
No Streamlit here, so the whole chain can be unit-tested and re-used by the exports.
"""
from __future__ import annotations

import copy

import pandas as pd

from . import engine as E
from .data import CompanyData, dps_latest, mapping_table, quality_items
from .engine import Assumptions, Bridge, ScenarioDelta, ValuationError, ok


# ----------------------------------------------------------------------------- suggestions from history
def _avg_last(vals, n=3):
    v = [x for x in vals if ok(x)][-n:]
    return sum(v) / len(v) if v else None


def suggest_inputs(c: CompanyData, n=5, g=0.04, midcycle=False, price_placeholder=None):
    """Editable starting values derived from the company's own history (never silently final)."""
    h, meta = E.derive_history(c.s)
    ct = E.cagr_table(h)
    notes = []
    cands = [x for x in (ct["revenue"][3], ct["revenue"][5]) if x is not None]
    if cands:
        start_g = max(-0.05, min(0.20, sum(cands) / len(cands)))
    else:
        start_g = 0.05
        notes.append("No revenue CAGR available - 5 % growth is a placeholder.")
    growth = [start_g + (g - start_g) * t / (n - 1) if n > 1 else start_g for t in range(n)]  # fades to terminal g

    span = 10 if midcycle else 3
    ebit_m = _avg_last(h["ebit_margin"], span)
    if ebit_m is None:
        ebit_m = 0.10
        notes.append("EBIT margin history missing - 10 % is a placeholder.")
    tax = _avg_last(h["tax_rate"], 3) or E.STATUTORY_TAX
    tax = min(max(tax, 0.15), 0.35)
    dep = _avg_last([sdep / r if ok(sdep) and ok(r) and r else None for sdep, r in zip(h["depreciation"], h["revenue"])], 3)
    if dep is None:
        dep = 0.03
        notes.append("Depreciation history missing - 3 % of revenue is a placeholder.")
    capex = _avg_last(h["capex_pct"], 3)
    if capex is None:
        capex = dep
        notes.append("CapEx not derivable - set equal to depreciation (maintenance level). Edit it.")
    nwc_pct = None
    last_nwc, last_rev = next((x for x in reversed(h["nwc"]) if ok(x)), None), c.latest("revenue")
    if ok(last_nwc) and ok(last_rev) and last_rev > 0:
        nwc_pct = last_nwc / last_rev
    else:
        nwc_pct = 0.0
        notes.append("Working-capital data missing - NWC % of revenue set to 0. Edit it.")
    base_rev = c.latest("revenue")

    fc = [x for x in h["fcff"] if ok(x)][-3:]
    fcff0 = sum(fc) / len(fc) if fc and sum(fc) / len(fc) > 0 else None

    m = c.market
    debt = c.latest("borrowings") or 0.0
    interest, avg_debt = c.latest("interest"), _avg_last(c.s.get("borrowings", []), 2)
    kd = interest / avg_debt if ok(interest) and ok(avg_debt) and avg_debt > 0 else (0.0 if debt == 0 else None)
    dps, _ = dps_latest(c)
    roe3 = _avg_last(h["roe"], 3)
    payout = c.latest("payout_pct")
    return {
        "mode": "B", "n": n, "g": g, "tv_method": "gordon", "exit_multiple": None,
        "wacc": {"rf": None, "beta": None, "erp": None, "kd": kd, "tax": tax, "extra": 0.0, "override": None},
        "bridge": {"debt": debt, "leases": 0.0, "minority": 0.0, "preferred": 0.0,
                   "cash": c.latest("cash") or 0.0, "nonop": 0.0},
        "shares": m.get("shares"),
        "assump": {"base_revenue": base_rev, "nwc0": last_nwc if ok(last_nwc) else None, "revenue_growth": growth,
                   "ebit_margin": [ebit_m] * n, "tax_rate": [tax] * n, "dep_pct": [dep] * n,
                   "capex_pct": [capex] * n, "nwc_pct": [nwc_pct] * n},
        "modeA": {"fcff0": fcff0, "growths": [start_g] * n},
        "deltas": {k: vars(v).copy() for k, v in E.DEFAULT_DELTAS.items()},
        "sens": {"waccs": None, "gs": None, "dg": [-0.02, -0.01, 0, 0.01, 0.02], "dm": [-0.02, -0.01, 0, 0.01, 0.02],
                 "fg": [0.0, 0.03, 0.06, 0.09, 0.12]},
        "peers": {"symbols": [], "pe": None, "ev_ebitda": None, "pb": None},
        "other": {"other_income": c.latest("other_income") or 0.0, "debt_ratio": None,
                  "ddm_g": g, "ddm_g1": start_g, "ddm_years": 5, "rim_roe_start": roe3,
                  "payout": (payout / 100 if ok(payout) else None), "nav_adj": {}, "segments": [],
                  "corp_cost": 0.0, "corp_multiple": 8.0, "dps": dps},
        "ret": {"years": 3, "future_value": None},
        "confirmed_market_inputs": False, "notes": notes, "midcycle": midcycle,
    }


def _bridge(d):
    return Bridge(**{k: d[k] for k in ("debt", "leases", "minority", "preferred", "cash", "nonop")})


def _assump(d):
    return Assumptions(base_revenue=d["base_revenue"], revenue_growth=list(d["revenue_growth"]),
                       ebit_margin=list(d["ebit_margin"]), tax_rate=list(d["tax_rate"]), dep_pct=list(d["dep_pct"]),
                       capex_pct=list(d["capex_pct"]), nwc_pct=list(d["nwc_pct"]), nwc0=d.get("nwc0"))


def _try(fn, *a, **k):
    try:
        return fn(*a, **k), None
    except ValuationError as e:
        return None, str(e)
    except (TypeError, ZeroDivisionError, KeyError, ValueError) as e:  # never crash the page on odd inputs
        return None, f"Calculation not possible with the current inputs ({e})."


# ----------------------------------------------------------------------------- peers
def peer_table(snap: pd.DataFrame, c: CompanyData, symbols, target_row=None):
    cols = ["Symbol", "Name", "Market Cap", "Sales CAGR 3Y", "OPM %", "ROE", "ROCE", "Stock P/E", "EV/EBITDA*", "P/B", "Dividend Yield"]
    if snap is None or not len(symbols):
        return pd.DataFrame(columns=cols)
    df = snap[snap["Symbol"].isin(symbols)].copy()
    if {"Market Cap", "Borrowings Latest", "EBITDA Latest"} <= set(df.columns):
        e = df["EBITDA Latest"]
        df["EV/EBITDA*"] = ((df["Market Cap"] + df["Borrowings Latest"].fillna(0)) / e).where(e > 0).round(2)
    else:
        df["EV/EBITDA*"] = None
    for k in cols:
        if k not in df.columns:
            df[k] = None
    return df[cols]


# ----------------------------------------------------------------------------- main
def compute_all(c: CompanyData, inp: dict, snap: pd.DataFrame | None = None) -> dict:
    R = {"warnings": [], "errors": {}}
    W = R["warnings"]
    m = c.market
    h, hmeta = E.derive_history(c.s)
    R.update(hist=h, hist_meta=hmeta, cagr=E.cagr_table(h), verdict=E.history_verdict(h))
    W.extend(R["verdict"]["flags"])

    price, shares = m.get("price"), inp.get("shares")
    bridge = _bridge(inp["bridge"])
    R["price"], R["shares"], R["bridge_in"] = price, shares, bridge

    # --- data-driven warnings
    ff = [x for x in h["fcff"] if ok(x)]
    if ff and (ff[-1] < 0 or sum(1 for x in ff[-3:] if x < 0) >= 2):
        W.append("DCF valuation has limited reliability because historical free cash flow is negative.")
    if not ff:
        W.append("Insufficient data to calculate historical FCFF (working capital or capex could not be derived).")
    if hmeta["nwc_method"] is None:
        W.append("Insufficient data to calculate working capital changes.")
    elif hmeta["nwc_method"] == "days":
        W.append("Working capital is estimated from Screener working-capital days x revenue; load the live schedules for exact balances.")
    if hmeta["capex_method"] == "net-block estimate":
        W.append("CapEx is estimated as the change in (fixed assets + CWIP) plus depreciation; it includes acquisitions, "
                 "leases and revaluations. Load live schedules for actual purchases of fixed assets.")
    if hmeta["capex_floored"]:
        W.append("Estimated CapEx was negative in " + ", ".join(hmeta["capex_floored"]) + " (asset disposals) and was floored at zero.")
    if hmeta["tax_fallback_years"]:
        W.append("Effective tax rate was unavailable/unusual for " + ", ".join(hmeta["tax_fallback_years"][-3:]) +
                 f"; the statutory rate {E.STATUTORY_TAX:.2%} was used.")
    nw = next((x for x in reversed(h["net_worth"]) if ok(x)), None)
    if ok(nw) and nw <= 0:
        W.append("Shareholders' equity is negative - ROE, P/B and residual-income models are not meaningful.")
    if ok(h["ebitda"][-1]) and h["ebitda"][-1] <= 0:
        W.append("Latest EBITDA is negative - EBITDA-based multiples are not meaningful.")
    if ok(h["pat"][-1]) and h["pat"][-1] <= 0:
        W.append("Latest PAT is negative - P/E is not meaningful and normalised earnings should be used.")
    oi, pbt = c.latest("other_income"), c.latest("pbt")
    if ok(oi) and ok(pbt) and pbt > 0 and oi / pbt > 0.4:
        W.append(f"Other income is {oi / pbt:.0%} of PBT; it may include exceptional or one-off items. "
                 "EBIT here excludes other income (cash/investments are valued separately in the bridge).")
    cp, dp = h["capex_pct"][-1], h["depreciation"][-1]
    if ok(cp) and cp > 0.12 and ok(h["capex"][-1]) and ok(dp) and dp > 0 and h["capex"][-1] > 2.5 * dp:
        W.append(f"CapEx is unusually high ({cp:.0%} of revenue, {h['capex'][-1] / dp:.1f}x depreciation) - FCFF is depressed by growth investment.")
    if c.age_days is not None and c.age_days > 7:
        W.append(f"Share price and financials were last updated {c.age_days:.0f} days ago (stale data).")
    W.extend(c.notes)
    if not c.deep:
        W.append("Cash, lease liabilities and minority interest are not in the stored data; they use the values entered in the "
                 "bridge inputs (default 0). Enter them, or load the live schedules, before relying on the equity value.")
    if inp["bridge"].get("leases", 0) == 0:
        W.append("Lease liabilities: Screener's Borrowings usually already include Ind AS 116 lease liabilities - enter lease "
                 "liabilities separately only if they are NOT inside borrowings (avoids double counting).")
    if c.category != "general":
        W.append("Financial-sector company: FCFF/EV-EBITDA valuation is not appropriate. Use P/B, Residual Income and DDM.")
    if not inp.get("confirmed_market_inputs"):
        W.append("Risk-free rate, beta and equity risk premium are analyst inputs and have not been marked as verified.")
    W.extend(inp.get("notes", []))

    # --- WACC
    w = inp["wacc"]
    mv_equity = m.get("mcap")
    wres, werr = _try(E.wacc_calc, w["rf"], w["beta"], w["erp"], w["kd"], w["tax"], mv_equity, bridge.debt + bridge.leases, w.get("extra") or 0.0)
    R["wacc"], R["errors"]["wacc"] = wres, werr
    wacc = w["override"] if ok(w.get("override")) else (wres["wacc"] if wres else None)
    R["wacc_used"] = wacc
    ke = wres["ke"] if wres else None
    R["ke"] = ke

    # --- DCF (primary mode)
    g = inp["g"]
    R["forecast"], R["fcffs"], R["assump"] = None, None, None
    if inp["mode"] == "B":
        a, err = _try(lambda: _assump(inp["assump"]))
        R["assump"] = a
        f, err2 = _try(E.forecast_operating, a) if a else (None, err)
        R["forecast"], R["errors"]["forecast"] = f, err2
        R["fcffs"] = f["fcff"] if f else None
        final_ebitda = f["ebitda"][-1] if f else None
    else:
        fa = inp["modeA"]
        fl, err2 = _try(E.forecast_fcff_growth, fa["fcff0"], fa["growths"])
        R["fcffs"], R["errors"]["forecast"] = fl, err2
        final_ebitda = None
    tvk = {"tv_method": inp["tv_method"], "exit_multiple": inp.get("exit_multiple")}
    R["primary"], R["errors"]["dcf"] = (None, "Cannot run the DCF until the forecast and WACC inputs are valid.")
    if R["fcffs"] and wacc is not None and shares:
        R["primary"], R["errors"]["dcf"] = _try(E.run_dcf, R["fcffs"], wacc, g, bridge, shares, final_ebitda=final_ebitda, **tvk)
    if R["primary"]:
        W.extend(R["primary"]["warnings"])
    R["iv"] = R["primary"]["per_share"] if R["primary"] else None
    R["mos"], R["upside"] = E.margin_of_safety(R["iv"], price), E.upside(R["iv"], price)
    R["final_ebitda"] = final_ebitda

    # --- cross-check terminal method (shown separately, never blended)
    R["tv_crosscheck"] = None
    if R["primary"] and inp["mode"] == "B" and wacc is not None:
        other = "exit_multiple" if inp["tv_method"] == "gordon" else "gordon"
        if other == "exit_multiple" and ok(inp.get("exit_multiple")):
            R["tv_crosscheck"], _ = _try(E.run_dcf, R["fcffs"], wacc, g, bridge, shares, final_ebitda=final_ebitda,
                                         tv_method="exit_multiple", exit_multiple=inp["exit_multiple"])
        elif other == "gordon":
            R["tv_crosscheck"], _ = _try(E.run_dcf, R["fcffs"], wacc, g, bridge, shares, final_ebitda=final_ebitda, tv_method="gordon")

    # --- scenarios (advanced mode only)
    R["scenarios"] = {}
    if inp["mode"] == "B" and R["assump"] and wacc is not None and shares:
        for name, d in inp["deltas"].items():
            r, err = _try(E.run_scenario, R["assump"], wacc, g, ScenarioDelta(**d), bridge, shares, price, **tvk)
            R["scenarios"][name] = r if r else {"error": err}

    # --- reverse DCF: growth shift that justifies today's price
    R["reverse"] = None
    if inp["mode"] == "B" and R["assump"] and wacc is not None and shares and ok(price):
        d = E.reverse_growth_delta(R["assump"], wacc, g, bridge, shares, price)
        if d is not None:
            base_avg = sum(R["assump"].revenue_growth) / len(R["assump"].revenue_growth)
            R["reverse"] = {"delta": d, "avg_growth_needed": base_avg + d, "base_avg_growth": base_avg}

    # --- sensitivity
    R["sens"] = {}
    if R["fcffs"] and wacc is not None and shares:
        sv = inp["sens"]
        waccs = sv["waccs"] or [wacc + d for d in (-0.02, -0.01, 0, 0.01, 0.02)]   # full precision; round only for display
        gs = sv["gs"] or [g + d for d in (-0.01, -0.005, 0, 0.005, 0.01)]
        R["sens"]["wacc_g"] = {"waccs": waccs, "gs": gs, "grid": E.sens_wacc_g(R["fcffs"], waccs, gs, bridge, shares, **tvk, final_ebitda=final_ebitda)}
        if inp["mode"] == "B" and R["assump"]:
            R["sens"]["growth_margin"] = {"dg": sv["dg"], "dm": sv["dm"],
                                          "grid": E.sens_growth_margin(R["assump"], sv["dg"], sv["dm"], wacc, g, bridge, shares)}
        if inp["mode"] == "A":
            fa = inp["modeA"]
            R["sens"]["fcff_growth_wacc"] = {"growths": sv["fg"], "waccs": waccs,
                                             "grid": E.sens_fcff_growth_wacc(fa["fcff0"], inp["n"], sv["fg"], waccs, g, bridge, shares)
                                             if ok(fa["fcff0"]) else None}

    # --- relative valuation
    ttm = c.ttm
    eps = ttm.get("eps") if ok(ttm.get("eps")) else c.latest("eps")
    revenue = ttm.get("revenue") if ok(ttm.get("revenue")) else c.latest("revenue")
    ebitda = ttm.get("operating_profit") if ok(ttm.get("operating_profit")) else c.latest("operating_profit")
    dep_last = c.latest("depreciation")
    ebit = ebitda - dep_last if ok(ebitda) and ok(dep_last) else None
    fcff_last = next((x for x in reversed(h["fcff"]) if ok(x)), None)
    fcf_last = next((x for x in reversed(h["fcf"]) if ok(x)), None)
    bvps = m.get("bvps") if ok(m.get("bvps")) else (nw / shares if ok(nw) and shares else None)
    avg_eps = _avg_last(c.s["eps"], 5)
    eps_g = R["cagr"]["eps"][5] if R["cagr"]["eps"][5] is not None else R["cagr"]["eps"][3]
    dps, dps_src = dps_latest(c)
    mult, notes = (E.multiples(price, shares, eps, bvps, revenue, ebitda, ebit, fcff_last, fcf_last, dps, bridge, avg_eps, eps_g)
                   if ok(price) and ok(shares) else ({}, {}))
    R["multiples"], R["multiple_notes"] = mult, notes
    R["rel_inputs"] = {"eps": eps, "revenue": revenue, "ebitda": ebitda, "ebit": ebit, "bvps": bvps, "dps": dps, "dps_src": dps_src,
                       "fcff": fcff_last, "fcf": fcf_last, "avg_eps": avg_eps, "eps_growth": eps_g}
    peers = peer_table(snap, c, inp["peers"]["symbols"])
    R["peers"] = peers
    R["peer_stats"] = {k: E.peer_stats(peers[col].tolist()) for k, col in (("pe", "Stock P/E"), ("ev_ebitda", "EV/EBITDA*"), ("pb", "P/B"))
                       if col in peers.columns and len(peers)}
    # target's EV/EBITDA on the SAME basis as the peer table (gross debt, no cash deduction)
    R["target_ev_ebitda_peer_basis"] = ((m["mcap"] + bridge.debt) / ebitda) if ok(m.get("mcap")) and ok(ebitda) and ebitda > 0 else None
    imp = {}
    for key in ("pe", "ev_ebitda", "pb"):
        stt = R["peer_stats"].get(key)
        if not stt:
            continue
        chosen = inp["peers"].get(key) if ok(inp["peers"].get(key)) else stt["median"]
        def val(mu):
            if key == "pe":
                return E.implied_price_multiple(mu, eps)
            if key == "pb":
                return E.implied_price_multiple(mu, bvps)
            r, _ = _try(E.implied_price_ev_ebitda, mu, ebitda, bridge, shares, False)
            return r["per_share"] if r else None
        imp[key] = {"selected": chosen, "price": val(chosen), "low": val(stt["p25"]), "high": val(stt["p75"]), "stats": stt}
    R["implied"] = imp

    # --- other models
    o, other = inp["other"], {}
    if ke is not None and shares:
        a = R["assump"]
        if a and c.category == "general":
            dr = o["debt_ratio"] if ok(o.get("debt_ratio")) else (bridge.debt / (bridge.debt + m["mcap"]) if ok(m.get("mcap")) else 0)
            other["fcfe"], other["fcfe_err"] = _try(E.fcfe_model, a, o["other_income"], w["kd"] or 0.0, bridge.debt, dr, ke, g)
            if other["fcfe"]:
                other["fcfe"]["per_share"] = other["fcfe"]["equity"] / shares
        if ok(o.get("dps")) and o["dps"] > 0:
            other["ddm1"], other["ddm1_err"] = _try(E.ddm_single, o["dps"], ke, o["ddm_g"])
            other["ddm2"], other["ddm2_err"] = _try(E.ddm_two_stage, o["dps"], ke, o["ddm_g1"], int(o["ddm_years"]), o["ddm_g"])
        else:
            other["ddm1_err"] = "No dividend per share is available (or it is zero) - DDM is not applicable."
        if ok(nw) and o.get("rim_roe_start") is not None and o.get("payout") is not None:
            ny = inp["n"]
            start = o["rim_roe_start"]
            roes = [start + (ke - start) * t / max(ny - 1, 1) for t in range(ny)]   # fade ROE toward Ke
            roes = o.get("rim_roes") or roes
            other["rim"], other["rim_err"] = _try(E.residual_income, nw, roes, o["payout"], ke, o["ddm_g"], shares)
            other["rim_roes"] = roes
        else:
            other["rim_err"] = "Needs positive book value, a starting ROE and a payout ratio."
    elif ke is None:
        other["note"] = "Enter risk-free rate, beta and equity risk premium (WACC tab) to value with Ke-based models."
    if ok(nw) and shares:
        other["nav"] = E.nav(nw, shares, o.get("nav_adj"))
    if o.get("segments") and shares:
        other["sotp"], other["sotp_err"] = _try(E.sotp, o["segments"], o["corp_cost"], o["corp_multiple"], bridge, shares)
    R["other"] = other

    # --- return analysis
    R["return"] = None
    if ok(inp["ret"].get("future_value")) and ok(price):
        R["return"] = E.expected_return(price, inp["ret"]["future_value"], inp["ret"]["years"], m.get("div_yield") or 0)

    # --- classification, suitability, quality
    R["category"], R["suitability"] = c.category, E.SUITABILITY[c.category]
    manual = set()
    if w["beta"] is not None:
        manual.add("Beta")
    if inp["bridge"]["cash"] and not c.deep:
        manual.add("Cash and cash equivalents")
    if inp["bridge"]["minority"]:
        manual.add("Minority interest")
    if ok(shares) and ok(m.get("shares")) and abs(shares - m["shares"]) > 1e-9:
        manual.add("Diluted shares outstanding")
    R["dq"] = E.data_quality(quality_items(c, manual), c.age_days, extra_flags=min(len(W) // 3, 4))
    R["mapping"] = mapping_table(c)
    R["final_table"] = final_table(R, inp, c)
    return R


def final_table(R, inp, c):
    rows = []
    for name in ("Bear", "Base", "Bull"):
        s = R["scenarios"].get(name)
        rows.append((f"DCF - {name}", s["per_share"] if s and "per_share" in s else None))
    if not R["scenarios"] and R["iv"] is not None:
        rows = [("DCF - selected case (Mode A)", R["iv"])]
    imp = R["implied"]
    rows.append(("P/E comparable valuation", imp.get("pe", {}).get("price")))
    rows.append(("EV/EBITDA comparable valuation (peer basis)", imp.get("ev_ebitda", {}).get("price")))
    rows.append(("P/B comparable valuation", imp.get("pb", {}).get("price")))
    o = R["other"]
    rows.append(("FCFE", o.get("fcfe", {}).get("per_share") if o.get("fcfe") else None))
    rows.append(("DDM (two-stage)", o.get("ddm2", {}).get("value") if o.get("ddm2") else None))
    rows.append(("Residual Income", o.get("rim", {}).get("per_share") if o.get("rim") else None))
    return rows


def suitable_range(R, category):
    """A reasoned range from the methods that suit the business type (NOT a blind average)."""
    pick = []
    if category == "general":
        keys = {"DCF - Bear", "DCF - Bull", "P/E comparable valuation", "EV/EBITDA comparable valuation (peer basis)", "DCF - selected case (Mode A)"}
    else:
        keys = {"P/B comparable valuation", "P/E comparable valuation", "DDM (two-stage)", "Residual Income"}
    for name, v in R["final_table"]:
        if name in keys and ok(v):
            pick.append((name, v))
    if not pick:
        return None
    vals = [v for _, v in pick]
    return {"low": min(vals), "high": max(vals), "methods": [n for n, _ in pick]}
