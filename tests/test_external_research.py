from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

# pandas is optional in the host environment; the Kronos Docker sidecar installs it.
import pytest

from banknifty_foundation.banknifty_time_machine import BankniftyTimeMachine
from banknifty_foundation.external_research import HISTORICAL_COUNTERFACTUAL
from banknifty_foundation.kronos_experiment import KronosForecastAdapter
from banknifty_foundation.models import (Candle, DatasetRevision, Instrument, RevisionCandle,
    TradingSession)
from banknifty_foundation.repository import MemoryRepository


IST = timezone(timedelta(hours=5, minutes=30))


def _session(day: date) -> TradingSession:
    return TradingSession(day, datetime.combine(day, time(9, 15), IST).astimezone(timezone.utc),
                          datetime.combine(day, time(15, 30), IST).astimezone(timezone.utc), "OPEN")


def _candles(session: TradingSession, seed: int = 0) -> list[Candle]:
    result = []
    current = session.open_at
    index = 0
    while current < session.close_at:
        base = Decimal("50000") + Decimal(seed * 7) + Decimal(index) / Decimal("10")
        wobble = Decimal((index * 13 + seed * 17) % 11) / Decimal("100")
        close = base + wobble
        result.append(Candle(current, base, close + Decimal("0.4"), base - Decimal("0.4"),
                             close, 0, None))
        current += timedelta(minutes=1)
        index += 1
    return result


def _repository(prior_sessions: int = 21):
    repository = MemoryRepository()
    instrument = Instrument(1, "NSE", "BANKNIFTY", "BANKNIFTY", "INDICES", "EQ", None,
                            Decimal("0.05"), 1)
    repository.instruments[1] = instrument
    days = [date(2025, 1, 1) + timedelta(days=offset) for offset in range(prior_sessions + 1)]
    rows: list[RevisionCandle] = []
    for offset, day in enumerate(days):
        session = _session(day)
        repository.sessions[day] = session
        for candle in _candles(session, offset):
            rows.append(RevisionCandle(1, candle, "source-job"))
    revision = DatasetRevision("revision-1", datetime(2025, 3, 1, tzinfo=timezone.utc),
        (1,), days[0], days[-1], len(rows), {}, ("source-job",), "checksum", "ACCEPTED")
    repository.revisions[revision.id] = revision
    repository.revision_rows[revision.id] = tuple(rows)
    return repository, days


def test_time_machine_is_deterministic_and_uses_only_complete_prior_sessions():
    repository, days = _repository()
    reference = datetime.combine(days[-1], time(11, 0), IST).astimezone(timezone.utc)
    machine = BankniftyTimeMachine(repository, sample_paths=500, seed=99)
    first = machine.run(revision_id="revision-1", instrument_id=1, reference_minute=reference)
    second = BankniftyTimeMachine(repository, sample_paths=500, seed=99).run(
        revision_id="revision-1", instrument_id=1, reference_minute=reference)
    assert first.status == "COMPLETED"
    assert first.summary["priorSessionCount"] == 21
    assert first.summary["samplingPoolChecksum"] == second.summary["samplingPoolChecksum"]
    assert {key: value.to_dict() for key, value in first.horizons.items()} == {
        key: value.to_dict() for key, value in second.horizons.items()}
    assert all(item["timestamp"] > reference.isoformat() for item in first.forecast_path)
    assert first.point_in_time_status == HISTORICAL_COUNTERFACTUAL
    assert first.summary["volumeStatus"] == "INDEX_VOLUME_NOT_USED"


def test_time_machine_does_not_read_current_future_or_future_sessions():
    repository, days = _repository()
    reference = datetime.combine(days[-1], time(11, 0), IST).astimezone(timezone.utc)
    baseline = BankniftyTimeMachine(repository, sample_paths=200, seed=7).run(
        revision_id="revision-1", instrument_id=1, reference_minute=reference)
    rows = list(repository.revision_rows["revision-1"])
    mutated = []
    for row in rows:
        if row.candle.timestamp > reference:
            mutated.append(RevisionCandle(1, replace(row.candle,
                open=Decimal("90000"), high=Decimal("91000"), low=Decimal("89000"),
                close=Decimal("90000")), row.source_job_id))
        else:
            mutated.append(row)
    repository.revision_rows["revision-1"] = tuple(mutated)
    changed = BankniftyTimeMachine(repository, sample_paths=200, seed=7).run(
        revision_id="revision-1", instrument_id=1, reference_minute=reference)
    assert changed.summary["samplingPoolChecksum"] == baseline.summary["samplingPoolChecksum"]
    assert {key: value.to_dict() for key, value in changed.horizons.items()} == {
        key: value.to_dict() for key, value in baseline.horizons.items()}


