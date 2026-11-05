"""Export tests: Excel sheets/formulas, PDF sections, CSV bundle."""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_pipeline import inputs_for, make_detail, peers_snapshot  # noqa: E402
from valuation import exports as X  # noqa: E402
from valuation.data import build_company  # noqa: E402
from valuation.pipeline import compute_all  # noqa: E402

SHEETS = ["Company Information", "Historical Financials", "Historical Ratios", "FCFF Calculation", "Forecast Assumptions",
          "WACC Calculation", "DCF Valuation", "Sensitivity Analysis", "Relative Valuation", "Scenario Analysis", "Final Summary"]


@pytest.fixture(scope="module")
def model():
    c = build_company(make_detail())
    inp = inputs_for(c)
    inp["peers"]["symbols"] = ["P1", "P2", "P3"]
    return c, inp, compute_all(c, inp, peers_snapshot())


def test_excel_has_all_sheets_and_formulas(model):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(X.to_excel(*model)))
    assert wb.sheetnames == SHEETS
    assert str(wb["DCF Valuation"]["B36"].value).startswith("=")       # per-share value is a live formula
    assert str(wb["WACC Calculation"]["B15"].value).startswith("=")
    assert wb["Forecast Assumptions"]["B6"].font.color.rgb.endswith("0000FF")   # manual input is blue


@pytest.mark.skipif(shutil.which("soffice") is None, reason="LibreOffice not installed")
def test_excel_formulas_recalculate_to_python_values(model):
    import openpyxl
    c, inp, R = model
    d = tempfile.mkdtemp()
    open(f"{d}/m.xlsx", "wb").write(X.to_excel(c, inp, R))
    subprocess.run(["soffice", "--headless", "--convert-to", "xlsx", "--outdir", f"{d}/out", f"{d}/m.xlsx"], capture_output=True, timeout=180)
    wb = openpyxl.load_workbook(f"{d}/out/m.xlsx", data_only=True)
    assert wb["DCF Valuation"]["B36"].value == pytest.approx(R["iv"], rel=1e-9)
    assert wb["WACC Calculation"]["B17"].value == pytest.approx(R["wacc_used"], rel=1e-9)
    s = wb["Sensitivity Analysis"]
    for i, row in enumerate(R["sens"]["wacc_g"]["grid"]):
        for j, v in enumerate(row):
            assert s.cell(row=4 + i, column=2 + j).value == pytest.approx(v, rel=1e-9)


def test_pdf_contains_all_sections(model):
    from pypdf import PdfReader
    txt = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(X.to_pdf(*model))).pages)
    assert "Fundamental Valuation & Investment Research Report" in " ".join(txt.split())   # title wraps across lines
    assert all(f"{i}. " in txt for i in range(1, 14)) and "D&A;" not in txt


def test_csv_bundle(model):
    z = zipfile.ZipFile(io.BytesIO(X.to_csv_zip(*model)))
    names = z.namelist()
    assert any("forecast_projection" in n for n in names) and any("sensitivity_wacc_g" in n for n in names)
