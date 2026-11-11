"""Conservative demo policies, not a complete legal compliance system."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.risk_rules import names_match

IST = ZoneInfo("Asia/Kolkata")
WATERMARK = "SPECIMEN · SYNTHETIC DEMO DATA · NOT A LEGAL DOCUMENT"


def _datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time())
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return result.replace(tzinfo=IST) if result.tzinfo is None else result


def _now(value=None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = _datetime(value)
    if parsed is None:
        raise ValueError("now must be an ISO date/time")
    return parsed


def check_message(text: str, *, borrower_facing: bool = True, now=None, third_party: bool = False) -> dict:
    reasons: list[str] = []
    value = re.sub(r"\s+", " ", str(text)).strip().casefold()
    if not value:
        reasons.append("A message must contain useful text.")
    if third_party:
        reasons.append("Fair practices prohibit contacting family, friends, employers or other third parties about a borrower's loan.")
    # Detect direct threats and promises. Explicit refusals may safely name the rule.
    unsafe_patterns = [
        (r"\b(?:guaranteed approval|approval is guaranteed|guarantee(?:d)? (?:your |the )?loan approval|100% approval)\b", "Approval cannot be guaranteed; a human credit officer must decide."),
        (r"\b(?:you (?:will|could) (?:be arrested|go to jail)|we (?:will|'ll) (?:arrest|shame|humiliate)|pay (?:now |up )?or|shame on you|deadbeat|defaulter deserves|ruin your (?:life|reputation)|send (?:goons|thugs)|break your)\b", "Threats, abuse, intimidation and shaming are prohibited."),
        (r"\b(?:contact|call|message|tell|notify|visit) (?:the borrower's |your |their )?(?:employer|boss|family|friends|parents|relatives|neighbours|neighbors)\b", "Contact only the borrower; contacting third parties is prohibited."),
        (r"\b(?:hide|conceal|undisclosed|secret) (?:the |any |our )?(?:fees|charges)\b", "Every fee must be disclosed in the Key Facts Statement."),
        (r"\b(?:stupid|idiot|worthless|scum)\b", "Abusive language is prohibited."),
    ]
    for pattern, reason in unsafe_patterns:
        for match in re.finditer(pattern, value):
            prefix = value[max(0, match.start() - 55):match.start()]
            suffix = value[match.end():match.end() + 40]
            negated = re.search(r"(?:cannot|can't|won't|will not|do not|must not|never|no)\s+(?:(?:offer|promise|provide|make|use|claim|give|say|that|any)\s+)*$", prefix)
            explained = re.match(r"\s+(?:is|are)\s+(?:prohibited|not permitted|not allowed|unavailable)", suffix)
            if not negated and not explained:
                reasons.append(reason)
                break
    if "hidden fees" in value and not re.search(r"\b(?:no|without|never any|do not charge|will not charge) hidden fees\b", value):
        reasons.append("Hidden fees are prohibited; disclose every fee in the Key Facts Statement.")
    result = {"allowed": not reasons, "reasons": list(dict.fromkeys(reasons))}
    # A supplied timestamp marks an outbound contact; interactive replies only
    # receive content checks here, and proactive senders must supply their clock.
    if borrower_facing and now is not None:
        local = _now(now).astimezone(IST)
        if not time(8, 0) <= local.time().replace(tzinfo=None) < time(19, 0):
            reasons.append("Borrower contact is permitted only from 08:00 to before 19:00 IST; schedule this message for 08:00 IST.")
            next_day = local.date() + timedelta(days=1) if local.hour >= 19 else local.date()
            result["next_contact_at"] = datetime.combine(next_day, time(8), tzinfo=IST).isoformat()
    result.update(allowed=not reasons, reasons=list(dict.fromkeys(reasons)))
    return result


def check_disbursement(loan: dict, *, now=None, demo_skip_cooling: bool = False) -> dict:
    reasons = []
    current = _now(now)
    approval = loan.get("human_approval") or {}
    approved = loan.get("human_approved") is True or approval.get("approved") is True
    role = loan.get("approved_by_role") or approval.get("role")
    approved_at = _datetime(loan.get("approved_at") or approval.get("at"))
    if not approved or role != "credit_officer" or approved_at is None or approved_at > current:
        reasons.append("A recorded, dated human credit officer approval is required before disbursement.")
    risk = loan.get("risk") or {}
    band = loan.get("risk_band") or (risk.get("band") if isinstance(risk, dict) else risk)
    if band == "high" and not str(loan.get("override_reason") or approval.get("reason") or "").strip():
        reasons.append("A high-risk override requires the credit officer's typed reason in the audit trail.")
    if band == "unverified":
        reasons.append("Required verification sources must be complete before disbursement.")
    if loan.get("account_verified") is not True or not loan.get("payout_account") or not names_match(loan.get("name", ""), loan.get("payout_name", "")):
        reasons.append("Disbursement is permitted only to the borrower's own verified bank account.")
    if str(loan.get("payout_account_type", "own")).lower() in {"pool", "third_party", "third-party", "pooled"} or loan.get("third_party_account"):
        reasons.append("Third-party and pool accounts cannot receive a disbursement.")
    if loan.get("consent") is not True:
        reasons.append("Recorded borrower consent is required.")
    accepted_at = _datetime(loan.get("kfs_accepted_at"))
    if loan.get("kfs_issued") is not True or loan.get("kfs_accepted") is not True or accepted_at is None or accepted_at > current:
        reasons.append("The borrower must receive and explicitly accept the Key Facts Statement, with a valid recorded acceptance time.")
    issued_at = _datetime(loan.get("kfs_issued_at"))
    if issued_at and accepted_at and issued_at > accepted_at:
        reasons.append("Key Facts Statement acceptance must follow its issue time.")
    try:
        days = Decimal(str(loan.get("cooling_off_days", 1)))
        valid_days = days.is_finite() and days >= 1 and days == days.to_integral_value() and days <= 365
    except (InvalidOperation, TypeError, ValueError):
        valid_days = False
    if not valid_days:
        reasons.append("The cooling-off period must be a configured whole number of days, at least one.")
    elif not demo_skip_cooling and accepted_at and current < accepted_at + timedelta(days=int(days)):
        reasons.append("The configured demo waiting period has not elapsed since KFS acceptance; the cooling-off exit right remains available.")
    return {"allowed": not reasons, "reasons": reasons, "demo_cooling_skipped": bool(demo_skip_cooling)}


def check_document(kind: str, data: dict) -> dict:
    reasons: list[str] = []
    normalized = kind.lower().replace("-", "_").replace(" ", "_")
    for key in ("loan_id", "date", "document_id"):
        if not data.get(key):
            reasons.append(f"Document must include {key.replace('_', ' ')}.")
    if data.get("date") and _datetime(data["date"]) is None:
        reasons.append("Document date must be a valid ISO date.")
    if data.get("watermark") != WATERMARK:
        reasons.append("The synthetic specimen watermark is required on every document.")
    if normalized in {"kfs", "key_facts_statement"}:
        aliases = {"loan_amount": ("amount", "approved_amount", "requested_amount"), "tenure_months": ("tenure",), "cooling_off_days": ("cooling_off_period",)}
        for key in ("loan_amount", "apr", "fees", "emi", "tenure_months", "total_cost", "cooling_off_days", "grievance_contact"):
            value = data.get(key)
            if value is None:
                value = next((data.get(alias) for alias in aliases.get(key, ()) if data.get(alias) is not None), None)
            if value is None or value == "":
                reasons.append(f"KFS must disclose {key.replace('_', ' ')}.")
            elif key not in {"grievance_contact", "fees"}:
                try:
                    number = Decimal(str(value))
                    if not number.is_finite() or number < 0 or (key in {"loan_amount", "tenure_months", "cooling_off_days"} and number <= 0):
                        raise ValueError("invalid number")
                except (InvalidOperation, TypeError, ValueError):
                    reasons.append(f"KFS {key.replace('_', ' ')} must be a valid non-negative number.")
        if data.get("exit_without_penalty") is False:
            reasons.append("The cooling-off period must permit exit without penalty.")
    if normalized in {"rejection", "rejection_letter"} and not data.get("reasons"):
        reasons.append("A rejection letter must explain the main reasons in plain language.")
    if normalized in {"grievance", "grievance_acknowledgment", "grievance_acknowledgement"}:
        if not (data.get("reference") or data.get("grievance_reference")):
            reasons.append("A grievance acknowledgment requires a reference number.")
        if data.get("resolution_days", 30) != 30:
            reasons.append("A grievance acknowledgment must state the 30-day resolution window.")
    if normalized in {"no_dues", "no_dues_certificate", "closure"}:
        try:
            if Decimal(str(data.get("outstanding_balance", data.get("outstanding", data.get("balance", "unknown"))))) != 0:
                raise ValueError("balance")
        except (InvalidOperation, TypeError, ValueError):
            reasons.append("A No Dues Certificate requires a confirmed zero outstanding balance.")
    for key in ("summary", "reasons", "text", "message"):
        value = data.get(key)
        if value:
            checked = check_message(" ".join(map(str, value)) if isinstance(value, list) else str(value), borrower_facing=False)
            reasons.extend(checked["reasons"])
    return {"allowed": not reasons, "reasons": list(dict.fromkeys(reasons))}


def grievance_acknowledgment(reference: str) -> str:
    return (f"Your grievance has been recorded with reference {reference}. We will work to resolve it within 30 days. "
            "Contact the Lendix Finance grievance officer at grievances@lendix.example. If your complaint remains unresolved "
            "after 30 days, or you are dissatisfied with the response, you may escalate to the RBI Ombudsman through "
            "the complaint management portal at https://cms.rbi.org.in/. This acknowledgment uses synthetic demo data.")
