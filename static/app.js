"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const roleNames = { risk_head: "Risk head", ops: "Operations", borrower: "Borrower" };
  const roleInitials = { risk_head: "RH", ops: "OP", borrower: "BR" };
  const contextActions = {
    "L-1042": [
      ["Review application", "review L-1042"],
      ["Add bank statement", "add bank statement L-1042"],
    ],
    "L-1043": [
      ["Review", "review L-1043"],
      ["Accept KFS", "accept KFS L-1043"],
      ["Disburse", "disburse L-1043"],
      ["Pay EMI", "pay EMI L-1043"],
      ["Close", "close L-1043"],
    ],
    "L-1045": [["Review policy exception", "review L-1045"]],
    "L-1044": [["Request hardship support", "I lost my job L-1044"], ["Review", "review L-1044"]],
  };
  let state = { loans: [], documents: [], users: [] };
  let selectedLoan = "L-1042";
  let posting = false;
  let uploading = false;
  let eventStream;
  let eventCount = 0;
  let refreshTimer;
  let refreshPromise;
  let toastTimer;
  let pendingTimer;
  const pendingJobs = new Set();
  const finishedJobs = new Set();
  const seenEvents = new Set();
  const localDocuments = new Map();
  const agentTimers = new Map();
  const exceptionDrafts = new Map();
  const conversationId = `console-${typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };

  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  }

  function humanize(value) {
    return String(value || "Not reviewed").replace(/[_-]/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function timeLabel(timestamp) {
    if (!timestamp) return "Now";
    const date = new Date(timestamp);
    if (Number.isNaN(date.getTime())) return "Now";
    return new Intl.DateTimeFormat("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }).format(date);
  }

  function showToast(message, isError = false) {
    const toast = $("toast");
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, isError ? 8000 : 4500);
  }

  function notice(message = "") {
    $("page-notice").textContent = message;
    $("page-notice").hidden = !message;
  }

  function safeFileUrl(value) {
    if (typeof value !== "string" || !value) return null;
    try {
      const url = new URL(value, location.origin);
      if (url.origin !== location.origin || !url.pathname.startsWith("/files/")) return null;
      return url.href;
    } catch (_) {
      return null;
    }
  }

  function fileLink(documentInfo, className = "message-file") {
    const url = safeFileUrl(documentInfo.url);
    if (!url) return null;
    const link = el("a", className);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.append(icon("file"), el("span", "", documentInfo.filename || "Download document"), icon("download"));
    link.setAttribute("aria-label", `Open ${documentInfo.filename || "document"} in a new tab`);
    return link;
  }

  function addMessage(text, options = {}) {
    $("welcome-card")?.remove();
    const container = $("messages");
    const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 90;
    const message = el("article", `message ${options.type || "agent"}`);
    const author = options.author || "Front Desk";
    const initials = author.split(/[\s_]+/).map((word) => word[0]).join("").slice(0, 2).toUpperCase();
    const avatar = el("span", "message-avatar", initials);
    avatar.setAttribute("aria-hidden", "true");
    const content = el("div", "message-content");
    const meta = el("div", "message-meta");
    meta.append(el("span", "message-author", author), el("time", "message-time", timeLabel(options.timestamp || new Date().toISOString())));
    content.append(meta);
    if (text) content.append(el("p", "message-text", text));
    if (options.document) {
      const link = fileLink(options.document);
      if (link) content.append(link);
    }
    message.append(avatar, content);
    container.append(message);
    if (nearBottom || options.type === "user") container.scrollTop = container.scrollHeight;
  }

  function selectedLoanInfo() {
    return state.loans.find((loan) => String(loan.id) === selectedLoan);
  }

  function loanRisk(loan) {
    const risk = loan?.risk;
    return typeof risk === "string" ? risk : risk?.band || risk?.risk_band || loan?.risk_band;
  }

  function renderContext() {
    renderEscalated();
    const loan = selectedLoanInfo();
    const status = String(loan?.status || "Not reviewed");
    const statusNode = $("loan-status");
    statusNode.textContent = humanize(status);
    statusNode.className = "status-badge";
    if (/declin|reject|high|block/i.test(status)) statusNode.classList.add("danger");
    else if (/approv|active|disburs|paid|closed|complete/i.test(status) && !/await|pending|recommend/i.test(status)) statusNode.classList.add("success");
    else if (/missing|await|pending|review|recommend|escalat/i.test(status)) statusNode.classList.add("warning");
    const risk = loanRisk(loan);
    $("loan-risk").textContent = risk ? `${humanize(risk)} risk` : "Risk not assessed";
    $("loan-risk").className = `risk-label ${["low", "medium", "high"].includes(risk) ? risk : ""}`;
    $("message-input").placeholder = `Ask the team to review ${selectedLoan}…`;
    $("composer-context").textContent = `Uploads go to ${selectedLoan}. Include a loan ID in messages.`;
    const actions = $("loan-actions");
    actions.replaceChildren();
    const commands = contextActions[selectedLoan] || [["Review application", `review ${selectedLoan}`]];
    for (const [label, command] of commands) {
      const button = el("button", "action-chip", label);
      button.type = "button";
      button.dataset.command = command;
      button.title = command;
      button.disabled = posting;
      actions.append(button);
    }
    document.querySelectorAll("[data-select-loan]").forEach((button) => {
      button.classList.toggle("selected", button.dataset.selectLoan === selectedLoan);
      button.setAttribute("aria-pressed", String(button.dataset.selectLoan === selectedLoan));
    });
    renderDocuments();
  }

  function selectLoan(loanId) {
    if (!loanId) return;
    selectedLoan = loanId;
    if (!Array.from($("loan-select").options).some((option) => option.value === loanId)) {
      const option = el("option", "", loanId);
      option.value = loanId;
      $("loan-select").append(option);
    }
    $("loan-select").value = loanId;
    renderContext();
  }

  function allDocuments() {
    const documents = new Map();
    for (const item of [...localDocuments.values(), ...(state.documents || [])]) {
      if (!item || !safeFileUrl(item.url)) continue;
      documents.set(item.url, item);
    }
    return Array.from(documents.values()).sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  }

  function renderDocuments() {
    const documents = allDocuments();
    $("metric-documents").textContent = String(documents.length);
    const filtered = $("document-filter").value === "selected" ? documents.filter((item) => item.loan_id === selectedLoan) : documents;
    $("documents-count").textContent = String(filtered.length);
    const list = $("documents-list");
    list.replaceChildren();
    if (!filtered.length) {
      const empty = el("div", "documents-empty");
      empty.append(icon("file"), el("strong", "", "Paperwork, handled."), el("p", "", "Reports, letters, and schedules will appear here as they’re ready."));
      list.append(empty);
      return;
    }
    for (const item of filtered) {
      const filename = item.filename || "Document";
      const extension = filename.includes(".") ? filename.split(".").pop().toLowerCase() : "file";
      const link = el("a", "document-item");
      link.href = safeFileUrl(item.url);
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = filename;
      link.setAttribute("aria-label", `Open ${filename} in a new tab`);
      const badge = el("span", `document-type ${["png", "jpg", "csv", "xlsx"].includes(extension) ? extension : ""}`, extension.slice(0, 5).toUpperCase());
      const info = el("div", "document-info");
      const meta = el("div", "document-meta");
      meta.append(el("span", "", item.loan_id || "Portfolio"));
      if (item.created_at) meta.append(el("span", "", timeLabel(item.created_at)));
      info.append(el("p", "document-name", filename), meta);
      link.append(badge, info, icon("download"));
      list.append(link);
    }
  }

  function renderState() {
    $("metric-loans").textContent = String(state.loans.length);
    const waiting = state.loans.filter((loan) => ["escalated", "restructure_requested"].includes(loan.status));
    $("metric-pending").textContent = String(waiting.length);
    $("queue-count").textContent = String(waiting.length);
    const select = $("loan-select");
    select.replaceChildren();
    for (const loan of state.loans) {
      const option = el("option", "", `${loan.id} · ${loan.name || loan.applicant_name || "Application"}`);
      option.value = String(loan.id);
      select.append(option);
    }
    if (!state.loans.some((loan) => String(loan.id) === selectedLoan) && state.loans.length) selectedLoan = String(state.loans[0].id);
    if (!state.loans.length) {
      const option = el("option", "", `${selectedLoan} · Application`);
      option.value = selectedLoan;
      select.append(option);
    }
    select.value = selectedLoan;
    renderContext();
    renderEscalated();
  }

  function renderEscalated() {
    const cases = state.loans.filter((loan) => ["escalated", "restructure_requested"].includes(loan.status));
    $("escalated-count").textContent = String(cases.length);
    const isRiskHead = $("role-select").value === "risk_head";
    $("escalated-help").textContent = isRiskHead ? "Record a specific reason for every exception decision." : "Select the Risk head demo role to decide credit exceptions.";
    const list = $("escalated-list");
    const active = document.activeElement;
    const editing = active?.closest(".exception-form");
    if (editing && ["INPUT", "TEXTAREA"].includes(active.tagName) && cases.some((loan) => loan.id === editing.dataset.exceptionLoan)) return;
    list.replaceChildren();
    if (!cases.length) {
      list.append(el("p", "escalated-empty", "No exceptions waiting. Review L-1045 to see a policy escalation."));
      return;
    }
    for (const loan of cases) {
      const card = el("article", "escalated-case");
      const focus = el("button", "escalated-focus", `${loan.id} · ${loan.name}`);
      focus.type = "button";
      focus.dataset.selectLoan = loan.id;
      card.append(focus, el("p", "escalated-reason", loan.servicing_escalation?.rule || loan.underwriting?.rule || "Policy exception"));
      const note = allDocuments().find((item) => item.loan_id === loan.id && item.kind === "decision_note");
      if (note) card.append(fileLink(note));
      if (loan.status === "restructure_requested") {
        card.append(el("p", "escalated-reason", "Hardship request for risk-head review. Revised terms require a separate servicing process."));
      } else {
        const draft = exceptionDrafts.get(loan.id) || {};
        const form = el("form", "exception-form");
        form.dataset.exceptionLoan = loan.id;
        const choiceLabel = el("label", "", "Decision");
        const choice = el("select");
        choice.name = "decision";
        for (const value of ["approve", "decline"]) { const option = el("option", "", humanize(value)); option.value = value; choice.append(option); }
        choice.value = draft.decision || "approve";
        choiceLabel.append(choice);
        const amountLabel = el("label", "", "Approval amount (INR)");
        const amount = el("input");
        amount.name = "amount"; amount.type = "number"; amount.min = "0.01"; amount.step = "0.01";
        amount.max = String(Math.min(loan.requested_amount, loan.credit?.eligible_amount || 0));
        amount.value = draft.amount || String(Math.min(200000, Number(amount.max)));
        amount.required = choice.value === "approve";
        amountLabel.append(amount);
        const reasonLabel = el("label", "", "Reason (required)");
        const reason = el("textarea");
        reason.name = "reason"; reason.required = true; reason.minLength = 10; reason.maxLength = 1500; reason.rows = 2;
        reason.placeholder = "Explain the evidence supporting this decision";
        reason.value = draft.reason || "";
        reasonLabel.append(reason);
        const submit = el("button", "send-button", "Record decision");
        submit.type = "submit";
        for (const field of [choice, amount, reason, submit]) field.disabled = !isRiskHead;
        form.append(choiceLabel, amountLabel, reasonLabel, submit);
        form.addEventListener("input", () => {
          amount.required = choice.value === "approve";
          exceptionDrafts.set(loan.id, {decision: choice.value, amount: amount.value, reason: reason.value});
        });
        form.addEventListener("submit", (event) => {
          event.preventDefault();
          if (!form.reportValidity()) return;
          const terms = choice.value === "approve" ? ` at ${amount.value}` : "";
          sendMessage(`${choice.value} ${loan.id}${terms} because ${reason.value.trim()}`);
        });
        card.append(form);
      }
      list.append(card);
    }
  }

  async function responseData(response) {
    let data;
    try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) {
      let detail = data.detail || data.message || data.error;
      if (Array.isArray(detail)) detail = detail.map((item) => item.msg || "Invalid request").join("; ");
      if (typeof detail !== "string") detail = null;
      throw new Error(detail || (response.status === 429 ? "The request limit has been reached. Please try again shortly." : `The office could not complete the request (${response.status}). Please try again.`));
    }
    return data;
  }

  async function refreshState() {
    if (refreshPromise) return refreshPromise;
    refreshPromise = (async () => {
      try {
        const data = await responseData(await fetch("/api/state", { headers: { Accept: "application/json" } }));
        state = { ...data, loans: Array.isArray(data.loans) ? data.loans : [], documents: Array.isArray(data.documents) ? data.documents : [] };
        renderState();
        notice();
      } catch (error) {
        notice(`The portfolio could not be refreshed. ${error.message}`);
      } finally {
        refreshPromise = null;
      }
    })();
    return refreshPromise;
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refreshState, 350);
  }

  function setPosting(value) {
    posting = value;
    $("send-button").disabled = value;
    $("send-button").querySelector("span").textContent = value ? "Sending…" : "Send";
    document.querySelectorAll("[data-command]").forEach((button) => { button.disabled = value; });
  }

  function setWorkStatus(text) {
    $("work-status").hidden = !text;
    if (text) $("work-status-text").textContent = text;
  }

  function markRequestAccepted(jobId) {
    if (!jobId || finishedJobs.has(jobId)) return;
    pendingJobs.add(jobId);
    setWorkStatus("Request accepted. The team is working in the background…");
    clearTimeout(pendingTimer);
    pendingTimer = setTimeout(() => {
      if (pendingJobs.size) setWorkStatus("Work may still be running. Follow its progress in the activity feed.");
    }, 60000);
  }

  async function sendMessage(message) {
    const text = String(message || "").trim();
    if (!text || posting) return;
    if (text.length > 8000) {
      showToast("Please keep your message under 8,000 characters.", true);
      return;
    }
    const role = $("role-select").value;
    const loanMatch = text.match(/\bL-\d+\b/i);
    if (loanMatch && state.loans.some((loan) => String(loan.id) === loanMatch[0].toUpperCase())) selectLoan(loanMatch[0].toUpperCase());
    setPosting(true);
    addMessage(text, { type: "user", author: `${roleNames[role]} · You` });
    $("message-input").value = "";
    $("message-input").style.height = "";
    try {
      const result = await responseData(await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ message: text, role, conversation_id: conversationId }),
      }));
      if (result.accepted === false) throw new Error(result.message || "Your request was not accepted. Please try again.");
      markRequestAccepted(result.job_id);
      scheduleRefresh();
    } catch (error) {
      addMessage(error.message, { type: "error", author: "Office status" });
      showToast(error.message, true);
      if (!$("message-input").value) $("message-input").value = text;
    } finally {
      setPosting(false);
      $("message-input").focus({ preventScroll: true });
    }
  }

  async function uploadFile(file) {
    if (!file || uploading) return;
    const allowed = ["csv", "json", "pdf", "png", "jpg", "jpeg", "docx", "xlsx"];
    const extension = file.name.split(".").pop().toLowerCase();
    if (!allowed.includes(extension)) {
      showToast("Choose a CSV, JSON, PDF, PNG, JPG, DOCX, or XLSX document.", true);
      $("file-input").value = "";
      return;
    }
    if (file.size > 5 * 1024 * 1024) {
      showToast("This file is larger than 5 MB. Please choose a smaller file.", true);
      $("file-input").value = "";
      return;
    }
    const loanId = selectedLoan;
    const role = $("role-select").value;
    const form = new FormData();
    form.append("file", file);
    form.append("loan_id", loanId);
    form.append("role", role);
    form.append("conversation_id", conversationId);
    uploading = true;
    $("upload-button").disabled = true;
    $("upload-button").querySelector("span").textContent = "Uploading…";
    try {
      const result = await responseData(await fetch("/api/upload", { method: "POST", body: form }));
      if (result.accepted === false) throw new Error(result.message || "The document was not accepted.");
      addMessage(`Attached ${file.name} to ${loanId}.`, { type: "user", author: `${roleNames[role]} · You` });
      markRequestAccepted(result.job_id);
      showToast(`Document received for ${loanId}.`);
      scheduleRefresh();
    } catch (error) {
      addMessage(`The upload could not be completed. ${error.message}`, { type: "error", author: "Office status" });
      showToast(error.message, true);
    } finally {
      uploading = false;
      $("upload-button").disabled = false;
      $("upload-button").querySelector("span").textContent = "Attach document";
      $("file-input").value = "";
    }
  }

  function addActivity(event) {
    $("activity-empty")?.remove();
    const feed = $("activity-feed");
    const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 70;
    const kind = ["reply", "file", "handoff", "tool", "status", "progress", "error", "mention", "office_action"].includes(event.kind) ? event.kind : "status";
    const item = el("article", `activity-item ${kind}`);
    const meta = el("div", "activity-meta");
    meta.append(el("span", "activity-agent", event.agent || "Office"), el("time", "activity-time", timeLabel(event.timestamp)));
    item.append(meta, el("div", "activity-text", event.text || event.data?.tool || humanize(kind)));
    const kindLabel = event.loan_id ? `${humanize(kind)} · ${event.loan_id}` : humanize(kind);
    item.append(el("span", "activity-kind", kindLabel));
    const progress = event.data?.progress ?? event.data?.percent;
    if (kind === "progress" && typeof progress === "number" && Number.isFinite(progress)) {
      const bar = el("progress", "event-progress");
      bar.max = 100;
      bar.value = Math.min(100, Math.max(0, progress));
      bar.setAttribute("aria-label", "Batch review progress");
      item.append(bar);
    }
    feed.append(item);
    eventCount += 1;
    $("activity-count").textContent = String(eventCount);
    while (feed.children.length > 250) feed.firstElementChild.remove();
    if (nearBottom) feed.scrollTop = feed.scrollHeight;
  }

  function highlightAgent(agent) {
    if (!agent) return;
    const name = String(agent).toLowerCase().replace(/_/g, " ");
    const roster = Array.from(document.querySelectorAll(".roster-agent")).find((item) => item.dataset.agent === name);
    if (!roster) return;
    clearTimeout(agentTimers.get(name));
    roster.classList.add("working");
    agentTimers.set(name, setTimeout(() => { roster.classList.remove("working"); }, 5000));
  }

  function handleEvent(event) {
    if (!event || typeof event !== "object") return;
    if (event.conversation_id && ![conversationId, "office"].includes(event.conversation_id)) return;
    if (event.id) {
      const id = String(event.id);
      if (seenEvents.has(id)) return;
      seenEvents.add(id);
    }
    if (["heartbeat", "ping", "connected"].includes(event.kind)) return;
    addActivity(event);
    highlightAgent(event.agent);
    if (event.kind === "reply" || event.kind === "mention" || event.kind === "error") {
      addMessage(event.text || "An update is available in the activity feed.", { author: event.agent || "Front Desk", timestamp: event.timestamp, type: event.kind === "error" ? "error" : "agent" });
    }
    if (event.kind === "file") {
      const data = event.data?.document || event.data || event;
      const documentInfo = {
        filename: data.filename || event.filename || "Document",
        url: data.url || event.url,
        loan_id: data.loan_id || event.loan_id,
        kind: data.kind,
        created_at: data.created_at || event.timestamp,
      };
      if (safeFileUrl(documentInfo.url)) {
        localDocuments.set(documentInfo.url, documentInfo);
        renderDocuments();
        addMessage(event.text || "Your document is ready.", { author: event.agent || "Reporter", timestamp: event.timestamp, document: documentInfo });
      }
    }
    if (event.kind === "progress") setWorkStatus(event.text || "The team is working through the review queue…");
    const terminal = event.data?.done === true || event.data?.complete === true || ["completed", "complete", "finished", "failed", "interrupted"].includes(event.data?.status);
    if (terminal) {
      if (event.data?.job_id) {
        finishedJobs.add(event.data.job_id);
        pendingJobs.delete(event.data.job_id);
      }
      if (!pendingJobs.size) {
        clearTimeout(pendingTimer);
        setWorkStatus("");
      }
    }
    scheduleRefresh();
  }

  function setConnection(status) {
    $("connection-state").className = `connection-state ${status}`;
    $("connection-label").textContent = status === "connected" ? "Office connected" : status === "connecting" ? "Connecting" : "Reconnecting…";
    $("activity-live-label").textContent = status === "connected" ? "LIVE" : "OFFLINE";
    $("activity-live-label").classList.toggle("offline", status !== "connected");
    if (status === "disconnected") setWorkStatus("Live updates disconnected. Reconnecting automatically…");
  }

  function connectEvents() {
    if (!("EventSource" in window)) {
      setConnection("disconnected");
      notice("This browser does not support live office updates. Open the console in a browser with Server-Sent Events support.");
      return;
    }
    setConnection("connecting");
    eventStream = new EventSource(`/api/events?conversation_id=${encodeURIComponent(conversationId)}`);
    eventStream.onopen = () => {
      setConnection("connected");
      if (!pendingJobs.size) setWorkStatus("");
      scheduleRefresh();
    };
    eventStream.onmessage = (message) => {
      try { handleEvent(JSON.parse(message.data)); } catch (_) {
        notice("One office update could not be read. The connection is still active; the portfolio will refresh automatically.");
        scheduleRefresh();
      }
    };
    eventStream.onerror = () => { setConnection("disconnected"); };
  }

  document.addEventListener("click", (event) => {
    const commandButton = event.target.closest("[data-command]");
    if (commandButton) {
      event.preventDefault();
      sendMessage(commandButton.dataset.command);
      return;
    }
    const loanButton = event.target.closest("[data-select-loan]");
    if (loanButton) {
      selectLoan(loanButton.dataset.selectLoan);
      $("loan-select").focus({ preventScroll: true });
    }
  });
  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage($("message-input").value);
  });
  $("message-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendMessage(event.currentTarget.value);
    }
  });
  $("message-input").addEventListener("input", (event) => {
    event.currentTarget.style.height = "auto";
    event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 160)}px`;
  });
  $("loan-select").addEventListener("change", (event) => { selectLoan(event.currentTarget.value); });
  $("role-select").addEventListener("change", (event) => {
    const role = event.currentTarget.value;
    $("role-avatar").textContent = roleInitials[role];
    renderEscalated();
    showToast(`Demo role changed to ${roleNames[role].toLowerCase()}. This is a synthetic identity, not authentication.`);
  });
  $("document-filter").addEventListener("change", renderDocuments);
  $("upload-button").addEventListener("click", () => { $("file-input").click(); });
  $("file-input").addEventListener("change", (event) => { uploadFile(event.currentTarget.files[0]); });
  window.addEventListener("online", () => { refreshState(); });
  window.addEventListener("offline", () => { setConnection("disconnected"); });
  window.addEventListener("pagehide", () => { eventStream?.close(); });
  window.addEventListener("pageshow", (event) => { if (event.persisted) connectEvents(); });
  $("today-label").textContent = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" }).format(new Date());
  renderContext();
  refreshState();
  connectEvents();
})();
