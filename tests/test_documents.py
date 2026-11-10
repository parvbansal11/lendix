import csv
import io
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook
from PIL import Image
from pypdf import PdfReader

from app.documents import DocumentService, TEMPLATES, WATERMARK


@pytest.fixture
def loan():
    return {
        "id": "L-1043", "name": "Meera Vasant", "status": "approved", "decision": "approved",
        "monthly_income": 50000, "requested_amount": 150000, "approved_amount": 150000,
        "tenure_months": 12, "annual_rate": .15, "fees": {"processing": 1500, "tax": 270},
        "credit": {"recommendation": "approve", "eligible_amount": 180000, "recommended_amount": 150000,
                   "max_emi": 25000, "emi": 13538.75, "foir": .27, "apr": .1721,
                   "total_repayment": 162465, "total_interest": 12465, "total_cost": 164235},
        "risk": {"band": "low", "flags": [], "source_status": "verified"},
        "consent": True, "consent_at": "2026-11-11T08:30:00+05:30", "consent_by": "Meera Vasant",
        "consent_purposes": ["Identity verification", "Income verification"],
        "cooling_off_days": 3, "grievance_contact": "grievances@saakh.example",
        "documents": ["identity", "income_statement"], "missing_documents": [],
    }


PDF_KINDS = [kind for kind in TEMPLATES if kind not in {"review_queue", "portfolio_dashboard", "audit_log", "missing_documents_request", "payment_reminder"}]


@pytest.mark.parametrize("kind", PDF_KINDS)
def test_required_pdf_templates_have_identity_and_watermark(tmp_path, loan, kind):
    artifact = DocumentService(tmp_path).render(kind, loan, {"reasons": ["The supplied information was reviewed."], "summary": "Synthetic application review."})
    reader = PdfReader(artifact["path"])
    assert reader.pages
    for page in reader.pages:
        text = page.extract_text()
        assert WATERMARK in text
        assert loan["id"] in text
        assert artifact["document_id"] in text
        assert artifact["created_at"][:10] in text
    assert artifact["mime_type"] == "application/pdf"


def test_kfs_discloses_all_required_terms(tmp_path, loan):
    artifact = DocumentService(tmp_path).render("kfs", loan)
    text = "\n".join(p.extract_text() for p in PdfReader(artifact["path"]).pages)
    for value in ("150,000.00", "17.21%", "processing: 1500", "tax: 270", "13,538.75", "14,235.00", "Cooling-off", "grievances@saakh.example", "without penalty"):
        assert value in text
    assert "15.00%" in text
    assert "164,235.00" in text


def test_live_rules_and_payment_fields_are_rendered(tmp_path, loan):
    loan["risk"] = {"band": "high", "codes": ["PAYOUT_NAME_MISMATCH"], "explanations": ["The payout holder differs from the applicant."], "missing_sources": []}
    loan["outstanding"] = 148926.25
    artifact = DocumentService(tmp_path).render("risk_report", loan)
    text = "\n".join(p.extract_text() for p in PdfReader(artifact["path"]).pages)
    assert "PAYOUT_NAME_MISMATCH" in text
    assert "The payout holder differs" in text
    assert "All required sources available" in text
    receipt = {"payment_reference": "PAY-123", "emi": 13538.75, "paid_at": "2026-12-11", "installment": 1}
    artifact = DocumentService(tmp_path).render("payment_receipt", loan, receipt)
    text = "\n".join(p.extract_text() for p in PdfReader(artifact["path"]).pages)
    for value in ("PAY-123", "13,538.75", "148,926.25", "2026-12-11"):
        assert value in text


def test_long_schedule_repeats_watermark_on_every_page(tmp_path, loan):
    rows = [{"installment": n, "due_date": "2027-01-01", "emi": 1000, "principal": 800, "interest": 200, "balance": 60000 - n * 800} for n in range(1, 61)]
    artifact = DocumentService(tmp_path).render("repayment_schedule", loan, {"schedule": rows})
    pages = PdfReader(artifact["path"]).pages
    assert len(pages) >= 2
    assert all(WATERMARK in page.extract_text() for page in pages)
    assert "60" in pages[-1].extract_text()


