from __future__ import annotations

import json
import pytest

from banknifty_foundation.cli import main, run_fixture_import, run_phase45_fixture


def test_offline_fixture_import_generates_expected_coverage(tmp_path, fixture_dir):
    output = tmp_path / "coverage.json"
    payload = run_fixture_import(output, fixture_dir)
    assert output.exists()
    assert payload["mode"] == "OFFLINE_FIXTURE_ONLY"
    assert payload["zerodha_network_calls"] == 0
    assert len(payload["imports"]) == 2
    by_expected = {item["expected_minutes"]: item for item in payload["coverage"]}
    assert by_expected[6]["coverage_percent"] in {66.67, 100.0}
    assert sorted(item["coverage_percent"] for item in payload["coverage"]) == [66.67, 100.0]
    assert json.loads(output.read_text(encoding="utf-8")) == payload


def test_real_import_cli_fails_before_configuration_without_confirmation():
    with pytest.raises(RuntimeError, match="confirm-read-only"):
        main(
            [
                "import-history",
                "--from",
                "2024-07-24",
                "--to",
                "2024-07-24",
                "--instrument-token",
                "260105",
            ]
        )


def test_phase45_fixture_is_identical_across_fresh_repositories(tmp_path, fixture_dir):
    first = run_phase45_fixture(tmp_path / "first.json", fixture_dir)
    second = run_phase45_fixture(tmp_path / "second.json", fixture_dir)
    assert first == second


def test_audit_phase05_computes_real_checks(tmp_path):
    output = tmp_path / "audit.json"
    assert main(["audit-phase0-5", "--output", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "PASS"
    assert payload["computed_checks"]
    assert all(payload["computed_checks"].values())
    assert payload["generated_paths"] == []


def test_audit_ignores_runtime_caches_but_detects_generated_environments(tmp_path, monkeypatch):
    import banknifty_foundation.cli as cli

    root = tmp_path / "product"
    (root / "src" / "banknifty_foundation" / "__pycache__").mkdir(parents=True)
    (root / ".pytest_cache").mkdir()
    (root / "src" / "banknifty_foundation" / "calendar.py").write_text(
        "# return only an explicitly stored session", encoding="utf-8"
    )
    (root / "src" / "banknifty_foundation" / "features.py").write_text(
        "UNAVAILABLE_NO_FILL = True", encoding="utf-8"
    )
    (root / "migrations").mkdir()
    for number, name in ((1, "phase_0_1"), (2, "accepted_datasets"), (3, "target_labels"),
                         (4, "point_in_time_features"), (5, "walk_forward_replay")):
        (root / "migrations" / f"{number:03d}_{name}.sql").write_text("-- test", encoding="utf-8")
    monkeypatch.setattr(cli, "ROOT", root)
    output = tmp_path / "cache-audit.json"
    assert main(["audit-phase0-5", "--output", str(output)]) == 0
    payload = json.loads(output.read_text())
    assert len(payload["transient_cache_warnings"]) == 2
    (root / ".venv").mkdir()
    assert main(["audit-phase0-5", "--output", str(output)]) == 1
    assert ".venv" in json.loads(output.read_text())["generated_paths"]
