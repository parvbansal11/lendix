"""Auditable lending state machine. Model output never grants authority."""
import asyncio
import copy
import re
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .agents import AGENTS
from .compliance import check_disbursement, check_document, check_message, grievance_acknowledgment
from .config import ROOT
from .documents import DocumentService
from .model import ModelUnavailable
from .policy import apply_policy, load_policy
from .risk_rules import run_risk_checks
from .store import utcnow
from .tools import bureau_lookup, compute_affordability, emi_schedule, validate_application

IST = ZoneInfo("Asia/Kolkata")
WATERMARK = "SPECIMEN · SYNTHETIC DEMO DATA · NOT A LEGAL DOCUMENT"
HELP = "Lendix handles loan applications, document checks, policy decisions, risk-head escalations, repayments and complaints. Try ‘review L-1042’, ‘list loans’, or tell me which lending task you need."


class Orchestrator:
    def __init__(self, store, settings, model, adapter=None, policy=None):
        self.store, self.settings, self.model, self.adapter = store, settings, model, adapter
        self.policy = policy if policy is not None else load_policy(settings.policy_path)
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
            await self.adapter.mention("risk_head", text)
        else:
            self.event("mention", "Reporter", f"@risk_head {text}", loan)

    async def progress(self, text):
        self.tool("Reporter", "post_update", lambda: None)
        if self.adapter:
            await self.adapter.post_update(text)
        else:
            self.event("progress", "Reporter", text)

    async def document(self, kind, loan, data=None, format="pdf", operation_id=None):
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
        if operation_id:
            artifact.update(operation_id=operation_id, operation_document=f"{kind}:{format}")
        saved = self.store.register_file(artifact, self.settings.public_base_url)
        self.event("document", "Reporter", f"Issued {kind}: {artifact['document_id']}", loan,
                   {"document_id": artifact["document_id"], "kind": kind})
        if self.adapter:
            await self.adapter.send_file(self.conversation, saved["url"], saved["filename"])
        else:
            self.event("file", "Reporter", saved["filename"], loan,
                       {k: v for k, v in saved.items() if k not in {"path", "token"}})
        return saved

    async def operation_document(self, loan, operation_id, kind, data=None, format="pdf"):
        """Resume an operation's artifacts without performing its transaction again."""
        key = f"{kind}:{format}"
        documents = loan.setdefault("operation_documents", {}).setdefault(operation_id, {})
        # Registration precedes delivery. A failure during delivery must not render
        # another document or repeat the operation on the next command.
        existing = next((item for item in self.store.all("file")
                         if item.get("loan_id") == loan["id"] and item.get("operation_id") == operation_id
                         and item.get("operation_document") == key), None)
        if existing:
            if documents.get(key) != existing["document_id"]:
                documents[key] = existing["document_id"]
                self.save(loan)
            return existing
        artifact = await self.document(kind, loan, data, format, operation_id=operation_id)
        documents[key] = artifact["document_id"]
        self.save(loan)
        return artifact

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
        if role not in {"risk_head", "ops", "borrower"}:
            return await self.reply("Please choose a valid demo role.")
        text = message.casefold()
        match = re.search(r"\bL-\d+\b", message, re.I)
        loan = self.store.get("loan", match.group().upper()) if match else None
        self.event("status", "Front Desk", f"Received a request as {role}.", loan)
        if match and not loan:
            return await self.reply("I cannot find that loan. Use ‘list loans’ to see the synthetic applications.")
        # Safety gates execute before model routing and cannot be bypassed by instructions in chat.
        if any(word in text for word in ("social media", "social profile", "social network")):
            return await self.reply("I only use consented application data. I cannot look up someone’s social media.", loan)
        if any(word in text for word in ("employer", "family", "friends", "third party", "third-party")) and any(word in text for word in ("contact", "message", "call", "tell", "send")):
            return await self.reply("I cannot contact an employer, family member, friend or other third party about a borrower’s debt. Fair practices require contacting the borrower directly and respectfully.", loan)
        if "default" in text and any(word in text for word in ("will", "predict", "person")):
            return await self.reply("I can explain recorded risk reasons and affordability; I cannot predict whether a person will default. Which loan would you like to review?", loan)
        if any(word in text for word in ("threaten", "shame", "abuse", "harass")):
            return await self.reply("I cannot threaten, shame or harass a borrower. I can prepare a polite reminder or a hardship request.", loan)
        if re.match(r"^(approve|decline|override)\b", text):
            if role != "risk_head":
                return await self.reply("Only the risk head can decide escalated loans or request an override, with a typed reason. I can prepare the case for review.", loan)
            if not loan:
                return await self.reply("Which escalated loan should the risk head decide? Include an ID such as L-1045.")
            if text.startswith("override"):
                return await self.request_override(loan, message, role)
            return await self.decide(loan, message, role)
        if "audit" in text:
            if role != "risk_head":
                return await self.reply("Only the risk head can export the audit log.")
            await self.document("audit_log", {"id": "PORTFOLIO"}, {"events": self.store.all_events()}, "json")
            return await self.reply("The audit log is ready. It records decisions, reasons, handoffs and document IDs.")
        if any(word in text for word in ("complaint", "grievance", "complain")):
            return await self.grievance(loan, message, role)
        if "review" in text and any(word in text for word in ("today", "applications", "batch")):
            if role == "borrower":
                return await self.reply("Portfolio review is for the ops team and risk head. I can help with a single application.")
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
                    await self.progress(f"Reviewed {index}/{len(rows)} applications; policy decisions and exceptions are being recorded.")
                await asyncio.sleep(0)
            await self.export_queue()
            await self.dashboard()
            processed = [self.store.get("loan", item["id"]) for item in rows]
            counts = self.decision_counts(processed)
            return await self.reply(f"Batch finished: {len(rows)} applications processed. {counts['auto_approved']} auto-approved, {counts['auto_declined']} auto-declined, {counts['escalated']} escalated. Download the review queue and dashboard.")
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
                return await self.reply("I’m sorry things are difficult. Which loan should I include in a request for the risk head?")
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
            if loan.get("status") not in {"new", "pending", "received", "awaiting_documents"}:
                return await self.reply("Documents cannot be changed after a decision. Ask the risk head to open a new application.", loan)
            loan["documents"] = sorted(set(loan.get("documents", [])) | {"bank_statement"})
            self.event("tool", "Intake", "request_document: synthetic bank statement received", loan)
            self.save(loan)
            return await self.review(loan)
        if loan and ("accept" in text and "kfs" in text):
            if role != "borrower":
                return await self.reply("KFS acceptance must be given by the borrower. Switch to the synthetic borrower role for this demonstration.", loan)
            if loan.get("status") != "approved" or not loan.get("kfs_issued"):
                return await self.reply("The loan needs policy or risk-head approval and an issued KFS before you can accept it.", loan)
            if loan.get("kfs_accepted"):
                await self.issue_agreement(loan)
                return await self.reply("Your KFS acceptance is already recorded.", loan)
            loan.update(kfs_accepted=True, kfs_accepted_at=now.isoformat(), kfs_accepted_by="borrower",
                        acceptance_operation_id="ACCEPT-" + secrets.token_hex(6).upper())
            self.save(loan)
            self.event("decision", "Disbursement", "Borrower accepted the issued KFS.", loan)
            await self.issue_agreement(loan)
            return await self.reply("Your KFS acceptance is recorded. You can exit without penalty during the cooling-off period. Ops can request the simulated payout after the demo waiting gate.", loan)
        if loan and "disburse" in text:
            if role == "borrower":
                return await self.reply("Only ops or the risk head can initiate the simulated payout.", loan)
            return await self.disburse(loan, now)
        if loan and re.match(r"^(exit|cancel)\b", text):
            return await self.exit_loan(loan, now)
        if loan and re.match(r"^(pay|record payment)\b", text):
            return await self.payment(loan)
        if loan and (text.startswith("close") or "fast-forward" in text):
            if role == "borrower":
                return await self.reply("Only ops or the risk head can fast-forward simulated payments to closure.", loan)
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
        if loan.get("status") in {"approved", "declined", "escalated"} and loan.get("decision_operation_id"):
            if not loan.get("decision_reported"):
                return await self.complete_decision(loan)
            return await self.reply(f"{loan['id']} is already {loan['status']}. Its recorded decision and reports are available in the documents panel.", loan)
        if loan.get("status") in {"approved", "declined", "disbursed", "closed", "restructure_requested", "withdrawn"}:
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
            notice = await self.document("missing_documents_request", loan, {"missing_documents": [missing[0]]}, "chat")
            return await self.reply(f"{loan['id']}: {missing[0]}\nDocument {notice['document_id']} · {notice['created_at'][:10]}\n{WATERMARK}", loan, "Intake")
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
        loan.update(credit=credit, bureau=bureau)
        self.handoff("Credit", "Underwriter", loan, "affordability computed; apply the written credit policy")
        evaluation = self.tool("Underwriter", "apply_policy", lambda: apply_policy(loan, self.policy), loan)
        loan["policy_evaluation"] = copy.deepcopy(evaluation)
        self.record_underwriting(loan, evaluation, "policy")
        pending = self.store.get("conversation", self.conversation)
        if pending and pending.get("loan_id") == loan["id"]:
            self.store.delete("conversation", self.conversation)
        return await self.complete_decision(loan)

    def checked_summary(self, text):
        result = self.tool("Compliance", "check_message", lambda: check_message(text))
        return text if result["allowed"] else "Please review the deterministic findings in this document."

    def checked_decision_summary(self, text, decision):
        summary = self.checked_summary(text)
        conflicting = {
            "decline": r"\b(?:approv\w*|sanctioned)\b",
            "escalate": r"\b(?:approved|declined|sanctioned)\b",
            "approve": r"\b(?:declined|rejected|rejection)\b",
            "approve_lower": r"\b(?:declined|rejected|rejection)\b",
        }
        if re.search(conflicting[decision["decision"]], summary, re.I):
            self.event("status", "Compliance", "Replaced explanatory wording that conflicted with the recorded decision.")
            return f"The recorded outcome is {decision['decision']}. Policy rule: {decision['rule']}. The recorded inputs and reasons explain this result."
        return summary

    def record_underwriting(self, loan, evaluation, source, reason=None):
        """Persist only a Python policy result or an authorized escalation decision."""
        decision = copy.deepcopy(evaluation)
        if reason:
            decision["reasons"] = [reason, *decision["reasons"]]
        loan["underwriting"] = decision
        loan["decision"] = decision["decision"]
        loan["decision_by"] = "risk_head" if source == "risk_head" else "underwriter"
        loan["decision_at"] = utcnow().isoformat()
        loan["decision_operation_id"] = "DEC-" + secrets.token_hex(6).upper()
        loan["decision_reported"] = False
        loan["approval_source"] = source
        loan["override_reason"] = reason
        loan.pop("decision_note_id", None)
        for key in ("sanction_document_id", "kfs_document_id", "kfs_issued_at", "kfs_accepted_at", "acceptance_operation_id"):
            loan.pop(key, None)
        loan.update(kfs_issued=False, kfs_accepted=False, human_approved=False)
        if decision["decision"] in {"approve", "approve_lower"}:
            amount = decision["approved_amount"]
            terms = compute_affordability({**loan, "requested_amount": amount})
            loan["credit"].update(terms)
            loan.update(status="approved", approved_amount=amount, human_approved=source == "risk_head",
                        approved_by_role="risk_head" if source == "risk_head" else "underwriter",
                        approved_at=loan["decision_at"], cooling_off_days=self.settings.cooling_off_days,
                        grievance_contact="grievance@lendix.example")
        elif decision["decision"] == "decline":
            loan["status"] = "declined"
            reasons = list(decision["reasons"])
            if "PAYOUT_NAME_MISMATCH" in loan.get("risk", {}).get("codes", []):
                reasons.insert(0, "The payout account is not in your name. Please use a verified bank account in your own name when applying again.")
            loan["rejection_reasons"] = reasons
            loan.pop("approved_amount", None)
        else:
            loan.update(status="escalated", escalated_for_decision=True)
            loan.pop("approved_amount", None)
        audit = {**copy.deepcopy(decision), "role": loan["decision_by"], "decision_source": source,
                 "override_reason": reason, "operation_id": loan["decision_operation_id"], "at": loan["decision_at"]}
        loan.setdefault("decision_history", []).append(copy.deepcopy(audit))
        self.save(loan)
        self.event("decision", "Underwriter", f"{loan['decision_by']}: {decision['decision']} · Policy rule: {decision['rule']}", loan, audit)
        if decision["decision"] == "escalate":
            self.event("escalation", "Underwriter", f"Escalated: {decision['rule']}", loan, audit)

    async def complete_decision(self, loan):
        decision = loan["underwriting"]
        if not decision.get("summary"):
            note = await self.model.structured(AGENTS["Underwriter"],
                {"decision": decision, "approval_source": loan["approval_source"], "override_reason": loan.get("override_reason")}, deep=True)
            # Numeric decisions and deterministic reasons never come back from the model.
            decision["summary"] = self.checked_decision_summary(note.summary, decision)
            self.tool("Underwriter", "draft_decision_note", lambda: decision["summary"], loan)
            self.save(loan)
        note_file = await self.operation_document(loan, loan["decision_operation_id"], "decision_note",
                                                   {"summary": decision["summary"], "reasons": decision["reasons"]})
        loan["decision_note_id"] = note_file["document_id"]
        self.save(loan)
        chart = next((item for item in reversed(self.store.all("file"))
                      if item["loan_id"] == loan["id"] and item["mime_type"] == "image/png"), None)
        links = f"[Decision note]({note_file['url']})"
        if chart:
            links += f" · [Risk chart]({chart['url']})"
        if decision["decision"] == "escalate":
            self.tool("Underwriter", "escalate", lambda: None, loan)
            self.handoff("Underwriter", "Reporter", loan, "policy exception needs the risk head")
            await self.mention(f"{loan['id']} is escalated. Policy rule: {decision['rule']}. {links} Please decide with a typed reason.", loan)
            await self.walk_to_risk_head(loan)
            reply = f"{loan['id']} · Risk {loan.get('risk', {}).get('band', 'unverified')} · Escalated. Policy rule: {decision['rule']}. The risk head must review this exception and record a reason."
        else:
            self.handoff("Underwriter", "Compliance", loan, f"{loan['approval_source']} decision recorded")
            if loan["status"] == "approved":
                self.handoff("Compliance", "Disbursement", loan, "approval authority recorded")
                await self.issue_approval_documents(loan)
                reply = f"Approval recorded for {loan['id']}: INR {loan['approved_amount']:,.2f} by {loan['approval_source']}. Policy rule: {decision['rule']}. The sanction letter and KFS are ready; the borrower must accept the KFS before simulated payout."
            else:
                await self.issue_rejection(loan)
                reply = "Thank you for applying. We cannot proceed with this application. " + " ".join(loan["rejection_reasons"]) + " Your rejection letter is attached."
            await self.progress(f"FYI @risk_team: {loan['id']} {decision['decision']} by {loan['approval_source']}. Policy v{decision['policy_version']} · {decision['rule']}. {links}")
        loan["decision_reported"] = True
        self.save(loan)
        return await self.reply(reply, loan, "Underwriter")

    async def walk_to_risk_head(self, loan):
        if self.adapter:
            await self.adapter.office_action("walk_to", user="risk_head", loan_id=loan["id"])
        else:
            self.event("office_action", "Reporter", "Local demonstration: walk_to risk_head", loan,
                       {"name": "walk_to", "user": "risk_head"})

    @staticmethod
    def typed_reason(message):
        reason = re.search(r"\b(?:because|reason)\s*[:=]?\s*(.+)$", message, re.I)
        return reason.group(1).strip() if reason and len(reason.group(1).strip()) >= 10 else None

    async def request_override(self, loan, message, role):
        if role != "risk_head":
            return await self.reply("Only the risk head can request an override with a typed reason.", loan)
        reason = self.typed_reason(message)
        if not reason:
            return await self.reply("An override needs a typed reason. Use ‘override L-1043 because …’ with a specific explanation.", loan)
        if not check_message(reason, borrower_facing=False)["allowed"]:
            return await self.reply("Please give a factual, respectful reason that follows the message policy.", loan)
        if loan.get("status") not in {"approved", "declined"} or loan.get("approval_source") != "policy":
            return await self.reply("Only an automatic decision before disbursement can be requested for override. An existing escalation needs an explicit approve or decline command with a reason.", loan)
        result = self.tool("Underwriter", "apply_policy", lambda: apply_policy(loan, self.policy, override_request=True), loan)
        loan["override_requested_by"] = role
        loan["override_requested_reason"] = reason
        loan["policy_evaluation"] = copy.deepcopy(result)
        self.record_underwriting(loan, result, "policy", reason)
        return await self.complete_decision(loan)

    async def decide(self, loan, message, role):
        if role != "risk_head":
            return await self.reply("Only the risk head can decide an escalated loan, with a typed reason.", loan)
        if loan.get("status") != "escalated" or not loan.get("escalated_for_decision"):
            return await self.reply("The risk head can only approve or decline escalated loans. For an automatic decision, first request an override with a typed reason.", loan)
        reason = self.typed_reason(message)
        if not reason:
            return await self.reply("Every risk-head decision needs a typed reason. Use ‘approve L-1045 at 200000 because …’ or ‘decline L-1045 because …’ with a specific explanation.", loan)
        if not check_message(reason, borrower_facing=False)["allowed"]:
            return await self.reply("Please give a factual, respectful reason that follows the message policy.", loan)
        command = re.split(r"\b(?:because|reason)\b", message, maxsplit=1, flags=re.I)[0].strip()
        parsed = re.fullmatch(r"(approve|decline)\s+(L-\d+)(?:\s+at\s+(\d+(?:\.\d{1,2})?))?", command, re.I)
        if not parsed or (parsed.group(1).lower() == "decline" and parsed.group(3)):
            return await self.reply("Use approve or decline, the loan ID, an optional positive approval amount, and a typed reason.", loan)
        decision = copy.deepcopy(loan["policy_evaluation"])
        decision.pop("summary", None)
        decision["decision"] = parsed.group(1).lower()
        decision["approved_amount"] = None
        if decision["decision"] == "approve":
            if loan.get("risk", {}).get("band") == "unverified":
                return await self.reply("Required verification sources are unavailable. Resolve them before approval; a typed reason cannot replace verification.", loan)
            amount = float(parsed.group(3)) if parsed.group(3) else min(loan["requested_amount"], loan["credit"]["eligible_amount"])
            limit = min(loan["requested_amount"], loan["credit"]["eligible_amount"])
            if not 0 < amount <= limit or amount <= loan["credit"]["fees"]:
                return await self.reply(f"Approval must be positive, exceed the disclosed fees and not exceed the affordability limit of INR {limit:,.2f}.", loan)
            decision.update(decision="approve_lower" if amount < loan["requested_amount"] else "approve", approved_amount=amount)
        self.record_underwriting(loan, decision, "risk_head", reason)
        return await self.complete_decision(loan)

    async def issue_approval_documents(self, loan):
        operation_id = loan["decision_operation_id"]
        sanction = await self.operation_document(loan, operation_id, "sanction_letter")
        loan["sanction_document_id"] = sanction["document_id"]
        if not loan.get("kfs_issued"):
            self.tool("Disbursement", "make_kfs", lambda: None, loan)
            kfs = await self.operation_document(loan, operation_id, "key_facts_statement")
            loan.update(kfs_issued=True, kfs_document_id=kfs["document_id"], kfs_issued_at=kfs["created_at"])
        self.save(loan)

    async def issue_rejection(self, loan):
        if not loan.get("decision_operation_id"):
            loan["decision_operation_id"] = "DEC-" + secrets.token_hex(6).upper()
            self.save(loan)
        await self.operation_document(loan, loan["decision_operation_id"], "rejection_letter",
                                      {"reasons": loan["rejection_reasons"]})

    async def issue_agreement(self, loan):
        if not loan.get("acceptance_operation_id"):
            loan["acceptance_operation_id"] = "ACCEPT-" + secrets.token_hex(6).upper()
            self.save(loan)
        await self.operation_document(loan, loan["acceptance_operation_id"], "loan_agreement_summary")

    async def issue_payout_documents(self, loan):
        operation_id = loan["simulated_utr"]
        await self.operation_document(loan, operation_id, "disbursement_advice")
        await self.operation_document(loan, operation_id, "repayment_schedule", {"schedule": loan["schedule"]}, "csv")
        await self.operation_document(loan, operation_id, "repayment_schedule", {"schedule": loan["schedule"]})

    async def issue_closure_document(self, loan):
        if not loan.get("closure_operation_id"):
            loan["closure_operation_id"] = "CLOSE-" + secrets.token_hex(6).upper()
            self.save(loan)
        data = {"outstanding": 0}
        if loan.get("exit_reason") == "cooling_off":
            data["summary"] = "Cooling-off exit completed using a simulated principal return. No penalty was charged; no money moved."
        await self.operation_document(loan, loan["closure_operation_id"], "no_dues_certificate", data)

    async def disburse(self, loan, now):
        if loan.get("simulated_utr") and loan.get("status") in {"disbursed", "restructure_requested", "closed"}:
            await self.issue_payout_documents(loan)
            return await self.reply("The simulated payout is already recorded; no duplicate payout was made.", loan)
        if loan.get("status") == "disbursed":
            return await self.reply("The seeded simulated payout is already recorded; no duplicate payout was made.", loan)
        if loan.get("status") != "approved":
            return await self.reply("The loan needs policy or risk-head approval before a simulated payout.", loan)
        result = self.tool("Compliance", "check_disbursement",
                           lambda: check_disbursement(loan, now=now, demo_skip_cooling=self.settings.demo_skip_cooling), loan)
        if not result["allowed"]:
            return await self.reply("Payout blocked: " + "; ".join(result["reasons"]), loan)
        await self.issue_agreement(loan)
        loan.update(status="disbursed", simulated_utr="SIM-" + secrets.token_hex(6).upper(),
                    disbursed_at=now.isoformat(), paid_installments=0, simulated=True)
        self.tool("Disbursement", "simulate_payout", lambda: None, loan)
        loan["schedule"] = self.schedule(loan)
        loan["outstanding"] = round(sum(row.get("emi", row.get("payment", row.get("amount", 0))) for row in loan["schedule"]), 2)
        loan["outstanding_balance"] = loan["outstanding"]
        self.save(loan)
        self.event("payout", "Disbursement", "Simulated payout recorded to the verified borrower account.", loan,
                   {"simulated_utr": loan["simulated_utr"], "cooling_wait_skipped": self.settings.demo_skip_cooling})
        self.handoff("Disbursement", "Collections", loan, "simulated payout complete")
        await self.issue_payout_documents(loan)
        return await self.reply(f"Simulated payout complete for {loan['id']}. Reference {loan['simulated_utr']}. No money moved. Your repayment schedule is attached.", loan)

    def schedule(self, loan):
        if loan.get("schedule"):
            return loan["schedule"]
        return self.tool("Collections", "emi_schedule", lambda: emi_schedule(loan), loan)

    async def payment(self, loan):
        pending_receipts = [item for item in loan.get("payments", []) if not item.get("document_id")]
        if pending_receipts:
            for receipt in pending_receipts:
                await self.issue_payment_receipt(loan, receipt)
            return await self.reply("The receipt for your recorded simulated payment is now ready. No additional EMI was recorded; send a new payment command when you intend to pay the next installment.", loan)
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
        await self.issue_payment_receipt(loan, receipt)
        return await self.reply(f"Simulated EMI {count + 1} recorded. Your receipt is attached.", loan)

    async def issue_payment_receipt(self, loan, receipt):
        artifact = await self.operation_document(loan, receipt["payment_reference"], "payment_receipt", receipt)
        receipt["document_id"] = artifact["document_id"]
        self.save(loan)

    async def close(self, loan):
        if loan.get("status") == "closed":
            await self.issue_closure_document(loan)
            return await self.reply("This loan is already closed; the No Dues Certificate is in the documents panel.", loan)
        if loan.get("status") not in {"disbursed", "restructure_requested"}:
            return await self.reply("Only a disbursed loan can be closed.", loan)
        schedule = self.schedule(loan)
        count = loan.get("paid_installments", 0)
        loan["closure_operation_id"] = "CLOSE-" + secrets.token_hex(6).upper()
        # This explicit demo command settles synthetic future installments; it is not a real payment.
        for index, row in enumerate(schedule[count:], count + 1):
            self.event("payment", "Collections", f"Demo fast-forward: simulated EMI {index}", loan,
                       {**row, "operation_id": loan["closure_operation_id"]})
        loan.update(paid_installments=len(schedule), outstanding=0, outstanding_balance=0, status="closed", closed_at=utcnow().isoformat())
        self.save(loan)
        await self.issue_closure_document(loan)
        return await self.reply("Demo fast-forward completed: all remaining installments were settled with simulated payments. The loan is closed and the No Dues Certificate is attached.", loan)

    async def hardship(self, loan, message):
        if loan.get("status") not in {"disbursed", "restructure_requested"}:
            return await self.reply("I’m sorry to hear about your situation. A restructure request needs an active disbursed loan. The risk head can discuss your application instead.", loan)
        self.handoff("Front Desk", "Collections", loan, "borrower reported hardship")
        draft = await self.model.structured(AGENTS["Collections"], {"message": message}, human_message=True)
        summary = self.checked_summary(draft.summary)
        self.tool("Collections", "request_restructure", lambda: None, loan)
        self.handoff("Collections", "Underwriter", loan, "restructure request requires policy escalation")
        evaluation = self.tool("Underwriter", "apply_policy", lambda: apply_policy(loan, self.policy, restructure_request=True), loan)
        operation_id = "RESTRUCTURE-" + secrets.token_hex(6).upper()
        loan.update(status="restructure_requested", hardship_summary=summary, servicing_escalation=evaluation,
                    servicing_escalation_id=operation_id)
        self.save(loan)
        audit = {**copy.deepcopy(evaluation), "role": "underwriter", "decision_source": "policy",
                 "operation_id": operation_id, "scope": "restructure"}
        self.tool("Underwriter", "escalate", lambda: None, loan)
        self.event("decision", "Underwriter", "Policy rule: restructure request", loan, audit)
        self.event("escalation", "Underwriter", "Restructure request escalated to the risk head.", loan, audit)
        note = await self.model.structured(AGENTS["Underwriter"], {"decision": evaluation}, deep=True)
        note_summary = self.checked_decision_summary(note.summary, evaluation)
        self.tool("Underwriter", "draft_decision_note", lambda: note_summary, loan)
        await self.operation_document(loan, operation_id, "decision_note",
                                      {"underwriting": evaluation, "summary": note_summary, "reasons": evaluation["reasons"]})
        await self.document("restructure_request", loan, {"summary": summary, "hardship": message})
        await self.mention(f"Please review the hardship request for {loan['id']}. No revised terms have been promised.", loan)
        await self.walk_to_risk_head(loan)
        return await self.reply("I’m sorry you’re going through this. I have prepared a restructure request for the risk head. They will review possible options; no revised terms are approved yet.", loan, "Collections")

    async def exit_loan(self, loan, now):
        if loan.get("status") == "closed" and loan.get("exit_reason") == "cooling_off":
            await self.issue_closure_document(loan)
            return await self.reply("Your cooling-off exit is already recorded. The No Dues Certificate is ready; no additional simulated principal return was recorded.", loan)
        if loan.get("status") == "withdrawn":
            return await self.reply("Your loan was already withdrawn before payout. No money moved and no penalty is payable.", loan)
        if loan.get("status") == "approved":
            loan.update(status="withdrawn", withdrawn_at=now.isoformat(), exit_penalty=0)
            self.save(loan)
            self.event("decision", "Disbursement", "Borrower withdrew before payout; no penalty.", loan)
            return await self.reply("Your loan was withdrawn before payout. No money moved and no penalty is payable.", loan)
        if loan.get("status") != "disbursed":
            return await self.reply("Cooling-off exit is available for an approved or newly disbursed loan. The risk head can help with other closure requests.", loan)
        started = datetime.fromisoformat(loan["disbursed_at"])
        if now > started + timedelta(days=loan.get("cooling_off_days", self.settings.cooling_off_days)):
            return await self.reply("The cooling-off window has ended. I can ask the risk head to review an early repayment request.", loan)
        loan.update(status="closed", outstanding=0, outstanding_balance=0, closed_at=now.isoformat(), exit_penalty=0,
                    exit_reason="cooling_off", simulated_refund=loan["approved_amount"],
                    closure_operation_id="CLOSE-" + secrets.token_hex(6).upper())
        self.save(loan)
        self.event("payment", "Collections", "Simulated principal return during cooling-off; zero penalty. No money moved.", loan,
                   {"simulated_principal_return": loan["approved_amount"], "penalty": 0, "operation_id": loan["closure_operation_id"]})
        await self.issue_closure_document(loan)
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
        message = f"A friendly reminder for {loan['id']}: please check your repayment schedule for the next EMI. If paying is difficult, we can ask the risk head to review your options."
        result = self.tool("Compliance", "check_message", lambda: check_message(message, now=now), loan)
        if not result["allowed"]:
            return await self.reply("The reminder is blocked by the contact policy.", loan)
        self.tool("Collections", "draft_reminder", lambda: message, loan)
        schedule = self.schedule(loan)
        due = schedule[min(loan.get("paid_installments", 0), len(schedule) - 1)]
        notice = await self.document("payment_reminder", loan, {"amount_due": due["emi"], "due_date": due["due_date"], "summary": message}, "chat")
        message += f"\nDocument {notice['document_id']} · {notice['created_at'][:10]}\n{WATERMARK}"
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
                 "decision": x.get("underwriting", {}).get("decision", "pending"),
                 "policy_rule": x.get("underwriting", {}).get("rule", "Not evaluated"),
                 "policy_version": x.get("underwriting", {}).get("policy_version"),
                 "approval_source": x.get("approval_source", "Not decided"),
                 "approved_amount": x.get("approved_amount"),
                 "requested_amount": x.get("requested_amount"),
                 "recommended_amount": x.get("credit", {}).get("recommended_amount")} for x in self.store.all("loan")]
        for fmt in ("csv", "xlsx"):
            await self.document("review_queue", {"id": "PORTFOLIO"}, {"rows": rows, "counts": self.decision_counts(self.store.all("loan"))}, fmt)

    @staticmethod
    def decision_counts(loans):
        return {
            "auto_approved": sum(x.get("approval_source") == "policy" and x.get("underwriting", {}).get("decision") in {"approve", "approve_lower"} for x in loans),
            "auto_declined": sum(x.get("approval_source") == "policy" and x.get("underwriting", {}).get("decision") == "decline" for x in loans),
            "escalated": sum(x.get("status") in {"escalated", "restructure_requested"} for x in loans),
            "risk_head_decided": sum(x.get("approval_source") == "risk_head" for x in loans),
        }

    async def dashboard(self):
        await self.document("portfolio_dashboard", {"id": "PORTFOLIO"}, {"loans": self.store.all("loan")}, "png")

    async def application(self, message, role, loan=None, supplied=None, *, new_application=False):
        pending = None if new_application else self.store.get("conversation", self.conversation)
        loan = loan or (self.store.get("loan", pending["loan_id"]) if pending else None)
        if loan and loan.get("status") not in {"new", "pending", "received", "awaiting_documents"}:
            return await self.reply("This application has already been reviewed. Ask the risk head to open a new application for changed terms.", loan)
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
