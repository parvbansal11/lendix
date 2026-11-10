"""Deterministic checks over consented, synthetic application records."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any


EXPLANATIONS = {
    "PAYOUT_NAME_MISMATCH": "The payout account holder's name does not match the applicant. Use a verified account in the applicant's own name.",
    "DOB_PAN_MISMATCH": "The application date of birth differs from the identity verification record.",
    "MULTIPLE_RECENT_APPLICATIONS": "This phone number or identity number appears in at least three applications within 30 days.",
    "SHARED_PAYOUT_ACCOUNT": "The payout account appears on applications from different people.",
    "NEW_NUMBER": "The mobile number was activated less than 90 days ago.",
    "INCOME_DOC_GAP": "Declared monthly income is more than 30% above the income shown in the bank statement.",
    "SOURCE_UNAVAILABLE": "One or more required verification sources are unavailable. Complete verification before a credit decision.",
}


def as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _name_tokens(name: str) -> list[str]:
    value = unicodedata.normalize("NFKD", str(name or "")).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return [token for token in re.findall(r"[^\W\d_]+", value) if token not in {"mr", "mrs", "ms", "dr", "shri", "smt"}]


def names_match(first: str, second: str) -> bool:
    """Allow word reordering, initials, and small spelling differences conservatively."""
    left, right = _name_tokens(first), _name_tokens(second)
    if not left or len(left) != len(right):
        return False
    # Initials alone do not establish an account holder's identity.
    if not any(len(token) > 1 for token in left) or not any(len(token) > 1 for token in right):
        return False

    def compatible(a: str, b: str) -> bool:
        if a == b:
            return True
        if len(a) == 1 or len(b) == 1:
            return a[0] == b[0]
        return min(len(a), len(b)) >= 5 and SequenceMatcher(None, a, b).ratio() >= 0.88

    def match(remaining: list[str], available: list[str]) -> bool:
        if not remaining:
            return True
        return any(compatible(remaining[0], token) and match(remaining[1:], available[:i] + available[i + 1:]) for i, token in enumerate(available))

    return match(sorted(left, key=len, reverse=True), right)


def _number(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _phone(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))[-10:]


def run_risk_checks(loan: dict, applications: list[dict] | None, today: date | str | None = None) -> dict:
    current_day = as_date(today) if today is not None else date.today()
    if current_day is None:
        raise ValueError("today must be a valid ISO date")
    codes: list[str] = []
    missing: list[str] = []

    if not loan.get("name") or not loan.get("payout_name") or not loan.get("payout_account"):
        missing.append("payout identity")
    elif not names_match(loan["name"], loan["payout_name"]):
        codes.append("PAYOUT_NAME_MISMATCH")

    dob, kyc_dob = as_date(loan.get("dob")), as_date(loan.get("kyc_dob"))
    if dob is None or kyc_dob is None or "identity" not in loan.get("documents", []):
        missing.append("identity verification")
    elif dob != kyc_dob:
        codes.append("DOB_PAN_MISMATCH")

    if applications is None or not loan.get("pan") or not loan.get("phone"):
        missing.append("application history")
    else:
        # Include this application exactly once even when the caller already included it.
        records = [record for record in applications if not loan.get("id") or record.get("id") != loan.get("id")]
        records.append(loan)
        recent: set[str] = set()
        for index, record in enumerate(records):
            same_identity = str(record.get("pan", "")).upper() == str(loan["pan"]).upper() or _phone(record.get("phone")) == _phone(loan["phone"])
            applied = as_date(record.get("applied_at"))
            if same_identity and applied is None:
                missing.append("application date")
            elif same_identity and 0 <= (current_day - applied).days <= 30:
                recent.add(str(record.get("id") or f"record-{index}"))
            if loan.get("payout_account") and record.get("payout_account") == loan["payout_account"]:
                different_pan = record.get("pan") and str(record["pan"]).upper() != str(loan["pan"]).upper()
                different_person = different_pan or (not record.get("pan") and record.get("name") and not names_match(record["name"], loan.get("name", "")))
                if different_person and "SHARED_PAYOUT_ACCOUNT" not in codes:
                    codes.append("SHARED_PAYOUT_ACCOUNT")
        if len(recent) >= 3:
            codes.append("MULTIPLE_RECENT_APPLICATIONS")

    activated = as_date(loan.get("phone_activated_at"))
    if activated is None or activated > current_day:
        missing.append("mobile activation")
    elif (current_day - activated).days < 90:
        codes.append("NEW_NUMBER")

    declared, statement = _number(loan.get("monthly_income")), _number(loan.get("statement_income"))
    if declared is None or statement is None or "bank_statement" not in loan.get("documents", []):
        missing.append("bank statement income")
    elif declared > statement * Decimal("1.30"):
        codes.append("INCOME_DOC_GAP")

    if missing:
        codes.append("SOURCE_UNAVAILABLE")
        band = "unverified"
    elif any(code in codes for code in {"PAYOUT_NAME_MISMATCH", "DOB_PAN_MISMATCH", "SHARED_PAYOUT_ACCOUNT"}) or len(codes) >= 3:
        band = "high"
    else:
        band = "medium" if codes else "low"
    codes = list(dict.fromkeys(codes))
    return {"band": band, "codes": codes, "explanations": [EXPLANATIONS[code] for code in codes], "missing_sources": sorted(set(missing))}
