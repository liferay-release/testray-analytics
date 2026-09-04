"""
config.py

Layout-agnostic config resolution for the testray-analytics modules.

Resolution order:
  1. $TRIAGE_CONFIG — explicit path override.
  2. Walk up from this file's location; the first parent containing
     `config/config.yml` OR `configs/config.yml` wins. From
     `testray_analytics/analysis/config.py` that resolves to the repo-root
     `config/config.yml`.

`config_dir()` also locates `module_component_map.csv`, which the CSV-based
component lookup in `prompt_helpers.py` reads (no DB dependency).
"""

import os
from functools import lru_cache
from pathlib import Path

_CONFIG_DIRS = ("config", "configs")


@lru_cache(maxsize=1)
def find_config_file() -> Path:
    """Absolute path to config.yml. Raises FileNotFoundError if none found."""
    override = os.environ.get("TRIAGE_CONFIG")
    if override:
        return Path(override).expanduser().resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        for d in _CONFIG_DIRS:
            candidate = parent / d / "config.yml"
            if candidate.exists():
                return candidate

    raise FileNotFoundError(
        "config.yml not found. Looked for config/config.yml or "
        "configs/config.yml walking up from this package. "
        "Set $TRIAGE_CONFIG to point at it explicitly."
    )


def config_dir() -> Path:
    """Directory holding config.yml — also where module_component_map.csv lives."""
    return find_config_file().parent


def project_root() -> Path:
    """Repo root — the parent of the config dir.

    Relative paths from config.yml resolve against this, never against the
    current working directory. The same config has to work when Jenkins runs
    the pipeline from a workspace root, when the queue runner shells out from
    wherever it happens to live, and when a human runs a command from inside
    `runs/`. Resolving against cwd would silently scatter state across three
    different places depending on who invoked what.
    """
    return config_dir().parent


def resolve_path(value: str | os.PathLike | None, default: str) -> Path:
    """A configured path, absolute-ised against the project root.

    `~` is expanded, an already-absolute path is left alone, and anything
    relative is anchored to `project_root()`.
    """
    raw = str(value).strip() if value not in (None, "") else default
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (project_root() / p).resolve()
