"""Run with:  python -m pytest -q tests"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from valuation import engine as E  # noqa: E402

FCFF = [100, 110, 120, 130, 140]
BRIDGE = E.Bridge(debt=200, leases=30, minority=20, preferred=0, cash=80, nonop=50)


# ------------------------------------------------------------------ known example (Decimal, calculated independently)
def test_dcf_known_example():
    r = E.run_dcf(FCFF, 0.10, 0.04, BRIDGE, 10)
    assert r["pv_sum"] == pytest.approx(447.69669235211206, rel=1e-12)
    assert r["tv"] == pytest.approx(2426.6666666666667, rel=1e-12)
    assert r["pv_tv"] == pytest.approx(1506.7690772902166, rel=1e-12)
    assert r["ev"] == pytest.approx(1954.4657696423286, rel=1e-12)
    assert r["bridge"]["equity"] == pytest.approx(1834.4657696423286, rel=1e-12)
    assert r["per_share"] == pytest.approx(183.44657696423286, rel=1e-12)


def test_dcf_reconciliation():
    r = E.run_dcf(FCFF, 0.10, 0.04, BRIDGE, 10)
    b = r["bridge"]
    assert r["ev"] == pytest.approx(sum(r["pv"]) + r["pv_tv"])
    assert b["equity"] == pytest.approx(r["ev"] - b["debt"] - b["leases"] - b["minority"] - b["preferred"] + b["cash"] + b["nonop"])
    assert b["per_share"] * b["shares"] == pytest.approx(b["equity"])
    assert r["tv_share"] == pytest.approx(r["pv_tv"] / r["ev"])


def test_tv_share_warning():
    r = E.dcf(FCFF, 0.10, 0.04)
    assert r["tv_share"] > 0.75 and any("Terminal value represents" in w for w in r["warnings"])


def test_exit_multiple_is_separate_from_gordon():
    r = E.dcf(FCFF, 0.10, 0.04, tv_method="exit_multiple", exit_multiple=12, final_ebitda=200)
    assert r["tv"] == 2400 and r["pv_tv"] == pytest.approx(2400 / 1.1 ** 5)
    assert r["tv_method"] == "exit_multiple" and "implied_growth" in r


# ------------------------------------------------------------------ WACC (hand calculation)
def test_wacc_known_example():
    w = E.wacc_calc(rf=0.07, beta=1.1, erp=0.055, kd=0.09, tax=0.25, mv_equity=800, debt=200)
    assert w["ke"] == pytest.approx(0.1305)
    assert w["kd_after_tax"] == pytest.approx(0.0675)
    assert (w["we"], w["wd"]) == (pytest.approx(0.8), pytest.approx(0.2))
    assert w["wacc"] == pytest.approx(0.8 * 0.1305 + 0.2 * 0.0675)  # 11.79 %


def test_wacc_no_debt_equals_cost_of_equity():
    w = E.wacc_calc(0.07, 1.0, 0.05, None, 0.25, 500, 0)
    assert w["wacc"] == pytest.approx(w["ke"])


@pytest.mark.parametrize("kw", [dict(rf=None), dict(beta=None), dict(erp=None), dict(mv_equity=0), dict(debt=-1), dict(tax=1.2), dict(beta=-0.5)])
def test_wacc_invalid_inputs(kw):
    base = dict(rf=0.07, beta=1.0, erp=0.05, kd=0.09, tax=0.25, mv_equity=100, debt=10)
    base.update(kw)
    with pytest.raises(E.ValuationError):
        E.wacc_calc(**base)


# ------------------------------------------------------------------ validation
def test_terminal_growth_must_be_below_wacc():
    for g in (0.10, 0.12):
        with pytest.raises(E.ValuationError):
            E.dcf(FCFF, 0.10, g)


def test_wacc_must_be_positive():
    with pytest.raises(E.ValuationError):
        E.dcf(FCFF, 0.0, 0.0)


def test_negative_terminal_fcff_blocks_gordon():
    with pytest.raises(E.ValuationError, match="not positive"):
        E.dcf([10, 5, -3], 0.10, 0.03)


def test_invalid_shares_and_bridge():
    for sh in (0, -5, None):
        with pytest.raises(E.ValuationError):
            E.equity_bridge(1000, E.Bridge(), sh)
    with pytest.raises(E.ValuationError):
        E.equity_bridge(1000, E.Bridge(debt=-1), 10)


def test_negative_equity_value_is_flagged_not_hidden():
    r = E.run_dcf([1, 1, 1], 0.10, 0.02, E.Bridge(debt=1000), 10)
    assert r["per_share"] < 0 and any("zero or negative" in w for w in r["warnings"])


# ------------------------------------------------------------------ operating forecast / FCFF / sign conventions
def make_assump(**over):
    n = 3
    base = dict(base_revenue=1000, revenue_growth=[0.10] * n, ebit_margin=[0.20] * n, tax_rate=[0.25] * n,
                dep_pct=[0.04] * n, capex_pct=[0.05] * n, nwc_pct=[0.10] * n, nwc0=100)
    base.update(over)
    return E.Assumptions(**base)


def test_forecast_fcff_by_hand():
    f = E.forecast_operating(make_assump())
    # year 1: revenue 1100, EBIT 220, tax 55, NOPAT 165, D&A 44, CapEx 55, NWC 110 (d = 10)
    assert f["revenue"][0] == pytest.approx(1100)
    assert f["ebit"][0] == pytest.approx(220) and f["ebitda"][0] == pytest.approx(264)
    assert f["nopat"][0] == pytest.approx(165)
    assert f["d_nwc"][0] == pytest.approx(10)
    assert f["fcff"][0] == pytest.approx(165 + 44 - 55 - 10)  # 144
    # year 2: revenue 1210, NOPAT 181.5, D&A 48.4, CapEx 60.5, d_NWC = 121-110 = 11
    assert f["fcff"][1] == pytest.approx(181.5 + 48.4 - 60.5 - 11)


def test_nwc_sign_convention():
    """A higher NWC requirement must REDUCE free cash flow."""
    low = E.forecast_operating(make_assump(nwc_pct=[0.10] * 3))["fcff"][0]
    high = E.forecast_operating(make_assump(nwc_pct=[0.20] * 3))["fcff"][0]
    assert high < low


def test_mode_a_equals_mode_b_when_fed_same_cash_flows():
    f = E.forecast_operating(make_assump())["fcff"]
    growths = [f[i] / f[i - 1] - 1 for i in range(1, 3)]
    mode_a = E.forecast_fcff_growth(f[0], growths)
    assert mode_a == pytest.approx(f[1:])


def test_assumption_validation():
    with pytest.raises(E.ValuationError):
        E.forecast_operating(make_assump(tax_rate=[0.25, 0.25]))      # wrong length
    with pytest.raises(E.ValuationError):
        E.forecast_operating(make_assump(base_revenue=-5))
    with pytest.raises(E.ValuationError):
        E.forecast_operating(make_assump(tax_rate=[0.9] * 3))


# ------------------------------------------------------------------ historical FCFF
def test_historical_fcff_and_cagr():
    s = {"years": ["Mar 2023", "Mar 2024"], "revenue": [1000, 1100], "operating_profit": [200, 230],
         "depreciation": [40, 44], "tax_pct": [25, 25], "pat": [120, 140], "eps": [12, 14],
         "equity_capital": [10, 10], "reserves": [490, 590], "borrowings": [100, 120],
         "fixed_assets": [400, 430], "cwip": [20, 30], "wc_days": [36.5, 36.5], "cfo": [150, 170]}
    h, meta = E.derive_history(s)
    # capex (net block) 2024 = (430+30) - (400+20) + 44 = 84 ; NWC = 36.5/365 * revenue = 100 -> 110
    assert h["capex"][1] == pytest.approx(84) and meta["capex_method"] == "net-block estimate"
    assert h["nwc"] == [pytest.approx(100), pytest.approx(110)] and h["d_nwc"][1] == pytest.approx(10)
    # FCFF 2024 = EBIT(1-t) + D&A - capex - dNWC = 186*0.75 + 44 - 84 - 10 = 89.5
    assert h["fcff"][1] == pytest.approx(89.5)
    assert h["ebit"][1] == pytest.approx(186) and h["ebitda_margin"][1] == pytest.approx(230 / 1100)
    assert h["roe"][1] == pytest.approx(140 / ((500 + 600) / 2))
    assert h["de"][1] == pytest.approx(120 / 600) and h["fcf"][1] == pytest.approx(170 - 84)
    assert E.series_cagr([100, 110, 121, 133.1], 3) == pytest.approx(0.10)


def test_capex_never_negative_and_flagged():
    s = {"years": ["Mar 2023", "Mar 2024"], "revenue": [1000, 1000], "operating_profit": [200, 200], "depreciation": [10, 10],
         "fixed_assets": [500, 300], "cwip": [0, 0]}
    h, meta = E.derive_history(s)
    assert h["capex"][1] == 0.0 and meta["capex_floored"] == ["Mar 2024"]


def test_tax_fallback_flagged_when_tax_missing():
    s = {"years": ["Mar 2024"], "revenue": [100], "operating_profit": [20], "depreciation": [2]}
    h, meta = E.derive_history(s)
    assert h["tax_rate"][0] == E.STATUTORY_TAX and meta["tax_fallback_years"] == ["Mar 2024"]


# ------------------------------------------------------------------ sensitivity & scenarios reconcile to the base DCF
def test_sensitivity_centre_equals_base():
    base = E.run_dcf(FCFF, 0.10, 0.04, BRIDGE, 10)["per_share"]
    grid = E.sens_wacc_g(FCFF, [0.09, 0.10, 0.11], [0.03, 0.04, 0.05], BRIDGE, 10)
    assert grid[1][1] == pytest.approx(base)
    assert grid[0][1] > grid[1][1] > grid[2][1]       # higher WACC -> lower value
    assert grid[1][0] < grid[1][1] < grid[1][2]       # higher g -> higher value
    assert E.sens_wacc_g(FCFF, [0.04], [0.05], BRIDGE, 10)[0][0] is None  # invalid cell is blank, not an error


def test_scenarios_ordering_and_base_reconciliation():
    a = make_assump()
    res = {k: E.run_scenario(a, 0.11, 0.04, d, BRIDGE, 10, price=100) for k, d in E.DEFAULT_DELTAS.items()}
    assert res["Bear"]["per_share"] < res["Base"]["per_share"] < res["Bull"]["per_share"]
    direct = E.run_dcf(E.forecast_operating(a)["fcff"], 0.11, 0.04, BRIDGE, 10)["per_share"]
    assert res["Base"]["per_share"] == pytest.approx(direct)
    assert res["Base"]["mos"] == pytest.approx((direct - 100) / direct * 100)


def test_growth_margin_grid_centre_equals_base():
    a = make_assump()
    grid = E.sens_growth_margin(a, [-0.01, 0, 0.01], [-0.01, 0, 0.01], 0.11, 0.04, BRIDGE, 10)
    base = E.run_dcf(E.forecast_operating(a)["fcff"], 0.11, 0.04, BRIDGE, 10)["per_share"]
    assert grid[1][1] == pytest.approx(base) and grid[2][2] > grid[0][0]


# ------------------------------------------------------------------ other models
def test_ddm_known_values():
    assert E.ddm_single(10, 0.12, 0.04)["value"] == pytest.approx(130.0)
    assert E.ddm_two_stage(10, 0.12, 0.15, 3, 0.05)["value"] == pytest.approx(194.01535167638484, rel=1e-12)
    with pytest.raises(E.ValuationError):
        E.ddm_single(10, 0.05, 0.05)
    with pytest.raises(E.ValuationError):
        E.ddm_single(0, 0.12, 0.04)


def test_residual_income_identity():
    """If ROE equals the cost of equity, residual income is zero and value equals book value."""
    r = E.residual_income(1000, [0.12] * 5, 0.3, 0.12, 0.03, 10)
    assert r["equity"] == pytest.approx(1000) and r["per_share"] == pytest.approx(100)
    better = E.residual_income(1000, [0.18] * 5, 0.3, 0.12, 0.03, 10)
    assert better["equity"] > 1000
    with pytest.raises(E.ValuationError):
        E.residual_income(-5, [0.1], 0.3, 0.12, 0.03, 10)


def test_fcfe_is_equity_value_and_no_net_debt_deduction():
    a = make_assump()
    r = E.fcfe_model(a, other_income=0, kd=0.09, debt0=100, debt_ratio=0.3, ke=0.13, g=0.04)
    pv = sum(x / 1.13 ** (i + 1) for i, x in enumerate(r["fcfe"]))
    tv = r["fcfe"][-1] * 1.04 / (0.13 - 0.04)
    assert r["equity"] == pytest.approx(pv + tv / 1.13 ** 3)
    # year-1 hand check: reinvestment = 55 - 44 + 10 = 21 ; borrowing = 6.3 ; interest = 9 ; PAT = (220-9)*0.75
    assert r["net_borrowing"][0] == pytest.approx(6.3) and r["pat"][0] == pytest.approx((220 - 9) * 0.75)
    with pytest.raises(E.ValuationError):
        E.fcfe_model(a, 0, 0.09, 100, 0.3, 0.13, 0.15)


def test_nav_and_sotp():
    n = E.nav(1000, 10, {"land revaluation": 200})
    assert n["bvps"] == 100 and n["adjusted_bvps"] == 120
    seg = [{"name": "A", "ebitda": 100, "method": "EV/EBITDA", "multiple": 10},
           {"name": "B", "revenue": 500, "method": "EV/Sales", "multiple": 2}]
    s = E.sotp(seg, corporate_cost=10, corporate_multiple=8, bridge=E.Bridge(debt=300, cash=100), shares=10)
    assert s["ev"] == 1000 + 1000 - 80 and s["per_share"] == pytest.approx((1920 - 300 + 100) / 10)
    with pytest.raises(E.ValuationError):
        E.sotp([{"name": "C", "method": "EV/EBITDA", "multiple": 10}], 0, 0, E.Bridge(), 10)


# ------------------------------------------------------------------ relative valuation
def test_multiples_and_not_meaningful_cases():
    m, notes = E.multiples(price=100, shares=10, eps=5, bvps=40, revenue=800, ebitda=150, ebit=120, fcff=60, fcf=50,
                           dps=2, bridge=E.Bridge(debt=200, cash=50), avg_eps=4, eps_growth=0.10)
    assert m["pe"] == 20 and m["pb"] == 2.5 and m["mcap"] == 1000 and m["ev"] == 1150
    assert m["ev_ebitda"] == pytest.approx(1150 / 150) and m["peg"] == pytest.approx(2.0) and m["div_yield"] == 2.0
    neg, nn = E.multiples(100, 10, -3, 40, 800, -5, 1, -2, -2, 0, E.Bridge())
    assert neg["pe"] is None and "pe" in nn and neg["ev_ebitda"] is None and neg["p_fcf"] is None


def test_peer_stats_and_implied_prices():
    st = E.peer_stats([10, 20, 30, 40, None, -5])
    assert st["n"] == 4 and st["mean"] == 25 and st["median"] == 25 and st["p25"] == 17.5 and st["p75"] == 32.5
    assert E.implied_price_multiple(20, 5) == 100 and E.implied_price_multiple(20, -1) is None
    r = E.implied_price_ev_ebitda(10, 150, E.Bridge(debt=200, cash=50), 10)
    assert r["ev"] == 1500 and r["per_share"] == pytest.approx((1500 - 200 + 50) / 10)  # EV -> equity -> per share
    r2 = E.implied_price_ev_ebitda(10, 150, E.Bridge(debt=200, cash=50), 10, include_cash=False)
    assert r2["per_share"] == pytest.approx((1500 - 200) / 10)


# ------------------------------------------------------------------ margin of safety / returns / classification / quality
def test_margin_of_safety_and_returns():
    assert E.margin_of_safety(150, 100) == pytest.approx(100 / 3)
    assert E.upside(150, 100) == pytest.approx(50)
    assert E.margin_of_safety(-5, 100) is None
    er = E.expected_return(100, 161.051, 5, dividend_yield=2)
    assert er["price_cagr"] == pytest.approx(0.10, abs=1e-6) and er["total"] == pytest.approx(0.12, abs=1e-6)


def test_classification_and_quality():
    assert E.classify_business("Financial Services", "Private Sector Bank") == "financial"
    assert E.classify_business("Healthcare", "Life Insurance") == "insurance"
    assert E.classify_business("Information Technology", "IT Services") == "general"
    full = E.data_quality({"a": ("reported", 1), "b": ("reported", 1)}, 2)
    poor = E.data_quality({"a": ("missing", 1), "b": ("derived", 1)}, 120, extra_flags=2)
    assert full["label"] == "High" and poor["label"] == "Low" and poor["score"] < full["score"]
    assert math.isfinite(poor["score"])


def test_reverse_dcf_recovers_price():
    a = make_assump()
    target = E.run_dcf(E.forecast_operating(E.apply_delta(a, E.ScenarioDelta(growth=0.02)))["fcff"], 0.11, 0.04, BRIDGE, 10)["per_share"]
    d = E.reverse_growth_delta(a, 0.11, 0.04, BRIDGE, 10, target)
    assert d == pytest.approx(0.02, abs=1e-6)
    assert E.reverse_growth_delta(a, 0.11, 0.04, BRIDGE, 10, 1e9) is None      # unreachable price -> no answer, no crash
