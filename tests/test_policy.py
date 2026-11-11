from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from app.policy import CreditPolicy, apply_policy, load_policy
from app.risk_rules import run_risk_checks
from app.tools import compute_affordability

ROOT = Path(__file__).parents[1]


@pytest.fixture
def policy():
    return load_policy(ROOT / "config" / "credit_policy.yaml")


def application(**updates):
    loan = {"requested_amount": 100000, "risk": {"band": "low", "codes": []},
            "credit": {"foir": .38, "eligible_amount": 250000}}
    loan.update(updates)
    return loan


def test_default_yaml_matches_written_policy(policy):
    assert policy.model_dump() == {
        "version": 1,
        "auto_decline": {"reason_codes": ["PAYOUT_NAME_MISMATCH", "DOB_PAN_MISMATCH", "SHARED_PAYOUT_ACCOUNT"], "foir_above": .5},
        "auto_approve": {"bands": ["low"], "max_amount": 200000, "foir_at_most": .5},
        "escalate": {"bands": ["medium", "unverified"], "amount_above": 200000, "on_override_request": True, "on_restructure_request": True},
    }


@pytest.mark.parametrize("code", ["PAYOUT_NAME_MISMATCH", "DOB_PAN_MISMATCH", "SHARED_PAYOUT_ACCOUNT"])
def test_hard_flags_decline_even_affordable_and_before_foir(policy, code):
    loan = application(risk={"band": "high", "codes": [code]}, credit={"foir": .8, "eligible_amount": 0})
    result = apply_policy(loan, policy)
    assert result["decision"] == "decline" and result["rule"] == "hard fraud flag"
    assert result["approved_amount"] is None
    assert result["inputs"]["reason_codes"] == [code]
    assert result["reasons"] and result["policy_version"] == 1


@pytest.mark.parametrize("band", ["low", "medium", "high", "unverified"])
def test_foir_decline_precedes_approval_and_escalation(policy, band):
    result = apply_policy(application(risk={"band": band, "codes": []}, credit={"foir": .500001, "eligible_amount": 1000}), policy)
    assert result["decision"] == "decline" and result["rule"] == "FOIR above limit"


@pytest.mark.parametrize("amount,foir", [(100000, .38), (200000, .5), (199999.99, 0)])
def test_automatic_approval_includes_exact_boundaries(policy, amount, foir):
    result = apply_policy(application(requested_amount=amount, credit={"foir": foir, "eligible_amount": amount + 1}), policy)
    assert result["decision"] == "approve" and result["approved_amount"] == amount
    assert result["rule"] == "automatic approval"


@pytest.mark.parametrize("band", ["medium", "unverified"])
def test_required_bands_escalate(policy, band):
    result = apply_policy(application(risk={"band": band, "codes": []}), policy)
    assert result["decision"] == "escalate" and result["rule"] == "risk band requires review"


def test_large_low_band_loan_escalates(policy):
    result = apply_policy(application(requested_amount=200000.01), policy)
    assert result["decision"] == "escalate" and result["rule"] == "amount above limit"


def test_high_band_without_hard_flag_uses_fallback(policy):
    result = apply_policy(application(risk={"band": "high", "codes": ["NEW_NUMBER"]}), policy)
    assert result["decision"] == "escalate" and result["rule"] == "outside automatic approval criteria"


@pytest.mark.parametrize("parameter,rule", [("override_request", "override request"), ("restructure_request", "restructure request")])
def test_control_requests_escalate_instead_of_reapplying_decline(policy, parameter, rule):
    loan = application(risk={"band": "high", "codes": ["PAYOUT_NAME_MISMATCH"]}, credit={"foir": .9})
    result = apply_policy(loan, policy, **{parameter: True})
    assert result["decision"] == "escalate" and result["rule"] == rule


