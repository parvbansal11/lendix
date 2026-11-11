import pytest

from app.compliance import WATERMARK, check_disbursement, check_document, check_message, grievance_acknowledgment


def payout_ready():
    return {"id": "L-7000", "name": "Mira Desai", "payout_name": "Desai M", "payout_account": "SYN-OWN-7000",
            "account_verified": True, "consent": True, "approval_source": "policy", "approved_amount": 200000,
            "underwriting": {"decision": "approve", "policy_version": 1, "rule": "automatic approval", "approved_amount": 200000,
                             "inputs": {"risk_band": "low", "foir": .4, "requested_amount": 200000}, "reasons": ["All automatic approval conditions are met."]},
            "approved_at": "2026-11-09T10:00:00+05:30", "kfs_issued": True, "kfs_issued_at": "2026-11-09T10:30:00+05:30",
            "kfs_accepted": True, "kfs_accepted_at": "2026-11-09T11:00:00+05:30", "cooling_off_days": 1, "risk_band": "low"}


@pytest.mark.parametrize("text", ["Guaranteed approval for your loan", "Approval is guaranteed", "We will shame you", "Pay now or face consequences", "You will go to jail", "Contact your employer", "We will hide the fees", "This loan has hidden fees", "You are an idiot"])
def test_abuse_threats_third_parties_and_false_promises_blocked(text):
    result = check_message(text)
    assert result["allowed"] is False
    assert result["reasons"]


@pytest.mark.parametrize("text", ["The written policy will determine the application outcome.", "We cannot contact your employer.", "We never contact your family.", "We do not offer guaranteed approval.", "There are no hidden fees."])
def test_polite_replies_and_explicit_refusals_allowed(text):
    assert check_message(text)["allowed"] is True


@pytest.mark.parametrize("clock,allowed", [("2026-11-11T07:59:59+05:30", False), ("2026-11-11T08:00:00+05:30", True), ("2026-11-11T18:59:59+05:30", True), ("2026-11-11T19:00:00+05:30", False), ("2026-11-11T22:00:00+05:30", False), ("2026-11-11T02:30:00Z", True)])
def test_contact_hours_in_ist(clock, allowed):
    result = check_message("Your EMI is due soon; please contact us if you need support.", now=clock)
    assert result["allowed"] is allowed
    if not allowed:
        assert "T08:00:00+05:30" in result["next_contact_at"]


def test_internal_messages_skip_contact_clock_but_not_abuse():
    assert check_message("Officer review requested.", borrower_facing=False, now="2026-11-11T22:00:00+05:30")["allowed"]
    assert not check_message("Pay now or face consequences", borrower_facing=False)["allowed"]
    assert not check_message("Please pay", third_party=True)["allowed"]


def test_disbursement_all_prerequisites():
    assert check_disbursement(payout_ready(), now="2026-11-11T12:00:00+05:30")["allowed"]


@pytest.mark.parametrize("field,value", [("approval_source", "model"), ("approval_source", None), ("underwriting", {}), ("approved_amount", 250000), ("approved_at", None), ("account_verified", False), ("payout_name", "Different Person"), ("payout_account", None), ("payout_account_type", "pool"), ("payout_account_type", "third_party"), ("consent", False), ("kfs_issued", False), ("kfs_accepted", False), ("kfs_accepted_at", None), ("kfs_accepted_at", "2026-11-12T12:00:00+05:30"), ("cooling_off_days", 0), ("risk_band", "unverified")])
def test_disbursement_prerequisite_failures(field, value):
    loan = dict(payout_ready(), **{field: value})
    assert not check_disbursement(loan, now="2026-11-11T12:00:00+05:30")["allowed"]


def test_actual_elapsed_wait_and_demo_override():
    loan = payout_ready()
    now = "2026-11-09T12:00:00+05:30"
    assert not check_disbursement(loan, now=now)["allowed"]
    assert check_disbursement(loan, now=now, demo_skip_cooling=True)["allowed"]
    loan["approval_source"] = "model"
    assert not check_disbursement(loan, now=now, demo_skip_cooling=True)["allowed"]


