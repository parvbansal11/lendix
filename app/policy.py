"""Validated lender policy and deterministic underwriting decisions."""

from __future__ import annotations

from math import isfinite
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, ValidationError, field_validator, model_validator

RiskBand = Literal["low", "medium", "high", "unverified"]
RiskCode = Literal["PAYOUT_NAME_MISMATCH", "DOB_PAN_MISMATCH", "MULTIPLE_RECENT_APPLICATIONS", "SHARED_PAYOUT_ACCOUNT", "NEW_NUMBER", "INCOME_DOC_GAP"]
Number = StrictFloat | StrictInt


class PolicySection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class AutoDecline(PolicySection):
    reason_codes: list[RiskCode] = Field(min_length=1)
    foir_above: Number = Field(gt=0, le=.5)

    @field_validator("reason_codes")
    @classmethod
    def unique_codes(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Reason codes must be unique")
        return value


class AutoApprove(PolicySection):
    bands: list[RiskBand] = Field(min_length=1)
    max_amount: Number = Field(gt=0)
    foir_at_most: Number = Field(gt=0, le=.5)

    @field_validator("bands")
    @classmethod
    def verified_unique_bands(cls, value):
        if len(value) != len(set(value)) or "unverified" in value:
            raise ValueError("Automatic approval bands must be unique and verified")
        return value


class Escalate(PolicySection):
    bands: list[RiskBand] = Field(min_length=1)
    amount_above: Number = Field(gt=0)
    on_override_request: StrictBool
    on_restructure_request: StrictBool

    @field_validator("bands")
    @classmethod
    def unique_bands(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Escalation bands must be unique")
        return value

    @field_validator("on_override_request", "on_restructure_request")
    @classmethod
    def preserve_explicit_escalations(cls, value):
        if value is not True:
            raise ValueError("Override and restructure requests must escalate")
        return value


class CreditPolicy(PolicySection):
    version: StrictInt = Field(gt=0)
    auto_decline: AutoDecline
    auto_approve: AutoApprove
    escalate: Escalate

    @model_validator(mode="after")
    def consistent_limits(self):
        if self.auto_approve.foir_at_most > self.auto_decline.foir_above:
            raise ValueError("Automatic approval FOIR cannot exceed the decline threshold")
        if self.auto_approve.max_amount > self.escalate.amount_above:
            raise ValueError("Automatic approval amount cannot exceed the escalation threshold")
        return self


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous duplicate mappings instead of silently replacing values."""


def _mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in mapping:
            raise ValueError("Policy mappings require unique string keys")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load_policy(path: str | Path) -> CreditPolicy:
    """Fail closed at boot for an absent, ambiguous, or invalid policy file."""
    try:
        source = Path(path).read_text(encoding="utf-8")
        if not source.strip() or len(source.encode("utf-8")) > 64 * 1024:
            raise ValueError("Policy must be nonempty and at most 64 KB")
        parsed = yaml.load(source, Loader=UniqueKeyLoader)
        if not isinstance(parsed, dict):
            raise ValueError("Policy root must be a mapping")
        return CreditPolicy.model_validate(parsed)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError, ValueError, RecursionError) as exc:
        raise ValueError(f"Invalid credit policy: {exc}") from None


def _number(value):
    if type(value) not in {int, float} or not isfinite(value):
        return None
    return float(value)


def apply_policy(loan: dict, policy: CreditPolicy, *, override_request=False, restructure_request=False) -> dict:
    """Evaluate rules in order; prose or model output never supplies authority."""
    if not isinstance(policy, CreditPolicy):
        raise ValueError("A validated CreditPolicy is required")
    risk = loan.get("risk") if isinstance(loan.get("risk"), dict) else {}
    credit = loan.get("credit") if isinstance(loan.get("credit"), dict) else {}
    band = risk.get("band")
    raw_codes = risk.get("codes")
    valid_codes = isinstance(raw_codes, list) and all(isinstance(item, str) for item in raw_codes)
    codes = list(dict.fromkeys(raw_codes)) if valid_codes else []
    foir = _number(credit.get("foir"))
    requested = _number(loan.get("requested_amount"))
    eligible = _number(credit.get("eligible_amount"))
    inputs = {"risk_band": band if isinstance(band, str) else None, "reason_codes": codes,
              "foir": foir, "requested_amount": requested, "eligible_amount": eligible}

    def result(decision, rule, reasons, approved_amount=None):
        return {"decision": decision, "policy_version": policy.version, "rule": rule,
                "inputs": inputs, "reasons": reasons, "approved_amount": approved_amount}

    if override_request:
        return result("escalate", "override request", ["An explicit override request must be reviewed by the risk head with a recorded reason."])
    if restructure_request:
        return result("escalate", "restructure request", ["A hardship restructure request requires the risk head to review revised terms."])
    hard_flags = [code for code in policy.auto_decline.reason_codes if code in codes]
    if hard_flags:
        explanations = {"PAYOUT_NAME_MISMATCH": "The payout account is not in your name. Please use a verified bank account in your own name when applying again.",
                        "DOB_PAN_MISMATCH": "The application and identity record have different dates of birth. Please correct the mismatch.",
                        "SHARED_PAYOUT_ACCOUNT": "The payout account is shared by different applicants. Please provide a verified account in your own name."}
        return result("decline", "hard fraud flag", [explanations.get(code, f"Policy excludes applications flagged {code}.") for code in hard_flags])
    if foir is not None and foir > policy.auto_decline.foir_above:
        return result("decline", "FOIR above limit", [f"The requested repayment obligations are {foir:.1%} of verified income, above the policy limit of {policy.auto_decline.foir_above:.0%}."])
    if band not in {"low", "medium", "high", "unverified"} or not valid_codes or foir is None or foir < 0 or requested is None or requested <= 0 or eligible is None or eligible < 0:
        return result("escalate", "missing policy inputs", ["Complete the deterministic risk and affordability inputs before a credit decision."])
    if "SOURCE_UNAVAILABLE" in codes or band == "unverified":
        return result("escalate", "risk band requires review", ["Verification sources are incomplete; the risk head must review the case."])
    if band in policy.auto_approve.bands and requested <= policy.auto_approve.max_amount and foir <= policy.auto_approve.foir_at_most:
        return result("approve", "automatic approval", ["The verified risk band, requested amount and repayment obligations meet every automatic approval condition."], round(requested, 2))
    if band in policy.escalate.bands:
        return result("escalate", "risk band requires review", [f"The {band} risk band requires the risk head's review under the written policy."])
    if requested > policy.escalate.amount_above:
        return result("escalate", "amount above limit", [f"The requested amount exceeds the automatic approval limit of INR {policy.escalate.amount_above:,.2f}."])
    return result("escalate", "outside automatic approval criteria", ["The application does not meet every automatic approval condition; the risk head must review it."])