def test_time_machine_handles_last_valid_reference_minute():
    repository, days = _repository()
    reference = datetime.combine(days[-1], time(14, 59), IST).astimezone(timezone.utc)
    result = BankniftyTimeMachine(repository, sample_paths=50, seed=5).run(
        revision_id="revision-1", instrument_id=1, reference_minute=reference)
    assert result.status == "COMPLETED"
    assert result.summary["referenceSlot"] == 344
    assert len(result.forecast_path) == 30


def test_time_machine_refuses_insufficient_prior_history():
    repository, days = _repository(prior_sessions=10)
    reference = datetime.combine(days[-1], time(11, 0), IST).astimezone(timezone.utc)
    result = BankniftyTimeMachine(repository, sample_paths=50).run(
        revision_id="revision-1", instrument_id=1, reference_minute=reference)
    assert result.status == "INSUFFICIENT_PRIOR_SESSIONS"
    assert result.summary["priorSessionCount"] == 10
    assert not result.horizons


class _StubPredictor:
    def predict(self, frame, x_timestamp, y_timestamp, **kwargs):
        import pandas as pd
        rows = []
        price = float(frame["close"].iloc[-1])
        for index in range(30):
            value = price * (1 + (index + 1) / 100000)
            rows.append({"open": value, "high": value + 0.2, "low": value - 0.2,
                         "close": value})
        return pd.DataFrame(rows)


def test_kronos_adapter_returns_research_contract_and_ignores_future_input():
    pytest.importorskip("pandas")
    repository, days = _repository()
    session = _session(days[-1])
    history = _candles(session, 21)
    reference = history[100].timestamp
    future = [Candle(reference + timedelta(minutes=1), Decimal("999999"),
                     Decimal("999999"), Decimal("999999"), Decimal("999999"), 0, None)]
    adapter = KronosForecastAdapter(predictor_factory=lambda: _StubPredictor(),
                                    sample_paths=1, enabled=True)
    result = adapter.forecast([*history, *future], as_of=reference, lookback=400)
    assert result.status == "COMPLETED"
    assert set(result.horizons) == {10, 20, 30}
    assert len(result.forecast_path) == 30
    assert result.summary["pathCount"] == 1
    assert result.summary["usedByPriceCoreV2"] is False


def test_kronos_unavailable_service_is_isolated():
    repository, days = _repository()
    session = _session(days[-1])
    history = _candles(session, 21)
    reference = history[100].timestamp
    adapter = KronosForecastAdapter(enabled=True, service_url="http://127.0.0.1:1",
                                    timeout_seconds=0.2)
    result = adapter.forecast(history, as_of=reference)
    assert result.status == "SERVICE_UNAVAILABLE"
    assert not result.horizons


def test_experimental_output_persistence_is_explicit_and_immutable():
    repository, days = _repository()
    reference = datetime.combine(days[-1], time(11, 0), IST).astimezone(timezone.utc)
    result = BankniftyTimeMachine(repository, sample_paths=50, seed=3).run(
        revision_id="revision-1", instrument_id=1, reference_minute=reference)
    assert repository.save_experimental_output(result, "revision-1", 1) is True
    assert repository.save_experimental_output(result, "revision-1", 1) is False
    rows = repository.experimental_outputs_for("revision-1", 1,
        model_name="BANKNIFTY_TIME_MACHINE_V1")
    assert len(rows) == 1 and rows[0]["model_name"] == "BANKNIFTY_TIME_MACHINE_V1"
    changed = replace(result, summary={**result.summary, "seed": 999})
    with pytest.raises(ValueError, match="immutable"):
        repository.save_experimental_output(changed, "revision-1", 1)
    with pytest.raises(ValueError, match="unknown experimental"):
        repository.experimental_outputs_for("revision-1", 1, model_name="GENERIC")