@pytest.mark.parametrize("kind", ["missing_documents_request", "payment_reminder", "application_acknowledgment", "rejection_letter", "grievance_acknowledgment"])
def test_chat_templates_have_metadata(tmp_path, loan, kind):
    artifact = DocumentService(tmp_path).render(kind, loan, format="chat")
    text = Path(artifact["path"]).read_text()
    assert WATERMARK in text and loan["id"] in text and artifact["document_id"] in text


@pytest.mark.parametrize("kind", ["consent_record", "audit_log"])
def test_json_exports_include_metadata_and_records(tmp_path, loan, kind):
    artifact = DocumentService(tmp_path).render(kind, loan, {"events": [{"action": "reviewed"}]}, "json")
    payload = json.loads(Path(artifact["path"]).read_text())
    assert payload["metadata"]["watermark"] == WATERMARK
    assert payload["metadata"]["document_id"] == artifact["document_id"]
    if kind == "consent_record":
        assert payload["consent"]["consent"] is True
        assert payload["consent"]["consent_by"] == loan["name"]
    else:
        assert payload["events"] == [{"action": "reviewed"}]


def test_queue_csv_and_excel_are_inert_and_typed(tmp_path, loan):
    rows = [dict(loan, name="=1+1")]
    service = DocumentService(tmp_path)
    csv_artifact = service.render("daily_review_queue", {}, {"rows": rows}, "csv")
    table = list(csv.reader(io.StringIO(Path(csv_artifact["path"]).read_text(encoding="utf-8-sig"))))
    assert table[1][1] == "'=1+1"
    assert any(WATERMARK in row for row in table)
    artifact = service.render("review_queue", {}, {"rows": rows}, "xlsx")
    workbook = load_workbook(artifact["path"])
    sheet = workbook.active
    assert sheet["B4"].value == "'=1+1"
    assert sheet["B4"].data_type != "f"
    assert sheet["C4"].value == 150000
    assert sheet.freeze_panes == "A4"
    assert any(cell.value == WATERMARK for row in sheet for cell in row)
    assert any(cell.value == artifact["document_id"] for row in sheet for cell in row)


def test_schedule_csv_preserves_supplied_math(tmp_path, loan):
    data = {"schedule": [{"installment": 1, "due_date": "2027-01-01", "emi": 100.12, "principal": 90.02, "interest": 10.1, "balance": 500.04}]}
    artifact = DocumentService(tmp_path).render("repayment_schedule", loan, data, "csv")
    rows = list(csv.reader(Path(artifact["path"]).open(encoding="utf-8-sig")))
    assert rows[1] == ["1", "2027-01-01", "100.12", "90.02", "10.1", "500.04"]


@pytest.mark.parametrize("kind", ["risk_chart", "risk_report", "portfolio_dashboard"])
def test_charts_include_metadata_and_pixels(tmp_path, loan, kind):
    artifact = DocumentService(tmp_path).render(kind, loan, {"loans": [loan]}, "png")
    with Image.open(artifact["path"]) as chart:
        assert chart.width >= 1200
        assert chart.info["Description"] == WATERMARK
        assert chart.info["Document ID"] == artifact["document_id"]
        assert chart.info["Loan ID"] == loan["id"]
        assert len(chart.convert("RGB").getcolors(2_000_000)) > 100


def test_output_path_cannot_escape_directory(tmp_path, loan):
    loan["id"] = "../../private/escape"
    artifact = DocumentService(tmp_path).render("application_acknowledgment", loan)
    assert Path(artifact["path"]).parent == tmp_path.resolve()


def test_unknown_template_and_format_rejected(tmp_path, loan):
    service = DocumentService(tmp_path)
    with pytest.raises(ValueError):
        service.render("unknown", loan)
    with pytest.raises(ValueError):
        service.render("sanction_letter", loan, format="exe")
    with pytest.raises(ValueError):
        service.render("audit_log", loan)
