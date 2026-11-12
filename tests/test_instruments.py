from __future__ import annotations

import json

from banknifty_foundation.instruments import select_banknifty_instruments


def test_only_banknifty_index_and_futures_are_selected(fixture_dir):
    rows = json.loads((fixture_dir / "instruments.json").read_text(encoding="utf-8"))
    selected = select_banknifty_instruments(rows)
    assert [item.instrument_token for item in selected] == [1100001, 260105]
    assert {item.exchange for item in selected} == {"NFO", "NSE"}
    assert all("INFY" not in item.tradingsymbol for item in selected)

