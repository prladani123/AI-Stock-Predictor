import os
from pathlib import Path


def memory_root() -> Path:
    return Path(os.getenv('MARKET_MEMORY_ROOT', str(Path(__file__).resolve().parents[2] / 'market_memory')))


def migrations_dir(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    configured = os.getenv("MIGRATIONS_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "migrations"


def migration_files(explicit: str | Path | None = None) -> list[Path]:
    root = migrations_dir(explicit)
    return sorted(root.glob("*.sql"))