@pytest.mark.parametrize("updates", [
    {"risk": {}}, {"risk": {"band": "unknown", "codes": []}}, {"risk": {"band": "low", "codes": "none"}},
    {"credit": {}}, {"credit": {"foir": None, "eligible_amount": 100000}},
    {"credit": {"foir": float("nan"), "eligible_amount": 100000}},
    {"credit": {"foir": True, "eligible_amount": 100000}},
    {"requested_amount": "100000"}, {"requested_amount": -1},
])
def test_missing_or_invalid_inputs_never_approve(policy, updates):
    result = apply_policy(application(**updates), policy)
    assert result["decision"] == "escalate" and result["rule"] == "missing policy inputs"


def test_source_unavailable_never_approves_even_if_band_incorrect(policy):
    result = apply_policy(application(risk={"band": "low", "codes": ["SOURCE_UNAVAILABLE"]}), policy)
    assert result["decision"] == "escalate"


def test_policy_changes_apply_without_code_changes(policy):
    values = policy.model_dump()
    values["version"] = 2
    values["auto_approve"]["max_amount"] = 150000
    values["escalate"]["amount_above"] = 150000
    changed = CreditPolicy.model_validate(values)
    result = apply_policy(application(requested_amount=175000), changed)
    assert result["decision"] == "escalate" and result["policy_version"] == 2


@pytest.mark.parametrize("contents", ["", "null", "[]", "version: 1\nversion: 2", "version: true", "version: '1'", "version: 1\nunknown: true", "auto_decline: [", "!!python/object:untrusted {}"])
def test_invalid_policy_text_fails_loading(tmp_path, contents):
    path = tmp_path / "policy.yaml"
    path.write_text(contents)
    with pytest.raises(ValueError, match="Invalid credit policy"):
        load_policy(path)


@pytest.mark.parametrize("section,key,value", [
    (None, "version", 0), (None, "version", True), (None, "extra", 1),
    ("auto_decline", "foir_above", "0.50"), ("auto_decline", "foir_above", float("nan")),
    ("auto_decline", "reason_codes", []), ("auto_decline", "reason_codes", ["UNKNOWN_FLAG"]),
    ("auto_decline", "reason_codes", ["NEW_NUMBER", "NEW_NUMBER"]),
    ("auto_approve", "bands", []), ("auto_approve", "bands", ["unverified"]),
    ("auto_approve", "max_amount", -100), ("auto_approve", "max_amount", True),
    ("auto_approve", "foir_at_most", .6), ("auto_approve", "unknown", 1),
    ("escalate", "bands", ["medium", "medium"]), ("escalate", "amount_above", 0),
    ("escalate", "on_override_request", "true"), ("escalate", "on_restructure_request", False),
])
def test_strict_policy_field_validation(tmp_path, policy, section, key, value):
    values = policy.model_dump()
    target = values if section is None else values[section]
    target[key] = value
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(values))
    with pytest.raises(ValueError, match="Invalid credit policy"):
        load_policy(path)


def test_nested_duplicate_keys_rejected(tmp_path):
    contents = (ROOT / "config" / "credit_policy.yaml").read_text().replace("  max_amount: 200000", "  max_amount: 200000\n  max_amount: 999999")
    path = tmp_path / "policy.yaml"
    path.write_text(contents)
    with pytest.raises(ValueError, match="unique"):
        load_policy(path)


def test_missing_policy_fails(tmp_path):
    with pytest.raises(ValueError, match="Invalid credit policy"):
        load_policy(tmp_path / "missing.yaml")


def test_seed_policy_outcomes_match_demo(policy):
    loans = json.loads((ROOT / "data" / "seed.json").read_text())
    expected = {"L-1042": ("decline", "hard fraud flag"), "L-1043": ("approve", "automatic approval"), "L-1045": ("escalate", "risk band requires review")}
    for identifier, outcome in expected.items():
        loan = deepcopy(next(item for item in loans if item["id"] == identifier))
        loan["documents"] = ["identity", "bank_statement"]
        loan["risk"] = run_risk_checks(loan, loans, today="2026-11-11")
        loan["credit"] = compute_affordability(loan)
        result = apply_policy(loan, policy)
        assert (result["decision"], result["rule"]) == outcome
        if identifier == "L-1042":
            assert loan["credit"]["foir"] == .38
            assert loan["credit"]["recommendation"] == "affordable"
            assert "own name" in " ".join(result["reasons"])