def test_risk_head_requires_logged_override_reason_but_never_third_party_payout():
    loan = dict(payout_ready(), risk_band="high", approval_source="risk_head", approved_by_role="risk_head",
                human_approved=True, escalated_for_decision=True)
    now = "2026-11-11T12:00:00+05:30"
    assert not check_disbursement(loan, now=now)["allowed"]
    loan["override_reason"] = "Reviewed supporting verification; documented exception."
    assert check_disbursement(loan, now=now)["allowed"]
    loan["payout_name"] = "Another Person"
    assert not check_disbursement(loan, now=now)["allowed"]


@pytest.mark.parametrize("field,value", [("human_approved", False), ("approved_by_role", "ops"), ("escalated_for_decision", False), ("override_reason", ""), ("override_reason", "   "), ("approval_source", "model")])
def test_risk_head_authority_requires_all_recorded_conditions(field, value):
    loan = dict(payout_ready(), approval_source="risk_head", approved_by_role="risk_head", human_approved=True,
                escalated_for_decision=True, override_reason="Reviewed the documented exception.", underwriting={})
    assert check_disbursement(loan, now="2026-11-11T12:00:00+05:30")["allowed"]
    loan[field] = value
    assert not check_disbursement(loan, now="2026-11-11T12:00:00+05:30")["allowed"]


@pytest.mark.parametrize("field,value", [("decision", "escalate"), ("decision", "decline"), ("policy_version", True), ("policy_version", 0), ("rule", ""), ("inputs", {}), ("reasons", []), ("reasons", [""]), ("approved_amount", None)])
def test_policy_authority_requires_recorded_decision_evidence(field, value):
    loan = payout_ready()
    loan["underwriting"][field] = value
    assert not check_disbursement(loan, now="2026-11-11T12:00:00+05:30")["allowed"]


def test_policy_approval_does_not_require_human_approval():
    loan = payout_ready()
    assert not loan.get("human_approved")
    assert check_disbursement(loan, now="2026-11-11T12:00:00+05:30")["allowed"]


def test_role_label_alone_cannot_authorize_a_payout():
    loan = dict(payout_ready(), approval_source=None, underwriting={}, approved_by_role="risk_head", human_approved=True)
    assert not check_disbursement(loan, now="2026-11-11T12:00:00+05:30")["allowed"]


def metadata():
    return {"loan_id": "L-7000", "date": "2026-11-11", "document_id": "DOC-EXAMPLE", "watermark": WATERMARK}


def kfs():
    return dict(metadata(), loan_amount=200000, apr=.19, fees=2000, emi=9984.82, tenure_months=24,
                total_cost=241635.66, cooling_off_days=1, grievance_contact="grievances@lendix.example", exit_without_penalty=True)


@pytest.mark.parametrize("key", ["loan_id", "date", "document_id", "watermark"])
def test_every_document_requires_specimen_identity(key):
    data = metadata()
    del data[key]
    assert not check_document("risk_report", data)["allowed"]


@pytest.mark.parametrize("key", ["loan_amount", "apr", "fees", "emi", "tenure_months", "total_cost", "cooling_off_days", "grievance_contact"])
def test_every_kfs_disclosure_is_required(key):
    data = kfs()
    assert check_document("kfs", data)["allowed"]
    del data[key]
    assert not check_document("kfs", data)["allowed"]


def test_rejection_reasons_checked_for_abuse():
    assert not check_document("rejection_letter", metadata())["allowed"]
    assert check_document("rejection_letter", dict(metadata(), reasons=["Please provide an account in your own name."]))["allowed"]
    assert not check_document("rejection_letter", dict(metadata(), reasons=["You are an idiot"]))["allowed"]


def test_cooling_off_exit_without_penalty():
    assert not check_document("kfs", dict(kfs(), exit_without_penalty=False))["allowed"]


def test_closure_requires_confirmed_zero_balance():
    assert not check_document("no_dues", metadata())["allowed"]
    assert not check_document("no_dues", dict(metadata(), outstanding_balance=1))["allowed"]
    assert check_document("no_dues", dict(metadata(), outstanding=0))["allowed"]


def test_grievance_reference_resolution_and_escalation():
    text = grievance_acknowledgment("GRV-0001")
    assert "GRV-0001" in text and "30 days" in text and "RBI Ombudsman" in text
    assert not check_document("grievance_acknowledgment", metadata())["allowed"]
    assert check_document("grievance_acknowledgment", dict(metadata(), reference="GRV-0001", resolution_days=30, text=text))["allowed"]
    assert not check_document("grievance_acknowledgment", dict(metadata(), reference="GRV-0001", resolution_days=60))["allowed"]
