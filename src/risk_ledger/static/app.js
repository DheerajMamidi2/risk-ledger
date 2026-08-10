"use strict";

const state = { session: null };
const byId = (id) => document.getElementById(id);
const mutationHeaders = { "Content-Type": "application/json", "X-RiskLedger-Client": "local-web" };

function text(element, value) {
  element.textContent = value;
}

function shortDigest(value) {
  return value ? `${value.slice(0, 12)}…${value.slice(-8)}` : "—";
}

function money(value) {
  if (value === undefined || value === null) return "—";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Number(value));
}

function showError(message) {
  const box = byId("error");
  text(box, message);
  box.hidden = false;
}

function clearError() {
  byId("error").hidden = true;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok || !payload.success) throw new Error(payload.error?.message || "Request failed.");
  return payload.data;
}

function statusClass(status) {
  if (status === "COMPLETE") return "complete";
  if (status === "AWAITING_APPROVAL") return "awaiting";
  if (["RISK_VETOED", "REJECTED", "FAILED_CLOSED", "APPROVAL_EXPIRED"].includes(status)) return "veto";
  return "neutral";
}

function renderSignals(session) {
  const proposal = session.proposal;
  byId("proposal-empty").hidden = Boolean(proposal);
  byId("proposal-content").hidden = !proposal;
  if (!proposal) return;
  text(byId("proposal-action"), `${proposal.side} ${proposal.quantity} ${proposal.symbol}`);
  text(byId("proposal-notional"), `${money(proposal.notional)} notional`);
  text(byId("proposal-rationale"), proposal.rationale);
  const signalList = byId("signals");
  signalList.replaceChildren();
  for (const signal of session.analysis?.signals || []) {
    const item = document.createElement("li");
    const title = document.createElement("strong");
    const detail = document.createElement("span");
    const citation = document.createElement("code");
    text(title, `${signal.kind} · ${signal.value}`);
    text(detail, ` ${signal.description}`);
    text(citation, `${signal.signal_id} → ${signal.bar_ids.join(", ")}`);
    item.append(title, detail, citation);
    signalList.append(item);
  }
}

function renderRisk(session) {
  const decision = session.risk_decision;
  byId("risk-empty").hidden = Boolean(decision);
  byId("risk-content").hidden = !decision;
  if (!decision) return;
  const verdict = byId("risk-verdict");
  text(verdict, decision.verdict === "PASS" ? "PASS · eligible for human review" : `${decision.verdict} · no trade`);
  verdict.className = `verdict ${decision.approved ? "pass" : "veto"}`;
  text(byId("policy-digest"), shortDigest(decision.policy_digest));
  const violations = byId("violations");
  violations.replaceChildren();
  if (!decision.violations.length) {
    const item = document.createElement("li");
    text(item, "All locked deterministic constraints passed. This is not approval.");
    violations.append(item);
  }
  for (const violation of decision.violations) {
    const item = document.createElement("li");
    text(item, `${violation.code}: ${violation.message} Observed ${violation.observed}; limit ${violation.limit}.`);
    violations.append(item);
  }
}

function renderFills(session) {
  const body = byId("fills");
  body.replaceChildren();
  if (!session.fills.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "empty-cell";
    text(cell, "No simulated fills.");
    row.append(cell);
    body.append(row);
  }
  for (const fill of session.fills) {
    const row = document.createElement("tr");
    for (const value of [shortDigest(fill.order_id), fill.side, fill.quantity, money(fill.price), money(fill.fee), fill.venue]) {
      const cell = document.createElement("td");
      text(cell, value);
      row.append(cell);
    }
    body.append(row);
  }
  const reconciliation = byId("reconciliation");
  if (session.reconciliation?.balanced) {
    text(reconciliation, "RECONCILED");
    reconciliation.className = "status complete";
  } else if (session.reconciliation) {
    text(reconciliation, "MISMATCH");
    reconciliation.className = "status veto";
  } else {
    text(reconciliation, "NO FILL");
    reconciliation.className = "status neutral";
  }
}

function renderTimeline(session) {
  const timeline = byId("timeline");
  timeline.replaceChildren();
  text(byId("event-count"), `${session.events.length} events`);
  for (const event of session.events) {
    const item = document.createElement("li");
    const sequence = document.createElement("span");
    const eventType = document.createElement("span");
    const reason = document.createElement("span");
    const hash = document.createElement("code");
    sequence.className = "sequence";
    eventType.className = "event-type";
    reason.className = "reason";
    text(sequence, event.sequence);
    text(eventType, event.event_type.replaceAll("_", " "));
    text(reason, event.reason || event.status);
    text(hash, shortDigest(event.event_hash));
    item.append(sequence, eventType, reason, hash);
    timeline.append(item);
  }
}

function render(session) {
  state.session = session;
  text(byId("session-title"), session.scenario.name);
  text(byId("session-summary"), `${session.scenario.symbol} · ${session.scenario.bar_count} sealed observations · ${session.outcome || "active"}`);
  const status = byId("status");
  text(status, session.status);
  status.className = `status ${statusClass(session.status)}`;
  text(byId("cash"), money(session.portfolio.cash));
  text(byId("equity"), money(session.portfolio.equity));
  text(byId("drawdown"), `${(Number(session.portfolio.drawdown_fraction) * 100).toFixed(2)}%`);
  text(byId("order-count"), session.portfolio.order_count);
  text(byId("dataset-digest"), shortDigest(session.dataset_digest));
  renderSignals(session);
  renderRisk(session);
  renderFills(session);
  renderTimeline(session);

  const awaiting = session.status === "AWAITING_APPROVAL" && session.approval_binding;
  byId("approval-card").hidden = !awaiting;
  byId("approve").disabled = !awaiting;
  byId("reject").disabled = !awaiting;
  if (awaiting) text(byId("approval-digest"), shortDigest(session.approval_binding.digest));
}

async function loadScenarios() {
  try {
    const scenarios = await api("/api/scenarios");
    const select = byId("scenario");
    select.replaceChildren();
    for (const scenario of scenarios) {
      const option = document.createElement("option");
      option.value = scenario.scenario_id;
      text(option, `${scenario.name} · ${scenario.symbol}`);
      select.append(option);
    }
  } catch (error) {
    showError(error.message);
  }
}

async function startSession() {
  clearError();
  const button = byId("start-session");
  button.disabled = true;
  try {
    const session = await api("/api/sessions", {
      method: "POST",
      headers: mutationHeaders,
      body: JSON.stringify({ scenario_id: byId("scenario").value }),
    });
    render(session);
  } catch (error) {
    showError(error.message);
  } finally {
    button.disabled = false;
  }
}

async function decide(action) {
  clearError();
  const session = state.session;
  if (!session?.approval_binding) return;
  const isApproval = action === "approve";
  const payload = {
    actor: byId("actor").value,
    decision_id: `${isApproval ? "approve" : "reject"}-${crypto.randomUUID()}`,
    expected_version: session.version,
    approval_digest: session.approval_binding.digest,
  };
  if (!isApproval) payload.reason = byId("reject-reason").value;
  byId("approve").disabled = true;
  byId("reject").disabled = true;
  try {
    render(await api(`/api/sessions/${session.session_id}/${action}`, {
      method: "POST",
      headers: mutationHeaders,
      body: JSON.stringify(payload),
    }));
  } catch (error) {
    showError(error.message);
    const refreshed = await api(`/api/sessions/${session.session_id}`);
    render(refreshed);
  }
}

byId("start-session").addEventListener("click", startSession);
byId("approve").addEventListener("click", () => decide("approve"));
byId("reject").addEventListener("click", () => decide("reject"));
loadScenarios();
