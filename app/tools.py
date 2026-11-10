"""Validation and reproducible financial calculations for synthetic loans."""

from __future__ import annotations

import calendar
import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP, localcontext
from typing import Any

from app.risk_rules import as_date

CENT = Decimal("0.01")
ZERO = Decimal(0)


def _decimal(value: Any, default: str = "0") -> Decimal:
    try:
        result = Decimal(str(value if value is not None else default))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Financial values must be numbers") from exc
    if not result.is_finite():
        raise ValueError("Financial values must be finite")
    return result


def _money(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _fees(value: Any) -> Decimal:
    if isinstance(value, dict):
        amounts = value.values()
    elif isinstance(value, list):
        amounts = [item.get("amount", 0) if isinstance(item, dict) else item for item in value]
    else:
        amounts = [value]
    result = ZERO
    for amount in amounts:
        fee = _decimal(amount)
        if fee < 0:
            raise ValueError("Fees cannot be negative")
        result += fee
    return result


def validate_application(loan: dict, today: date | str | None = None) -> list[str]:
    """Return ordered, single-question prompts; callers ask only the first."""
    prompts: list[str] = []
    current_day = as_date(today) if today is not None else date.today()
    if current_day is None:
        raise ValueError("today must be a valid ISO date")
    for key, label in (("name", "full name"), ("dob", "date of birth in YYYY-MM-DD format"), ("pan", "synthetic PAN"), ("phone", "Indian mobile number")):
        if not str(loan.get(key) or "").strip():
            prompts.append(f"What is the applicant's {label}?")
    if loan.get("dob"):
        dob = as_date(loan["dob"])
        if dob is None:
            prompts.append("What is the applicant's valid date of birth in YYYY-MM-DD format?")
        else:
            age = current_day.year - dob.year - ((current_day.month, current_day.day) < (dob.month, dob.day))
            if not 21 <= age <= 60:
                prompts.append("This demo accepts ages 21–60; can you confirm an eligible applicant's date of birth?")
    if loan.get("pan") and not re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", str(loan["pan"]).strip().upper()):
        prompts.append("What is the valid-format synthetic PAN (five letters, four digits, one letter)?")
    if loan.get("phone"):
        phone = re.sub(r"[\s()-]", "", str(loan["phone"]))
        if not re.fullmatch(r"(?:\+91|91)?[6-9][0-9]{9}", phone):
            prompts.append("What is the applicant's valid Indian mobile number?")
    for key, label, positive in (("monthly_income", "monthly income", True), ("existing_emi", "existing monthly EMI total", False), ("requested_amount", "requested loan amount", True)):
        try:
            value = _decimal(loan.get(key))
            valid = key in loan and value > 0 if positive else key in loan and value >= 0
        except ValueError:
            valid = False
        if not valid:
            prompts.append(f"What is the applicant's {'positive ' if positive else 'non-negative '}{label}?")
    try:
        tenure = _decimal(loan.get("tenure_months"))
        valid_tenure = 1 <= tenure <= 120 and tenure == tenure.to_integral_value()
    except ValueError:
        valid_tenure = False
    if not valid_tenure:
        prompts.append("What repayment tenure, in whole months from 1 to 120, is requested?")
    try:
        if not 0 <= _decimal(loan.get("annual_rate")) <= 1 or "annual_rate" not in loan:
            raise ValueError("rate")
    except ValueError:
        prompts.append("What annual interest rate between 0 and 1 applies to this demo loan?")
    try:
        _fees(loan.get("fees", 0))
    except ValueError:
        prompts.append("What non-negative fees apply to this demo loan?")
    if loan.get("consent") is not True:
        prompts.append("Does the applicant consent to identity, statement and credit checks for this application?")
    documents = loan.get("documents") or []
    for kind, label in (("identity", "synthetic identity document"), ("bank_statement", "synthetic bank statement")):
        if kind not in documents:
            prompts.append(f"Please provide the {label} for this application.")
    for key, label in (("payout_account", "payout account identifier"), ("payout_name", "payout account holder's name")):
        if not loan.get(key):
            prompts.append(f"What is the applicant's {label}?")
    if loan.get("account_verified") is not True:
        prompts.append("Can the applicant's own payout account be verified in the synthetic fixture?")
    return prompts


def _emi(principal: Decimal, monthly_rate: Decimal, months: int) -> Decimal:
    if monthly_rate == 0:
        return principal / months
    factor = (1 + monthly_rate) ** months
    return principal * monthly_rate * factor / (factor - 1)


def _apr(principal: Decimal, fees: Decimal, monthly_payments: list[Decimal]) -> Decimal:
    """Nominal annualized monthly IRR on net disbursal, including upfront fees."""
    net = principal - fees
    if net <= 0:
        raise ValueError("Fees must be less than the loan amount")
    low, high = ZERO, Decimal(1)
    def present_value(rate: Decimal) -> Decimal:
        return sum((payment / (1 + rate) ** month for month, payment in enumerate(monthly_payments, 1)), ZERO)
    while present_value(high) > net:
        high *= 2
    for _ in range(120):
        middle = (low + high) / 2
        if present_value(middle) > net:
            low = middle
        else:
            high = middle
    return ((low + high) / 2 * 12).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _amortize(principal: Decimal, annual_rate: Decimal, months: int) -> list[dict]:
    rate = annual_rate / 12
    payment = _emi(principal, rate, months).quantize(CENT, rounding=ROUND_HALF_UP)
    balance = principal.quantize(CENT, rounding=ROUND_HALF_UP)
    rows = []
    for number in range(1, months + 1):
        interest = (balance * rate).quantize(CENT, rounding=ROUND_HALF_UP)
        due = balance + interest if number == months else min(payment, balance + interest)
        principal_part = due - interest
        balance -= principal_part
        rows.append({"installment": number, "emi": _money(due), "principal": _money(principal_part), "interest": _money(interest), "balance": _money(balance)})
    return rows


def compute_affordability(loan: dict) -> dict:
    """Recommend only; every decision remains with a human credit officer."""
    with localcontext() as context:
        context.prec = 36
        income = _decimal(loan.get("monthly_income"))
        statement = _decimal(loan.get("statement_income"))
        verified_income = min(income, statement)
        existing = _decimal(loan.get("existing_emi"))
        requested = _decimal(loan.get("requested_amount"))
        annual = _decimal(loan.get("annual_rate"))
        tenure = _decimal(loan.get("tenure_months"))
        fees = _fees(loan.get("fees", 0))
        if income < 0 or statement < 0 or existing < 0 or requested <= 0 or not 0 <= annual <= 1 or not 1 <= tenure <= 120 or tenure != tenure.to_integral_value():
            raise ValueError("Loan inputs require positive principal, non-negative income/EMIs, rate 0–1 and tenure 1–120 whole months")
        months = int(tenure)
        max_emi = max(ZERO, verified_income * Decimal("0.50") - existing).quantize(CENT, rounding=ROUND_DOWN)
        unit_emi = _emi(Decimal(1), annual / 12, months)
        eligible = (max_emi / unit_emi).quantize(CENT, rounding=ROUND_DOWN)
        recommended = min(requested, eligible)
        risk = loan.get("risk", {})
        band = loan.get("risk_band") or (risk.get("band") if isinstance(risk, dict) else risk)
        reasons = []
        recommendation = "approve" if requested <= eligible else "approve_lower"
        if requested > eligible:
            reasons.append("The requested EMI would exceed the 50% fixed-obligation-to-income cap using verified income.")
        if band in {"high", "unverified"}:
            recommendation = "decline"
            reasons.append("Identity or fraud checks require resolution before lending." if band == "high" else "Complete the missing verification sources before lending.")
        if verified_income <= 0 or recommended <= fees or "bank_statement" not in loan.get("documents", []):
            recommendation = "decline"
            reasons.append("Verified income and sufficient repayment capacity are required.")
        if recommendation == "decline":
            recommended = ZERO
        if not reasons:
            reasons.append("The requested amount fits the 50% fixed-obligation-to-income cap; a human officer must decide.")
        # Disclose requested-loan economics even when the recommendation is a decline.
        schedule = _amortize(requested, annual, months)
        payment = _decimal(schedule[0]["emi"])
        interest = sum((_decimal(row["interest"]) for row in schedule), ZERO)
        apr = _apr(requested, fees, [_decimal(row["emi"]) for row in schedule])
        return {"foir": float(((existing + payment) / verified_income).quantize(Decimal("0.000001"))) if verified_income > 0 else None,
                "max_emi": _money(max_emi), "emi": _money(payment), "eligible_amount": _money(eligible), "recommended_amount": _money(recommended),
                "tenure_months": months, "annual_rate": float(annual), "apr": float(apr), "total_interest": _money(interest),
                "total_cost": _money(requested + interest + fees), "fees": _money(fees), "recommendation": recommendation, "reasons": reasons}


def emi_schedule(loan: dict, start_date: date | str | None = None) -> list[dict]:
    """First payment is one calendar month after start; preserve the anchor day."""
    anchor = as_date(start_date or loan.get("disbursed_at")) or date.today()
    principal = _decimal(loan.get("approved_amount", loan.get("requested_amount")))
    annual = _decimal(loan.get("annual_rate"))
    tenure = _decimal(loan.get("tenure_months"))
    if principal <= 0 or not 0 <= annual <= 1 or tenure != tenure.to_integral_value() or not 1 <= tenure <= 120:
        raise ValueError("Invalid repayment schedule inputs")
    rows = _amortize(principal, annual, int(tenure))
    for row in rows:
        absolute_month = anchor.year * 12 + anchor.month - 1 + row["installment"]
        year, month_index = divmod(absolute_month, 12)
        month = month_index + 1
        due = date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))
        row.update({"due_date": due.isoformat(), "status": "due", "loan_id": loan.get("id")})
    return rows


def bureau_lookup(loan: dict) -> dict:
    """Return a labeled fixture, never a real bureau lookup or default prediction."""
    fixture = loan.get("bureau_fixture") or {}
    return {"synthetic": True, "source": "Saakh synthetic fixture", "score": int(fixture.get("score", 725)),
            "past_due_accounts": int(fixture.get("past_due_accounts", 0)), "existing_emi": float(_decimal(loan.get("existing_emi"))),
            "disclaimer": "Demonstration data only; this is not a prediction of a person's future repayment."}
