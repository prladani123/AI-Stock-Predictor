from __future__ import annotations

import pytest

from banknifty_foundation.config import Settings


def test_database_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings.from_env()


def test_live_credentials_are_disabled_by_default():
    settings = Settings(database_url="")
    with pytest.raises(RuntimeError, match="disabled"):
        settings.require_live_credentials()


def test_invalid_boolean_configuration_is_rejected(monkeypatch):
    monkeypatch.setenv("ALLOW_REAL_ZERODHA", "maybe")
    with pytest.raises(ValueError, match="true or false"):
        Settings.from_env(require_database=False)


def test_ml_thread_limit_fails_closed(monkeypatch):
    monkeypatch.setenv("ML_THREADS", "3")
    with pytest.raises(ValueError, match="must not exceed 2"):
        Settings.from_env(require_database=False)
