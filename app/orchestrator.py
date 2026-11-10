"""Auditable lending state machine. Model output never grants authority."""
import asyncio
import re
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .agents import AGENTS
from .compliance import check_disbursement, check_document, check_message, grievance_acknowledgment
from .config import ROOT
from .documents import DocumentService
from .model import ModelUnavailable
from .risk_rules import run_risk_checks
from .store import utcnow
from .tools import bureau_lookup, compute_affordability, emi_schedule, validate_application

IST = ZoneInfo("Asia/Kolkata")
WATERMARK = "SPECIMEN · SYNTHETIC DEMO DATA · NOT A LEGAL DOCUMENT"
HELP = "Saakh handles loan applications, document checks, risk reviews, officer decisions, repayments and complaints. Try ‘review L-1042’, ‘list loans’, or tell me which lending task you need."


class Orchestrator:
    def __init__(self, store, settings, model, adapter=None):
        self.store, self.settings, self.model, self.adapter = store, settings, model, adapter
        self.documents = DocumentService(settings.output_dir)
        self.lock = asyncio.Lock()
        self.conversation = "office"

    def event(self, kind, agent, text, loan=None, data=None):
        return self.store.emit(kind, agent, text, conversation_id=self.conversation,
                               loan_id=loan.get("id") if loan else None, data=data)

    def handoff(self, source, target, loan, reason):
        self.event("handoff", target, f"{source} → {target}: {loan['id']} handed over ({reason})", loan)

    def tool(self, agent, name, execute, loan=None):
        AGENTS[agent].authorize(name)
        self.event("tool", agent, name, loan)
        result = execute()
        self.event("status", agent, f"{name} complete", loan)
        return result

    async def model_tool(self, agent, name, data, execute, loan):
        return await self.model.tool(AGENTS[agent], name, data,
                                     lambda: self.tool(agent, name, execute, loan))

    async def reply(self, text, loan=None, agent="Front Desk"):
        result = self.tool("Compliance", "check_message", lambda: check_message(text), loan)
        if not result["allowed"]:
            self.event("status", "Compliance", "Blocked a draft that did not meet the message policy.", loan)
            text = "I cannot send that wording. I can help with a respectful, factual lending request."
        if self.adapter:
            await self.adapter.send_reply(self.conversation, text)
        else:
            self.event("reply", agent, text, loan)
        return text

    async def mention(self, text, loan):
        self.tool("Reporter", "mention", lambda: None, loan)
        if self.adapter:
            await self.adapter.mention("officer", text)
        else:
            self.event("mention", "Reporter", f"@officer {text}", loan)

    async def progress(self, text):
        self.tool("Reporter", "post_update", lambda: None)
        if self.adapter:
            await self.adapter.post_update(text)
        else:
            self.event("progress", "Reporter", text)

    async def document(self, kind, loan, data=None, format="pdf"):
        data = data or {}
        merged = {**loan, **loan.get("credit", {}), **data,
                  "loan_id": loan["id"], "date": utcnow().date().isoformat(),
                  "document_id": "pending", "watermark": WATERMARK}
        result = self.tool("Compliance", "check_document", lambda: check_document(kind, merged), loan)
        if not result["allowed"]:
            raise ValueError("Document blocked: " + "; ".join(result["reasons"]))
        name = "make_chart" if format == "png" else "make_document"
        AGENTS["Reporter"].authorize(name)
        self.event("tool", "Reporter", name, loan)
        artifact = await asyncio.to_thread(self.documents.render, kind, loan, data, format=format)
        self.event("status", "Reporter", f"{name} complete", loan)
        artifact["path"] = str(artifact["path"])
        saved = self.store.register_file(artifact, self.settings.public_base_url)
        self.event("document", "Reporter", f"Issued {kind}: {artifact['document_id']}", loan,
                   {"document_id": artifact["document_id"], "kind": kind})
        if self.adapter:
            await self.adapter.send_file(self.conversation, saved["url"], saved["filename"])
        else:
            self.event("file", "Reporter", saved["filename"], loan,
                       {k: v for k, v in saved.items() if k not in {"path", "token"}})
        return saved

    def save(self, loan):
        return self.store.put("loan", loan["id"], loan)

    async def run(self, message, role="ops", conversation_id="office", now=None):
        async with self.lock:
            self.conversation = conversation_id
            try:
                return await self._handle(message.strip(), role, now or utcnow())
            except ModelUnavailable as exc:
                self.event("error", "Front Desk", "Model service unavailable; the request can be retried.")
                return await self.reply(str(exc))
            except (ValueError, PermissionError) as exc:
                return await self.reply(str(exc))

    async def _handle(self, message, role, now):
        if role not in {"credit_officer", "ops", "borrower"}:
            return await self.reply("Please choose a valid demo role.")
        text = message.casefold()
        match = re.search(r"\bL-\d+\b", message, re.I)
        loan = self.store.get("loan", match.group().upper()) if match else None
        self.event("status", "Front Desk", f"Received a request as {role}.", loan)
        if match and not loan:
            return await self.reply("I cannot find that loan. Use ‘list loans’ to see the synthetic applications.")
        # Safety gates execute before model routing and cannot be bypassed by instructions in chat.
        if any(word in text for word in ("social media", "instagram", "facebook", "linkedin")):
            return await self.reply("I only use consented application data. I cannot look up someone’s social media.", loan)
        if any(word in text for word in ("employer", "family", "friends", "third party", "third-party")) and any(word in text for word in ("contact", "message", "call", "tell", "send")):
            return await self.reply("I cannot contact an employer, family member, friend or other third party about a borrower’s debt. Fair practices require contacting the borrower directly and respectfully.", loan)
        if "default" in text and any(word in text for word in ("will", "predict", "person")):
            return await self.reply("I can explain recorded risk reasons and affordability; I cannot predict whether a person will default. Which loan would you like to review?", loan)
        if any(word in text for word in ("threaten", "shame", "abuse", "harass")):
            return await self.reply("I cannot threaten, shame or harass a borrower. I can prepare a polite reminder or a hardship request.", loan)
        if re.match(r"^(approve|decline)\b", text):
            if role != "credit_officer":
                return await self.reply("Only the credit officer can approve or decline a loan. I can prepare the case for their review.", loan)
            if not loan:
                return await self.reply("Which loan should the officer decide? Include an ID such as L-1042.")
            return await self.decide(loan, message, role)
        if "audit" in text:
            if role != "credit_officer":
                return await self.reply("Only the credit officer can export the audit log.")
            await self.document("audit_log", {"id": "PORTFOLIO"}, {"events": self.store.all_events()}, "json")
            return await self.reply("The audit log is ready. It records decisions, reasons, handoffs and document IDs.")
        if any(word in text for word in ("complaint", "grievance", "complain")):
            return await self.grievance(loan, message, role)
        if "review" in text and any(word in text for word in ("today", "applications", "batch")):
            if role == "borrower":
                return await self.reply("Portfolio review is for the ops team and credit officer. I can help with a single application.")
            await self.reply("I have started reviewing up to 20 of today’s pending applications. I will post progress and return a review queue and portfolio chart.")
            candidates = [x for x in self.store.all("loan") if x.get("status", "new") in {"new", "received", "pending", "awaiting_documents"} and x.get("applied_at", "")[:10] == now.date().isoformat()]
            batch_fixtures = [x for x in candidates if x.get("today_batch")]
            rows = (batch_fixtures or candidates)[:20]
            for index, item in enumerate(rows, 1):
                try:
                    await self.review(item)
                except (ModelUnavailable, ValueError) as exc:
                    self.event("error", "Reporter", f"{item['id']}: review paused; needs attention.", item)
                    await self.reply(str(exc), item)
                if index % 4 == 0 or index == len(rows):
                    await self.progress(f"Reviewed {index}/{len(rows)} applications; human decisions remain pending.")
                await asyncio.sleep(0)
            await self.export_queue()
            await self.dashboard()
            return await self.reply(f"Batch finished: {len(rows)} applications processed. Download the review queue and dashboard. No loans were automatically approved.")
        if "dashboard" in text or "portfolio" in text:
            await self.dashboard()
            return await self.reply("The portfolio dashboard is ready.")
        if "review queue" in text:
            await self.export_queue()
            return await self.reply("The review queue is ready in CSV and Excel.")
        if text in {"list", "list loans", "loans"}:
            loans = self.tool("Front Desk", "list_loans", lambda: self.store.all("loan"))
            return await self.reply("\n".join(f"{x['id']} · {x['name']} · {x.get('status', 'new')}" for x in loans))
        if any(word in text for word in ("can't pay", "cannot pay", "lost my job", "hardship", "restructure")):
            if not loan:
                return await self.reply("I’m sorry things are difficult. Which loan should I include in a request for the officer?")
            return await self.hardship(loan, message)
        if "reminder" in text:
            if not loan:
                return await self.reply("Which loan needs a repayment reminder?")
            if role == "borrower":
                return await self.reply("The ops team can schedule borrower reminders. I can show your repayment schedule.", loan)
            return await self.reminder(loan, now)
        if loan and (text.startswith("review") or text.startswith("check")):
            return await self.review(loan)
        if loan and ("add bank statement" in text or "document arrived" in text):
            if loan.get("status") not in {"new", "pending", "received", "awaiting_documents", "awaiting_decision"}:
                return await self.reply("Documents cannot be changed after a decision. Ask the officer to open a new application.", loan)
            loan["documents"] = sorted(set(loan.get("documents", [])) | {"bank_statement"})
            self.event("tool", "Intake", "request_document: synthetic bank statement received", loan)
            self.save(loan)
            return await self.review(loan)
        if loan and ("accept" in text and "kfs" in text):
            if role != "borrower":
                return await self.reply("KFS acceptance must be given by the borrower. Switch to the synthetic borrower role for this demonstration.", loan)
            if loan.get("status") != "approved" or not loan.get("kfs_issued"):
                return await self.reply("The officer must approve the loan and issue the KFS before you can accept it.", loan)
            if loan.get("kfs_accepted"):
                return await self.reply("Your KFS acceptance is already recorded.", loan)
            loan.update(kfs_accepted=True, kfs_accepted_at=now.isoformat(), kfs_accepted_by="borrower")
            self.save(loan)
            self.event("decision", "Disbursement", "Borrower accepted the issued KFS.", loan)
            await self.document("loan_agreement_summary", loan)
            return await self.reply("Your KFS acceptance is recorded. You can exit without penalty during the cooling-off period. Ops can request the simulated payout after the demo waiting gate.", loan)
        if loan and "disburse" in text:
            if role == "borrower":
                return await self.reply("Only ops or the credit officer can initiate the simulated payout.", loan)
            return await self.disburse(loan, now)
        if loan and re.match(r"^(exit|cancel)\b", text):
            return await self.exit_loan(loan, now)
        if loan and re.match(r"^(pay|record payment)\b", text):
            return await self.payment(loan)
        if loan and (text.startswith("close") or "fast-forward" in text):
            if role == "borrower":
                return await self.reply("Only ops or the credit officer can fast-forward simulated payments to closure.", loan)
            return await self.close(loan)
        if loan and "schedule" in text:
            if loan.get("status") not in {"disbursed", "closed", "restructure_requested"}:
                return await self.reply("The repayment schedule is issued after disbursement.", loan)
            await self.document("repayment_schedule", loan, {"schedule": self.schedule(loan)}, "pdf")
            return await self.reply("Your repayment schedule is ready.", loan)
        if text.startswith(("apply", "application", "i consent")) or self.store.get("conversation", self.conversation):
            return await self.application(message, role, loan)
        route = await self.model.structured(AGENTS["Front Desk"], {"message": message})
        self.tool("Front Desk", "route", lambda: route.model_dump(), loan)
        if route.intent == "application":
            return await self.application(message, role, loan)
        if route.intent == "grievance":
            return await self.grievance(loan, message, role)
        if route.intent == "hardship" and loan:
            return await self.hardship(loan, message)
        if route.intent == "review" and loan:
            return await self.review(loan)
        return await self.reply(HELP + "\nWhich loan or task should we work on?")

    async def review(self, loan):
        if loan.get("status") in {"approved", "declined", "disbursed", "closed", "restructure_requested"}:
            return await self.reply(f"{loan['id']} is already {loan['status']}. Its recorded decision and reports are available in the documents panel.", loan)
        self.handoff("Front Desk", "Intake", loan, "application review requested")
        if not loan.get("acknowledged"):
            await self.document("application_acknowledgment", loan)
            loan["acknowledged"] = True
        missing = self.tool("Intake", "validate_application", lambda: validate_application(loan), loan)
        if missing:
            loan["status"] = "awaiting_documents"
            self.save(loan)
            self.tool("Intake", "request_document", lambda: missing[0], loan)
            return await self.reply(f"{loan['id']}: {missing[0]}", loan, "Intake")
        if not loan.get("consent_recorded"):
            record = {"consented_to": ["identity checks", "income and affordability assessment", "loan servicing"],
                      "consented_at": loan.get("consented_at", loan.get("applied_at")),
                      "consented_by": loan["name"], "consent": True, "source": "synthetic fixture" if not loan.get("created_from_chat") else "borrower declaration"}
            await self.document("consent_record", loan, record, "json")
            await self.document("consent_record", loan, record)
            loan["consent_recorded"] = True
        await self.document("kyc_verification_summary", loan)
        self.handoff("Intake", "Risk", loan, "documents complete")
        risk = await self.model_tool("Risk", "run_risk_checks", {"loan_id": loan["id"]},
                                     lambda: run_risk_checks(loan, self.store.all("loan")), loan)
        loan["risk"] = risk
        risk_words = await self.model.structured(AGENTS["Risk"], {"risk": risk})
        summary = self.checked_summary(risk_words.summary)
        await self.document("risk_report", loan, {"risk": risk, "summary": summary})
        await self.document("risk_report", loan, {"risk": risk}, "png")
        self.handoff("Risk", "Credit", loan, f"{risk['band']} risk; recorded rule findings")
        credit = await self.model_tool("Credit", "compute_affordability", {"loan_id": loan["id"]},
                                       lambda: compute_affordability(loan), loan)
        bureau = self.tool("Credit", "bureau_lookup", lambda: bureau_lookup(loan), loan)
        if risk["band"] in {"high", "unverified"}:
            credit["recommendation"] = "decline" if risk["band"] == "high" else "manual_review"
            credit["reasons"] = list(risk["explanations"]) + credit.get("reasons", [])
        note = await self.model.structured(AGENTS["Credit"], {"risk": risk, "credit": credit, "bureau": bureau}, deep=True)
        credit["summary"] = self.checked_summary(note.summary)
        loan.update(credit=credit, status="awaiting_decision")
        self.save(loan)
        pending = self.store.get("conversation", self.conversation)
        if pending and pending.get("loan_id") == loan["id"]:
            self.store.delete("conversation", self.conversation)
        self.tool("Credit", "draft_decision_note", lambda: credit["summary"], loan)
        await self.document("credit_appraisal", loan, {"summary": credit["summary"], "reasons": credit["reasons"]})
        self.handoff("Credit", "Compliance", loan, "recommendation ready; human decision required")
        self.handoff("Compliance", "Reporter", loan, "reports checked")
        await self.mention(f"{loan['id']} needs your decision. Risk: {risk['band']}; recommendation: {credit['recommendation']}. Risk report, chart and decision note are attached.", loan)
        if self.adapter:
            await self.adapter.office_action("request_officer_review", loan_id=loan["id"])
        else:
            self.event("office_action", "Reporter", "Local demonstration: request officer review", loan)
        reasons = "; ".join(risk["explanations"]) or "No rule flags; available sources checked."
        return await self.reply(f"{loan['id']} · Risk {risk['band']} · Recommend {credit['recommendation']}. {reasons} The credit officer makes the decision.", loan, "Reporter")

    def checked_summary(self, text):
        result = self.tool("Compliance", "check_message", lambda: check_message(text))
        return text if result["allowed"] else "Please review the deterministic findings in this document."

    async def decide(self, loan, message, role):
        if loan.get("status") != "awaiting_decision":
            return await self.reply("This loan is not awaiting a decision. Complete its document and risk review first, or inspect its existing decision.", loan)
        if message.casefold().startswith("decline"):
            reasons = loan["credit"].get("reasons") or ["The application does not meet the current lending criteria."]
            if "PAYOUT_NAME_MISMATCH" in loan["risk"]["codes"]:
                reasons = ["The payout account is not in your name. Please use a verified bank account in your own name when applying again.", *reasons]
            loan.update(status="declined", decision="decline", decision_by=role, decision_at=utcnow().isoformat(), rejection_reasons=reasons)
            self.save(loan)
            self.event("decision", "Credit", f"Officer declined {loan['id']}", loan, {"role": role, "reasons": reasons})
            await self.document("rejection_letter", loan, {"reasons": reasons})
            return await self.reply("Thank you for applying. We cannot proceed with this application. " + " ".join(reasons) + " Your rejection letter is attached.", loan)
        if loan["risk"]["band"] == "unverified":
            return await self.reply("Required sources are unavailable. Verify them and rerun the review before approving.", loan)
        reason = re.search(r"\b(?:because|reason)\s*[:=]?\s*(.+)$", message, re.I)
        if loan["risk"]["band"] == "high" and (not reason or len(reason.group(1).strip()) < 10):
            return await self.reply("Overriding high risk requires a typed reason. Use ‘approve L-1042 because …’ with a specific explanation. Payout safeguards still apply.", loan)
        amount_match = re.search(r"\bat\s+(\d+(?:\.\d{1,2})?)\b", message, re.I)
        amount = float(amount_match.group(1)) if amount_match else loan["credit"]["recommended_amount"]
        limit = min(loan["requested_amount"], loan["credit"]["eligible_amount"])
        if not 0 < amount <= limit:
            return await self.reply(f"The amount must be positive and no greater than the affordability limit of INR {limit:,.2f}. Use ‘approve {loan['id']} at AMOUNT’.", loan)
        loan["approved_amount"] = amount
        approved_terms = compute_affordability({**loan, "requested_amount": amount})
        loan["credit"].update({k: v for k, v in approved_terms.items() if k not in {"summary", "reasons", "recommendation"}})
        loan.update(status="approved", human_approved=True, approved_by_role=role,
                    approved_at=utcnow().isoformat(), override_reason=reason.group(1) if reason else None,
                    cooling_off_days=self.settings.cooling_off_days, grievance_contact="grievance@saakh.example",
                    kfs_accepted=False)
        self.save(loan)
        self.event("decision", "Credit", f"Officer approved INR {amount:,.2f}", loan,
                   {"role": role, "override_reason": loan["override_reason"]})
        self.handoff("Credit", "Disbursement", loan, "human approval recorded")
        await self.document("sanction_letter", loan)
        self.tool("Disbursement", "make_kfs", lambda: None, loan)
        kfs = await self.document("key_facts_statement", loan)
        loan.update(kfs_issued=True, kfs_document_id=kfs["document_id"], kfs_issued_at=utcnow().isoformat())
        self.save(loan)
        return await self.reply(f"Officer approval recorded for {loan['id']}: INR {amount:,.2f}. The sanction letter and KFS are ready. The borrower must read the KFS and type ‘accept KFS {loan['id']}’ before payout.", loan)

    async def disburse(self, loan, now):
        if loan.get("status") == "disbursed":
            return await self.reply("The simulated payout is already recorded; no duplicate payout was made.", loan)
        if loan.get("status") != "approved":
            return await self.reply("The loan needs officer approval before a simulated payout.", loan)
        result = self.tool("Compliance", "check_disbursement",
                           lambda: check_disbursement(loan, now=now, demo_skip_cooling=self.settings.demo_skip_cooling), loan)
        if not result["allowed"]:
            return await self.reply("Payout blocked: " + "; ".join(result["reasons"]), loan)
        loan.update(status="disbursed", simulated_utr="SIM-" + secrets.token_hex(6).upper(),
                    disbursed_at=now.isoformat(), paid_installments=0, simulated=True)
        self.tool("Disbursement", "simulate_payout", lambda: None, loan)
        loan["schedule"] = self.schedule(loan)
        loan["outstanding"] = round(sum(row.get("emi", row.get("payment", row.get("amount", 0))) for row in loan["schedule"]), 2)
        loan["outstanding_balance"] = loan["outstanding"]
        self.save(loan)
        self.event("payout", "Disbursement", "Simulated payout recorded to the verified borrower account.", loan,
                   {"simulated_utr": loan["simulated_utr"], "cooling_wait_skipped": self.settings.demo_skip_cooling})
        await self.document("disbursement_advice", loan)
        self.handoff("Disbursement", "Collections", loan, "simulated payout complete")
        await self.document("repayment_schedule", loan, {"schedule": loan["schedule"]}, "csv")
        await self.document("repayment_schedule", loan, {"schedule": loan["schedule"]})
        return await self.reply(f"Simulated payout complete for {loan['id']}. Reference {loan['simulated_utr']}. No money moved. Your repayment schedule is attached.", loan)

    def schedule(self, loan):
        if loan.get("schedule"):
            return loan["schedule"]
        return self.tool("Collections", "emi_schedule", lambda: emi_schedule(loan), loan)

    async def payment(self, loan):
        if loan.get("status") not in {"disbursed", "restructure_requested"}:
            return await self.reply("Payments can be recorded only against an active, disbursed loan.", loan)
        schedule = self.schedule(loan)
        count = loan.get("paid_installments", 0)
        if count >= len(schedule):
            return await self.reply("All scheduled installments are paid. Ops can issue the closure certificate.", loan)
        row = schedule[count]
        loan["paid_installments"] = count + 1
        loan["outstanding"] = round(sum(x.get("emi", x.get("payment", x.get("amount", 0))) for x in schedule[count + 1:]), 2)
        loan["outstanding_balance"] = loan["outstanding"]
        self.tool("Collections", "record_payment", lambda: None, loan)
        receipt = {**row, "payment_reference": "PAY-" + secrets.token_hex(5).upper(), "paid_at": utcnow().isoformat(), "installment": count + 1, "simulated": True}
        loan.setdefault("payments", []).append(receipt)
        self.save(loan)
        self.event("payment", "Collections", f"Recorded simulated EMI {count + 1}", loan, receipt)
        await self.document("payment_receipt", loan, receipt)
        return await self.reply(f"Simulated EMI {count + 1} recorded. Your receipt is attached.", loan)

    async def close(self, loan):
        if loan.get("status") == "closed":
            return await self.reply("This loan is already closed; the No Dues Certificate is in the documents panel.", loan)
        if loan.get("status") not in {"disbursed", "restructure_requested"}:
            return await self.reply("Only a disbursed loan can be closed.", loan)
        schedule = self.schedule(loan)
        count = loan.get("paid_installments", 0)
        # This explicit demo command settles synthetic future installments; it is not a real payment.
        for index, row in enumerate(schedule[count:], count + 1):
            self.event("payment", "Collections", f"Demo fast-forward: simulated EMI {index}", loan, row)
        loan.update(paid_installments=len(schedule), outstanding=0, outstanding_balance=0, status="closed", closed_at=utcnow().isoformat())
        self.save(loan)
        await self.document("no_dues_certificate", loan, {"outstanding": 0})
        return await self.reply("Demo fast-forward completed: all remaining installments were settled with simulated payments. The loan is closed and the No Dues Certificate is attached.", loan)

    async def hardship(self, loan, message):
        if loan.get("status") not in {"disbursed", "restructure_requested"}:
            return await self.reply("I’m sorry to hear about your situation. A restructure request needs an active disbursed loan. The officer can discuss your application instead.", loan)
        self.handoff("Front Desk", "Collections", loan, "borrower reported hardship")
        draft = await self.model.structured(AGENTS["Collections"], {"message": message}, human_message=True)
        summary = self.checked_summary(draft.summary)
        self.tool("Collections", "request_restructure", lambda: None, loan)
        loan.update(status="restructure_requested", hardship_summary=summary)
        self.save(loan)
        await self.document("restructure_request", loan, {"summary": summary, "hardship": message})
        await self.mention(f"Please review the hardship request for {loan['id']}. No revised terms have been promised.", loan)
        return await self.reply("I’m sorry you’re going through this. I have prepared a restructure request for the credit officer. They will review possible options; no revised terms are approved yet.", loan, "Collections")

    async def exit_loan(self, loan, now):
        if loan.get("status") == "approved":
            loan.update(status="withdrawn", withdrawn_at=now.isoformat(), exit_penalty=0)
            self.save(loan)
            self.event("decision", "Disbursement", "Borrower withdrew before payout; no penalty.", loan)
            return await self.reply("Your loan was withdrawn before payout. No money moved and no penalty is payable.", loan)
        if loan.get("status") != "disbursed":
            return await self.reply("Cooling-off exit is available for an approved or newly disbursed loan. The officer can help with other closure requests.", loan)
        started = datetime.fromisoformat(loan["disbursed_at"])
        if now > started + timedelta(days=loan.get("cooling_off_days", self.settings.cooling_off_days)):
            return await self.reply("The cooling-off window has ended. I can ask the credit officer to review an early repayment request.", loan)
        loan.update(status="closed", outstanding=0, outstanding_balance=0, closed_at=now.isoformat(), exit_penalty=0,
                    exit_reason="cooling_off", simulated_refund=loan["approved_amount"])
        self.save(loan)
        self.event("payment", "Collections", "Simulated principal return during cooling-off; zero penalty. No money moved.", loan,
                   {"simulated_principal_return": loan["approved_amount"], "penalty": 0})
        await self.document("no_dues_certificate", loan, {"outstanding": 0, "summary": "Cooling-off exit completed using a simulated principal return. No penalty was charged; no money moved."})
        return await self.reply("Cooling-off exit completed in the simulation. The principal return is simulated, the penalty is zero, and your No Dues Certificate is attached. No money moved.", loan)

    async def grievance(self, loan, message, role):
        reference = "GRV-" + secrets.token_hex(4).upper()
        item = {"reference": reference, "message": message, "received_at": utcnow().isoformat(), "role": role,
                "resolution_due": (utcnow() + timedelta(days=30)).date().isoformat(), "loan_id": loan["id"] if loan else "GENERAL"}
        self.tool("Front Desk", "log_grievance", lambda: self.store.put("grievance", reference, item), loan)
        text = grievance_acknowledgment(reference)
        await self.document("grievance_acknowledgment", loan or {"id": "GENERAL"}, {**item, "summary": text})
        return await self.reply(text, loan)

    async def reminder(self, loan, now):
        if loan.get("status") not in {"disbursed", "restructure_requested"}:
            return await self.reply("Reminders are available only for active disbursed loans.", loan)
        local = now.astimezone(IST)
        if not 8 <= local.hour < 19:
            due = local.replace(hour=8, minute=0, second=0, microsecond=0)
            if local.hour >= 19:
                due += timedelta(days=1)
            self.store.put("reminder", loan["id"], {"loan_id": loan["id"], "due_at": due.isoformat(), "conversation_id": self.conversation, "status": "scheduled"})
            self.event("status", "Collections", "Reminder scheduled for 08:00 IST.", loan)
            return await self.reply("Borrower contact is allowed only from 08:00 to 19:00 IST. I have scheduled the reminder for 08:00 IST.", loan)
        message = f"A friendly reminder for {loan['id']}: please check your repayment schedule for the next EMI. If paying is difficult, we can ask the officer to review your options."
        result = self.tool("Compliance", "check_message", lambda: check_message(message, now=now), loan)
        if not result["allowed"]:
            return await self.reply("The reminder is blocked by the contact policy.", loan)
        self.tool("Collections", "draft_reminder", lambda: message, loan)
        return await self.reply(message, loan, "Collections")

    async def deliver_due_reminders(self, now=None):
        now = now or utcnow()
        for item in self.store.all("reminder"):
            if item["status"] != "scheduled" or datetime.fromisoformat(item["due_at"]) > now:
                continue
            async with self.lock:
                self.conversation = item["conversation_id"]
                loan = self.store.get("loan", item["loan_id"])
                if loan and loan.get("status") in {"disbursed", "restructure_requested"}:
                    if not 8 <= now.astimezone(IST).hour < 19:
                        continue
                    await self.reminder(loan, now)
                item["status"] = "delivered" if loan and loan.get("status") != "closed" else "cancelled"
                self.store.put("reminder", item["loan_id"], item)

    async def export_queue(self):
        rows = [{"loan_id": x["id"], "name": x["name"], "status": x.get("status", "new"),
                 "risk_band": x.get("risk", {}).get("band", "unverified"),
                 "recommendation": x.get("credit", {}).get("recommendation", "pending"),
                 "requested_amount": x.get("requested_amount"),
                 "recommended_amount": x.get("credit", {}).get("recommended_amount")} for x in self.store.all("loan")]
        for fmt in ("csv", "xlsx"):
            await self.document("review_queue", {"id": "PORTFOLIO"}, {"rows": rows}, fmt)

    async def dashboard(self):
        await self.document("portfolio_dashboard", {"id": "PORTFOLIO"}, {"loans": self.store.all("loan")}, "png")

    async def application(self, message, role, loan=None, supplied=None, *, new_application=False):
        pending = None if new_application else self.store.get("conversation", self.conversation)
        loan = loan or (self.store.get("loan", pending["loan_id"]) if pending else None)
        if loan and loan.get("status") not in {"new", "pending", "received", "awaiting_documents"}:
            return await self.reply("This application has already been reviewed. Ask the officer to open a new application for changed terms.", loan)
        if not loan:
            next_id = max(int(x["id"].split("-")[1]) for x in self.store.all("loan")) + 1
            loan = {"id": f"L-{next_id}", "name": "", "status": "new", "documents": [],
                    "annual_rate": .18, "fees": 1000, "consent": False, "account_verified": False,
                    "created_from_chat": True, "applied_at": utcnow().isoformat()}
        if supplied is None:
            extracted = await self.model.structured(AGENTS["Intake"], {"message": message})
            supplied = extracted.model_dump(exclude_none=True)
            # Consent is explicit data, never a model inference.
            supplied.pop("consent", None)
            if re.search(r"\bi consent\b", message, re.I):
                supplied["consent"] = True
        allowed = set(AGENTS["Intake"].output_schema.model_fields)
        loan.update({k: v for k, v in supplied.items() if k in allowed})
        if loan.get("consent"):
            loan["consented_at"] = utcnow().isoformat()
            loan["consented_by_role"] = role
        self.save(loan)
        self.store.put("conversation", self.conversation, {"loan_id": loan["id"]})
        missing = self.tool("Intake", "validate_application", lambda: validate_application(loan), loan)
        if not loan.get("acknowledged"):
            await self.document("application_acknowledgment", loan)
            loan["acknowledged"] = True
        loan["status"] = "awaiting_documents" if missing else "new"
        self.save(loan)
        return await self.reply(f"Application {loan['id']} saved. " + (missing[0] if missing else "The fields are complete. Ask ops to verify the documents and request review."), loan, "Intake")
