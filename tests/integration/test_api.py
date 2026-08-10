import pytest


def _create(client, headers, scenario_id="steady-rise", **extra):
    return client.post(
        "/api/sessions",
        headers=headers,
        json={"scenario_id": scenario_id, **extra},
    )


@pytest.mark.integration
def test_health_and_scenarios_are_public_and_security_hardened(client) -> None:
    health = client.get("/api/health")
    scenarios = client.get("/api/scenarios")

    assert health.status_code == 200
    assert health.json()["data"]["mode"] == "PAPER_ONLY"
    assert scenarios.status_code == 200
    assert {item["scenario_id"] for item in scenarios.json()["data"]} >= {
        "steady-rise",
        "stale-price-veto",
        "insufficient-evidence",
    }
    assert health.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in health.headers["content-security-policy"]
    assert health.headers["cache-control"] == "no-store"


@pytest.mark.integration
def test_session_stops_for_human_approval_then_completes_after_approved_fill(
    client, mutation_headers
) -> None:
    created = _create(client, mutation_headers)

    assert created.status_code == 201
    session = created.json()["data"]
    assert session["status"] == "AWAITING_APPROVAL"
    assert session["outcome"] is None
    assert session["proposal"]["signal_ids"]
    assert session["risk_decision"]["approved"] is True
    assert session["fills"] == []

    approved = client.post(
        f"/api/sessions/{session['session_id']}/approve",
        headers=mutation_headers,
        json={
            "actor": "recruiter-demo",
            "decision_id": "approve-demo-001",
            "expected_version": session["version"],
            "approval_digest": session["approval_binding"]["digest"],
        },
    )

    assert approved.status_code == 200
    final = approved.json()["data"]
    assert final["status"] == "COMPLETE"
    assert final["outcome"] == "COMPLETE"
    assert len(final["fills"]) == 1
    assert final["fills"][0]["simulated"] is True
    assert final["reconciliation"]["balanced"] is True
    assert final["portfolio"]["cash"] != session["portfolio"]["cash"]
    assert [event["sequence"] for event in final["events"]] == list(range(1, final["version"] + 1))


@pytest.mark.integration
def test_human_rejection_halts_without_execution(client, mutation_headers) -> None:
    session = _create(client, mutation_headers).json()["data"]

    rejected = client.post(
        f"/api/sessions/{session['session_id']}/reject",
        headers=mutation_headers,
        json={
            "actor": "risk-reviewer",
            "decision_id": "reject-demo-001",
            "expected_version": session["version"],
            "approval_digest": session["approval_binding"]["digest"],
            "reason": "Evidence is not persuasive enough for this demonstration.",
        },
    )

    assert rejected.status_code == 200
    result = rejected.json()["data"]
    assert result["status"] == "REJECTED"
    assert result["outcome"] == "HALTED"
    assert result["fills"] == []


@pytest.mark.integration
@pytest.mark.parametrize(
    ("scenario_id", "status", "evidence_field"),
    [
        ("stale-price-veto", "RISK_VETOED", "risk_decision"),
        ("insufficient-evidence", "INSUFFICIENT_EVIDENCE", "analysis"),
    ],
)
def test_safety_stop_states_are_explicit(
    client, mutation_headers, scenario_id, status, evidence_field
) -> None:
    response = _create(client, mutation_headers, scenario_id)

    assert response.status_code == 201
    session = response.json()["data"]
    assert session["status"] == status
    assert session["outcome"] == "HALTED"
    assert session[evidence_field] is not None
    assert session["fills"] == []


@pytest.mark.integration
def test_public_create_cannot_relax_server_risk_policy(client, mutation_headers) -> None:
    attempted_override = _create(
        client,
        mutation_headers,
        "stale-price-veto",
        risk_limits={"max_price_age_seconds": 3_600},
    )

    assert attempted_override.status_code == 422
    assert attempted_override.json()["error"] == {
        "code": "INVALID_REQUEST",
        "message": "Request validation failed.",
    }

    fixed_policy = _create(client, mutation_headers, "stale-price-veto")
    assert fixed_policy.status_code == 201
    session = fixed_policy.json()["data"]
    assert session["status"] == "RISK_VETOED"
    assert session["risk_limits"]["policy_version"] == "risk-policy-v1"
    assert session["risk_limits"]["max_price_age_seconds"] == 300
    assert session["approval_binding"] is None
    assert session["orders"] == []
    assert session["fills"] == []
    assert {item["code"] for item in session["risk_decision"]["violations"]} == {"STALE_PRICE"}


@pytest.mark.integration
def test_mutations_require_local_csrf_header_and_valid_content_size(
    client, mutation_headers
) -> None:
    missing_header = client.post("/api/sessions", json={"scenario_id": "steady-rise"})
    too_large = client.post(
        "/api/sessions",
        headers={**mutation_headers, "Content-Length": "20000"},
        content=b"{}",
    )

    assert missing_header.status_code == 403
    assert missing_header.json() == {
        "success": False,
        "error": {"code": "FORBIDDEN", "message": "Mutation request was rejected."},
    }
    assert too_large.status_code == 413


@pytest.mark.integration
def test_body_limit_counts_received_bytes_instead_of_trusting_declared_size(
    client, mutation_headers
) -> None:
    dishonest = client.post(
        "/api/sessions",
        headers={
            **mutation_headers,
            "Content-Type": "application/json",
            "Content-Length": "2",
        },
        content=b'{"padding":"' + (b"x" * 17_000) + b'"}',
    )

    assert dishonest.status_code == 413
    assert dishonest.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


