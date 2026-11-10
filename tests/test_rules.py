from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.risk_rules import names_match, run_risk_checks
from app.tools import bureau_lookup, compute_affordability, emi_schedule, validate_application


TODAY = date(2026, 11, 11)


def clean_loan():
    return {"id": "L-7000", "name": "Mira Desai", "dob": "1993-06-15", "pan": "ZXQPA2000Z", "phone": "+919000000001",
            "monthly_income": 50000, "statement_income": 50000, "existing_emi": 5000, "requested_amount": 200000,
            "tenure_months": 24, "annual_rate": .18, "fees": 2000, "payout_account": "SYN-OWN-7000",
            "payout_name": "Desai Mira", "account_verified": True, "consent": True,
            "documents": ["identity", "bank_statement"], "kyc_dob": "1993-06-15", "phone_activated_at": "2025-01-01",
            "applied_at": TODAY.isoformat(), "status": "new"}


@pytest.mark.parametrize("left,right,expected", [
    ("Mira Desai", "Desai Mira", True), ("Mira Desai", "M. Desai", True),
    ("Aarav Mehta", "Mehta A", True), ("Dr. Mira Desai", "mira desai", True),
    ("Mira Desai", "Riya Shah", False), ("Mira Desai", "M D", False), ("", "", False),
    ("Mira Desai", "R Desai", False), ("Nikhil Sharma", "Sharma Nikhil", True),
])
def test_name_normalization(left, right, expected):
    assert names_match(left, right) is expected


def test_complete_clean_sources_are_low():
    assert run_risk_checks(clean_loan(), [], TODAY) == {"band": "low", "codes": [], "explanations": [], "missing_sources": []}


@pytest.mark.parametrize("field,value,code,band", [
    ("payout_name", "Ritvik Soman", "PAYOUT_NAME_MISMATCH", "high"),
    ("kyc_dob", "1991-01-01", "DOB_PAN_MISMATCH", "high"),
    ("phone_activated_at", "2026-10-01", "NEW_NUMBER", "medium"),
    ("monthly_income", 65001, "INCOME_DOC_GAP", "medium"),
])
def test_individual_flags(field, value, code, band):
    loan = clean_loan()
    loan[field] = value
    result = run_risk_checks(loan, [], TODAY)
    assert result["band"] == band
    assert result["codes"] == [code]
    assert result["explanations"]


def test_repeated_applications_count_current_once_and_normalize_phone():
    loan = clean_loan()
    other = dict(loan, id="L-7001", pan="ZXQPA2001Z", phone="9000000001")
    third = dict(loan, id="L-7002", phone="9000000002", applied_at="2026-10-12")
    result = run_risk_checks(loan, [loan, other, third, third], TODAY)
    assert "MULTIPLE_RECENT_APPLICATIONS" in result["codes"]
    assert "SHARED_PAYOUT_ACCOUNT" in result["codes"]
    assert "MULTIPLE_RECENT_APPLICATIONS" not in run_risk_checks(loan, [loan, other, other], TODAY)["codes"]
    third["applied_at"] = "2026-10-11"
    assert "MULTIPLE_RECENT_APPLICATIONS" not in run_risk_checks(loan, [other, third], TODAY)["codes"]
    third["applied_at"] = "2026-11-12"
    assert "MULTIPLE_RECENT_APPLICATIONS" not in run_risk_checks(loan, [other, third], TODAY)["codes"]


def test_shared_account_same_applicant_does_not_flag():
    loan = clean_loan()
    assert "SHARED_PAYOUT_ACCOUNT" not in run_risk_checks(loan, [dict(loan, id="L-7001")], TODAY)["codes"]


@pytest.mark.parametrize("field,value", [("kyc_dob", None), ("phone_activated_at", None), ("phone_activated_at", "2027-01-01"), ("statement_income", None), ("documents", ["identity"]), ("payout_name", ""), ("applied_at", "invalid")])
def test_missing_sources_never_become_low(field, value):
    loan = clean_loan()
    loan[field] = value
    result = run_risk_checks(loan, [], TODAY)
    assert result["band"] == "unverified"
    assert "SOURCE_UNAVAILABLE" in result["codes"]


def test_missing_history_and_multiple_flags():
    assert run_risk_checks(clean_loan(), None, TODAY)["band"] == "unverified"
    loan = dict(clean_loan(), payout_name="Different Person", phone_activated_at="2026-11-01")
    result = run_risk_checks(loan, [], TODAY)
    assert result["band"] == "high"
    assert set(result["codes"]) == {"PAYOUT_NAME_MISMATCH", "NEW_NUMBER"}


