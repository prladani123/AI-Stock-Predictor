# Kronos and BANKNIFTY Time Machine Integration

Status: **CODE IMPLEMENTED - EXPERIMENTAL RESEARCH - NOT USED BY PRICE_CORE_V2**.
Data activation and empirical validation remain separate from implementation. No production
PRICE_CORE_V2 model, threshold, approval record or probability is changed by this work.

## Vendored upstream snapshots

| Project | Local path | Upstream | Exact fetched commit | Fetch date | Licence |
|---|---|---|---|---|---|
| Kronos | `third_party/Kronos/` | https://github.com/shiyu-coder/Kronos | `67b630e67f6a18c9e9be918d9b4337c960db1e9a` | 2026-11-12 | MIT |
| India Trader | `third_party/india-trader-skill/` | https://github.com/nawed1111/india-trader-skill | `cd9767f40241ac914cd13e2a06fd3a60b3af42d4` | 2026-11-12 | MIT |

`third_party/Kronos/UPSTREAM.md` and `third_party/india-trader-skill/UPSTREAM.md`
record provenance. Nested Git metadata and caches are excluded. Docker builds use these
committed local snapshots; Docker never clones either repository.

## Kronos model cache

Only `Kronos-mini` and `Kronos-Tokenizer-2k` are used. The Hugging Face model cards declare MIT.

| Model | Local file | Size | SHA-256 |
|---|---|---:|---|
| `NeoQuasar/Kronos-mini` | `third_party/Kronos/models/Kronos-mini/model.safetensors` | 16,440,776 | `A7D5F37E2E9FBD9891F7D7D4F72574512DD1F704FEE14223E0A8CD0FBF54197C` |
| `NeoQuasar/Kronos-Tokenizer-2k` | `third_party/Kronos/models/Kronos-Tokenizer-2k/model.safetensors` | 15,842,376 | `B97EC46B3B72160509E289183EAF7BDF5F0DAC5BB9B49522F6D46638A99A8717` |

`third_party/Kronos/models/README.md` documents the cache. The service uses the local
cache at runtime and never downloads weights. The `.gitignore` contains explicit exceptions
for these two weight files because the repository uses a broad `*.safetensors` ignore rule.

## Local Docker runtime

- Server: `third_party/Kronos/kronos_research_server.py`
- Image: `third_party/Kronos/Dockerfile.research`
- Compose service: `kronos-research` with `profiles: ["research"]`
- Internal URL: `http://kronos-research:8010`
- CPU only by default; limits 2 CPUs and 3 GB; model threads default 2
- Health: `GET /health`; prediction: `POST /predict`
- Prediction input is canonical BANKNIFTY 1-minute OHLC only. The API rejects future
  candles, invalid OHLC, duplicate reference ambiguity and non-30-minute requests. Volume
  is explicitly marked `INDEX_VOLUME_NOT_USED`.

Start the research stack explicitly:

```powershell
docker compose --profile research up --build
```

Normal Compose startup does not start Kronos. The Python application treats Kronos as optional;
an unavailable service returns `SERVICE_UNAVAILABLE` and never changes production intelligence.

## Shared research contract

`src/banknifty_foundation/external_research.py` provides the 10/20/30-minute horizon contract:
meaningful-move probability, upward/downward conditional probabilities, expected return,
expected magnitude, median and p05/p25/p50/p75/p95, maximum up/down excursion, uncertainty,
path count, threshold policy and point-in-time fields. Big Move V2 thresholds are reused, not
redefined. Volume is never fabricated.

Persisted rows reuse `experimental_model_outputs` from migration `010_big_move_v2.sql` with
explicit model names:

- `KRONOS_MINI_ZERO_SHOT_V1`
- `BANKNIFTY_TIME_MACHINE_V1`

Lineage, seed, path count, sampling checksum, thresholds and the point-in-time status live in
the existing JSON fields. Query and API code separates `kronosExperimental` from
`timeMachineExperimental`; generic "latest experimental output" selection is prohibited.

## BANKNIFTY Time Machine

`src/banknifty_foundation/banknifty_time_machine.py` adapts the `bharat-timemachine`
time-of-day replay concept only. For reference date D it uses complete sessions strictly before D,
requires at least 20 prior sessions, samples bar-to-bar OHLC ratios from a default +/-2-minute
time-of-day pool, and generates 30 future one-minute bars using 10,000 deterministic NumPy paths
by default. It emits the same research contract and a sampling-pool checksum. It does not use
Yahoo, broker endpoints, orders, position sizing, stops, targets or execution.

Market Memory remains separate. SimilarityEngine finds historically similar market states;
Time Machine estimates a time-of-day conditional future distribution. Neither replaces the other.
