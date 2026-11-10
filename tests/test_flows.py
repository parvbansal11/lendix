from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import ROOT, Settings
from app.model import FakeClient
from app.orchestrator import Orchestrator
from app.store import Store


@pytest.fixture
def office(tmp_path):
    settings = Settings(gemini_api_key="test-only", model_fast="fast-test", model_deep="deep-test",
                        db_path=str(tmp_path / "loans.db"), output_dir=str(tmp_path / "out"), demo_skip_cooling=True)
    store = Store(settings.db_path)
    store.seed(ROOT / "data" / "seed.json")
    client = FakeClient()
    orchestrator = Orchestrator(store, settings, client)
    yield orchestrator, store, client
    store.engine.dispose()


def artifacts(store, loan_id):
    return [item for item in store.all("file") if item["loan_id"] == loan_id]


@pytest.mark.asyncio
async def test_mismatched_application_complete_to_respectful_rejection(office):
    runner, store, model = office
    missing = await runner.run("review L-1042", role="ops")
    assert "bank statement" in missing.lower()
    assert store.get("loan", "L-1042")["status"] == "awaiting_documents"
    assert not model.calls
    review = await runner.run("add bank statement L-1042", role="ops")
    loan = store.get("loan", "L-1042")
    assert loan["status"] == "awaiting_decision"
    assert loan["risk"]["band"] == "high"
    assert {"PAYOUT_NAME_MISMATCH", "NEW_NUMBER"} <= set(loan["risk"]["codes"])
    assert loan["credit"]["recommendation"] == "decline"
    assert "credit officer" in review.lower()
    assert len(model.calls) <= 6
    assert any(event["kind"] == "mention" and "@officer" in event["text"] for event in store.all_events())
    assert {"risk_report", "credit_appraisal"} <= {item["kind"] for item in artifacts(store, "L-1042")}
    assert any(item["mime_type"] == "image/png" for item in artifacts(store, "L-1042"))
    rejected = await runner.run("decline L-1042", role="credit_officer")
    assert "account in your own name" in rejected
    assert store.get("loan", "L-1042")["status"] == "declined"
    assert any(item["kind"] == "rejection_letter" for item in artifacts(store, "L-1042"))
    assert all(Path(item["path"]).stat().st_size > 100 for item in artifacts(store, "L-1042"))
    document_ids = {item["document_id"] for item in artifacts(store, "L-1042")}
    logged = {event["data"].get("document_id") for event in store.all_events() if event["kind"] == "document"}
    assert document_ids <= logged


@pytest.mark.asyncio
async def test_clean_application_full_lifecycle_and_repeat_guards(office):
    runner, store, _ = office
    await runner.run("review L-1043", role="ops")
    loan = store.get("loan", "L-1043")
    assert loan["risk"]["band"] == "low" and loan["status"] == "awaiting_decision"
    approval = await runner.run("approve L-1043", role="credit_officer")
    assert "approval recorded" in approval.lower()
    assert store.get("loan", "L-1043")["kfs_issued"]
    blocked = await runner.run("disburse L-1043", role="ops")
    assert "Payout blocked" in blocked and "Key Facts Statement" in blocked
    await runner.run("accept KFS L-1043", role="borrower")
    accepted_at = store.get("loan", "L-1043")["kfs_accepted_at"]
    assert "already recorded" in await runner.run("accept KFS L-1043", role="borrower")
    assert store.get("loan", "L-1043")["kfs_accepted_at"] == accepted_at
    payout = await runner.run("disburse L-1043", role="ops")
    assert "No money moved" in payout
    loan = store.get("loan", "L-1043")
    assert loan["status"] == "disbursed" and loan["simulated_utr"].startswith("SIM-")
    assert len(loan["schedule"]) == loan["tenure_months"]
    assert loan["schedule"][-1]["balance"] == 0
    original_utr = loan["simulated_utr"]
    assert "no duplicate" in (await runner.run("disburse L-1043", role="ops")).lower()
    assert store.get("loan", "L-1043")["simulated_utr"] == original_utr
    await runner.run("pay EMI L-1043", role="borrower")
    assert store.get("loan", "L-1043")["paid_installments"] == 1
    await runner.run("fast-forward L-1043 to closure", role="ops")
    loan = store.get("loan", "L-1043")
    assert loan["status"] == "closed" and loan["outstanding"] == 0
    assert {"sanction_letter", "key_facts_statement", "loan_agreement_summary", "disbursement_advice", "repayment_schedule", "payment_receipt", "no_dues_certificate"} <= {item["kind"] for item in artifacts(store, "L-1043")}
    count = len(artifacts(store, "L-1043"))
    assert "already closed" in await runner.run("close L-1043", role="ops")
    assert len(artifacts(store, "L-1043")) == count
    decisions = [event for event in store.all_events() if event["kind"] == "decision"]
    assert any(event["data"].get("role") == "credit_officer" for event in decisions)


