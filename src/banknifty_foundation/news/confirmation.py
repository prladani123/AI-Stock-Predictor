from __future__ import annotations

from typing import Any

from .models import MarketNewsEvent


def market_confirmation(event: MarketNewsEvent, state: dict[str, Any]) -> dict[str, Any]:
    expected = event.directional_context
    if expected not in {"BULLISH", "BEARISH"}:
        return {"state": "INSUFFICIENT_DATA", "drivers": ["Event has no supported directional context."]}
    signs: list[tuple[str, int]] = []
    for key, label in (("return_5m", "5-minute price momentum"), ("return_10m", "10-minute price momentum")):
        value = state.get(key)
        if isinstance(value, (int, float)):
            signs.append((label, 1 if value > 0 else -1 if value < 0 else 0))
    buildup = state.get("futures_buildup")
    if buildup in {"LONG_BUILDUP", "SHORT_COVERING"}: signs.append((f"Futures {buildup}", 1))
    if buildup in {"SHORT_BUILDUP", "LONG_UNWINDING"}: signs.append((f"Futures {buildup}", -1))
    imbalance = state.get("oi_change_imbalance")
    if isinstance(imbalance, (int, float)): signs.append(("Option OI change imbalance", 1 if imbalance > 0 else -1))
    if not signs:
        return {"state": "INSUFFICIENT_DATA", "drivers": ["No current price/derivatives confirmation fields are available."]}
    wanted = 1 if expected == "BULLISH" else -1
    aligned = [name for name, sign in signs if sign == wanted]
    opposed = [name for name, sign in signs if sign == -wanted]
    status = "CONFIRMED" if len(aligned) >= 2 and len(aligned) > len(opposed) else "PARTIALLY_CONFIRMED" if aligned else "CONTRADICTED" if opposed else "INSUFFICIENT_DATA"
    return {"state": status, "drivers": [f"Aligned: {name}" for name in aligned] + [f"Opposed: {name}" for name in opposed]}
