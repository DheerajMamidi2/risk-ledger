# RiskLedger Interview Guide

This guide is a compact narrative for a recruiter screen, engineering interview, or portfolio walkthrough. Lead with the controls, then show the happy path. The strongest result in this project is often a safe no-trade decision.

## 30-second pitch

> RiskLedger is a deterministic, event-sourced paper-trading replay that demonstrates how I design bounded, safety-critical automation. It analyzes a fixed historical scenario without look-ahead, applies a separate deterministic risk engine, pauses on a digest-bound human approval, simulates locally, and independently reconciles the result. There is no broker connection, real money, credential, financial advice, or profitability claim.

## What to emphasize

- **Determinism:** identical scenario data and locked inputs produce identical analysis, risk, simulated execution, and reconciliation results.
- **Bounded agency:** steps, retries, active elapsed time, cost units, and a locked approval TTL (15 minutes by default) all have explicit limits.
- **Separation of authority:** the analyzer proposes, the risk engine can veto, the human can reject or approve only an exact risk-passed artifact, and the reconciler decides whether accounting balanced.
- **Event provenance:** state is a projection of ordered, hash-chained immutable events; optimistic versions reject stale commands.
- **Structural paper-only scope:** no production broker adapter, broker hostname, credential field, or real-money route exists.
- **Honest claims:** the synthetic fixtures prove software invariants, not predictive performance.

## Suggested five-minute walkthrough

### 0:00–0:45 — Establish the boundary

Point to the permanent paper-only disclosure and `/api/health` mode. Explain that removing live connectivity is an architectural control, not a boolean configuration that could be flipped accidentally.

### 0:45–1:30 — Show a model that can abstain

Run `insufficient-evidence`. The expected outcome is `INSUFFICIENT_EVIDENCE` with no proposal and no fill. Explain that “no trade” is a valid successful terminal outcome, so the workflow has no incentive to manufacture an action.

### 1:30–2:15 — Show an independent veto

Run `stale-price-veto`. Inspect the risk violation and the absent approval action. A human cannot override this state; only `AWAITING_APPROVAL` accepts an approval or rejection command.

### 2:15–3:30 — Inspect evidence and approval binding

Run `steady-rise`. Trace signal IDs back to source bar IDs, then show the proposal, portfolio, risk decision, event-stream version, and approval digest. Explain that changing the reviewed artifact makes the submitted approval stale.

### 3:30–4:30 — Approve a local simulation

Approve the exact digest. Show the `LOCAL_PAPER_SIMULATOR` fill and the independent reconciliation. `COMPLETE` means the accounting balanced; merely attempting an order is not success.

### 4:30–5:00 — Close on auditability

Walk down the monotonically sequenced, hash-chained event timeline and budget counters. Then state the limitations: fixed synthetic data, in-memory event storage, simple fill model, and local-only request controls.

## Whiteboard narrative

```mermaid
sequenceDiagram
    actor Human
    participant API as Local API
    participant Analyzer as Deterministic analyzer
    participant Risk as Independent risk engine
    participant Events as Append-only events
    participant Sim as Paper simulator
    participant Recon as Independent reconciler

    Human->>API: Start bundled scenario
    API->>Analyzer: Sealed scenario; read pre-decision bars only
    Analyzer-->>API: Signals + cited proposal or abstain
    API->>Risk: Frozen proposal + portfolio + policy
    Risk-->>API: Pass or non-overridable veto
    API->>Events: Append analysis and risk facts
    API-->>Human: AWAITING_APPROVAL + digest + version
    Human->>API: Approve exact digest and version
    API->>Sim: Execute locally after revalidation
    Sim-->>API: Simulated fill
    API->>Recon: Prior portfolio + fill + projected portfolio
    Recon-->>API: Balanced or mismatch
    API->>Events: Append fill, reconciliation, terminal event
    API-->>Human: COMPLETE or FAILED_CLOSED
```

The proposal generator never serves as its own judge. The UI is a command surface and read model, not an authority: the server rechecks state, expected version, and approval digest.

## Failure modes worth demonstrating

| Failure or attack | Expected behavior | Verification |
|---|---|---|
| Future execution bar is changed | Proposal remains unchanged | No-look-ahead unit test |
| Scenario data is malformed or crosses the decision boundary | Loader fails closed | Scenario validation tests |
| Price is stale or a locked limit is exceeded | Stable risk code, `RISK_VETOED`, zero fills | Risk threshold and API tests |
| Approval digest is altered | HTTP conflict; session remains unexecuted | Approval-binding integration test |
| Client submits a stale expected version | HTTP conflict; no duplicate transition | Event-store and API conflict tests |
| Human rejects | `REJECTED`/`HALTED`, zero fills | Rejection integration test |
| Decision arrives after the locked TTL | The late command records `APPROVAL_EXPIRED`, zero fills | Injectable service clock and terminal expiry event |
| Work exceeds a configured cap | `FAILED_CLOSED` rather than unbounded retry | Budget unit tests |
| Projected portfolio is tampered with | Reconciliation reports mismatch | Ledger tamper test |
| Mutation header is missing, request is oversized, or rate is exceeded | Generic 403, 413, or 429 response | Boundary integration tests |

