"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const node = (tag, className = "", text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  };
  const storage = { get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }, set(key, value) { try { localStorage.setItem(key, value); } catch (_) {} } };
  const roles = new Set(["ops", "risk_head", "borrower"]);
  const params = new URLSearchParams(location.search);
  let role = params.get("role") || storage.get("lendix.role") || "ops";
  if (!roles.has(role)) role = "ops";
  $("role-select").value = role;
  storage.set("lendix.role", role);
  let selected = params.get("loan") || storage.get("lendix.loan") || "L-1042";
  let state = { loans: [], documents: [] };
  let filter = "all", search = "", posting = false, refreshTimer, renderTimer, toastTimer, refreshing;
  let queueLimit = 4, activityExpanded = false;
  let eventLoan = null, source;
  const notes = new Map(), work = new Map(), jobs = new Map(), completedJobs = new Set(), seenEvents = new Set(), events = [];
  const openDetails = new Set();
  const stages = ["Application", "Intake", "Risk", "Credit", "Decision", "Compliance", "Payout", "Repayments", "Closed"];
  const stageTargets = ["application", "application", "findings", "money", "decision", "compliance", "payout", "repayments", "repayments"];
  const agentSteps = { "Front Desk": 0, Intake: 1, Risk: 2, Credit: 3, Underwriter: 4, Compliance: 5, Disbursement: 6, Collections: 7 };
  const outcomes = { approve: "Approved", approve_lower: "Approved lower", decline: "Declined", escalate: "Escalated" };
  const statusWords = { new: "Ready for review", pending: "Ready for review", received: "Received", awaiting_documents: "Needs documents", escalated: "Escalated", approved: "Approved", declined: "Declined", disbursed: "In repayment", closed: "Closed", restructure_requested: "Hardship review", withdrawn: "Withdrawn" };
  const docNames = { application_acknowledgment: "Application acknowledgment", missing_documents_request: "Missing documents request", consent_record: "Consent record", kyc_verification_summary: "KYC verification summary", risk_report: "Risk report", risk_chart: "Risk chart", decision_note: "Underwriting decision note", credit_appraisal: "Credit appraisal", sanction_letter: "Sanction letter", rejection_letter: "Rejection letter", key_facts_statement: "Key Facts Statement", loan_agreement_summary: "Loan agreement summary", disbursement_advice: "Simulated disbursement advice", repayment_schedule: "Repayment schedule", payment_receipt: "Payment receipt", payment_reminder: "Payment reminder", restructure_request: "Restructure request", no_dues_certificate: "No Dues Certificate", grievance_acknowledgment: "Grievance acknowledgment", review_queue: "Daily review queue", portfolio_dashboard: "Portfolio dashboard", audit_log: "Audit log" };
  const docStages = { application_acknowledgment: 0, consent_record: 1, kyc_verification_summary: 1, missing_documents_request: 1, risk_report: 2, risk_chart: 2, decision_note: 4, credit_appraisal: 4, rejection_letter: 4, sanction_letter: 5, key_facts_statement: 5, loan_agreement_summary: 5, disbursement_advice: 6, repayment_schedule: 7, payment_receipt: 7, payment_reminder: 7, restructure_request: 7, no_dues_certificate: 8 };
  const money = (value) => Number.isFinite(Number(value)) && value !== null && value !== "" && value !== undefined ? "₹" + new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(Number(value)) : "Not recorded";
  const pct = (value) => Number.isFinite(Number(value)) && value !== null && value !== undefined ? new Intl.NumberFormat("en-IN", { style: "percent", maximumFractionDigits: 1 }).format(value) : "Not recorded";
  const words = (value) => String(value || "Not recorded").replace(/_/g, " ");
  const time = (value) => { const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }).format(date); };
  const date = (value) => { const parsed = new Date(value); return Number.isNaN(parsed.getTime()) ? "Not recorded" : new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" }).format(parsed); };
  const loan = () => state.loans.find((item) => item.id === selected);
  const docsFor = (id) => state.documents.filter((item) => item.loan_id === id).sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  const needsPerson = (item) => ["escalated", "restructure_requested"].includes(item.status);
  function safeUrl(value) { try { const url = new URL(value, location.origin); return url.origin === location.origin && url.pathname.startsWith("/files/") ? url.pathname + url.search : null; } catch (_) { return null; } }
  function button(label, callback, className = "button") { const element = node("button", className, label); element.type = "button"; element.addEventListener("click", callback); return element; }
  function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $("toast").hidden = true; }, 5000); }
  function notice(message = "") { $("page-notice").textContent = message; $("page-notice").hidden = !message; }
  function pill(text, tone = "") { const element = node("span", "pill " + tone); element.append(node("span", "pill-dot"), document.createTextNode(text)); element.firstChild.setAttribute("aria-hidden", "true"); return element; }
  function statusTone(item) { if (["declined"].includes(item.status)) return "amber"; if (needsPerson(item)) return "blue"; if (["approved", "closed"].includes(item.status)) return "green"; return ""; }
  function stageOf(item) {
    if (item.status === "closed") return 8;
    if (["disbursed", "restructure_requested"].includes(item.status)) return 7;
    if (item.kfs_accepted) return 6;
    if (item.kfs_issued) return 5;
    if (item.underwriting) return 4;
    if (item.credit) return 3;
    if (item.risk) return 2;
    if (item.status === "awaiting_documents") return 1;
    return 0;
  }
  function primaryAction(item) {
    if (role === "ops") {
      if (["new", "pending", "received"].includes(item.status)) return { label: "Run review", message: `review ${item.id}` };
      if (item.status === "awaiting_documents") return (item.documents || []).includes("bank_statement") ? { label: "Add documents", upload: true } : { label: "Add bank statement", message: `add bank statement ${item.id}` };
      if (item.status === "approved" && item.kfs_accepted) return { label: "Simulate payout", message: `disburse ${item.id}` };
      if (item.status === "disbursed") return { label: "Settle remaining EMIs", message: `fast-forward ${item.id}` };
    }
    if (role === "risk_head" && item.status === "escalated") return { label: "Decide", decide: true };
    if (role === "borrower") {
      if (item.status === "approved" && item.kfs_issued && !item.kfs_accepted) return { label: "Accept Key Facts Statement", terms: true };
      if (["disbursed", "restructure_requested"].includes(item.status) && (!item.schedule || (item.paid_installments || 0) < item.schedule.length)) return { label: "Pay EMI", message: `pay EMI ${item.id}` };
    }
    return null;
  }
  function calmStatus(item) {
    if (work.get(item.id)?.active) return `Working… ${work.get(item.id).agent || "Front Desk"} is ${work.get(item.id).agent === "Risk" ? "checking this loan" : "working on this loan"}.`;
    if (item.status === "escalated") return "Waiting for the risk head to consider this exception.";
    if (item.status === "restructure_requested") return "A hardship request is with the risk head. Revised terms are awaiting review.";
    if (item.status === "declined") return "The policy decision is recorded. The borrower can review the reasons below.";
    if (item.status === "closed") return "The loan is closed. Its No Dues Certificate is in Documents.";
    if (item.status === "approved" && !item.kfs_accepted) return "Approved. The borrower needs to read and accept the Key Facts Statement.";
    if (item.status === "approved") return "Terms accepted. Operations can request the simulated payout.";
    if (item.status === "awaiting_documents") return "Intake is waiting for the missing application document.";
    if (item.status === "disbursed") return role === "ops" ? "Demo settlement records all remaining installments. No real money moves." : "The simulated loan is in repayment. Ask the office if paying is difficult.";
    return role === "ops" ? "Ready when you are. Run a review to begin." : "Operations can run the application review.";
  }
  function renderQueue() {
    const focusId = document.activeElement?.id;
    $("queue-total").textContent = String(state.loans.length);
    $("mobile-count").textContent = `(${state.loans.length})`;
    const matches = state.loans.filter((item) => {
      const found = `${item.id} ${item.name}`.toLowerCase().includes(search.toLowerCase());
      const match = filter === "all" || (filter === "person" && needsPerson(item)) || (filter === "progress" && ["new", "pending", "received", "awaiting_documents", "disbursed"].includes(item.status)) || item.status === filter;
      return found && match;
    }).sort((a, b) => Number(needsPerson(b)) - Number(needsPerson(a)) || a.id.localeCompare(b.id));
    const list = $("queue-list"); list.replaceChildren();
    if (!matches.length) list.append(node("p", "queue-empty", "No applications match. Try another name or filter."));
    const visible = matches.slice(0, queueLimit);
    const current = matches.find((item) => item.id === selected);
    if (current && !visible.includes(current) && visible.length) visible[visible.length - 1] = current;
    for (const item of visible) {
      const row = button("", () => selectLoan(item.id), "queue-row" + (item.id === selected ? " selected" : ""));
      row.id = `queue-${item.id}`; row.setAttribute("aria-current", item.id === selected ? "true" : "false");
      row.setAttribute("aria-label", `${item.id}, ${item.name}, ${money(item.requested_amount)}, ${statusWords[item.status] || words(item.status)}`);
      const top = node("span", "queue-row-top"); const id = node("span", "mono", item.id); top.append(id);
      if (needsPerson(item)) { const dot = node("span", "needs-dot"); dot.setAttribute("aria-hidden", "true"); top.append(dot); }
      top.append(node("span", "queue-row-stage", stages[stageOf(item)]));
      const bottom = node("span", "queue-row-bottom"); bottom.append(pill(statusWords[item.status] || words(item.status), statusTone(item)));
      row.append(top, node("span", "queue-row-name", item.name), node("span", "queue-row-amount", money(item.approved_amount || item.requested_amount)), bottom); list.append(row);
    }
    if (matches.length > visible.length) list.append(button(`Show ${Math.min(4, matches.length - visible.length)} more · ${matches.length - visible.length} remaining`, () => { queueLimit += 4; renderQueue(); }, "text-button queue-more"));
    if (queueLimit > 4) list.append(button("Show fewer applications", () => { queueLimit = 4; renderQueue(); }, "text-button queue-more"));
    if (focusId?.startsWith("queue-") && $(focusId)) $(focusId).focus({ preventScroll: true });
  }
  function selectLoan(id) {
    if (!state.loans.some((item) => item.id === id)) return;
    selected = id; activityExpanded = false; openDetails.delete("progress"); storage.set("lendix.loan", id);
    const url = new URL(location.href); url.searchParams.set("loan", id); history.replaceState(null, "", url.pathname + url.search);
    $("loan-queue").classList.remove("open"); $("queue-toggle").setAttribute("aria-expanded", "false");
    renderQueue(); renderLoan(); renderActivity(); $("loan-view").scrollTop = 0;
    window.scrollTo({ top: 0, behavior: "instant" });
  }
  function section(id, title, label) { const wrap = node("section", "case-content-section"); wrap.id = id; const heading = node("div", "case-section-heading"); heading.append(node("h2", "", title)); if (label) heading.append(node("span", "mono", label)); wrap.append(heading); return wrap; }
  function detailPair(label, value, className = "") { const pair = node("div", className); pair.append(node("dt", "", label), node("dd", "", value)); return pair; }
  function renderRail(item) {
    const control = node("details", "progress-control"); control.id = "office-progress";
    control.open = openDetails.has("progress");
    control.addEventListener("toggle", () => { if (control.open) openDetails.add("progress"); else openDetails.delete("progress"); });
    const rail = node("ol", "lifecycle"); rail.setAttribute("aria-label", "Loan lifecycle");
    const currentWork = work.get(item.id), stage = currentWork?.active ? currentWork.stage : stageOf(item);
    const summary = node("summary", "progress-summary"); summary.id = "progress-summary";
    const label = currentWork?.active ? `${currentWork.agent || stages[stage]} is working` : `${String(stage + 1).padStart(2, "0")} ${stages[stage]}`;
    summary.append(node("span", "progress-label", label));
    const segments = node("span", "progress-segments"); segments.setAttribute("aria-hidden", "true");
    stages.forEach((_, index) => segments.append(node("span", index < stage ? "done" : index === stage ? "current" : "")));
    summary.append(segments, node("span", "progress-count", `${item.status === "closed" ? 9 : stage}/9 complete`), node("span", "progress-chevron", "⌄"));
    control.append(summary, rail);
    stages.forEach((name, index) => {
      const li = node("li", index < stage ? "done" : index === stage ? "current" + (currentWork?.active ? " working" : "") : "");
      const action = button("", () => {
        const target = $(stageTargets[index]);
        if (!target) { toast(`${name} has not started for this loan.`); return; }
        if (target.tagName === "DETAILS") target.open = true;
        const parentRecord = target.closest("details.record-disclosure"); if (parentRecord) parentRecord.open = true;
        control.open = false; openDetails.delete("progress");
        target.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "start" });
      }, ""); action.id = `step-${index}`; action.disabled = index > stage; action.setAttribute("aria-label", `${index + 1}. ${name}${index < stage ? ", complete" : index === stage ? ", current" : ", not started"}`);
      if (index === stage) action.setAttribute("aria-current", "step");
      action.append(node("span", "step-number", index < stage ? "✓" : String(index + 1).padStart(2, "0")), node("span", "step-name", name)); li.append(action); rail.append(li);
    }); return control;
  }
  function disclosure(id, title, caption, children) {
    const key = `${selected}:${id}`, wrap = node("details", "record-disclosure"); wrap.id = id; wrap.open = openDetails.has(key);
    const summary = node("summary"); summary.append(node("span", "", title), node("span", "disclosure-caption", caption), node("span", "disclosure-chevron", "+"));
    wrap.append(summary, ...children);
    wrap.addEventListener("toggle", () => { if (wrap.open) openDetails.add(key); else openDetails.delete(key); });
    return wrap;
  }
  function findLink(item, kind, label) { const file = docsFor(item.id).find((doc) => doc.kind === kind); if (!file || !safeUrl(file.url)) return null; const link = node("a", "", label || docNames[kind] || words(kind)); link.href = safeUrl(file.url); link.target = "_blank"; link.rel = "noopener noreferrer"; return link; }
  function renderFindings(item) {
    const wrap = section("findings", "What the agents found", item.risk ? "Recorded evidence" : "Awaiting Risk");
    const risk = item.risk;
    if (!risk) {
      const empty = node("div", "pending-finding"), heading = node("h3", "", "The record is here."); heading.append(node("span", "", "The checks come next.")); empty.append(heading, node("p", "", ["disbursed", "restructure_requested"].includes(item.status) ? "This synthetic loan is already in repayment. No application review findings are recorded here." : "Run the review to see identity, payout and income checks alongside their evidence.")); wrap.append(empty); return wrap;
    }
    const codes = risk.codes || [];
    if (!codes.length && risk.band !== "unverified") {
      const clear = node("div", "clear-finding"); const mark = node("span", "clear-mark", "✓"); mark.setAttribute("aria-hidden", "true"); const copy = node("div"); copy.append(node("h3", "", "Nothing unusual found"), node("p", "", "The available verification records passed the configured risk checks.")); clear.append(mark, copy); wrap.append(clear); return wrap;
    }
    const findings = [...codes]; if (risk.band === "unverified") findings.push("SOURCE_UNAVAILABLE");
    for (const [index, code] of findings.entries()) {
      const info = findingInfo(item, code), card = node("article", "finding-card");
      const copy = node("div"); copy.append(node("span", "severity", info.severity), node("h3", "finding-title", info.sentence));
      const origin = node("p", "finding-source", `Risk rule ${code} · ${info.source}`);
      const link = findLink(item, info.kind, "Open the verification document ↗"); if (link) origin.append(link); copy.append(origin);
      const paper = node("div", "field-evidence"); paper.append(node("span", "mono", info.source));
      info.fields.forEach(([label, value, flagged]) => { const pair = detailPair(label, value, "evidence-pair"); if (flagged) pair.lastChild.classList.add("flagged"); paper.append(pair); });
      paper.append(node("p", "evidence-caption", "Recorded fields from the synthetic application and fixtures.")); card.append(copy);
      if (index === 0) card.append(paper);
      else { const evidence = node("details", "finding-evidence"), key = `${item.id}:evidence:${code}`; evidence.open = openDetails.has(key); evidence.append(node("summary", "", "View source fields"), paper); evidence.addEventListener("toggle", () => { if (evidence.open) openDetails.add(key); else openDetails.delete(key); }); card.append(evidence); }
      wrap.append(card);
    }
    return wrap;
  }
  function findingInfo(item, code) {
    const dictionary = {
      PAYOUT_NAME_MISMATCH: { severity: "High", sentence: `The payout account is in ${item.payout_name}'s name, rather than ${item.name}'s.`, source: "Payout & identity records", kind: "kyc_verification_summary", fields: [["Applicant", item.name], ["Payout account holder", item.payout_name, true]] },
      DOB_PAN_MISMATCH: { severity: "High", sentence: "The date of birth on the application differs from the identity record.", source: "Application & KYC record", kind: "kyc_verification_summary", fields: [["Application date of birth", date(item.dob), true], ["KYC date of birth", date(item.kyc_dob)]] },
      NEW_NUMBER: { severity: "Note", sentence: "The mobile number was activated less than 90 days ago.", source: "Mobile activation fixture", kind: "risk_report", fields: [["Number ending", String(item.phone || "").slice(-4)], ["Activated on", date(item.phone_activated_at), true]] },
      INCOME_DOC_GAP: { severity: "High", sentence: "Declared income is more than 30% above the bank statement amount.", source: "Application & statement", kind: "risk_report", fields: [["Declared monthly income", money(item.monthly_income), true], ["Statement monthly income", money(item.statement_income)]] },
      SHARED_PAYOUT_ACCOUNT: { severity: "High", sentence: "The payout account appears on applications from more than one person.", source: "Application history", kind: "risk_report", fields: [["Payout account", item.payout_account, true], ["Applicant", item.name]] },
      MULTIPLE_RECENT_APPLICATIONS: { severity: "Note", sentence: "The same phone or identity number appears in at least three recent applications.", source: "30-day application history", kind: "risk_report", fields: [["Identity reference", item.pan, true], ["Phone ending", String(item.phone || "").slice(-4)]] },
      SOURCE_UNAVAILABLE: { severity: "Note", sentence: "Verification is incomplete. A missing source cannot be treated as a clear result.", source: "Risk source availability", kind: "risk_report", fields: [["Missing sources", (item.risk?.missing_sources || []).join(", ") || "Required source unavailable", true], ["Risk band", "Unverified"]] },
    };
    return dictionary[code] || { severity: "Note", sentence: item.risk?.explanations?.[item.risk.codes.indexOf(code)] || "A verification flag needs review.", source: "Risk report", kind: "risk_report", fields: [["Reason code", code, true]] };
  }
  function renderDecision(item) {
    const decision = item.servicing_escalation || item.underwriting; if (!decision) return null;
    const wrap = section("decision", item.servicing_escalation ? "Hardship review" : "The decision", "On record"); const layout = node("div", "decision-layout"), left = node("div");
    left.append(node("div", "decision-word " + (decision.decision === "decline" ? "amber" : ["approve", "approve_lower"].includes(decision.decision) ? "green" : ""), outcomes[decision.decision] || words(decision.decision)), node("div", "policy-rule", `Policy rule / ${decision.rule || "Not recorded"}`));
    const right = node("div", "decision-reasons"); const reasons = [...new Set(decision.reasons || item.rejection_reasons || [])].filter((reason) => reason !== item.override_reason); reasons.slice(0, 3).forEach((reason) => right.append(node("p", "", reason)));
    if (item.approved_amount && !item.servicing_escalation) right.prepend(node("p", "", `${money(item.approved_amount)} approved${item.approved_amount < item.requested_amount ? ` against ${money(item.requested_amount)} requested` : ""}.`));
    const attribution = node("div", "decision-attribution"); const who = item.approval_source === "risk_head" && !item.servicing_escalation ? "Risk head, with reason on record" : `Underwriter, by policy v${decision.policy_version || "—"}`; attribution.append(node("strong", "", who)); if (item.override_reason && !item.servicing_escalation) attribution.append(node("p", "", item.override_reason)); right.append(attribution); layout.append(left, right); wrap.append(layout); return wrap;
  }
  function renderMoney(item) {
    const credit = item.credit; if (!credit) return null;
    const wrap = section("money", "The numbers", item.approved_amount ? "At the approved amount" : "At the requested amount"), grid = node("dl", "money-grid");
    [["Verified monthly income", money(Math.min(item.monthly_income, item.statement_income))], ["Existing EMIs", money(item.existing_emi)], ["New monthly EMI", money(credit.emi)]].forEach(([label, value]) => grid.append(detailPair(label, value, "money-item"))); wrap.append(grid);
    const heading = node("div", "foir-row"); heading.append(node("span", "", "Income committed to repayments"), node("strong", "", pct(credit.foir))); wrap.append(heading);
    const track = node("div", "foir-track"); track.setAttribute("role", "meter"); track.setAttribute("aria-label", "Fixed obligation to income ratio, with a 50 percent policy cap"); track.setAttribute("aria-valuemin", "0"); track.setAttribute("aria-valuemax", "100"); const value = Math.max(0, Math.min(100, Number(credit.foir || 0) * 100)); track.setAttribute("aria-valuenow", String(value)); const fill = node("span", "foir-fill"); fill.style.width = `${value}%`; track.append(fill, node("span", "foir-cap")); wrap.append(track);
    const axis = node("div", "foir-axis"); axis.append(node("span", "", "0%"), node("span", "", "Policy cap 50%"), node("span", "", "100%")); wrap.append(axis);
    const eligible = node("div", "eligible-row"); eligible.append(node("span", "", "Eligible up to"), node("strong", "", money(credit.eligible_amount))); wrap.append(eligible); return wrap;
  }
  function renderDocuments(item) {
    const files = docsFor(item.id), wrap = section("documents", "Documents", `${files.length} on file`);
    if (!files.length) wrap.append(node("p", "section-note", "Reports, disclosures and letters appear here as the application moves forward."));
    const groups = new Map(); files.forEach((file) => { const index = docStages[file.kind] ?? 0; if (!groups.has(index)) groups.set(index, []); groups.get(index).push(file); });
    [...groups.keys()].sort((a, b) => a - b).forEach((index) => { const group = node("div", "doc-group"); group.append(node("h3", "", `${String(index + 1).padStart(2, "0")} / ${stages[index]}`));
      groups.get(index).forEach((file) => { const url = safeUrl(file.url); if (!url) return; const link = node("a", "document-row"); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.setAttribute("aria-label", `Open ${docNames[file.kind] || words(file.kind)} in a new tab`);
        const copy = node("div"), extension = String(file.filename || "").split(".").pop().toUpperCase(); copy.append(node("div", "document-name", (file.kind === "risk_report" && extension === "PNG") ? "Risk chart" : docNames[file.kind] || words(file.kind))); const meta = node("div", "document-metadata"); meta.append(node("span", "", extension), node("time", "", time(file.created_at))); copy.append(meta); const action = node("span", "document-download", "Open"); action.append(node("span", "", "↗")); link.append(copy, action); group.append(link); }); wrap.append(group);
    });
    if (["new", "pending", "received", "awaiting_documents"].includes(item.status)) { const upload = button("Upload a document", () => $("upload-input").click(), "text-button"); wrap.append(upload); }
    return disclosure("document-records", "Documents", `${files.length} on file`, [wrap]);
  }
  function renderApplication(item) {
    const wrap = node("details", "application-record"); wrap.id = "application"; wrap.open = openDetails.has("application"); wrap.addEventListener("toggle", () => { if (wrap.open) openDetails.add("application"); else openDetails.delete("application"); }); wrap.append(node("summary", "", "Application record")); const grid = node("dl", "application-grid");
    [["Applicant", item.name], ["Requested", money(item.requested_amount)], ["Tenure", `${item.tenure_months} months`], ["Payout account holder", item.payout_name], ["Date of birth", date(item.dob)], ["Consent", item.consent ? "Recorded" : "Not recorded"]].forEach(([label, value]) => grid.append(detailPair(label, value))); wrap.append(grid); return wrap;
  }
  function renderServicing(item) {
    const sections = [];
    if (item.kfs_issued) {
      const wrap = section("compliance", "Before payout", "Safeguards"); const list = node("ul", "checklist");
      [[true, "Key Facts Statement issued"], [item.kfs_accepted, "Borrower acceptance recorded"], [item.account_verified, "Verified payout account recorded"]].forEach(([done, label]) => { const li = node("li"); li.append(node("span", done ? "done" : "waiting", done ? "✓" : "—"), document.createTextNode(label)); list.append(li); });
      wrap.append(list, node("p", "section-note", `Cooling-off period: ${item.cooling_off_days || 1} day${(item.cooling_off_days || 1) === 1 ? "" : "s"}. The borrower can exit without penalty.`)); sections.push(wrap);
    }
    if (item.disbursed_at) { const wrap = section("payout", "Payout", "Simulated"); wrap.append(node("p", "section-note", `${money(item.approved_amount)} · ${date(item.disbursed_at)}. No real money moved.`)); if (item.simulated_utr) wrap.append(node("p", "policy-rule", item.simulated_utr)); sections.push(wrap); }
    if (["disbursed", "restructure_requested", "closed"].includes(item.status)) {
      const wrap = section("repayments", item.status === "closed" ? "Repayments complete" : "Repayments", item.status === "closed" ? "Closed" : "In service"); const summary = node("div", "repayment-summary"); const count = node("div", "muted", "Installments recorded"); count.append(node("strong", "", `${item.paid_installments || 0} / ${item.schedule?.length || item.tenure_months}`)); const balance = node("div", "muted", "Remaining scheduled dues"); balance.append(node("strong", "", money(item.outstanding ?? item.outstanding_balance))); summary.append(count, balance); wrap.append(summary);
      if (item.hardship_summary) { const support = node("div", "support-note"); support.append(node("h3", "", "A little breathing room"), node("p", "", "Your hardship request is with the risk head. No revised terms have been approved.")); wrap.append(support); }
      sections.push(wrap);
    }
    return sections;
  }
  function renderNote(item) { const note = notes.get(item.id); if (!note) return null; const wrap = node("aside", "inline-note" + (note.error ? " error" : "")); wrap.setAttribute("aria-label", "Latest office reply"); wrap.append(node("p", "", note.text)); if (note.error && note.retry) wrap.append(button("Retry", () => sendCommand(note.retry, item.id, note.role || role), "text-button")); if (note.error) return wrap; return disclosure("office-reply", "Office reply", "Read the latest update", [wrap]); }
  function renderLoan() {
    const item = loan(), root = $("loan-content"); if (!item) return;
    const focusId = document.activeElement?.id;
    root.replaceChildren();
    const header = node("section", "case-header"), top = node("div", "case-header-top"), identity = node("div", "case-identity");
    identity.append(renderRail(item), node("span", "case-id", `${item.id} / Loan application`)); top.append(identity, pill(statusWords[item.status] || words(item.status), statusTone(item)));
    const heading = node("div", "case-heading-row"), name = node("div"); name.append(node("h1", "", item.name)); heading.append(name); const action = primaryAction(item);
    if (action) { const actions = node("div", "case-action"), primary = button(action.label, () => { if (action.decide) openDecision(item); else if (action.terms) openTerms(item); else if (action.upload) $("upload-input").click(); else sendCommand(action.message, item.id); }); primary.id = "primary-action"; primary.disabled = !!work.get(item.id)?.active; actions.append(primary); heading.append(actions); }
    const meta = node("div", "case-meta"); meta.append(node("span", "amount", money(item.approved_amount || item.requested_amount)), node("span", "separator"), node("span", "", `${item.tenure_months} months`)); if (item.risk?.band || item.risk_band) meta.append(node("span", "separator"), node("span", "", `${words(item.risk?.band || item.risk_band)} risk`));
    const status = node("div", "case-status-line"); status.setAttribute("role", "status"); if (work.get(item.id)?.active) { const mark = node("span", "working-mark"); mark.setAttribute("aria-hidden", "true"); status.append(mark); } status.append(document.createTextNode(calmStatus(item)));
    header.append(top, heading, meta, status); root.append(header);
    const overview = node("div", "loan-overview"), findings = renderFindings(item); overview.append(findings);
    if (!item.risk && ["disbursed", "restructure_requested"].includes(item.status)) findings.classList.add("seeded-record");
    const facts = node("div", "loan-facts"), decision = renderDecision(item), numbers = renderMoney(item);
    if (decision) facts.append(decision); if (numbers) facts.append(numbers); if (facts.childNodes.length) overview.append(facts); else overview.classList.add("no-decision");
    root.append(overview);
    const records = node("div", "loan-records"), note = renderNote(item); if (note) records.append(note);
    const servicing = renderServicing(item); if (servicing.length) records.append(disclosure("servicing-records", "Payout and repayments", item.status === "closed" ? `${item.paid_installments || 0} installments complete` : "Safeguards and records", servicing));
    records.append(renderDocuments(item), renderApplication(item)); root.append(records);
    $("command-input").placeholder = `Review ${item.id}`;
    renderSuggestions(item);
    if (focusId && $(focusId) && ["primary-action", "progress-summary", ...stages.map((_, index) => `step-${index}`)].includes(focusId)) $(focusId).focus({ preventScroll: true });
  }
  function renderSuggestions(item) {
    const root = $("command-suggestions"); root.replaceChildren(); const suggestions = [];
    if (item.underwriting) suggestions.push(["See the decision", () => $("decision")?.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" })]);
    if (["disbursed", "restructure_requested"].includes(item.status) && role === "borrower") suggestions.push(["Report job loss", () => sendCommand(`I lost my job and can't pay ${item.id}`, item.id)]);
    if (["new", "pending", "received", "awaiting_documents"].includes(item.status)) suggestions.push(["Upload a document", () => $("upload-input").click()]);
    if (item.status === "approved" && role === "borrower") suggestions.push(["Cooling-off exit", () => sendCommand(`exit ${item.id}`, item.id)]);
    if (role === "risk_head") suggestions.push(["Export audit log", () => sendCommand("export audit log", item.id)]);
    suggestions.push(["Review queue", () => sendCommand("review queue", item.id)]);
    suggestions.slice(0, 3).forEach(([label, callback]) => root.append(button(label, callback, "")));
  }
  function activityTitle(group) {
    const event = group.events[group.events.length - 1];
    if (event.kind === "handoff") { const match = event.text.match(/^(.+?) → (.+?): (L-\d+)/); if (match) return `${match[1]} handed ${match[3]} to ${match[2]}`; }
    if (group.type === "checks") return `${group.agent} ${group.agent === "Compliance" ? "checked messages and documents" : "completed its recorded tools"} · ${group.events.length} ${group.events.length === 1 ? "event" : "events"}`;
    if (event.kind === "decision") return `${event.data?.role === "risk_head" ? "Risk head" : "Underwriter"} recorded ${outcomes[event.data?.decision]?.toLowerCase() || "a decision"}`;
    if (event.kind === "document") return `Reporter prepared ${docNames[event.data?.kind]?.toLowerCase() || "a document"}`;
    if (event.kind === "mention") return "Reporter sent the case to the risk head";
    if (event.kind === "escalation") return "Underwriter escalated the exception";
    return event.text.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/@/g, "").slice(0, 180);
  }
  function relative(value) { const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000)); if (!Number.isFinite(seconds)) return ""; return seconds < 60 ? `${seconds} s ago` : seconds < 3600 ? `${Math.floor(seconds / 60)} min ago` : time(value); }
  function renderActivity() {
    const list = $("activity-list"), groups = []; const relevant = events.filter((event) => event._loan === selected || (!event._loan && ["progress", "error"].includes(event.kind))).filter((event) => !["status", "reply", "file", "office_action"].includes(event.kind));
    $("activity-panel").classList.toggle("expanded-record", activityExpanded);
    const checks = new Map();
    relevant.forEach((event) => {
      if (event.kind === "tool") { const key = event.agent; if (checks.has(key)) checks.get(key).events.push(event); else { const group = { agent: key, type: "checks", events: [event] }; checks.set(key, group); groups.push(group); } }
      else groups.push({ agent: event.agent, type: event.kind, events: [event] });
    });
    groups.sort((a, b) => Number(b.events[b.events.length - 1].id) - Number(a.events[a.events.length - 1].id));
    const expanded = new Set(Array.from(list.querySelectorAll("details[open]")).map((element) => element.dataset.group)); list.replaceChildren();
    if (!groups.length) list.append(node("p", "activity-empty", "When this application moves, each handoff and completed check appears here."));
    groups.slice(0, activityExpanded ? 14 : 4).forEach((group) => { const detail = node("details", "activity-entry"), last = group.events[group.events.length - 1], key = `${group.agent}-${group.type}-${group.type === "checks" ? "" : last.id}`; detail.dataset.group = key; detail.open = expanded.has(key); const summary = node("summary"); summary.append(node("span", "activity-entry-title", activityTitle(group))); const timestamp = node("time", "", relative(last.timestamp)); timestamp.dateTime = last.timestamp; timestamp.dataset.timestamp = last.timestamp; summary.append(timestamp, node("span", "activity-expand", "Expand record")); const technical = node("ol", "activity-technical"); group.events.slice(-12).forEach((event) => technical.append(node("li", "", `${time(event.timestamp)} · ${event.agent} · ${event.text}`))); detail.append(summary, technical); list.append(detail); });
    if (groups.length > 4) list.append(button(activityExpanded ? "Show recent activity" : `Show earlier activity · ${groups.length - 4}`, () => { activityExpanded = !activityExpanded; renderActivity(); }, "text-button activity-more"));
  }
  function renderSoon() { clearTimeout(renderTimer); renderTimer = setTimeout(() => { renderQueue(); renderLoan(); renderActivity(); }, 100); }
  async function responseData(response) { let data; try { data = await response.json(); } catch (_) { data = {}; } if (!response.ok) { const detail = Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join("; ") : data.detail; throw new Error(typeof detail === "string" ? detail : "The office could not complete this request. Please retry."); } return data; }
  async function refreshState() {
    if (refreshing) return refreshing;
    refreshing = (async () => { try { const data = await responseData(await fetch("/api/state")); state = { loans: Array.isArray(data.loans) ? data.loans : [], documents: Array.isArray(data.documents) ? data.documents : [] }; if (!loan() && state.loans.length) selected = state.loans[0].id; notice(); renderSoon(); } catch (_) { notice("The office could not load the portfolio. Check the connection and retry."); } finally { refreshing = null; } })(); return refreshing;
  }
  function scheduleRefresh() { if (refreshTimer) return; refreshTimer = setTimeout(() => { refreshTimer = null; refreshState(); }, 250); }
  function finishJob(id, status) { completedJobs.add(id); const job = jobs.get(id); if (job) { const active = work.get(job.loanId); if (active?.jobId === id) active.active = false; if (status === "failed" || status === "interrupted") notes.set(job.loanId, { text: "The office could not finish this request. Please retry; the recorded loan state is preserved.", error: true, retry: job.retry, role: job.role }); jobs.delete(id); } renderSoon(); scheduleRefresh(); }
  async function pollJob(id) { if (completedJobs.has(id)) return; try { const job = await responseData(await fetch(`/api/jobs/${encodeURIComponent(id)}`)); if (!["queued", "running"].includes(job.status)) { finishJob(id, job.status); return; } } catch (_) { /* SSE remains the primary source while a job request is unavailable. */ } if (jobs.has(id)) setTimeout(() => pollJob(id), 1200); }
  async function sendCommand(message, loanId = selected, requestRole = role) {
    const text = String(message || "").trim(); if (!text || posting) return; if (text.length > 8000) { toast("Keep the request under 8,000 characters."); return; }
    posting = true; const matched = text.match(/\bL-\d+\b/i); if (matched) loanId = matched[0].toUpperCase();
    const retry = /^(approve|decline|override)\b/i.test(text) ? `review ${loanId}` : text;
    try { const result = await responseData(await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message: text, role: requestRole, conversation_id: "office" }) }));
      if (result.accepted === false) throw new Error("The office could not accept the request. Please retry.");
      if (!completedJobs.has(result.job_id)) { jobs.set(result.job_id, { loanId, retry, role: requestRole }); work.set(loanId, { active: true, stage: stageOf(state.loans.find((item) => item.id === loanId) || {}), agent: "Front Desk", jobId: result.job_id }); setTimeout(() => pollJob(result.job_id), 1000); }
      notes.delete(loanId); renderSoon(); scheduleRefresh();
    } catch (error) { notes.set(loanId, { text: error.message, error: true, retry, role: requestRole }); renderSoon(); } finally { posting = false; }
  }
  function handleEvent(event) {
    if (!event || typeof event !== "object" || (event.id && seenEvents.has(event.id))) return; if (event.id) seenEvents.add(event.id);
    const inferred = event.loan_id || String(event.text || "").match(/\bL-\d+\b/)?.[0]; if (inferred && state.loans.some((item) => item.id === inferred)) eventLoan = inferred;
    const context = inferred || eventLoan; event._loan = context; events.push(event); if (events.length > 600) events.shift();
    if (["tool", "handoff"].includes(event.kind) && context && agentSteps[event.agent] !== undefined) {
      const previous = work.get(context) || {};
      const step = event.agent === "Compliance" && (previous.stage ?? 0) < 4 ? (previous.stage ?? 0) : agentSteps[event.agent];
      work.set(context, { ...previous, active: true, agent: event.agent, stage: step });
    }
    if (event.kind === "reply" || event.kind === "error") {
      if (context) { const previous = work.get(context); if (previous) previous.active = false; const last = [...jobs.values()].find((job) => job.loanId === context); const error = event.kind === "error" || /trouble reaching|please retry|retry the|model returned|daily model-call limit|could not (?:finish|complete)/i.test(event.text || ""); notes.set(context, { text: String(event.text || ""), error, retry: last?.retry || `review ${context}`, role: last?.role || role }); }
      else if (event.kind === "error") notice(event.text);
    }
    if (event.data?.job_id && ["completed", "failed", "interrupted"].includes(event.data.status)) finishJob(event.data.job_id, event.data.status);
    renderSoon(); scheduleRefresh();
  }
  function connectEvents() { source = new EventSource("/api/events?conversation_id=office"); source.onopen = () => { $("connection-label").textContent = "Office connected"; $("activity-connection").textContent = "Live record"; scheduleRefresh(); }; source.onerror = () => { $("connection-label").textContent = "Reconnecting to the office"; $("activity-connection").textContent = "Reconnecting"; }; source.onmessage = (message) => { try { handleEvent(JSON.parse(message.data)); } catch (_) { notice("An office update could not be read. The loan record will refresh automatically."); scheduleRefresh(); } }; }
  function openDecision(item) { $("decision-form").reset(); $("decision-form-error").hidden = true; $("decision-loan-id").textContent = item.id; $("decision-dialog").dataset.loan = item.id; $("decision-context").textContent = `${money(item.requested_amount)} requested · ${item.tenure_months} months. Policy rule: ${item.underwriting?.rule || "Exception review"}.`; $("decision-amount-field").hidden = true; $("decision-amount").required = false; $("decision-dialog").showModal(); }
  function openTerms(item) {
    $("terms-dialog").dataset.loan = item.id; $("terms-loan-id").textContent = item.id; $("terms-confirm").checked = false; $("terms-accept").disabled = true; const details = $("terms-details"); details.replaceChildren(); const credit = item.credit || {};
    [["Loan amount", money(item.approved_amount)], ["APR", pct(credit.apr)], ["Monthly EMI", money(credit.emi)], ["Tenure", `${item.tenure_months} months`], ["Every disclosed fee", money(credit.fees ?? item.fees)], ["Total amount payable", money(credit.total_cost)], ["Cooling-off period", `${item.cooling_off_days || 1} day(s)`], ["Grievance contact", item.grievance_contact || "See the full KFS"]].forEach(([label, value]) => details.append(detailPair(label, value)));
    const doc = $("terms-document"); doc.replaceChildren(); const link = findLink(item, "key_facts_statement", "Read the full Key Facts Statement ↗"); if (link) doc.append(link); $("terms-dialog").showModal();
  }
  $("decision-outcome").addEventListener("change", (event) => { const item = state.loans.find((entry) => entry.id === $("decision-dialog").dataset.loan); if (!item) return; const choice = event.target.value; const amount = $("decision-amount"), lower = choice === "approve_lower"; $("decision-amount-field").hidden = choice !== "approve" && !lower; amount.required = choice === "approve" || lower; amount.readOnly = choice === "approve"; amount.max = String(lower ? Math.min(item.requested_amount - .01, item.credit?.eligible_amount || 0) : item.requested_amount); amount.value = String(lower ? Math.min(200000, Number(amount.max)) : item.requested_amount); $("decision-amount-help").textContent = `Verified affordability limit: ${money(item.credit?.eligible_amount)}.`; });
  $("decision-form").addEventListener("submit", (event) => { event.preventDefault(); const id = $("decision-dialog").dataset.loan, item = state.loans.find((entry) => entry.id === id), reason = $("decision-reason").value.trim(), outcome = $("decision-outcome").value, amount = Number($("decision-amount").value); if (!item || reason.length < 10 || (outcome !== "decline" && !(amount > 0 && amount <= Math.min(item.requested_amount, item.credit?.eligible_amount || 0)))) { $("decision-form-error").textContent = "Choose an affordable amount and give a specific reason of at least 10 characters."; $("decision-form-error").hidden = false; return; } $("decision-dialog").close(); sendCommand(`${outcome === "decline" ? "decline" : "approve"} ${id}${outcome === "decline" ? "" : ` at ${amount}`} because ${reason}`, id); });
  $("terms-confirm").addEventListener("change", (event) => { $("terms-accept").disabled = !event.target.checked; });
  $("terms-accept").addEventListener("click", () => { if (!$("terms-confirm").checked) return; const id = $("terms-dialog").dataset.loan; $("terms-dialog").close(); sendCommand(`accept KFS ${id}`, id); });
  document.querySelectorAll("[data-close-dialog]").forEach((element) => element.addEventListener("click", () => $(element.dataset.closeDialog).close()));
  $("role-select").addEventListener("change", (event) => { role = event.target.value; storage.set("lendix.role", role); renderLoan(); });
  $("loan-search").addEventListener("input", (event) => { search = event.target.value; queueLimit = 4; renderQueue(); });
  $("queue-filters").addEventListener("click", (event) => { const target = event.target.closest("[data-filter]"); if (!target) return; filter = target.dataset.filter; queueLimit = 4; document.querySelectorAll("[data-filter]").forEach((element) => { element.classList.toggle("active", element === target); element.setAttribute("aria-pressed", String(element === target)); }); renderQueue(); });
  $("command-form").addEventListener("submit", (event) => { event.preventDefault(); const text = $("command-input").value.trim(); if (!text || posting) return; $("command-input").value = ""; sendCommand(text); });
  $("batch-button").addEventListener("click", () => { if (role === "borrower") { toast("Choose Operations or Risk head to review the portfolio."); return; } sendCommand("Review today's 20 applications"); });
  $("upload-input").addEventListener("change", async (event) => { const file = event.target.files[0], item = loan(); if (!file || !item) return; if (file.size > 5 * 1024 * 1024) { toast("Choose a document of 5 MB or smaller."); event.target.value = ""; return; } const form = new FormData(); form.append("file", file); form.append("loan_id", item.id); form.append("role", role); form.append("conversation_id", "office"); try { const result = await responseData(await fetch("/api/upload", { method: "POST", body: form })); if (!completedJobs.has(result.job_id)) { jobs.set(result.job_id, { loanId: item.id, retry: `review ${item.id}`, role }); work.set(item.id, { active: true, agent: "Intake", stage: 1, jobId: result.job_id }); setTimeout(() => pollJob(result.job_id), 1000); } toast("Document received for review."); renderSoon(); } catch (error) { notes.set(item.id, { text: error.message, error: true }); renderSoon(); } finally { event.target.value = ""; } });
  function togglePanel(id, control, open) { $(id).classList.toggle("open", open); $(control).setAttribute("aria-expanded", String(open)); if (open) $(id).querySelector("button,input")?.focus(); }
  $("queue-toggle").addEventListener("click", () => togglePanel("loan-queue", "queue-toggle", !$("loan-queue").classList.contains("open")));
  $("queue-close").addEventListener("click", () => { togglePanel("loan-queue", "queue-toggle", false); $("queue-toggle").focus(); });
  $("activity-toggle").addEventListener("click", () => togglePanel("activity-panel", "activity-toggle", !$("activity-panel").classList.contains("open")));
  $("activity-close").addEventListener("click", () => { togglePanel("activity-panel", "activity-toggle", false); $("activity-toggle").focus(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") { const progress = $("office-progress"); if (progress?.open) { progress.open = false; openDetails.delete("progress"); $("progress-summary").focus(); } if ($("loan-queue").classList.contains("open")) { togglePanel("loan-queue", "queue-toggle", false); $("queue-toggle").focus(); } if ($("activity-panel").classList.contains("open")) { togglePanel("activity-panel", "activity-toggle", false); $("activity-toggle").focus(); } } });
  setInterval(() => document.querySelectorAll("time[data-timestamp]").forEach((element) => { element.textContent = relative(element.dataset.timestamp); }), 15000);
  window.addEventListener("pagehide", () => source?.close());
  window.addEventListener("pageshow", (event) => { if (event.persisted) connectEvents(); });
  window.addEventListener("online", refreshState);
  refreshState().then(connectEvents);
})();
