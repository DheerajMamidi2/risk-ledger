import pytest


@pytest.mark.e2e
def test_recruiter_demo_page_and_full_api_journey(client, mutation_headers) -> None:
    page = client.get("/")

    assert page.status_code == 200
    assert "RiskLedger" in page.text
    assert "Paper-only simulation" in page.text
    assert "/assets/app.js" in page.text
    assert "financial advice" in page.text.lower()

    created = client.post(
        "/api/sessions",
        headers=mutation_headers,
        json={"scenario_id": "steady-rise"},
    ).json()["data"]
    fetched = client.get(f"/api/sessions/{created['session_id']}").json()["data"]

    assert fetched == created
    assert fetched["events"][-1]["status"] == "AWAITING_APPROVAL"
