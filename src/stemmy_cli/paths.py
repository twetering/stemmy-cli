"""
Path resolution for standalone and development modes.

Works when:
- Installed as package (pip install): uses cwd + package-bundled static/music
- Run from source (PYTHONPATH=src): uses project root (stemmy_cli/ or surrounded/)
"""

from pathlib import Path


def _package_root() -> Path:
    """stemmy_cli package directory."""
    return Path(__file__).resolve().parent


def _project_root() -> Path:
    """
    Project root when in development (stemmy_cli/ or surrounded/).
    When installed as package, returns cwd.
    """
    pkg = _package_root()
    # pkg = .../src/stemmy_cli or .../site-packages/stemmy_cli
    parent = pkg.parent
    grandparent = parent.parent
    # Project root: has data/, static/music, pyproject.toml, or scripts/
    for root in (grandparent, parent):
        if (root / "data" / "stemmy.db").exists():
            return root
        if (root / "static" / "music").exists():
            return root
        if (root / "pyproject.toml").exists():
            return root
        if (root / "scripts" / "migrate_words_data.py").exists():
            return root
    return Path.cwd()


def get_output_dir() -> Path:
    """Output directory for MP3s."""
    d = _project_root() / "output"
    d.mkdir(exist_ok=True, parents=True)
    return d


def get_music_dir() -> Path:
    """Music directory. Checks: project/static/music, package/static/music, cwd/static/music."""
    candidates = [
        _project_root() / "static" / "music",
        _package_root() / "static" / "music",
        Path.cwd() / "static" / "music",
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]


def get_project_root() -> Path:
    """Project root for subprocess cwd, etc."""
    return _project_root()
