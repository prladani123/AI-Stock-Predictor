from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol


class LiveMarketAdapter(Protocol):
    def connect(self) -> None: ...
    def subscribe(self, tokens: Sequence[int]) -> None: ...
    def unsubscribe(self, tokens: Sequence[int]) -> None: ...
    def set_mode(self, mode: str, tokens: Sequence[int]) -> None: ...
    def close(self) -> None: ...


class KiteTickerLiveAdapter:
    """Minimal, read-only wrapper around the official KiteTicker callbacks."""

    SAFE_PUBLIC_METHODS = frozenset({"connect", "subscribe", "unsubscribe", "set_mode", "close"})

    def __init__(
        self,
        api_key: str,
        access_token: str,
        on_ticks: Callable[[list[dict[str, Any]]], None],
        on_state: Callable[[str, str | None], None],
        *,
        ticker_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not api_key or not access_token:
            raise ValueError("real ticker credentials are required")
        if ticker_factory is None:
            from kiteconnect import KiteTicker
            ticker_factory = KiteTicker
        self._ticker = ticker_factory(api_key, access_token, reconnect=True, reconnect_max_tries=10)
        self._on_ticks_callback = on_ticks
        self._on_state_callback = on_state
        self._ticker.on_ticks = lambda _ws, ticks: self._on_ticks_callback(ticks)
        self._ticker.on_connect = lambda _ws, _response: self._on_state_callback("CONNECTED", None)
        self._ticker.on_close = lambda _ws, code, reason: self._on_state_callback("DISCONNECTED", f"{code}:{reason}")
        self._ticker.on_error = lambda _ws, code, reason: self._on_state_callback("DEGRADED", f"{code}:{reason}")
        self._ticker.on_reconnect = lambda _ws, attempts: self._on_state_callback("RECONNECTING", str(attempts))
        self._ticker.on_noreconnect = lambda _ws: self._on_state_callback("AUTH_EXPIRED", "reconnect exhausted")

    def connect(self) -> None:
        self._on_state_callback("CONNECTING", None)
        self._ticker.connect(threaded=True)

    def subscribe(self, tokens: Sequence[int]) -> None:
        if tokens:
            self._ticker.subscribe(sorted(set(int(item) for item in tokens)))

    def unsubscribe(self, tokens: Sequence[int]) -> None:
        if tokens:
            self._ticker.unsubscribe(sorted(set(int(item) for item in tokens)))

    def set_mode(self, mode: str, tokens: Sequence[int]) -> None:
        if mode != "full":
            raise ValueError("Live ticker requires full mode")
        if tokens:
            self._ticker.set_mode(self._ticker.MODE_FULL, sorted(set(int(item) for item in tokens)))

    def close(self) -> None:
        self._ticker.close()


class SimulatedLiveAdapter:
    """Deterministic in-process adapter; it never imports or opens a network client."""

    SAFE_PUBLIC_METHODS = KiteTickerLiveAdapter.SAFE_PUBLIC_METHODS | frozenset({"emit", "drop", "reconnect"})

    def __init__(
        self,
        on_ticks: Callable[[list[dict[str, Any]]], None],
        on_state: Callable[[str, str | None], None],
    ) -> None:
        self._on_ticks = on_ticks
        self._on_state = on_state
        self.connected = False
        self.active_tokens: set[int] = set()
        self.operations: list[dict[str, Any]] = []

    def connect(self) -> None:
        self.operations.append({"operation": "connect"})
        self._on_state("CONNECTING", None)
        self.connected = True
        self._on_state("CONNECTED", None)

    def subscribe(self, tokens: Sequence[int]) -> None:
        added = sorted(set(tokens) - self.active_tokens)
        self.active_tokens.update(added)
        self.operations.append({"operation": "subscribe", "tokens": added})

    def unsubscribe(self, tokens: Sequence[int]) -> None:
        removed = sorted(set(tokens) & self.active_tokens)
        self.active_tokens.difference_update(removed)
        self.operations.append({"operation": "unsubscribe", "tokens": removed})

    def set_mode(self, mode: str, tokens: Sequence[int]) -> None:
        self.operations.append({"operation": "set_mode", "mode": mode, "tokens": sorted(set(tokens))})

    def close(self) -> None:
        self.connected = False
        self.operations.append({"operation": "close"})
        self._on_state("STOPPED", "simulated close")

    def emit(self, ticks: list[dict[str, Any]]) -> None:
        if not self.connected:
            raise RuntimeError("simulated adapter is disconnected")
        self._on_ticks(ticks)

    def drop(self, reason: str = "simulated connection failure") -> None:
        self.connected = False
        self.active_tokens.clear()
        self.operations.append({"operation": "drop", "reason": reason})
        self._on_state("DISCONNECTED", reason)

    def reconnect(self) -> None:
        self.operations.append({"operation": "reconnect"})
        self._on_state("RECONNECTING", "1")
        self.connected = True
        self._on_state("CONNECTED", "reconnected")