def test_exact_income_and_mobile_thresholds():
    loan = dict(clean_loan(), monthly_income=65000, phone_activated_at="2026-08-13")
    assert run_risk_checks(loan, [], TODAY)["band"] == "low"


def test_intake_missing_document_and_consent():
    loan = clean_loan()
    assert validate_application(loan, TODAY) == []
    loan["documents"] = ["identity"]
    assert validate_application(loan, TODAY) == ["Please provide the synthetic bank statement for this application."]
    loan["consent"] = False
    assert "consent" in validate_application(loan, TODAY)[0]


@pytest.mark.parametrize("dob,valid", [("2005-11-11", True), ("2005-11-12", False), ("1965-11-11", False), ("1965-11-12", True), ("invalid", False)])
def test_intake_age_boundaries(dob, valid):
    prompts = validate_application(dict(clean_loan(), dob=dob), TODAY)
    assert (not prompts) is valid


@pytest.mark.parametrize("field,value", [("pan", "bad"), ("phone", "1234567890"), ("existing_emi", -1), ("tenure_months", 12.5), ("annual_rate", 18), ("monthly_income", float("nan"))])
def test_intake_invalid_fields(field, value):
    assert validate_application(dict(clean_loan(), **{field: value}), TODAY)


def test_affordability_emi_and_fee_inclusive_apr():
    loan = clean_loan()
    result = compute_affordability(loan)
    assert result["emi"] == 9984.82
    assert result["max_emi"] == 20000
    assert result["recommendation"] == "approve"
    assert result["apr"] > result["annual_rate"]
    assert result["total_cost"] == round(loan["requested_amount"] + result["total_interest"] + loan["fees"], 2)
    assert compute_affordability(dict(loan, fees=0))["apr"] == .18


def test_affordability_uses_lower_verified_income_and_preserves_cap():
    result = compute_affordability(dict(clean_loan(), statement_income=20000))
    assert result["max_emi"] == 5000
    assert result["recommendation"] == "approve_lower"
    assert 0 < result["recommended_amount"] < 200000
    modified = dict(clean_loan(), statement_income=20000, requested_amount=result["recommended_amount"])
    assert compute_affordability(modified)["foir"] <= .5


@pytest.mark.parametrize("band", ["high", "unverified"])
def test_credit_never_recommends_unresolved_identity(band):
    result = compute_affordability(dict(clean_loan(), risk={"band": band}))
    assert result["recommendation"] == "decline"
    assert result["recommended_amount"] == 0


def test_zero_interest_and_schedule_month_end_rounding():
    loan = dict(clean_loan(), requested_amount=1000, annual_rate=0, fees=0, tenure_months=3)
    rows = emi_schedule(loan, "2026-01-31")
    assert [row["due_date"] for row in rows] == ["2026-02-28", "2026-03-31", "2026-04-30"]
    assert [row["emi"] for row in rows] == [333.33, 333.33, 333.34]
    assert rows[-1]["balance"] == 0
    assert compute_affordability(loan)["apr"] == 0


def test_amortization_reconciles_principal_and_final_balance():
    loan = clean_loan()
    rows = emi_schedule(loan, "2027-12-31")
    assert rows[1]["due_date"] == "2028-02-29"
    assert sum(Decimal(str(row["principal"])) for row in rows) == Decimal(str(loan["requested_amount"]))
    assert rows[-1]["balance"] == 0
    assert sum(Decimal(str(row["interest"])) for row in rows) == Decimal(str(compute_affordability(loan)["total_interest"]))


def test_fixture_bureau_is_explicitly_synthetic():
    assert bureau_lookup(clean_loan())["synthetic"] is True


def test_seed_includes_hero_clean_hardship_and_twenty_today_batch():
    loans = json.loads((Path(__file__).parents[1] / "data" / "seed.json").read_text())
    assert len(loans) >= 25
    by_id = {loan["id"]: loan for loan in loans}
    assert "bank_statement" not in by_id["L-1042"]["documents"]
    hero = deepcopy(by_id["L-1042"])
    hero["documents"].append("bank_statement")
    assert {"PAYOUT_NAME_MISMATCH", "NEW_NUMBER"} <= set(run_risk_checks(hero, loans, TODAY)["codes"])
    assert run_risk_checks(by_id["L-1043"], loans, TODAY)["band"] == "low"
    assert by_id["L-1044"]["status"] == "disbursed"
    assert sum(loan["today_batch"] for loan in loans) == 20
