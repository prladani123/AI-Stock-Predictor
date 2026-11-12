from __future__ import annotations

import ast
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import pytest

from banknifty_foundation.zerodha import ReadOnlyZerodhaAdapter, create_read_only_zerodha_adapter


FORBIDDEN = {
    "orders",
    "place_order",
    "modify_order",
    "cancel_order",
    "positions",
    "holdings",
    "margins",
    "place_gtt",
    "modify_gtt",
    "delete_gtt",
}


class DangerousFakeClient:
    def __init__(self):
        self.safe_calls = []
        self.forbidden_calls = []

    def instruments(self, exchange=None):
        self.safe_calls.append(("instruments", exchange))
        return []

    def historical_data(self, *args, **kwargs):
        self.safe_calls.append(("historical_data", args, kwargs))
        return []

    def __getattr__(self, name):
        if name in FORBIDDEN:
            def forbidden(*_args, **_kwargs):
                self.forbidden_calls.append(name)
                raise AssertionError(f"forbidden method reached: {name}")
            return forbidden
        raise AttributeError(name)


def test_adapter_exposes_only_two_public_capabilities():
    adapter = ReadOnlyZerodhaAdapter(DangerousFakeClient())
    public_callables = {
        name for name in dir(adapter)
        if not name.startswith("_") and callable(getattr(adapter, name))
    }
    assert public_callables == {"instruments", "historical_data"}
    for name in FORBIDDEN:
        assert not hasattr(adapter, name)


def test_guard_prevents_real_client_construction():
    factory_called = False

    def factory(**_kwargs):
        nonlocal factory_called
        factory_called = True
        raise AssertionError("factory must not run")

    with pytest.raises(RuntimeError, match="disabled"):
        create_read_only_zerodha_adapter("key", "token", client_factory=factory)
    assert factory_called is False


def test_history_is_forced_to_safe_one_minute_shape():
    client = DangerousFakeClient()
    adapter = ReadOnlyZerodhaAdapter(client)
    now = datetime.now(timezone.utc)
    adapter.historical_data(1, now, now)
    assert client.safe_calls[-1][2] == {"continuous": False, "oi": True}
    with pytest.raises(ValueError, match="one-minute"):
        adapter.historical_data(1, now, now, interval="day")
    assert client.forbidden_calls == []


def test_production_code_has_no_forbidden_kite_method_calls():
    source_root = Path(__file__).resolve().parents[1] / "src" / "banknifty_foundation"
    violations = []
    for path in source_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN:
                violations.append(f"{path.name}:{node.lineno}:{node.func.attr}")
    assert violations == []


def test_current_official_kiteconnect_release_is_installed():
    assert version("kiteconnect") == "5.2.0"