@pytest.mark.asyncio
async def test_hardship_requests_officer_review_without_promising_terms(office):
    runner, store, model = office
    reply = await runner.run("L-1044 I lost my job and can't pay", role="borrower")
    assert "sorry" in reply and "no revised terms are approved" in reply
    assert store.get("loan", "L-1044")["status"] == "restructure_requested"
    assert ("Collections", "CollectionsOutput") in model.calls
    assert any(item["kind"] == "restructure_request" for item in artifacts(store, "L-1044"))
    assert any(event["kind"] == "mention" and event["loan_id"] == "L-1044" for event in store.all_events())
    await runner.run("close L-1044", role="ops")
    assert any(item["kind"] == "no_dues_certificate" for item in artifacts(store, "L-1044"))


@pytest.mark.asyncio
async def test_today_batch_returns_twenty_reviews_queue_excel_and_chart(office):
    runner, store, _ = office
    reply = await runner.run("Review today's 20 applications", role="ops")
    assert "20 applications processed" in reply
    batch = [loan for loan in store.all("loan") if loan.get("today_batch")]
    assert len(batch) == 20
    assert all(loan["status"] == "awaiting_decision" for loan in batch)
    assert all(not loan.get("human_approved") for loan in batch)
    outputs = artifacts(store, "PORTFOLIO")
    assert {"text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "image/png"} <= {item["mime_type"] for item in outputs}
    progress = [event for event in store.all_events() if event["kind"] == "progress"]
    assert len(progress) >= 5 and "20/20" in progress[-1]["text"]


@pytest.mark.asyncio
async def test_night_reminder_scheduled_then_delivered_once(office):
    runner, store, _ = office
    night = datetime(2026, 11, 11, 22, tzinfo=ZoneInfo("Asia/Kolkata"))
    reply = await runner.run("send a reminder now L-1044", role="ops", now=night)
    assert "scheduled" in reply and "08:00" in reply
    scheduled = store.get("reminder", "L-1044")
    assert scheduled["status"] == "scheduled"
    assert scheduled["due_at"] == "2026-11-12T08:00:00+05:30"
    assert not any(event["kind"] == "tool" and event["text"] == "draft_reminder" for event in store.all_events())
    morning = datetime(2026, 11, 12, 8, tzinfo=ZoneInfo("Asia/Kolkata"))
    await runner.deliver_due_reminders(morning)
    await runner.deliver_due_reminders(morning)
    assert store.get("reminder", "L-1044")["status"] == "delivered"
    assert sum(event["kind"] == "tool" and event["text"] == "draft_reminder" for event in store.all_events()) == 1


@pytest.mark.asyncio
async def test_grievance_is_recorded_with_reference_and_acknowledgment(office):
    runner, store, _ = office
    reply = await runner.run("I have a complaint about L-1044", role="borrower")
    assert "GRV-" in reply and "30 days" in reply and "RBI Ombudsman" in reply
    assert len(store.all("grievance")) == 1
    assert any(item["kind"] == "grievance_acknowledgment" for item in artifacts(store, "L-1044"))
