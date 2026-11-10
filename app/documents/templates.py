"""Fixed document content and field selections for the synthetic lending office."""

from dataclasses import dataclass

WATERMARK = "SPECIMEN · SYNTHETIC DEMO DATA · NOT A LEGAL DOCUMENT"


@dataclass(frozen=True)
class Template:
    title: str
    introduction: str
    fields: tuple[tuple[str, str], ...]


TEMPLATES = {
    "application_acknowledgment": Template("Application acknowledgment", "We have received your application. A credit officer will review the information and make a decision.", (("Requested amount", "requested_amount"), ("Tenure (months)", "tenure_months"), ("Application status", "status"))),
    "missing_documents_request": Template("Missing documents request", "Please provide the items below so that we can continue reviewing your application.", (("Required items", "missing_documents"),)),
    "consent_record": Template("Consent record", "This record identifies the purpose, scope and person associated with the recorded consent.", (("Consent provided", "consent"), ("Consented purposes", "consent_purposes"), ("Recorded at", "consent_at"), ("Consented by", "consent_by"))),
    "kyc_verification_summary": Template("KYC verification summary", "Identity checks use the supplied synthetic records. An unavailable source remains unverified.", (("Identity check status", "kyc_status"), ("Documents received", "documents"), ("Missing documents", "missing_documents"))),
    "risk_report": Template("Risk report", "This report describes rule-based verification flags. It does not predict whether a person will default.", (("Risk band", "risk.band"), ("Source status", "risk.source_status"))),
    "credit_appraisal": Template("Credit appraisal and decision note", "Recommendation for the credit officer. This document does not approve a loan.", (("Recommendation", "credit.recommendation"), ("Requested amount", "requested_amount"), ("Eligible amount", "credit.eligible_amount"), ("Recommended amount", "credit.recommended_amount"), ("Monthly income", "monthly_income"), ("Existing monthly obligations", "existing_emi"), ("Maximum EMI", "credit.max_emi"), ("Proposed EMI", "credit.emi"), ("FOIR", "credit.foir"), ("FOIR cap", "foir_cap"), ("Annual percentage rate (APR)", "credit.apr"))),
    "sanction_letter": Template("Sanction letter", "This specimen records the credit officer's decision. Disbursement remains subject to accepted disclosures and completed prerequisites.", (("Officer decision", "decision"), ("Approved amount", "approved_amount"), ("Annual interest rate", "annual_rate"), ("Tenure (months)", "tenure_months"), ("Monthly EMI", "credit.emi"), ("Fees", "fees"), ("Approving officer", "approved_by"))),
    "rejection_letter": Template("Application decision", "Thank you for applying. The credit officer has declined this application. You may ask for clarification or submit corrected information.", (("Officer decision", "decision"),)),
    "key_facts_statement": Template("Key Facts Statement", "Review these terms before accepting the loan. Every fee must be disclosed before disbursement.", (("Loan amount", "approved_amount"), ("Annual interest rate", "annual_rate"), ("Annual percentage rate (APR)", "credit.apr"), ("Fees (all amounts)", "fees"), ("Monthly EMI", "credit.emi"), ("Tenure (months)", "tenure_months"), ("Total repayments", "credit.total_repayment"), ("Total charges (interest and fees)", "credit.total_charges"), ("Total amount payable (including fees)", "credit.total_cost"), ("Cooling-off period (days)", "cooling_off_days"), ("Grievance contact", "grievance_contact"))),
    "loan_agreement_summary": Template("Loan agreement summary", "This summary describes the accepted terms. It is a synthetic demonstration and does not create an enforceable loan agreement.", (("Loan amount", "approved_amount"), ("Tenure (months)", "tenure_months"), ("Monthly EMI", "credit.emi"), ("Annual percentage rate (APR)", "credit.apr"), ("Fees", "fees"), ("KFS accepted", "kfs_accepted"), ("KFS accepted at", "kfs_accepted_at"))),
    "disbursement_advice": Template("Disbursement advice", "SIMULATED PAYOUT. No money has moved. The simulated payout must use the borrower's own verified account.", (("Payout amount", "payout_amount"), ("Simulated UTR", "utr"), ("Payout date", "payout_at"), ("Account holder", "payout_account_name"), ("Account ending", "account_last_four"), ("Account verified", "account_verified"))),
    "repayment_schedule": Template("Repayment schedule", "Amounts are in INR. The last installment accounts for rounding so the closing principal balance is zero.", (("Loan amount", "approved_amount"), ("Annual interest rate", "annual_rate"), ("Tenure (months)", "tenure_months"))),
    "payment_receipt": Template("Payment receipt", "This receipt records a simulated payment against the loan. No real funds are collected in this demonstration.", (("Receipt reference", "receipt_id"), ("Payment amount", "payment_amount"), ("Payment date", "payment_date"), ("Installment number", "installment"), ("Remaining balance", "outstanding_balance"))),
    "payment_reminder": Template("Payment reminder", "This is a polite reminder of your upcoming installment. If you are facing difficulty, please contact us to discuss support.", (("Amount due", "amount_due"), ("Due date", "due_date"), ("Grievance contact", "grievance_contact"))),
    "restructure_request": Template("Restructure request note", "The borrower has requested help with repayment. A credit officer must assess any change to terms. No revised terms are approved by this note.", (("Hardship reported", "hardship"), ("Outstanding balance", "outstanding_balance"), ("Requested support", "requested_support"), ("Review status", "restructure_status"))),
    "no_dues_certificate": Template("No Dues Certificate", "This specimen records that the loan has been closed after all recorded dues were settled.", (("Loan status", "status"), ("Outstanding balance", "outstanding_balance"), ("Closure date", "closed_at"))),
    "grievance_acknowledgment": Template("Grievance acknowledgment", "We acknowledge your complaint and will work to resolve it within 30 days. If it remains unresolved, you may escalate it to the RBI Ombudsman under the applicable process.", (("Grievance reference", "grievance_id"), ("Received at", "received_at"), ("Resolution window", "resolution_window"), ("Grievance contact", "grievance_contact"))),
    "review_queue": Template("Daily review queue", "Applications awaiting human review. All applicants and values are synthetic.", ()),
    "portfolio_dashboard": Template("Portfolio dashboard", "Counts and collections describe the supplied synthetic portfolio.", ()),
    "audit_log": Template("Audit log export", "Recorded actions and handoffs for the synthetic lending office.", ()),
}

ALIASES = {
    "application_ack": "application_acknowledgment", "application_acknowledgement": "application_acknowledgment",
    "missing_documents": "missing_documents_request", "consent": "consent_record",
    "kyc_summary": "kyc_verification_summary", "kyc": "kyc_verification_summary",
    "decision_note": "credit_appraisal", "credit_appraisal_decision_note": "credit_appraisal",
    "credit_appraisal_/_decision_note_for_the_officer": "credit_appraisal", "credit_decision_note": "credit_appraisal",
    "sanction": "sanction_letter", "rejection": "rejection_letter", "kfs": "key_facts_statement",
    "loan_agreement": "loan_agreement_summary", "disbursement": "disbursement_advice",
    "schedule": "repayment_schedule", "receipt": "payment_receipt", "reminder": "payment_reminder",
    "restructure_request_note": "restructure_request", "restructure_note": "restructure_request",
    "no_dues": "no_dues_certificate", "grievance": "grievance_acknowledgment",
    "grievance_acknowledgement": "grievance_acknowledgment", "daily_review_queue": "review_queue",
    "dashboard": "portfolio_dashboard", "audit_log_export": "audit_log",
}
