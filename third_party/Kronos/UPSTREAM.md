# Kronos vendored snapshot

- Upstream URL: https://github.com/shiyu-coder/Kronos
- Fetched commit SHA: `67b630e67f6a18c9e9be918d9b4337c960db1e9a`
- Fetch date: 2026-11-12
- License: MIT (Copyright 2025 ShiYu), see `LICENSE`
- Local path: `third_party/Kronos/`

This is a complete source snapshot of the upstream repository excluding only
nested Git metadata and generated caches. Our Docker image and runtime import
from this local directory and never clone GitHub at build or run time.

Model weights are not part of this source snapshot. See
`third_party/Kronos/models/README.md` for the local model cache policy.
