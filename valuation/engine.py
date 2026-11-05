"""
Valuation engine - pure calculations, no Streamlit, no file/network access.

Conventions
  * amounts in INR Crore, per-share values in INR, share counts in Crore
  * rates are decimals (0.12 = 12 %)
  * full float precision internally; round only when displaying
  * forecast cash flows are discounted at the END of each forecast year
    (year 1 -> t = 1), exactly as in  PV(TV) = TV / (1 + WACC) ** N
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from statistics import median

STATUTORY_TAX = 0.2517  # India corporate tax, new regime (used only as a flagged fallback)


class ValuationError(ValueError):
    """Inputs make a valuation mathematically invalid or misleading."""


# ----------------------------------------------------------------------------- helpers
def ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))


def sdiv(a, b):
    if not ok(a) or not ok(b) or b == 0:
        return None
    return a / b


def cagr(start, end, years):
    if not (ok(start) and ok(end)) or years <= 0 or start <= 0 or end <= 0:
        return None
    return (end / start) ** (1 / years) - 1


def _mean(vals):
    vals = [v for v in vals if ok(v)]
    return sum(vals) / len(vals) if vals else None


def _need_rate(name, x, lo=-0.5, hi=1.0):
    if not ok(x):
        raise ValuationError(f"{name} is missing.")
    if not lo <= x <= hi:
        raise ValuationError(f"{name} = {x:.2%} is outside the sensible range ({lo:.0%} to {hi:.0%}).")


# ----------------------------------------------------------------------------- historical analysis
H_KEYS = [
    "revenue", "ebitda", "ebit", "pat", "eps", "other_income", "interest", "depreciation",
    "ebitda_margin", "ebit_margin", "net_margin", "net_worth", "borrowings", "cash", "net_debt",
    "roe", "roce", "roic", "de", "interest_cov", "cfo", "capex", "fcf", "cash_conv", "capex_pct",
    "nwc", "d_nwc", "tax_rate", "nopat", "fcff",
]


def derive_history(s: dict, tax_fallback: float = STATUTORY_TAX):
    """s = {'years': [...], '<name>': [values aligned to years], ...}  ->  (history, meta)."""
    years = list(s["years"])
    n = len(years)

    def v(name, i):
        lst = s.get(name)
        if lst is None or i < 0 or i >= len(lst):
            return None
        return lst[i] if ok(lst[i]) else None

    h = {k: [None] * n for k in H_KEYS}
    h["years"] = years
    meta = {"tax_fallback_years": [], "capex_method": None, "capex_floored": [], "nwc_method": None,
            "roic_basis": "capital employed less cash" if any(ok(x) for x in s.get("cash", [])) else
            "capital employed (cash not available)"}

    sched_ok = sum(1 for i in range(n) if all(ok(v(k, i)) for k in ("inventory", "receivables", "payables"))) >= 2
    days_ok = any(ok(v("wc_days", i)) or all(ok(v(k, i)) for k in ("debtor_days", "inventory_days", "payable_days"))
                  for i in range(n))
    meta["nwc_method"] = "schedule" if sched_ok else ("days" if days_ok else None)
    has_cap_cf = sum(1 for i in range(n) if ok(v("capex_gross", i))) >= max(2, n // 2)
    meta["capex_method"] = "cash-flow statement" if has_cap_cf else "net-block estimate"

    for i in range(n):
        rev, op, dep = v("revenue", i), v("operating_profit", i), v("depreciation", i)
        ebit = op - dep if ok(op) and ok(dep) else None
        h["revenue"][i], h["ebitda"][i], h["ebit"][i] = rev, op, ebit
        h["pat"][i], h["eps"][i] = v("pat", i), v("eps", i)
        h["other_income"][i], h["interest"][i], h["depreciation"][i] = v("other_income", i), v("interest", i), dep
        h["ebitda_margin"][i], h["ebit_margin"][i] = sdiv(op, rev), sdiv(ebit, rev)
        h["net_margin"][i] = sdiv(h["pat"][i], rev)

        tp = v("tax_pct", i)
        if ok(tp) and 0 <= tp <= 50:
            t = tp / 100
        else:
            t = tax_fallback
            if ebit is not None:
                meta["tax_fallback_years"].append(years[i])
        h["tax_rate"][i] = t
        h["nopat"][i] = ebit * (1 - t) if ebit is not None else None

        eq, res, debt = v("equity_capital", i), v("reserves", i), v("borrowings", i)
        nw = eq + res if ok(eq) and ok(res) else None
        h["net_worth"][i], h["borrowings"][i], h["cash"][i] = nw, debt, v("cash", i)
        cash = h["cash"][i]
        h["net_debt"][i] = debt - cash if ok(debt) and ok(cash) else None
        h["de"][i] = sdiv(debt, nw) if ok(nw) and nw > 0 else None
        h["interest_cov"][i] = sdiv(ebit, v("interest", i)) if ok(v("interest", i)) and v("interest", i) > 0 else None

        # returns on average capital
        def avg_prev(series_fn):
            cur, prev = series_fn(i), series_fn(i - 1) if i > 0 else None
            if ok(cur) and ok(prev):
                return (cur + prev) / 2
            return cur if ok(cur) else None

        nw_f = lambda j: (v("equity_capital", j) + v("reserves", j)) if ok(v("equity_capital", j)) and ok(v("reserves", j)) else None
        ce_f = lambda j: (nw_f(j) + v("borrowings", j)) if ok(nw_f(j)) and ok(v("borrowings", j)) else None
        ic_f = lambda j: (ce_f(j) - (v("cash", j) or 0)) if ok(ce_f(j)) else None
        a_nw, a_ce, a_ic = avg_prev(nw_f), avg_prev(ce_f), avg_prev(ic_f)
        h["roe"][i] = sdiv(h["pat"][i], a_nw) if ok(a_nw) and a_nw > 0 else None
        h["roce"][i] = sdiv(ebit, a_ce) if ok(a_ce) and a_ce > 0 else None
        h["roic"][i] = sdiv(h["nopat"][i], a_ic) if ok(a_ic) and a_ic > 0 else None

        # capital expenditure
        cg = v("capex_gross", i)
        capex = None
        if ok(cg):
            capex = abs(cg)
        elif i > 0 and ok(dep) and ok(v("fixed_assets", i)) and ok(v("fixed_assets", i - 1)):
            cur = v("fixed_assets", i) + (v("cwip", i) or 0)
            prv = v("fixed_assets", i - 1) + (v("cwip", i - 1) or 0)
            capex = cur - prv + dep
            if capex < 0:
                capex = 0.0
                meta["capex_floored"].append(years[i])
        h["capex"][i] = capex
        h["capex_pct"][i] = sdiv(capex, rev)

        # operating working capital
        nwc = None
        if meta["nwc_method"] == "schedule" and all(ok(v(k, i)) for k in ("inventory", "receivables", "payables")):
            nwc = (v("inventory", i) + v("receivables", i) + (v("other_op_assets", i) or 0)
                   - v("payables", i) - (v("other_op_liabilities", i) or 0))
        elif meta["nwc_method"] == "days" and ok(rev):
            if ok(v("wc_days", i)):
                nwc = v("wc_days", i) * rev / 365
            elif all(ok(v(k, i)) for k in ("debtor_days", "inventory_days", "payable_days")):
                nwc = (v("debtor_days", i) + v("inventory_days", i) - v("payable_days", i)) * rev / 365
        h["nwc"][i] = nwc
        if i > 0 and ok(nwc) and ok(h["nwc"][i - 1]):
            h["d_nwc"][i] = nwc - h["nwc"][i - 1]

        cfo = v("cfo", i)
        h["cfo"][i] = cfo
        h["fcf"][i] = cfo - capex if ok(cfo) and ok(capex) else None
        h["cash_conv"][i] = sdiv(cfo, h["pat"][i]) if ok(h["pat"][i]) and h["pat"][i] > 0 else None

        # FCFF = EBIT(1-t) + D&A - CapEx - change in NWC
        if all(ok(x) for x in (h["nopat"][i], dep, capex, h["d_nwc"][i])):
            h["fcff"][i] = h["nopat"][i] + dep - capex - h["d_nwc"][i]
    return h, meta


def series_cagr(vals, n):
    if len(vals) <= n:
        return None
    return cagr(vals[-1 - n], vals[-1], n)


def cagr_table(h):
    return {m: {n: series_cagr(h[m], n) for n in (3, 5, 10)} for m in ("revenue", "ebitda", "ebit", "pat", "eps")}


def history_verdict(h):
    """Multi-signal view (never a single ratio). Returns verdict, signals and combination flags."""
    def last(k, back=0):
        vals = [x for x in h[k] if ok(x)]
        return vals[-1 - back] if len(vals) > back else None

    signals, flags = [], []

    def add(text, sign):
        signals.append((text, sign))

    rc = series_cagr(h["revenue"], 3)
    if rc is not None:
        add(f"Revenue 3Y CAGR {rc:.1%}", 1 if rc > 0.05 else (-1 if rc < 0 else 0))
    m_now, m_old = last("ebit_margin"), last("ebit_margin", 3)
    if m_now is not None and m_old is not None:
        d = m_now - m_old
        add(f"EBIT margin {m_old:.1%} -> {m_now:.1%} over 3 years", 1 if d > 0.01 else (-1 if d < -0.01 else 0))
    r_now, r_old = last("roce"), last("roce", 3)
    if r_now is not None and r_old is not None:
        d = r_now - r_old
        add(f"ROCE {r_old:.1%} -> {r_now:.1%} over 3 years", 1 if d > 0.02 else (-1 if d < -0.02 else 0))
    cc = [x for x in h["cash_conv"] if ok(x)][-3:]
    if len(cc) >= 2:
        a = sum(cc) / len(cc)
        add(f"Average CFO/PAT {a:.2f} (last {len(cc)} years)", 1 if a >= 0.8 else (-1 if a < 0.6 else 0))
    d_now, d_old = last("de"), last("de", 3)
    if d_now is not None and d_old is not None:
        d = d_now - d_old
        add(f"Debt/equity {d_old:.2f} -> {d_now:.2f} over 3 years", 1 if d < -0.1 else (-1 if d > 0.2 else 0))
    ff = [x for x in h["fcff"] if ok(x)][-3:]
    if len(ff) >= 2:
        neg = sum(1 for x in ff if x < 0)
        add(f"FCFF positive in {len(ff) - neg} of last {len(ff)} years", 1 if neg == 0 else (-1 if neg >= 2 else 0))

    score = sum(s for _, s in signals)
    verdict = "Insufficient data" if len(signals) < 3 else (
        "Improving" if score >= 3 else ("Deteriorating" if score <= -2 else "Mixed / stable"))

    # combination flags
    if rc is not None and rc > 0.03 and m_now is not None and m_old is not None and m_now - m_old < -0.01:
        flags.append("Revenue is rising but EBIT margin is declining - growth may be coming at the cost of profitability.")
    cc5 = [x for x in h["cash_conv"] if ok(x)][-5:]
    if len(cc5) >= 3 and sum(1 for x in cc5 if x < 0.7) >= 3:
        flags.append("Cash conversion (CFO/PAT) has been below 0.7 in most recent years - check earnings quality.")
    b_now, b_old = last("borrowings"), last("borrowings", 3)
    pc = series_cagr(h["pat"], 3)
    if b_now is not None and b_old is not None and b_old > 0 and b_now / b_old > 1.3 and (pc is None or pc < 0.05):
        flags.append("Borrowings rose over 30 % in 3 years while profit growth was weak.")
    if r_now is not None and r_old is not None and r_now - r_old < -0.03:
        flags.append("Return on capital employed has fallen by more than 3 percentage points in 3 years.")
    if ff and sum(1 for x in ff if x < 0) >= 2:
        flags.append("FCFF was negative in at least two of the last three years.")
    return {"verdict": verdict, "score": score, "signals": signals, "flags": flags}


# ----------------------------------------------------------------------------- forecasting
@dataclass
class Assumptions:
    """Advanced (Mode B) operating forecast. All per-year lists have the same length N."""
    base_revenue: float
    revenue_growth: list
    ebit_margin: list
    tax_rate: list
    dep_pct: list
    capex_pct: list
    nwc_pct: list
    nwc0: float | None = None  # actual operating NWC at base year (None -> nwc_pct[0] * base revenue)

    @property
    def n(self):
        return len(self.revenue_growth)


def validate_assumptions(a: Assumptions):
    if not ok(a.base_revenue) or a.base_revenue <= 0:
        raise ValuationError("Base-year revenue must be positive.")
    n = a.n
    if n < 1:
        raise ValuationError("Forecast period must be at least 1 year.")
    for name in ("ebit_margin", "tax_rate", "dep_pct", "capex_pct", "nwc_pct"):
        lst = getattr(a, name)
        if len(lst) != n:
            raise ValuationError(f"Assumption '{name}' needs {n} values (one per forecast year).")
        if not all(ok(x) for x in lst):
            raise ValuationError(f"Assumption '{name}' has a missing value.")
    if not all(ok(x) for x in a.revenue_growth):
        raise ValuationError("Revenue growth has a missing value.")
    if any(t < 0 or t >= 0.6 for t in a.tax_rate):
        raise ValuationError("Tax rate must be between 0 % and 60 %.")
    if any(g <= -0.9 for g in a.revenue_growth):
        raise ValuationError("Revenue growth of -90 % or worse is not a valid assumption.")


def forecast_operating(a: Assumptions) -> dict:
    validate_assumptions(a)
    rows = {k: [] for k in ("revenue", "growth", "ebitda", "ebitda_margin", "ebit", "ebit_margin", "tax", "nopat",
                            "dep", "capex", "nwc", "d_nwc", "fcff")}
    rev_prev = a.base_revenue
    nwc_prev = a.nwc0 if ok(a.nwc0) else a.nwc_pct[0] * a.base_revenue
    for t in range(a.n):
        rev = rev_prev * (1 + a.revenue_growth[t])
        ebit = rev * a.ebit_margin[t]
        dep = rev * a.dep_pct[t]
        ebitda = ebit + dep
        tax = ebit * a.tax_rate[t]          # tax on operating profit (unlevered)
        nopat = ebit - tax
        capex = rev * a.capex_pct[t]
        nwc = rev * a.nwc_pct[t]
        d_nwc = nwc - nwc_prev              # increase in NWC = cash outflow
        fcff = nopat + dep - capex - d_nwc
        for k, val in (("revenue", rev), ("growth", a.revenue_growth[t]), ("ebitda", ebitda),
                       ("ebitda_margin", ebitda / rev if rev else None), ("ebit", ebit),
                       ("ebit_margin", a.ebit_margin[t]), ("tax", tax), ("nopat", nopat), ("dep", dep),
                       ("capex", capex), ("nwc", nwc), ("d_nwc", d_nwc), ("fcff", fcff)):
            rows[k].append(val)
        rev_prev, nwc_prev = rev, nwc
    return rows


def forecast_fcff_growth(current_fcff, growths):
    """Mode A: FCFF_t = FCFF_{t-1} * (1 + g_t)."""
    if not ok(current_fcff):
        raise ValuationError("Current FCFF is missing.")
    if not growths or not all(ok(g) for g in growths):
        raise ValuationError("FCFF growth assumptions are missing.")
    out, prev = [], current_fcff
    for g in growths:
        if g <= -1:
            raise ValuationError("FCFF growth of -100 % or worse is not valid.")
        prev = prev * (1 + g)
        out.append(prev)
    return out


# ----------------------------------------------------------------------------- WACC
def wacc_calc(rf, beta, erp, kd, tax, mv_equity, debt, extra_premium=0.0):
    """CAPM cost of equity and market-value-weighted WACC (FCFF must be discounted at WACC, not Ke)."""
    for name, x in (("Risk-free rate", rf), ("Beta", beta), ("Equity risk premium", erp),
                    ("Tax rate", tax), ("Market value of equity", mv_equity), ("Debt", debt)):
        if not ok(x):
            raise ValuationError(f"{name} is missing - enter it manually.")
    if mv_equity <= 0:
        raise ValuationError("Market value of equity must be positive.")
    if debt < 0:
        raise ValuationError("Debt cannot be negative.")
    if not 0 <= tax < 1:
        raise ValuationError("Tax rate must be between 0 % and 100 %.")
    if beta < 0:
        raise ValuationError("Beta cannot be negative for this model.")
    if debt > 0 and not ok(kd):
        raise ValuationError("Cost of debt is missing - enter it manually.")
    kd = kd if ok(kd) else 0.0
    ke = rf + beta * erp + extra_premium
    kd_at = kd * (1 - tax)
    total = mv_equity + debt
    we, wd = mv_equity / total, debt / total
    w = we * ke + wd * kd_at
    return {"rf": rf, "beta": beta, "erp": erp, "extra_premium": extra_premium, "ke": ke, "kd": kd, "tax": tax,
            "kd_after_tax": kd_at, "equity": mv_equity, "debt": debt, "we": we, "wd": wd, "wacc": w}


# ----------------------------------------------------------------------------- DCF
def dcf(fcffs, wacc, g, tv_method="gordon", exit_multiple=None, final_ebitda=None):
    n = len(fcffs)
    if n < 1:
        raise ValuationError("At least one forecast year is required.")
    if not all(ok(f) for f in fcffs):
        raise ValuationError("Forecast FCFF contains a missing value.")
    if not ok(wacc) or wacc <= 0:
        raise ValuationError("WACC must be a positive number.")
    if tv_method == "gordon":
        if not ok(g):
            raise ValuationError("Terminal growth rate is missing.")
        if g >= wacc:
            raise ValuationError(f"Terminal growth ({g:.2%}) must be lower than WACC ({wacc:.2%}).")
        if fcffs[-1] <= 0:
            raise ValuationError("Final-year FCFF is not positive, so a Gordon-growth terminal value is not meaningful. "
                                 "Revise margins / capex / working-capital assumptions or extend the forecast.")
        tv = fcffs[-1] * (1 + g) / (wacc - g)
    elif tv_method == "exit_multiple":
        if not ok(exit_multiple) or exit_multiple <= 0:
            raise ValuationError("Exit EV/EBITDA multiple must be positive.")
        if not ok(final_ebitda) or final_ebitda <= 0:
            raise ValuationError("Final-year EBITDA must be positive for the exit-multiple method.")
        tv = final_ebitda * exit_multiple
    else:
        raise ValuationError(f"Unknown terminal value method: {tv_method}")

    factors = [1 / (1 + wacc) ** (t + 1) for t in range(n)]
    pvs = [f * d for f, d in zip(fcffs, factors)]
    pv_sum = sum(pvs)
    pv_tv = tv * factors[-1]
    ev = pv_sum + pv_tv
    res = {"fcff": list(fcffs), "factors": factors, "pv": pvs, "pv_sum": pv_sum, "tv": tv, "tv_factor": factors[-1],
           "pv_tv": pv_tv, "ev": ev, "tv_share": pv_tv / ev if ev > 0 else None, "wacc": wacc, "g": g,
           "tv_method": tv_method, "exit_multiple": exit_multiple, "warnings": []}
    # cross-checks between the two terminal-value approaches (informational only)
    if tv_method == "gordon" and ok(final_ebitda) and final_ebitda > 0:
        res["implied_exit_multiple"] = tv / final_ebitda
    if tv_method == "exit_multiple":
        res["implied_growth"] = (tv * wacc - fcffs[-1]) / (tv + fcffs[-1]) if (tv + fcffs[-1]) != 0 else None
    if res["tv_share"] is not None and res["tv_share"] > 0.75:
        res["warnings"].append(f"Terminal value represents {res['tv_share']:.0%} of Enterprise Value. "
                               "Valuation is highly sensitive to long-term assumptions.")
    if sum(1 for f in fcffs if f < 0) >= max(1, n // 2):
        res["warnings"].append("Half or more of the forecast FCFFs are negative - DCF reliability is limited.")
    return res


@dataclass
class Bridge:
    debt: float = 0.0
    leases: float = 0.0       # only lease liabilities NOT already inside `debt`
    minority: float = 0.0
    preferred: float = 0.0
    cash: float = 0.0
    nonop: float = 0.0        # non-operating investments

    def validate(self):
        for k, val in vars(self).items():
            if not ok(val):
                raise ValuationError(f"Bridge item '{k}' is missing.")
        if self.debt < 0 or self.leases < 0 or self.minority < 0 or self.preferred < 0 or self.cash < 0 or self.nonop < 0:
            raise ValuationError("Debt, leases, minority interest, preferred equity, cash and investments cannot be negative.")


def equity_bridge(ev, b: Bridge, shares):
    b.validate()
    if not ok(shares) or shares <= 0:
        raise ValuationError("Diluted shares outstanding must be positive.")
    equity = ev - b.debt - b.leases - b.minority - b.preferred + b.cash + b.nonop
    return {"ev": ev, "debt": b.debt, "leases": b.leases, "minority": b.minority, "preferred": b.preferred,
            "cash": b.cash, "nonop": b.nonop, "equity": equity, "shares": shares, "per_share": equity / shares}


def run_dcf(fcffs, wacc, g, bridge: Bridge, shares, **tv_kw):
    d = dcf(fcffs, wacc, g, **tv_kw)
    br = equity_bridge(d["ev"], bridge, shares)
    if br["equity"] <= 0:
        d["warnings"].append("Equity value is zero or negative after deducting debt and other claims.")
    return {**d, "bridge": br, "per_share": br["per_share"]}


# ----------------------------------------------------------------------------- scenarios
@dataclass
class ScenarioDelta:
    growth: float = 0.0   # added to every year's revenue growth
    margin: float = 0.0   # added to every year's EBIT margin
    nwc: float = 0.0      # added to NWC % of revenue (positive = more working capital)
    wacc: float = 0.0
    g: float = 0.0


DEFAULT_DELTAS = {
    "Bear": ScenarioDelta(-0.03, -0.02, 0.02, 0.01, -0.01),
    "Base": ScenarioDelta(),
    "Bull": ScenarioDelta(0.03, 0.02, -0.01, -0.01, 0.005),
}


def apply_delta(a: Assumptions, d: ScenarioDelta) -> Assumptions:
    return replace(a, revenue_growth=[x + d.growth for x in a.revenue_growth],
                   ebit_margin=[x + d.margin for x in a.ebit_margin],
                   nwc_pct=[max(0.0, x + d.nwc) for x in a.nwc_pct])


def run_scenario(a: Assumptions, wacc, g, d: ScenarioDelta, bridge: Bridge, shares, price=None, **tv_kw):
    sc = apply_delta(a, d)
    f = forecast_operating(sc)
    w, gg = wacc + d.wacc, g + d.g
    res = run_dcf(f["fcff"], w, gg, bridge, shares, final_ebitda=f["ebitda"][-1], **tv_kw)
    out = {"forecast": f, "wacc": w, "g": gg, "ev": res["ev"], "equity": res["bridge"]["equity"],
           "per_share": res["per_share"], "tv_share": res["tv_share"], "warnings": res["warnings"]}
    if ok(price) and price > 0:
        out["mos"] = margin_of_safety(res["per_share"], price)
        out["upside"] = upside(res["per_share"], price)
    return out


# ----------------------------------------------------------------------------- sensitivity
def sens_wacc_g(fcffs, waccs, gs, bridge, shares, **tv_kw):
    grid = []
    for w in waccs:
        row = []
        for g in gs:
            try:
                row.append(run_dcf(fcffs, w, g, bridge, shares, **tv_kw)["per_share"])
            except ValuationError:
                row.append(None)
        grid.append(row)
    return grid


def sens_growth_margin(a: Assumptions, growth_deltas, margin_deltas, wacc, g, bridge, shares):
    grid = []
    for dg in growth_deltas:
        row = []
        for dm in margin_deltas:
            try:
                f = forecast_operating(apply_delta(a, ScenarioDelta(growth=dg, margin=dm)))
                row.append(run_dcf(f["fcff"], wacc, g, bridge, shares)["per_share"])
            except ValuationError:
                row.append(None)
        grid.append(row)
    return grid


def sens_fcff_growth_wacc(current_fcff, n, growths, waccs, g, bridge, shares):
    grid = []
    for gr in growths:
        row = []
        for w in waccs:
            try:
                row.append(run_dcf(forecast_fcff_growth(current_fcff, [gr] * n), w, g, bridge, shares)["per_share"])
            except ValuationError:
                row.append(None)
        grid.append(row)
    return grid


# ----------------------------------------------------------------------------- return analysis
def margin_of_safety(iv, price):
    if not ok(iv) or iv <= 0 or not ok(price):
        return None
    return (iv - price) / iv * 100


def upside(iv, price):
    if not ok(iv) or not ok(price) or price <= 0:
        return None
    return (iv - price) / price * 100


def expected_return(price, future_value, years, dividend_yield=0.0):
    c = cagr(price, future_value, years)
    if c is None:
        return None
    return {"price_cagr": c, "dividend_yield": (dividend_yield or 0) / 100, "total": c + (dividend_yield or 0) / 100}


# ----------------------------------------------------------------------------- additional models
def fcfe_model(a: Assumptions, other_income, kd, debt0, debt_ratio, ke, g):
    """FCFE = PAT + D&A - CapEx - dNWC + net borrowing.  Discounted at Ke -> result IS equity value."""
    if not 0 <= debt_ratio <= 1:
        raise ValuationError("Debt ratio must be between 0 and 1.")
    f = forecast_operating(a)
    debt, rows = debt0, {k: [] for k in ("pat", "net_borrowing", "interest", "fcfe")}
    for t in range(a.n):
        reinvest = f["capex"][t] - f["dep"][t] + f["d_nwc"][t]
        nb = debt_ratio * reinvest
        interest = kd * debt                       # on opening debt
        pbt = f["ebit"][t] + other_income - interest
        pat = pbt - max(pbt, 0) * a.tax_rate[t]
        fcfe = pat + f["dep"][t] - f["capex"][t] - f["d_nwc"][t] + nb
        for k, val in (("pat", pat), ("net_borrowing", nb), ("interest", interest), ("fcfe", fcfe)):
            rows[k].append(val)
        debt += nb
    if not ok(ke) or ke <= 0 or g >= ke:
        raise ValuationError("Cost of equity must be positive and above the terminal growth rate.")
    if rows["fcfe"][-1] <= 0:
        raise ValuationError("Final-year FCFE is not positive; terminal value not meaningful.")
    fac = [1 / (1 + ke) ** (t + 1) for t in range(a.n)]
    pv = [x * d for x, d in zip(rows["fcfe"], fac)]
    tv = rows["fcfe"][-1] * (1 + g) / (ke - g)
    equity = sum(pv) + tv * fac[-1]
    return {**rows, "factors": fac, "pv": pv, "tv": tv, "pv_tv": tv * fac[-1], "equity": equity}


def ddm_single(d0, ke, g):
    if not ok(d0) or d0 <= 0:
        raise ValuationError("DDM needs a positive current dividend per share.")
    if ke <= g:
        raise ValuationError("Cost of equity must exceed the dividend growth rate.")
    d1 = d0 * (1 + g)
    return {"d1": d1, "value": d1 / (ke - g)}


def ddm_two_stage(d0, ke, g1, years, g2):
    if not ok(d0) or d0 <= 0:
        raise ValuationError("DDM needs a positive current dividend per share.")
    if ke <= g2:
        raise ValuationError("Cost of equity must exceed the stable-stage growth rate.")
    if years < 1:
        raise ValuationError("High-growth period must be at least 1 year.")
    divs, d = [], d0
    for _ in range(years):
        d *= 1 + g1
        divs.append(d)
    pv = sum(x / (1 + ke) ** (t + 1) for t, x in enumerate(divs))
    tv = divs[-1] * (1 + g2) / (ke - g2)
    return {"dividends": divs, "pv_dividends": pv, "tv": tv, "pv_tv": tv / (1 + ke) ** years, "value": pv + tv / (1 + ke) ** years}


def residual_income(book0, roes, payout, ke, g, shares):
    """Equity value = BV0 + PV(residual income); RI_t = NI_t - Ke * BV_{t-1}; BV rolls with retained earnings."""
    if not ok(book0) or book0 <= 0:
        raise ValuationError("Residual income model needs positive book value (negative equity found).")
    if ke <= g:
        raise ValuationError("Cost of equity must exceed the terminal growth rate.")
    if not 0 <= payout <= 1:
        raise ValuationError("Payout ratio must be between 0 and 1.")
    bv, rows = book0, {k: [] for k in ("ni", "ri", "bv_end", "pv_ri")}
    for t, roe in enumerate(roes):
        ni = roe * bv
        ri = ni - ke * bv
        bv = bv + ni * (1 - payout)
        for k, val in (("ni", ni), ("ri", ri), ("bv_end", bv), ("pv_ri", ri / (1 + ke) ** (t + 1))):
            rows[k].append(val)
    tv = rows["ri"][-1] * (1 + g) / (ke - g)
    pv_tv = tv / (1 + ke) ** len(roes)
    equity = book0 + sum(rows["pv_ri"]) + pv_tv
    return {**rows, "tv": tv, "pv_tv": pv_tv, "equity": equity, "per_share": equity / shares if ok(shares) and shares > 0 else None}


def nav(net_worth, shares, adjustments=None):
    adj = sum(adjustments.values()) if adjustments else 0.0
    adjusted = net_worth + adj
    return {"nav": net_worth, "adjustments": adj, "adjusted_nav": adjusted,
            "bvps": sdiv(net_worth, shares), "adjusted_bvps": sdiv(adjusted, shares)}


def sotp(segments, corporate_cost, corporate_multiple, bridge: Bridge, shares):
    """segments: list of dicts {name, revenue, ebitda, ebit, method, multiple}."""
    rows, total = [], 0.0
    for s in segments:
        metric = {"EV/EBITDA": s.get("ebitda"), "EV/EBIT": s.get("ebit"), "EV/Sales": s.get("revenue")}.get(s.get("method"))
        if not ok(metric) or not ok(s.get("multiple")):
            raise ValuationError(f"Segment '{s.get('name')}' is missing its metric or multiple.")
        ev = metric * s["multiple"]
        rows.append({**s, "ev": ev})
        total += ev
    corp = (corporate_cost or 0) * (corporate_multiple or 0)
    ev_total = total - corp
    br = equity_bridge(ev_total, bridge, shares)
    return {"segments": rows, "corporate_drag": corp, "ev": ev_total, "bridge": br, "per_share": br["per_share"]}


# ----------------------------------------------------------------------------- relative valuation
def multiples(price, shares, eps, bvps, revenue, ebitda, ebit, fcff, fcf, dps, bridge: Bridge,
              avg_eps=None, eps_growth=None):
    """Trailing multiples; None + a note whenever a multiple is not meaningful."""
    notes, out = {}, {}
    mcap = price * shares if ok(price) and ok(shares) else None
    ev = mcap + bridge.debt + bridge.leases + bridge.minority + bridge.preferred - bridge.cash - bridge.nonop if ok(mcap) else None

    def pos(name, num, den, what):
        if ok(num) and ok(den) and den > 0:
            out[name] = num / den
        else:
            out[name] = None
            notes[name] = f"Not meaningful: {what} is zero, negative or unavailable."

    pos("pe", price, eps, "EPS")
    pos("pb", price, bvps, "book value per share")
    pos("ps", mcap, revenue, "revenue")
    pos("p_fcf", mcap, fcf, "free cash flow (CFO - CapEx)")
    pos("ev_ebitda", ev, ebitda, "EBITDA")
    pos("ev_ebit", ev, ebit, "EBIT")
    pos("ev_sales", ev, revenue, "revenue")
    pos("ev_fcff", ev, fcff, "FCFF")
    pos("normalized_pe", price, avg_eps, "5-year average EPS")
    out["div_yield"] = dps / price * 100 if ok(dps) and ok(price) and price > 0 else None
    if out.get("pe") and ok(eps_growth) and eps_growth > 0:
        out["peg"] = out["pe"] / (eps_growth * 100)
    else:
        out["peg"] = None
        notes["peg"] = "Not meaningful: needs positive P/E and positive EPS growth."
    out["ev"], out["mcap"] = ev, mcap
    out["forward_pe"] = None
    notes["forward_pe"] = "Not calculated: no reliable earnings forecasts in the database."
    return out, notes


def peer_stats(values):
    v = sorted(x for x in values if ok(x) and x > 0)
    if not v:
        return None

    def pct(p):
        k = (len(v) - 1) * p
        lo, hi = math.floor(k), math.ceil(k)
        return v[lo] + (v[hi] - v[lo]) * (k - lo)

    return {"n": len(v), "mean": sum(v) / len(v), "median": median(v), "p25": pct(0.25), "p75": pct(0.75),
            "min": v[0], "max": v[-1]}


def implied_price_multiple(multiple, per_share_metric):
    """Implied price = multiple x per-share metric (P/E x EPS, P/B x BVPS)."""
    if not ok(multiple) or not ok(per_share_metric) or per_share_metric <= 0:
        return None
    return multiple * per_share_metric


def implied_price_ev_ebitda(multiple, ebitda, bridge: Bridge, shares, include_cash=True):
    """EV/EBITDA: implied EV first, then bridge to equity, then per share."""
    if not ok(multiple) or not ok(ebitda) or ebitda <= 0 or not ok(shares) or shares <= 0:
        return None
    ev = multiple * ebitda
    b = bridge if include_cash else replace(bridge, cash=0.0, nonop=0.0)
    return equity_bridge(ev, b, shares)


# ----------------------------------------------------------------------------- business type & method fit
def classify_business(sector, industry):
    text = f"{sector or ''} {industry or ''}".lower()
    if "insurance" in text:
        return "insurance"
    if any(k in text for k in ("bank", "nbfc", "non banking", "finance", "financial", "housing finance")):
        return "financial"
    return "general"


SUITABILITY = {
    "general": {"DCF (FCFF)": "Primary", "Relative (P/E, EV/EBITDA)": "Primary", "FCFE": "Secondary", "DDM": "Only if dividends are significant",
                "Residual Income": "Cross-check", "Asset-based / NAV": "Rarely relevant (asset-heavy or holding companies)",
                "SOTP": "Only for diversified groups"},
    "financial": {"DCF (FCFF)": "Not appropriate (debt is raw material)", "Relative (P/E, EV/EBITDA)": "P/B and P/E only; EV/EBITDA not meaningful",
                  "FCFE": "Use with caution (reinvestment is regulatory capital)", "DDM": "Primary", "Residual Income": "Primary",
                  "Asset-based / NAV": "Cross-check (book value)", "SOTP": "Only for financial conglomerates"},
    "insurance": {"DCF (FCFF)": "Not appropriate", "Relative (P/E, EV/EBITDA)": "P/B and P/E only; embedded-value data is not in this database",
                  "FCFE": "Not appropriate", "DDM": "Secondary", "Residual Income": "Primary (book value based)",
                  "Asset-based / NAV": "Cross-check", "SOTP": "Not applicable"},
}


# ----------------------------------------------------------------------------- data-quality indicator
CREDIT = {"reported": 1.0, "derived": 0.7, "manual": 0.8, "missing": 0.0}


def data_quality(items: dict, age_days, extra_flags=0):
    """items: {name: (status, weight)}.  A transparent heuristic score - NOT a probability."""
    tw = sum(w for _, w in items.values())
    comp = sum(CREDIT[s] * w for s, w in items.values()) / tw if tw else 0
    if age_days is None:
        fresh = 0.5
    else:
        fresh = 1.0 if age_days <= 7 else 0.8 if age_days <= 30 else 0.5 if age_days <= 90 else 0.2
    score = max(0.0, 100 * (0.85 * comp + 0.15 * fresh) - 3 * extra_flags)
    label = "High" if score >= 80 else "Moderate" if score >= 60 else "Low"
    return {"score": round(score), "label": label, "completeness": comp, "freshness": fresh,
            "note": "Heuristic completeness/freshness indicator - not a statistical probability."}


# ----------------------------------------------------------------------------- reverse DCF
def reverse_growth_delta(a: Assumptions, wacc, g, bridge: Bridge, shares, price, lo=-0.20, hi=0.30):
    """Uniform shift to every year's revenue growth that makes the DCF value equal today's price
    (answers: what business performance is needed to justify the current price?). None if not bracketed."""
    if not ok(price) or price <= 0:
        return None

    def f(d):
        try:
            fc = forecast_operating(apply_delta(a, ScenarioDelta(growth=d)))["fcff"]
            return run_dcf(fc, wacc, g, bridge, shares)["per_share"] - price
        except ValuationError:
            return None

    flo, fhi = f(lo), f(hi)
    if flo is None or fhi is None or flo * fhi > 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        fm = f(mid)
        if fm is None:
            return None
        if flo * fm <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2
