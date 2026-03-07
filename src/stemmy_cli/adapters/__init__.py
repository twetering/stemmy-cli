"""Backend adapters for Stemmy CLI."""

from stemmy_cli.adapters.sqlite import SQLiteAdapter
from stemmy_cli.adapters.http import HTTPAdapter

__all__ = ["SQLiteAdapter", "HTTPAdapter"]
