# Source and leakage policy

1. Daily OHLC comes from historical BANKNIFTY index data pages and is cross-checked against contemporaneous reports where practical.
2. A news article published **after** the move may explain the day, but it is not eligible as a live predictive input at an earlier timestamp.
3. Exact event times are stored only when the source exposes them (e.g. RBI 10:50, live-blog timestamps, intraday recovery articles).
4. If the precise trigger minute cannot be verified, the file says so rather than guessing.
5. Causality is graded. Grade C is intentionally allowed: not every move has an identifiable news trigger.
6. Historical option OI/PCR/IV/futures values are not fabricated. Those should be joined from the application's own stored point-in-time data.
7. Closing Auction Session effects are structural/microstructure events and must be separated from regular-session price discovery.
