import json
from pathlib import Path

import pytest

from risk_ledger.scenarios import ScenarioCatalog, ScenarioDataError


@pytest.mark.unit
def test_bundled_dataset_has_stable_digest_and_strict_next_bar_boundary() -> None:
    first = ScenarioCatalog.bundled()
    second = ScenarioCatalog.bundled()
    scenario = first.get("steady-rise")

    assert first.dataset_digest == second.dataset_digest
    assert len(first.dataset_digest) == 64
    assert scenario.last_analysis_bar.timestamp <= scenario.decision_time
    assert scenario.execution_bar.timestamp > scenario.decision_time


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps({"dataset_version": "1", "scenarios": []}),
        json.dumps(
            {
                "dataset_version": "1",
                "scenarios": [
                    {
                        "scenario_id": "broken",
                        "name": "Broken",
                        "description": "Execution bar leaks into analysis.",
                        "symbol": "ACME",
                        "decision_time": "2026-01-05T15:30:00Z",
                        "initial_cash": "10000.00",
                        "analysis_bars": [
                            {
                                "bar_id": "FUTURE-1",
                                "timestamp": "2026-01-05T15:31:00Z",
                                "open": "1",
                                "high": "1",
                                "low": "1",
                                "close": "1",
                                "volume": 1,
                            }
                        ],
                        "execution_bar": {
                            "bar_id": "EXEC-1",
                            "timestamp": "2026-01-05T15:32:00Z",
                            "open": "1",
                            "high": "1",
                            "low": "1",
                            "close": "1",
                            "volume": 1,
                        },
                    }
                ],
            }
        ),
    ],
)
def test_missing_malformed_or_lookahead_dataset_fails_closed(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "bad-scenarios.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ScenarioDataError):
        ScenarioCatalog.from_path(path)
