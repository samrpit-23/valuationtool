# Stock Screener Lite  +  Valuation & Research module

## How it works
1. `scraper.py` pulls company pages from screener.in -> `data/details/*.json` + `data/snapshot.csv`
2. GitHub Actions runs it every weekday evening and commits the data (free storage)
3. Streamlit Cloud redeploys on each commit; `app.py` only reads local files, so it is instant

## Tabs
Ask | Screener | Company | **Valuation & Research** (new)

## Valuation & Research module (`valuation/`)
| File | Role |
|---|---|
| `valuation/engine.py` | Pure calculation engine (FCFF, WACC, DCF, bridge, FCFE, DDM, residual income, NAV, SOTP, multiples, scenarios, sensitivity, reverse DCF, data-quality). No UI, no I/O. |
| `valuation/data.py` | Reads the existing company JSON, normalises units, builds the field-mapping / missing-data report. |
| `valuation/pipeline.py` | Connects data + engine, builds editable starting assumptions from history, collects warnings. |
| `valuation/report.py` | Tables, 13-section analyst report, risk analysis, charts. |
| `valuation/exports.py` | Excel (live formulas), PDF, CSV bundle. |
| `valuation/ui.py` | Streamlit interface only. |
| `tests/` | 54 tests (formulas vs independent Decimal calculations, validation, reconciliation, exports). |

Run the tests:  `pip install pytest pypdf && python -m pytest -q tests`

## Setup
1. Push everything to GitHub (keep the `valuation/` and `tests/` folders and `.github/workflows/scrape.yml`).
2. Settings -> Actions -> General -> Workflow permissions -> Read and write.
3. Actions -> "Scrape Screener data" -> Run workflow (rebuilds `data/` with the new columns).
4. share.streamlit.io -> deploy `app.py`. Optional AI search: add `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` under Secrets.

## Data limits (never invented)
Screener does not provide beta, diluted shares, minority interest or segment data - these are manual inputs.
Cash, inventory, receivables, payables, leases and actual capex are only available through the module's
"Fetch live data + detailed schedules" option (best effort); otherwise they are estimated or entered manually,
and every estimate is flagged in the warnings.
