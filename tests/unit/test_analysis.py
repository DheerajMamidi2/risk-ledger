import pytest

from risk_ledger.analysis import StrategyAnalyzer
from risk_ledger.scenarios import ScenarioCatalog


@pytest.mark.unit
def test_strategy_analysis_creates_proposal_with_existing_signal_citations() -> None:
    scenario = ScenarioCatalog.bundled().get("steady-rise")

    result = StrategyAnalyzer().analyze(scenario)

    assert result.sufficient_evidence is True
    assert result.proposal is not None
    assert len(result.signals) >= 2
    assert set(result.proposal.signal_ids) == {signal.signal_id for signal in result.signals}
    cited_bar_ids = {bar_id for signal in result.signals for bar_id in signal.bar_ids}
    assert cited_bar_ids
    assert cited_bar_ids <= {bar.bar_id for bar in scenario.analysis_bars}


@pytest.mark.unit
def test_analysis_is_deterministic_for_same_replay() -> None:
    scenario = ScenarioCatalog.bundled().get("steady-rise")
    analyzer = StrategyAnalyzer()

    first = analyzer.analyze(scenario)
    second = analyzer.analyze(scenario)

    assert first == second


@pytest.mark.unit
def test_analysis_cannot_see_or_be_changed_by_future_execution_bar() -> None:
    scenario = ScenarioCatalog.bundled().get("steady-rise")
    changed_future = scenario.model_copy(
        update={
            "execution_bar": scenario.execution_bar.model_copy(
                update={
                    "open": scenario.execution_bar.open * 100,
                    "close": scenario.execution_bar.close * 100,
                }
            )
        }
    )

    first = StrategyAnalyzer().analyze(scenario)
    second = StrategyAnalyzer().analyze(changed_future)

    assert first == second


@pytest.mark.unit
def test_flat_short_replay_stops_with_insufficient_evidence() -> None:
    scenario = ScenarioCatalog.bundled().get("insufficient-evidence")

    result = StrategyAnalyzer().analyze(scenario)

    assert result.sufficient_evidence is False
    assert result.proposal is None
    assert result.insufficient_reason == "At least five analysis bars are required."