## Likely questions and concise answers

### “Why event sourcing for a small demo?”

The domain is a sequence of decisions whose provenance matters. An append-only hash chain makes ordering and payload integrity explicit, supports a transparent UI timeline, and lets the read model be rebuilt. Optimistic expected versions also turn stale approval attempts into conflicts instead of duplicate side effects. For the MVP the store is in memory; production durability is deliberately not implied.

### “What exactly is deterministic?”

Scenario parsing, decision-time visibility, signal generation, proposal content, risk codes, paper fill arithmetic, and reconciliation are functions of locked inputs. Money uses `Decimal`, and source data has a stable digest. A fresh operational session ID may differ; the domain decision does not.

### “How do you prevent look-ahead bias?”

The scenario schema enforces `analysis_bar.timestamp <= decision_time < execution_bar.timestamp`. Analyzer logic reads only `analysis_bars`. A test mutates the future execution bar by 100× and asserts that the proposal does not change.

### “Where is the AI?”

This MVP deliberately uses a deterministic proposer so the safety control plane can be demonstrated offline and tested at exact boundaries. A future probabilistic proposer could sit behind the same typed proposal, evidence, budget, risk, approval, and reconciliation contracts; it would never replace those deterministic authorities. The project claims safe workflow design, not model novelty.

### “Why isn't the risk engine another model?”

Risk thresholds should be explainable, testable at exact boundaries, and fail closed. The engine is pure deterministic code that emits stable violation codes. The analyzer cannot edit the policy or override a veto.

### “What does the human approval bind to?”

The server issues a digest for the exact reviewed session artifacts plus an event-stream version. Approval must return both before expiry. A wrong digest or stale version returns a conflict and produces no fill; a risk veto has no approval transition at all.

### “Why reconcile if execution is simulated?”

Execution and accounting can disagree even in a simulator because of implementation defects or tampering. A separate function reconstructs expected cash and position from the prior portfolio and fill. The session is complete only when this independent check balances.

### “Is the custom header authentication?”

No. `X-RiskLedger-Client: local-web` is a local CSRF-style mutation guard. Request-size and rate limits, CSP, `nosniff`, no-store responses, validation, and generic errors add defense in depth. A remotely hosted or multi-user version would require real identity, authorization, CSRF strategy, a trusted proxy model, durable throttling, and audit retention.

### “Why call it bounded?”

The workflow has caps on steps, retries, active elapsed milliseconds, and cost units. The human gate separately uses a session-locked TTL, defaulting to 15 minutes; expiry is recorded when a late decision arrives. A cap cannot be relaxed automatically; exhaustion transitions to a safe stop.

### “Would you connect this to a live broker?”

Not as an incremental toggle. Live execution changes the threat model, regulatory obligations, failure recovery, credential handling, and idempotency requirements. It would require a separate design and ADR. This repository intentionally contains no live path.

### “What would you build next?”

1. Persist events in an append-only database with transactional optimistic concurrency and integrity verification.
2. Add authenticated identities, role-based approval, signed approval records, and durable rate limits.
3. Expand property-based tests around risk boundaries, state transitions, and reconciliation invariants.
4. Add more realistic—but still explicitly simulated—fees, slippage, partial fills, and walk-forward evaluation.
5. Add observability for state transitions, stop reasons, latency, and budget consumption without logging sensitive payloads.

None of those steps requires or implies live trading.

## Technical choices to defend

| Choice | Why it fits the MVP | Trade-off accepted |
|---|---|---|
| FastAPI + Pydantic | Typed HTTP boundary, clear schemas, strong validation | In-process app is not a distributed architecture |
| Frozen domain models | Prevents accidental mutation of reviewed artifacts | Updates require explicit copies |
| `Decimal` money | Exact, reviewable accounting | More verbose than binary floats |
| Bundled scenario data | Offline, repeatable, no secret or feed dependency | Small synthetic sample has no market-validity claim |
| Pure risk and reconciliation functions | Independent, fast boundary tests | Policies and fill model are intentionally narrow |
| In-memory append-only event store | Makes event semantics visible with minimal infrastructure | Restart loses sessions; no multi-process coordination |
| Bundled local demo surface | One-command recruiter experience | Not production authentication or tenancy |

## Commands to keep ready

```bash
uv sync --locked --all-groups
uv run uvicorn risk_ledger.main:app --host 127.0.0.1 --port 8000 --reload
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov=risk_ledger --cov-report=term-missing --cov-fail-under=80
```

Avoid saying the project predicts returns, beats a benchmark, is safe for real capital, or provides financial advice. Say instead that it demonstrates deterministic replay, explicit authority boundaries, bounded execution, safe no-trade states, and independently checked accounting.
