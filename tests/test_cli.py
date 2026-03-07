"""CLI integration tests."""

import subprocess
import sys


def test_version():
    """stemmy version runs and exits 0."""
    result = subprocess.run(
        [sys.executable, "-m", "stemmy_cli.main", "version"],
        capture_output=True,
        text=True,
        cwd=".",
    )
    assert result.returncode == 0
    assert "v0.1.0" in result.stdout or "Audio" in result.stdout


def test_help():
    """stemmy --help runs and exits 0."""
    result = subprocess.run(
        [sys.executable, "-m", "stemmy_cli.main", "--help"],
        capture_output=True,
        text=True,
        cwd=".",
    )
    assert result.returncode == 0
    assert "formats" in result.stdout
    assert "compilations" in result.stdout
    assert "db" in result.stdout


def test_db_info_with_tmp_db(env_with_db):
    """stemmy db info works with a database."""
    import os
    env = {**os.environ, "STEMMY_DB_PATH": env_with_db}
    env.pop("TURSO_DB_URL", None)
    env.pop("TURSO_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-m", "stemmy_cli.main", "db", "info"],
        capture_output=True,
        text=True,
        cwd=".",
        env=env,
    )
    assert result.returncode == 0
    assert "formats" in result.stdout.lower() or "1" in result.stdout
