from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from risk_ledger.api import create_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(mutation_limit=100)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def mutation_headers() -> dict[str, str]:
    return {"X-RiskLedger-Client": "local-web"}