@pytest.mark.integration
def test_validation_not_found_and_transition_errors_are_generic(client, mutation_headers) -> None:
    invalid = _create(client, mutation_headers, scenario_id="bad id!")
    missing = client.get("/api/sessions/not-a-real-session")
    unknown_scenario = _create(client, mutation_headers, scenario_id="unknown-scenario")
    session = _create(client, mutation_headers).json()["data"]
    conflict = client.post(
        f"/api/sessions/{session['session_id']}/approve",
        headers=mutation_headers,
        json={
            "actor": "demo",
            "decision_id": "decision-conflict-001",
            "expected_version": 0,
            "approval_digest": session["approval_binding"]["digest"],
        },
    )

    assert invalid.status_code == 422
    assert invalid.json()["error"] == {
        "code": "INVALID_REQUEST",
        "message": "Request validation failed.",
    }
    assert missing.status_code == 404
    assert unknown_scenario.status_code == 404
    assert conflict.status_code == 409
    assert "traceback" not in conflict.text.lower()


@pytest.mark.integration
def test_mutation_rate_limit_is_enforced() -> None:
    from fastapi.testclient import TestClient

    from risk_ledger.api import create_app

    with TestClient(create_app(mutation_limit=1)) as limited_client:
        headers = {"X-RiskLedger-Client": "local-web"}
        first = _create(limited_client, headers)
        second = _create(limited_client, headers)

    assert first.status_code == 201
    assert second.status_code == 429
    assert second.headers["retry-after"]


@pytest.mark.integration
def test_approval_is_bound_to_exact_artifacts_and_veto_cannot_be_overridden(
    client, mutation_headers
) -> None:
    awaiting = _create(client, mutation_headers).json()["data"]
    wrong_binding = client.post(
        f"/api/sessions/{awaiting['session_id']}/approve",
        headers=mutation_headers,
        json={
            "actor": "demo",
            "decision_id": "wrong-binding-001",
            "expected_version": awaiting["version"],
            "approval_digest": "a" * 64,
        },
    )
    vetoed = _create(client, mutation_headers, "stale-price-veto").json()["data"]
    veto_override = client.post(
        f"/api/sessions/{vetoed['session_id']}/approve",
        headers=mutation_headers,
        json={
            "actor": "demo",
            "decision_id": "veto-override-001",
            "expected_version": vetoed["version"],
            "approval_digest": "b" * 64,
        },
    )

    assert wrong_binding.status_code == 409
    assert veto_override.status_code == 409
    unchanged = client.get(f"/api/sessions/{vetoed['session_id']}").json()["data"]
    assert unchanged["status"] == "RISK_VETOED"
    assert unchanged["fills"] == []


@pytest.mark.integration
def test_expired_approval_fails_closed_and_records_terminal_no_trade() -> None:
    from datetime import UTC, datetime, timedelta

    from fastapi.testclient import TestClient

    from risk_ledger.api import create_app
    from risk_ledger.service import SessionService

    class MutableClock:
        def __init__(self) -> None:
            self.current = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

        def __call__(self) -> datetime:
            return self.current

    clock = MutableClock()
    service = SessionService(clock=clock)
    with TestClient(create_app(service=service, mutation_limit=100)) as expiring_client:
        headers = {"X-RiskLedger-Client": "local-web"}
        session = _create(expiring_client, headers).json()["data"]
        clock.current += timedelta(seconds=901)
        expired = expiring_client.post(
            f"/api/sessions/{session['session_id']}/approve",
            headers=headers,
            json={
                "actor": "demo",
                "decision_id": "expired-approval-001",
                "expected_version": session["version"],
                "approval_digest": session["approval_binding"]["digest"],
            },
        )
        final = expiring_client.get(f"/api/sessions/{session['session_id']}").json()["data"]

    assert expired.status_code == 409
    assert expired.json()["error"]["code"] == "APPROVAL_EXPIRED"
    assert final["status"] == "APPROVAL_EXPIRED"
    assert final["outcome"] == "HALTED"
    assert final["fills"] == []


@pytest.mark.integration
def test_session_service_does_not_accept_a_risk_engine_policy_override() -> None:
    from typing import Any

    from risk_ledger.service import SessionService

    class ForgedApprovingRiskEngine:
        def evaluate(self, **_kwargs: Any) -> None:
            raise AssertionError("A caller-supplied risk engine must never be installed")

    with pytest.raises(TypeError):
        SessionService(risk_engine=ForgedApprovingRiskEngine())  # type: ignore[call-arg]


@pytest.mark.integration
def test_risk_engine_exception_becomes_error_veto_without_approval_or_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any

    from fastapi.testclient import TestClient

    from risk_ledger.api import create_app
    from risk_ledger.risk import DeterministicRiskEngine
    from risk_ledger.service import SessionService

    def broken_evaluate(_engine: DeterministicRiskEngine, **_kwargs: Any) -> None:
        raise ValueError("malformed judge data")

    monkeypatch.setattr(DeterministicRiskEngine, "evaluate", broken_evaluate)
    service = SessionService()
    with TestClient(create_app(service=service, mutation_limit=100)) as closed_client:
        response = _create(closed_client, {"X-RiskLedger-Client": "local-web"})

    session = response.json()["data"]
    assert session["status"] == "RISK_VETOED"
    assert session["risk_decision"]["verdict"] == "ERROR_VETO"
    assert session["approval_binding"] is None
    assert session["fills"] == []
