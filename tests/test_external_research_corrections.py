from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from banknifty_foundation.external_research import (
    ExternalForecastResult, HISTORICAL_COUNTERFACTUAL, KRONOS_MODEL_NAME,
    PROSPECTIVE_POST_DECISION, checksum_payload,
)
from banknifty_foundation.repository import MemoryRepository, PostgresRepository

NOW = datetime(2025, 1, 20, 6, tzinfo=timezone.utc)


def output():
    return ExternalForecastResult(KRONOS_MODEL_NAME, "fixture", NOW, NOW,
        NOW + timedelta(minutes=1), NOW + timedelta(minutes=1), NOW,
        HISTORICAL_COUNTERFACTUAL, "COMPLETED", {}, summary={"pathCount": 1},
        input_checksum=checksum_payload("input"), configuration={"lookback": 100}, seed=1)


def test_rerun_retains_first_availability_but_config_seed_and_content_are_immutable():
    repo = MemoryRepository()
    first = output()
    assert repo.save_experimental_output(first, "revision", 1)
    later = replace(first, computed_at=NOW + timedelta(hours=1), available_at=NOW + timedelta(hours=1))
    assert not repo.save_experimental_output(later, "revision", 1)
    assert next(iter(repo.experimental_outputs.values()))["available_at"] == first.available_at
    for changed in (replace(later, seed=2), replace(later, configuration={"lookback": 200})):
        assert repo.save_experimental_output(changed, "revision", 1)
    with pytest.raises(ValueError, match="immutable"):
        repo.save_experimental_output(replace(first, forecast_path=({"close": 123},)), "revision", 1)


def test_channels_retry_independently(monkeypatch):
    from banknifty_foundation import external_research_runtime as runtime
    runtime._last_success.clear()
    for flag in ("TIME_MACHINE_ENABLED", "KRONOS_RESEARCH_ENABLED"):
        monkeypatch.setenv(flag, "true")
    calls = []
    def run(*args):
        channel = args[-1]
        calls.append(channel)
        if channel == "timeMachine":
            runtime._last_success[channel] = args[-2]
        else:
            raise RuntimeError("Kronos unavailable")
    monkeypatch.setattr(runtime, "_run", run)
    class InlineThread:
        def __init__(self, target, args, kwargs, **_):
            self.invoke = lambda: target(*args, **kwargs)
        def start(self):
            self.invoke()
    monkeypatch.setattr(runtime, "Thread", InlineThread)
    for _ in range(2):
        assert runtime.capture_best_effort(None, None, NOW, None, 1,
            decision_at=NOW + timedelta(minutes=1), prediction_ids=("prediction",),
            replay_pool_revision_id="replay")
    assert calls == ["timeMachine", "kronos", "kronos"]
    monkeypatch.setenv("TIME_MACHINE_SEED", "9")
    runtime.capture_best_effort(None, None, NOW, None, 1,
        decision_at=NOW + timedelta(minutes=1), prediction_ids=("prediction",),
        replay_pool_revision_id="replay")
    assert calls[-2:] == ["timeMachine", "kronos"]
    runtime._last_success.clear()


def test_repository_history_sql_and_migration_parse_without_postgres():
    pglast = pytest.importorskip("pglast")
    statements = []
    class DB:
        def execute(self, query, params):
            sql = query.as_string()
            assert sql.count("%s") == len(params)
            pglast.parse_sql(sql.replace("%s", "NULL"))
            statements.append(sql)
            return SimpleNamespace(fetchall=lambda: [], fetchone=lambda: (1,))
    @contextmanager
    def connection():
        yield DB()
    repo = object.__new__(PostgresRepository)
    repo._connection = connection
    assert repo.experimental_outputs_for(None, 1) == []
    assert repo.experimental_outputs_for("00000000-0000-0000-0000-000000000001", 1,
                                         model_name=KRONOS_MODEL_NAME) == []
    assert len(statements) == 2
    assert repo.save_experimental_output(output(), "00000000-0000-0000-0000-000000000001", 1)
    assert statements[-1].startswith("INSERT INTO experimental_model_outputs")
    migration = Path("migrations/022_experimental_research_identity.sql").read_text()
    pglast.parse_sql(migration)
    # Parse PL/pgSQL function bodies too, not merely CREATE FUNCTION envelopes.
    import re
    for function in re.findall(r"CREATE OR REPLACE FUNCTION.*?\$\$ LANGUAGE plpgsql;", migration, re.S):
        pglast.parse_plpgsql(function)


def test_display_rejects_stale_or_unlinked_external_output():
    from banknifty_foundation.product_queries import ProductDataReader
    reader = object.__new__(ProductDataReader)
    row = {"as_of": NOW - timedelta(minutes=1), "summary": {"predictionIds": ["p"]}}
    assert not reader._experimental_payload(row, NOW, {"p"})["available"]
    row["as_of"] = NOW
    assert not reader._experimental_payload(row, NOW, {"other"})["available"]


