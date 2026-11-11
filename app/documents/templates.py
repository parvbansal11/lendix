"""Fixed document content and field selections for the synthetic lending office."""

from dataclasses import dataclass

WATERMARK = "SPECIMEN · SYNTHETIC DEMO DATA · NOT A LEGAL DOCUMENT"


@dataclass(frozen=True)
class Template:
    title: str
    introduction: str
    fields: tuple[tuple[str, str], ...]


TEMPLATES = {
    "application_acknowledgment": Template("Application acknowledgment", "We have received your application. The Underwriter will apply the lending policy and refer exceptions to the risk head.", (("Requested amount", "requested_amount"), ("Tenure (months)", "tenure_months"), ("Application status", "status"))),
    "missing_documents_request": Template("Missing documents request", "Please provide the items below so that we can continue reviewing your application.", (("Required items", "missing_documents"),)),
    "consent_record": Template("Consent record", "This record identifies the purpose, scope and person associated with the recorded consent.", (("Consent provided", "consent"), ("Consented purposes", "consent_purposes"), ("Recorded at", "consent_at"), ("Consented by", "consent_by"))),
    "kyc_verification_summary": Template("KYC verification summary", "Identity checks use the supplied synthetic records. An unavailable source remains unverified.", (("Identity check status", "kyc_status"), ("Documents received", "documents"), ("Missing documents", "missing_documents"))),
    "risk_report": Template("Risk report", "This report describes rule-based verification flags. It does not predict whether a person will default.", (("Risk band", "risk.band"), ("Source status", "risk.source_status"))),
    "decision_note": Template("Underwriting decision note", "The Underwriter applies the written lending policy. The model explains the result; it does not choose or change the decision. Escalated cases require a reasoned decision by the risk head.", (("Recorded decision", "underwriting.decision"), ("Policy version", "underwriting.policy_version"), ("Approval source", "approval_source"), ("Decision by", "decision_by"), ("Approved amount", "approved_amount"), ("Risk-head reason", "override_reason"))),
    "sanction_letter": Template("Sanction letter", "This specimen records approval by the lending policy or by the risk head on an escalated case. Disbursement remains subject to accepted disclosures and completed prerequisites.", (("Decision", "decision"), ("Approval source", "approval_source"), ("Policy version", "underwriting.policy_version"), ("Approved amount", "approved_amount"), ("Annual interest rate", "annual_rate"), ("Tenure (months)", "tenure_months"), ("Monthly EMI", "credit.emi"), ("Fees", "fees"), ("Decision by", "decision_by"))),
    "rejection_letter": Template("Application decision", "Thank you for applying. Your application has been declined under the recorded decision below. You may ask for clarification or submit corrected information.", (("Decision", "decision"), ("Decision source", "approval_source"), ("Policy rule", "underwriting.rule"), ("Policy version", "underwriting.policy_version"),)),
    "key_facts_statement": Template("Key Facts Statement", "Review these terms before accepting the loan. Every fee must be disclosed before disbursement.", (("Loan amount", "approved_amount"), ("Annual interest rate", "annual_rate"), ("Annual percentage rate (APR)", "credit.apr"), ("Fees (all amounts)", "fees"), ("Monthly EMI", "credit.emi"), ("Tenure (months)", "tenure_months"), ("Total repayments", "credit.total_repayment"), ("Total charges (interest and fees)", "credit.total_charges"), ("Total amount payable (including fees)", "credit.total_cost"), ("Cooling-off period (days)", "cooling_off_days"), ("Grievance contact", "grievance_contact"))),
    "loan_agreement_summary": Template("Loan agreement summary", "This summary describes the accepted terms. It is a synthetic demonstration and does not create an enforceable loan agreement.", (("Loan amount", "approved_amount"), ("Tenure (months)", "tenure_months"), ("Monthly EMI", "credit.emi"), ("Annual percentage rate (APR)", "credit.apr"), ("Fees", "fees"), ("KFS accepted", "kfs_accepted"), ("KFS accepted at", "kfs_accepted_at"))),
    "disbursement_advice": Template("Disbursement advice", "SIMULATED PAYOUT. No money has moved. The simulated payout must use the borrower's own verified account.", (("Payout amount", "payout_amount"), ("Simulated UTR", "utr"), ("Payout date", "payout_at"), ("Account holder", "payout_account_name"), ("Account ending", "account_last_four"), ("Account verified", "account_verified"))),
    "repayment_schedule": Template("Repayment schedule", "Amounts are in INR. The last installment accounts for rounding so the closing principal balance is zero.", (("Loan amount", "approved_amount"), ("Annual interest rate", "annual_rate"), ("Tenure (months)", "tenure_months"))),
    "payment_receipt": Template("Payment receipt", "This receipt records a simulated payment against the loan. No real funds are collected in this demonstration.", (("Receipt reference", "receipt_id"), ("Payment amount", "payment_amount"), ("Payment date", "payment_date"), ("Installment number", "installment"), ("Remaining balance", "outstanding_balance"))),
    "payment_reminder": Template("Payment reminder", "This is a polite reminder of your upcoming installment. If you are facing difficulty, please contact us to discuss support.", (("Amount due", "amount_due"), ("Due date", "due_date"), ("Grievance contact", "grievance_contact"))),
    "restructure_request": Template("Restructure request note", "The borrower has requested help with repayment. The risk head must assess any change to terms. No revised terms are approved by this note.", (("Hardship reported", "hardship"), ("Outstanding balance", "outstanding_balance"), ("Requested support", "requested_support"), ("Review status", "restructure_status"))),
    "no_dues_certificate": Template("No Dues Certificate", "This specimen records that the loan has been closed after all recorded dues were settled.", (("Loan status", "status"), ("Outstanding balance", "outstanding_balance"), ("Closure date", "closed_at"))),
    "grievance_acknowledgment": Template("Grievance acknowledgment", "We acknowledge your complaint and will work to resolve it within 30 days. If it remains unresolved, you may escalate it to the RBI Ombudsman under the applicable process.", (("Grievance reference", "grievance_id"), ("Received at", "received_at"), ("Resolution window", "resolution_window"), ("Grievance contact", "grievance_contact"))),
    "review_queue": Template("Daily review queue", "Policy decisions and cases escalated to the risk head. All applicants and values are synthetic.", ()),
    "portfolio_dashboard": Template("Portfolio dashboard", "Counts and collections describe the supplied synthetic portfolio.", ()),
    "audit_log": Template("Audit log export", "Recorded actions and handoffs for the synthetic lending office.", ()),
}

ALIASES = {
    "application_ack": "application_acknowledgment", "application_acknowledgement": "application_acknowledgment",
    "missing_documents": "missing_documents_request", "consent": "consent_record",
    "kyc_summary": "kyc_verification_summary", "kyc": "kyc_verification_summary",
    "credit_appraisal": "decision_note", "credit_appraisal_decision_note": "decision_note",
    "credit_appraisal_/_decision_note_for_the_officer": "decision_note", "credit_decision_note": "decision_note",
    "sanction": "sanction_letter", "rejection": "rejection_letter", "kfs": "key_facts_statement",
    "loan_agreement": "loan_agreement_summary", "disbursement": "disbursement_advice",
    "schedule": "repayment_schedule", "receipt": "payment_receipt", "reminder": "payment_reminder",
    "restructure_request_note": "restructure_request", "restructure_note": "restructure_request",
    "no_dues": "no_dues_certificate", "grievance": "grievance_acknowledgment",
    "grievance_acknowledgement": "grievance_acknowledgment", "daily_review_queue": "review_queue",
    "dashboard": "portfolio_dashboard", "audit_log_export": "audit_log",
}
