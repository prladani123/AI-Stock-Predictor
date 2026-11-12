from __future__ import annotations

import re
import json
from ..paths import memory_root
from typing import Any


SYMBOLS = {
    "HDFCBANK": ("hdfc bank", "hdfcbank"), "ICICIBANK": ("icici bank", "icicibank"),
    "SBIN": ("state bank", "sbi", "sbin"), "AXISBANK": ("axis bank", "axisbank"),
    "KOTAKBANK": ("kotak bank", "kotak mahindra", "kotakbank"),
}
CATEGORY_RULES = (
    ("RBI", ("reserve bank", "rbi", "repo rate", "monetary policy committee")),
    ("FED", ("federal reserve", "fed ", "fomc", "powell")),
    ("INFLATION", ("inflation", "cpi", "wpi")),
    ("INTEREST_RATES", ("interest rate", "rate hike", "rate cut", "bond yield", "yields")),
    ("CRUDE_OIL", ("crude", "brent", "oil price", "opec", "hormuz")),
    ("GEOPOLITICS", ("geopolit", "war", "iran", "israel", "sanction", "conflict")),
    ("USD_INR", ("rupee", "usd/inr", "dollar")),
    ("FII_DII", ("fii", "dii", "foreign investor", "institutional flow")),
    ("INDEX_REBALANCE", ("msci", "rebalance", "index inclusion")),
    ("NSE_MARKET_STRUCTURE", ("closing auction", "cas", "nse circular", "market structure")),
    ("BANK_REGULATION", ("bank regulation", "capital requirement", "liquidity coverage", "banking regulation")),
    ("BANK_EARNINGS", ("bank earnings", "bank profit", "net interest margin", "nim", "quarter result")),
    ("GLOBAL_RISK", ("global selloff", "risk-off", "asian markets", "wall street")),
)


def _contains(text: str, phrase: str) -> bool:
    normalized = phrase.strip().lower()
    return re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text) is not None


def classify(headline: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    text = headline.lower()
    configured=json.loads((memory_root()/'config'/'constituents.json').read_text(encoding='utf-8'))['symbols']
    symbols = tuple(symbol for symbol, words in configured.items() if any(_contains(text, word) for word in words))
    direct = (metadata or {}).get('scope') == 'CONSTITUENT_DIRECT'
    if direct:
        symbols = tuple(dict.fromkeys((*symbols, *[s for s in metadata.get('affectedSymbols',[]) if s in configured])))
    category = "MAJOR_BANK_NEWS" if symbols else "OTHER_RELEVANT"
    matched: list[str] = []
    categories=[]
    for candidate, words in CATEGORY_RULES:
        hits = [word.strip() for word in words if _contains(text, word)]
        if hits:
            categories.append(candidate)
            matched.extend(hits)
    if categories: category=categories[0]
    high_categories = {"RBI", "FED", "CRUDE_OIL", "GEOPOLITICS", "BANK_REGULATION", "BANK_EARNINGS", "INDEX_REBALANCE", "NSE_MARKET_STRUCTURE"}
    category_weight = .35 if category in high_categories else .15 if category != "OTHER_RELEVANT" else 0
    score = min(1.0, .30 + category_weight + (.25 if symbols else 0) + min(.15, len(matched) * .05))
    relevance = "HIGH" if score >= .7 else "MEDIUM" if score >= .5 else "LOW"
    reason = ("Direct BANKNIFTY constituent impact." if symbols else
              "Macro/rate/oil event with material banking valuation or risk implications." if category in high_categories else
              "No direct BANKNIFTY relevance rule matched; retained only when explicitly requested.")
    tags = tuple(dict.fromkeys([category.lower().replace("_", "-"), *[c.lower().replace('_','-') for c in categories], *[re.sub(r"\s+", "-", item) for item in matched], *[item.lower() for item in symbols]]))
    scope='CONSTITUENT' if symbols else 'MARKET_STRUCTURE' if category in {'INDEX_REBALANCE','NSE_MARKET_STRUCTURE'} else 'BANKING_SECTOR' if category.startswith('BANK_') else 'INDIA_MACRO' if category in {'RBI','INFLATION','USD_INR','FII_DII'} else 'GLOBAL_MACRO'
    if direct and symbols: scope='CONSTITUENT_DIRECT'
    return {"category": category, "tags": tags, "affected_symbols": symbols,
            "relevance_score": round(score, 2), "relevance": relevance,
            "banknifty_relevance": relevance, "relevance_reason": reason,
            'raw_metadata':{**(metadata or {}),'primaryCategory':category,'secondaryCategories':categories[1:],'scope':scope}}
