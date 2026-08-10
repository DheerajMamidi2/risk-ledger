"""Fail-closed loader for bundled deterministic replay scenarios."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, ValidationError

from risk_ledger.hashing import canonical_digest
from risk_ledger.models import ImmutableModel, ReplayScenario, ScenarioSummary

MAX_DATASET_BYTES = 1_000_000


class ScenarioDataError(ValueError):
    """Raised when sealed replay data is missing or malformed."""


class ScenarioNotFoundError(LookupError):
    """Raised when a requested bundled scenario does not exist."""


class DatasetEnvelope(ImmutableModel):
    dataset_version: Annotated[str, Field(min_length=3, max_length=80)]
    scenarios: Annotated[tuple[ReplayScenario, ...], Field(min_length=1, max_length=50)]


class ScenarioCatalog:
    """Immutable in-memory view over a validated, content-addressed dataset."""

    def __init__(self, envelope: DatasetEnvelope) -> None:
        scenario_ids = tuple(scenario.scenario_id for scenario in envelope.scenarios)
        if len(set(scenario_ids)) != len(scenario_ids):
            raise ScenarioDataError("Scenario IDs must be unique")
        self._envelope = envelope
        self._scenarios = {scenario.scenario_id: scenario for scenario in envelope.scenarios}
        self.dataset_digest = canonical_digest(envelope)

    @classmethod
    def bundled(cls) -> Self:
        return cls.from_path(Path(__file__).parent / "data" / "scenarios.json")

    @classmethod
    def from_path(cls, path: Path) -> Self:
        try:
            size = path.stat().st_size
            if size > MAX_DATASET_BYTES:
                raise ScenarioDataError("Replay dataset exceeds the size limit")
            raw = path.read_text(encoding="utf-8")
            decoded = json.loads(raw)
            envelope = DatasetEnvelope.model_validate(decoded)
            return cls(envelope)
        except ScenarioDataError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise ScenarioDataError("Replay dataset failed closed during validation") from exc

    def get(self, scenario_id: str) -> ReplayScenario:
        try:
            return self._scenarios[scenario_id]
        except KeyError as exc:
            raise ScenarioNotFoundError("Scenario was not found") from exc

    def list_summaries(self) -> tuple[ScenarioSummary, ...]:
        return tuple(
            ScenarioSummary(
                scenario_id=scenario.scenario_id,
                name=scenario.name,
                description=scenario.description,
                symbol=scenario.symbol,
                bar_count=len(scenario.analysis_bars),
                decision_time=scenario.decision_time,
            )
            for scenario in self._envelope.scenarios
        )