def test_kronos_rejects_old_gap_even_with_enough_recent_candles():
    from banknifty_foundation.kronos_experiment import KronosForecastAdapter
    from banknifty_foundation.models import Candle
    candles = [Candle(NOW - timedelta(minutes=i), 100, 101, 99, 100, 0, None)
               for i in range(80) if i != 50]
    adapter = KronosForecastAdapter(enabled=False)
    assert adapter.forecast(candles, as_of=NOW).status == "INPUT_GAP"
    assert adapter.forecast(candles, as_of=NOW.replace(hour=10)).status == "SESSION_BOUNDARY"


def test_kronos_rejects_invalid_output_ohlc():
    from banknifty_foundation.kronos_experiment import KronosForecastAdapter
    from banknifty_foundation.models import Candle
    path = [{"timestamp": (NOW + timedelta(minutes=i)).isoformat(),
             "open": 100, "high": 90, "low": 99, "close": 100} for i in range(1, 31)]
    result = KronosForecastAdapter()._result(
        [Candle(NOW, 100, 101, 99, 100, 0, None)], NOW, {"paths": [path]}, HISTORICAL_COUNTERFACTUAL)
    assert result.status == "FAILED" and result.to_dict()["horizons"] == {}


def test_live_identity_rejects_training_revision_and_false_decision_availability():
    first = replace(output(), prediction_ids=("p",), decision_at=NOW,
                    point_in_time_status=PROSPECTIVE_POST_DECISION)
    assert first.to_record(dataset_revision_id=None, instrument_id=1)["prediction_ids"] == ["p"]
    with pytest.raises(ValueError, match="live research"):
        first.to_record(dataset_revision_id="training-revision", instrument_id=1)
    with pytest.raises(ValueError, match="point-in-time"):
        replace(first, point_in_time_status="PROSPECTIVE_DECISION_AVAILABLE").to_record(
            dataset_revision_id=None, instrument_id=1)


def test_retry_reads_original_journal_decision(monkeypatch):
    from banknifty_foundation import external_research_runtime as runtime
    monkeypatch.setenv("TIME_MACHINE_ENABLED", "true")
    @contextmanager
    def connection():
        yield SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=lambda: [("p", NOW)]))
    calls = []
    monkeypatch.setattr(runtime, "capture_best_effort", lambda *args, **kwargs: calls.append(kwargs) or True)
    assert runtime.retry_committed(SimpleNamespace(_connection=connection), None, NOW, 1, ("p",))
    assert calls[0]["decision_at"] == NOW
    assert calls[0]["prediction_ids"] == ("p",)


def test_sidecar_verifies_committed_hashes_and_eval_mode(tmp_path):
    # Exercise real loader functions with tiny fixture weights, without Torch/model inference.
    import ast
    import hashlib
    import os
    module = ast.parse(Path("third_party/Kronos/kronos_research_server.py").read_text())
    functions = [node for node in module.body if isinstance(node, ast.FunctionDef)
                 and node.name in ("_sha256", "_verify", "_load")]
    model = tmp_path / "model"
    tokenizer = tmp_path / "tokenizer"
    model.mkdir()
    tokenizer.mkdir()
    (model / "model.safetensors").write_bytes(b"fixture-model")
    (tokenizer / "model.safetensors").write_bytes(b"fixture-tokenizer")
    evaluated = []
    factory = SimpleNamespace(from_pretrained=lambda path: SimpleNamespace(eval=lambda: evaluated.append(path)))
    scope = dict(hashlib=hashlib, os=os, MODEL_DIR=str(model), TOKENIZER_DIR=str(tokenizer),
        MODEL_SHA256=hashlib.sha256(b"fixture-model").hexdigest().upper(),
        TOKENIZER_SHA256=hashlib.sha256(b"fixture-tokenizer").hexdigest().upper(),
        THREADS=1, MAX_CONTEXT=2048, STATE={}, torch=SimpleNamespace(set_num_threads=lambda _: None),
        Kronos=factory, KronosTokenizer=factory, KronosPredictor=lambda *args, **kwargs: "loaded")
    exec(compile(ast.Module(body=functions, type_ignores=[]), "sidecar-loader", "exec"), scope)
    scope["_load"]()
    assert scope["STATE"]["status"] == "READY"
    assert evaluated == [str(tokenizer), str(model)]
    (model / "model.safetensors").write_bytes(b"corrupt")
    scope["_load"]()
    assert scope["STATE"]["status"] == "SERVICE_UNAVAILABLE"


def test_time_machine_excludes_short_and_special_prior_sessions():
    from test_external_research import _repository
    from banknifty_foundation.banknifty_time_machine import BankniftyTimeMachine
    repo, days = _repository()
    repo.sessions[days[0]] = replace(repo.sessions[days[0]], status="SPECIAL")
    repo.sessions[days[1]] = replace(repo.sessions[days[1]],
        close_at=repo.sessions[days[1]].close_at - timedelta(minutes=1))
    reference = repo.sessions[days[-1]].open_at + timedelta(minutes=100)
    result = BankniftyTimeMachine(repo, sample_paths=1, minimum_prior_sessions=1).run(
        revision_id="revision-1", instrument_id=1, reference_minute=reference)
    assert result.status == "INSUFFICIENT_PRIOR_SESSIONS"
    assert result.summary["priorSessionCount"] == 19
