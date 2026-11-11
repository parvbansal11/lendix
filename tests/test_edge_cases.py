"""Regression coverage for upload isolation, explicit consent and recoverable work."""
import json
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.main import create_app
from app.model import FakeClient
from app.orchestrator import Orchestrator
from app.store import Store


def settings_for(tmp_path):
    return Settings(gemini_api_key="test-only", model_fast="fast-test", model_deep="deep-test",
                    db_path=str(tmp_path / "loans.db"), output_dir=str(tmp_path / "out"), demo_skip_cooling=True)


@pytest.fixture
def web_office(tmp_path):
    app = create_app(settings_for(tmp_path), FakeClient())
    with TestClient(app) as client:
        yield client, app.state.store, app.state.orchestrator


def wait_for_job(client, response):
    assert response.status_code == 202, response.text
    identifier = response.json()["job_id"]
    for _ in range(250):
        job = client.get(f"/api/jobs/{identifier}").json()
        if job["status"] not in {"queued", "running"}:
            assert job["status"] == "completed", job
            return job
        time.sleep(.01)
    pytest.fail("Background upload did not finish within 2.5 seconds")


def upload_json(client, rows, **form):
    return client.post("/api/upload", files={"file": ("applications.json", json.dumps(rows).encode(), "application/json")},
                       data={"role": "borrower", "conversation_id": "edge-test", **form})


def test_multi_application_upload_creates_distinct_loans(web_office):
    client, store, _ = web_office
    before = {loan["id"] for loan in store.all("loan")}
    response = upload_json(client, [{"name": "Synthetic Anika", "consent": True}, {"name": "Synthetic Dev", "consent": False}])
    wait_for_job(client, response)
    added = [loan for loan in store.all("loan") if loan["id"] not in before]
    assert len(added) == 2
    assert {loan["name"] for loan in added} == {"Synthetic Anika", "Synthetic Dev"}
    assert {loan["name"]: loan["consent"] for loan in added} == {"Synthetic Anika": True, "Synthetic Dev": False}


@pytest.mark.parametrize("consent", ["yes", "true", 1])
def test_json_consent_requires_boolean_not_coercion(web_office, consent):
    client, store, _ = web_office
    before = {loan["id"] for loan in store.all("loan")}
    response = upload_json(client, {"name": "Consent Example", "consent": consent})
    if response.status_code == 202:
        wait_for_job(client, response)
    else:
        assert response.status_code in {400, 422}
    added = [loan for loan in store.all("loan") if loan["id"] not in before]
    assert all(loan.get("consent") is not True for loan in added)


def test_selected_upload_rechecks_state_after_waiting_for_lock(web_office):
    client, store, office = web_office
    client.portal.call(office.lock.acquire)
    try:
        response = upload_json(client, {"name": "Replacement Name"}, loan_id="L-1043")
        assert response.status_code == 202
        # Simulate a preceding review completing while the upload waits its turn.
        reviewed = store.get("loan", "L-1043")
        reviewed.update(status="approved", risk={"band": "low", "codes": []}, approval_source="policy")
        store.put("loan", "L-1043", reviewed)
    finally:
        client.portal.call(office.lock.release)
    wait_for_job(client, response)
    retained = store.get("loan", "L-1043")
    assert retained["status"] == "approved"
    assert retained["name"] == "Mira Desai"
    assert retained["risk"]["band"] == "low"


@pytest.fixture
def office(tmp_path):
    settings = settings_for(tmp_path)
    store = Store(settings.db_path)
    store.seed(ROOT / "data" / "seed.json")
    yield Orchestrator(store, settings, FakeClient()), store
    store.engine.dispose()


@pytest.mark.asyncio
async def test_application_without_name_asks_for_name_first(office):
    runner, store = office
    reply = await runner.run("apply for a loan", role="borrower", conversation_id="new-app")
    assert "full name" in reply.lower()
    created = [loan for loan in store.all("loan") if loan.get("created_from_chat")]
    assert len(created) == 1 and not created[0].get("name")


