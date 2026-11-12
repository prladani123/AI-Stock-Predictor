from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Protocol


class _KiteReadClient(Protocol):
    def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]: ...
    def ltp(self, *instruments: str) -> dict[str, Any]: ...

    def historical_data(
        self,
        instrument_token: int,
        from_date: datetime,
        to_date: datetime,
        interval: str,
        continuous: bool = False,
        oi: bool = False,
    ) -> list[dict[str, Any]]: ...


class ReadOnlyZerodhaAdapter:
    """Capability wrapper limited to instrument, historical-candle and LTP reads."""

    __slots__ = ("__client",)
    SAFE_PUBLIC_METHODS = frozenset({"instruments", "historical_data", "ltp"})

    def __init__(self, client: _KiteReadClient) -> None:
        self.__client = client

    def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        return self.__client.instruments(exchange)

    def ltp(self, *instruments: str) -> dict[str, Any]:
        if not instruments: raise ValueError('At least one exchange-qualified instrument is required')
        return self.__client.ltp(*instruments)

    def historical_data(
        self,
        instrument_token: int,
        from_date: datetime,
        to_date: datetime,
        interval: str = "minute",
        continuous: bool = False,
        oi: bool = True,
    ) -> list[dict[str, Any]]:
        if interval != "minute":
            raise ValueError("Historical importer supports only one-minute candles")
        try:
            return self.__client.historical_data(
                instrument_token,
                from_date,
                to_date,
                interval,
                continuous=continuous,
                oi=oi,
            )
        except Exception as exc:
            if type(exc).__name__ == "NetworkException":
                raise ConnectionError("Transient Zerodha network failure") from exc
            raise


def create_read_only_zerodha_adapter(
    api_key: str,
    access_token: str,
    *,
    allow_real_api: bool = False,
    client_factory: Callable[..., _KiteReadClient] | None = None,
) -> ReadOnlyZerodhaAdapter:
    """Guarded composition root; never called by fixture/test commands."""
    if not allow_real_api:
        raise RuntimeError("Real Zerodha API construction is disabled")
    if not api_key or not access_token:
        raise ValueError("api_key and access_token are required")
    if client_factory is None:
        from kiteconnect import KiteConnect  # Imported only after the explicit guard.

        client_factory = KiteConnect
    client = client_factory(api_key=api_key)
    client.set_access_token(access_token)  # type: ignore[attr-defined]
    return ReadOnlyZerodhaAdapter(client)
