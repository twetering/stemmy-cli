"""Configuration management for Stemmy CLI."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel


class StemmyConfig(BaseModel):
    """Configuration for Stemmy CLI."""

    api_base_url: str = "http://localhost:5000"
    database_path: Optional[str] = None
    default_format: str = "table"
    verbose: bool = False

    @classmethod
    def load(cls) -> "StemmyConfig":
        """Load configuration from environment and .env files."""
        pkg = Path(__file__).resolve().parent
        project_root = pkg.parent.parent  # src/stemmy_cli -> project root
        env_paths = [
            Path.cwd() / ".env",
            project_root / ".env",
            Path.home() / ".stemmy" / ".env",
        ]

        for env_path in env_paths:
            if env_path.exists():
                load_dotenv(env_path)
                break

        return cls(
            api_base_url=os.getenv("STEMMY_API_URL", "http://localhost:5000"),
            database_path=os.getenv("STEMMY_DB_PATH") or os.getenv("VOXPOP_DB_PATH"),
            default_format=os.getenv("STEMMY_OUTPUT_FORMAT", "table"),
            verbose=os.getenv("STEMMY_VERBOSE", "").lower() in ("1", "true", "yes"),
        )


_config: Optional[StemmyConfig] = None


def get_config() -> StemmyConfig:
    """Get the current configuration (lazy loaded)."""
    global _config
    if _config is None:
        _config = StemmyConfig.load()
    return _config


def get_database_path() -> Path:
    """Get the SQLite database path."""
    config = get_config()
    if config.database_path:
        return Path(config.database_path)

    from stemmy_cli.paths import get_project_root
    root = get_project_root()
    default_paths = [
        root / "data" / "stemmy.db",
        Path.cwd() / "data" / "stemmy.db",
        Path.cwd() / "stemmy.db",
        Path.home() / ".stemmy" / "stemmy.db",
    ]

    for path in default_paths:
        if path.exists():
            return path

    return default_paths[0]