@pytest.mark.asyncio
async def test_failed_kfs_render_can_be_retried_without_duplicate_approval(office, monkeypatch):
    runner, store = office
    render = runner.documents.render
    failures = []

    def fail_once(kind, *args, **kwargs):
        if kind == "key_facts_statement" and not failures:
            failures.append(True)
            raise ValueError("Synthetic document rendering failure; retry the operation.")
        return render(kind, *args, **kwargs)

    monkeypatch.setattr(runner.documents, "render", fail_once)
    reply = await runner.run("review L-1043", role="ops")
    assert "failure" in reply.lower() or "retry" in reply.lower()
    assert not store.get("loan", "L-1043").get("kfs_issued")
    await runner.run("review L-1043", role="ops")
    assert store.get("loan", "L-1043").get("kfs_issued") is True
    approvals = [event for event in store.all_events() if event["kind"] == "decision" and event["data"].get("role") == "underwriter"]
    assert len(approvals) == 1


def fail_render_once(monkeypatch, runner, kind, format="pdf"):
    render = runner.documents.render
    failed = []

    def wrapper(actual_kind, *args, **kwargs):
        if actual_kind == kind and kwargs.get("format", "pdf") == format and not failed:
            failed.append(True)
            raise ValueError("Synthetic document rendering failure; retry the same operation.")
        return render(actual_kind, *args, **kwargs)

    monkeypatch.setattr(runner.documents, "render", wrapper)
    return failed


def issued(store, kind, loan_id):
    return [item for item in store.all("file") if item["kind"] == kind and item["loan_id"] == loan_id]


async def approve_clean(runner):
    await runner.run("review L-1043", role="ops")


async def disburse_clean(runner):
    await approve_clean(runner)
    await runner.run("accept KFS L-1043", role="borrower")
    await runner.run("disburse L-1043", role="ops")


