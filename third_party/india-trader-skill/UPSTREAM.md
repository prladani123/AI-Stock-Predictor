# India Trader Skill vendored snapshot

- Upstream URL: https://github.com/nawed1111/india-trader-skill
- Fetched commit SHA: `cd9767f40241ac914cd13e2a06fd3a60b3af42d4`
- Fetch date: 2026-11-12
- License: MIT (Copyright 2026 Jake Schincariol; Copyright 2026 Nawed Anjum, India port), see `LICENSE`
- Local path: `third_party/india-trader-skill/`

This is a complete source snapshot of the upstream repository excluding only
nested Git metadata and generated caches. It is retained for reference and
audit. Only the time-of-day Monte Carlo concept is adapted into
`src/banknifty_foundation/banknifty_time_machine.py`; broker, order, Yahoo
data, budget, entry/exit, and risk execution modules are not integrated.

Our Docker images and runtime never clone GitHub at build or run time.
