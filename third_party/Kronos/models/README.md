# Local Kronos model cache

These weights are the exact files needed by the optional Kronos research
service. They are loaded from this directory at container start; nothing is
downloaded from Hugging Face at runtime.

| Directory | Upstream | License | SHA-256 |
| --- | --- | --- | --- |
| `Kronos-mini/` | https://huggingface.co/NeoQuasar/Kronos-mini | MIT | `A7D5F37E2E9FBD9891F7D7D4F72574512DD1F704FEE14223E0A8CD0FBF54197C` |
| `Kronos-Tokenizer-2k/` | https://huggingface.co/NeoQuasar/Kronos-Tokenizer-2k | MIT | `B97EC46B3B72160509E289183EAF7BDF5F0DAC5BB9B49522F6D46638A99A8717` |

The model card metadata for both repositories declares `license: mit`, so
redistribution is permitted. Sizes are approximately 16.4 MB and 15.8 MB.
The files are committed to this repository so Docker builds are reproducible
and do not depend on future upstream availability.

Only `Kronos-mini` and `Kronos-Tokenizer-2k` are used. `Kronos-small`,
`Kronos-base`, and `Kronos-large` are intentionally not cached on the target
4-core / 12 GB machine.