@pytest.mark.asyncio
async def test_rejection_render_retry_preserves_one_decision(office, monkeypatch):
    runner, store = office
    failed = fail_render_once(monkeypatch, runner, "rejection_letter")
    await runner.run("add bank statement L-1042", role="ops")
    first = store.get("loan", "L-1042")
    assert failed and first["status"] == "declined"
    assert not issued(store, "rejection_letter", "L-1042")
    await runner.run("review L-1042", role="ops")
    await runner.run("review L-1042", role="ops")
    retained = store.get("loan", "L-1042")
    assert retained["decision_operation_id"] == first["decision_operation_id"]
    assert retained["decision_at"] == first["decision_at"]
    assert len(issued(store, "rejection_letter", "L-1042")) == 1
    assert sum(event["kind"] == "decision" and event["data"].get("role") == "underwriter" for event in store.all_events()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,format", [("disbursement_advice", "pdf"), ("repayment_schedule", "csv"), ("repayment_schedule", "pdf")])
async def test_payout_artifact_retry_never_repeats_payout(office, monkeypatch, kind, format):
    runner, store = office
    await approve_clean(runner)
    await runner.run("accept KFS L-1043", role="borrower")
    failed = fail_render_once(monkeypatch, runner, kind, format)
    await runner.run("disburse L-1043", role="ops")
    first = store.get("loan", "L-1043")
    assert failed and first["status"] == "disbursed"
    await runner.run("disburse L-1043", role="ops")
    await runner.run("disburse L-1043", role="ops")
    retained = store.get("loan", "L-1043")
    assert retained["simulated_utr"] == first["simulated_utr"]
    assert retained["disbursed_at"] == first["disbursed_at"]
    assert retained["outstanding"] == first["outstanding"]
    assert len(issued(store, "disbursement_advice", "L-1043")) == 1
    assert len(issued(store, "repayment_schedule", "L-1043")) == 2
    assert sum(event["kind"] == "payout" for event in store.all_events()) == 1
    assert len(retained["operation_documents"][retained["simulated_utr"]]) == 3


@pytest.mark.asyncio
async def test_receipt_retry_does_not_record_next_installment(office, monkeypatch):
    runner, store = office
    failed = fail_render_once(monkeypatch, runner, "payment_receipt")
    await runner.run("pay EMI L-1044", role="borrower")
    first = store.get("loan", "L-1044")
    assert failed and first["paid_installments"] == 1
    assert not issued(store, "payment_receipt", "L-1044")
    reply = await runner.run("pay EMI L-1044", role="borrower")
    retained = store.get("loan", "L-1044")
    assert "No additional EMI" in reply
    assert retained["paid_installments"] == 1 and retained["outstanding"] == first["outstanding"]
    assert len(retained["payments"]) == 1
    assert retained["payments"][0]["payment_reference"] == first["payments"][0]["payment_reference"]
    assert retained["payments"][0]["document_id"]
    assert len(issued(store, "payment_receipt", "L-1044")) == 1
    assert sum(event["kind"] == "payment" for event in store.all_events()) == 1
    # Once recovery is complete, a new command deliberately records the next EMI.
    await runner.run("pay EMI L-1044", role="borrower")
    assert store.get("loan", "L-1044")["paid_installments"] == 2


@pytest.mark.asyncio
async def test_closure_certificate_retry_does_not_repeat_settlement(office, monkeypatch):
    runner, store = office
    failed = fail_render_once(monkeypatch, runner, "no_dues_certificate")
    await runner.run("close L-1044", role="ops")
    first = store.get("loan", "L-1044")
    assert failed and first["status"] == "closed"
    count = sum(event["kind"] == "payment" for event in store.all_events())
    await runner.run("close L-1044", role="ops")
    await runner.run("close L-1044", role="ops")
    retained = store.get("loan", "L-1044")
    assert retained["closure_operation_id"] == first["closure_operation_id"]
    assert retained["closed_at"] == first["closed_at"]
    assert retained["outstanding"] == retained["outstanding_balance"] == 0
    assert sum(event["kind"] == "payment" for event in store.all_events()) == count
    assert len(issued(store, "no_dues_certificate", "L-1044")) == 1


@pytest.mark.asyncio
async def test_agreement_retry_preserves_original_acceptance(office, monkeypatch):
    runner, store = office
    await approve_clean(runner)
    failed = fail_render_once(monkeypatch, runner, "loan_agreement_summary")
    await runner.run("accept KFS L-1043", role="borrower")
    first = store.get("loan", "L-1043")
    assert failed and first["kfs_accepted"]
    await runner.run("accept KFS L-1043", role="borrower")
    retained = store.get("loan", "L-1043")
    assert retained["kfs_accepted_at"] == first["kfs_accepted_at"]
    assert retained["acceptance_operation_id"] == first["acceptance_operation_id"]
    assert len(issued(store, "loan_agreement_summary", "L-1043")) == 1
    assert sum(event["kind"] == "decision" and "Borrower accepted" in event["text"] for event in store.all_events()) == 1


@pytest.mark.asyncio
async def test_cooling_exit_certificate_retry_preserves_single_principal_return(office, monkeypatch):
    runner, store = office
    await disburse_clean(runner)
    failed = fail_render_once(monkeypatch, runner, "no_dues_certificate")
    await runner.run("exit L-1043", role="borrower")
    first = store.get("loan", "L-1043")
    assert failed and first["status"] == "closed" and first["exit_penalty"] == 0
    await runner.run("exit L-1043", role="borrower")
    retained = store.get("loan", "L-1043")
    assert retained["closure_operation_id"] == first["closure_operation_id"]
    assert retained["simulated_refund"] == first["simulated_refund"]
    assert retained["outstanding"] == retained["outstanding_balance"] == 0
    assert len(issued(store, "no_dues_certificate", "L-1043")) == 1
    assert sum(event["kind"] == "payment" and "principal return" in event["text"] for event in store.all_events()) == 1


@pytest.mark.asyncio
async def test_borrower_can_withdraw_approved_loan_without_payout(office):
    runner, store = office
    await approve_clean(runner)
    reply = await runner.run("exit L-1043", role="borrower")
    loan = store.get("loan", "L-1043")
    assert "withdrawn" in reply
    assert loan["status"] == "withdrawn" and loan["exit_penalty"] == 0
    assert not loan.get("simulated_utr")
    assert not any(event["kind"] == "payout" for event in store.all_events())
    await runner.run("disburse L-1043", role="ops")
    assert store.get("loan", "L-1043")["status"] == "withdrawn"


@pytest.mark.asyncio
async def test_cooling_exit_after_window_preserves_outstanding(office):
    runner, store = office
    await disburse_clean(runner)
    before = store.get("loan", "L-1043")
    later = datetime.fromisoformat(before["disbursed_at"]) + timedelta(days=before["cooling_off_days"], seconds=1)
    reply = await runner.run("exit L-1043", role="borrower", now=later)
    after = store.get("loan", "L-1043")
    assert "window has ended" in reply
    assert after["status"] == "disbursed" and after["outstanding"] == before["outstanding"]
    assert not after.get("simulated_refund")
    assert not issued(store, "no_dues_certificate", "L-1043")
