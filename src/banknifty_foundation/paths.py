import os
from pathlib import Path

def memory_root() -> Path:
    return Path(os.getenv('MARKET_MEMORY_ROOT', str(Path(__file__).resolve().parents[2] / 'market_memory')))
