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


# Files that mark a directory as THE config dir even when config.yml is absent.
# A fresh clone has no config.yml — it is gitignored — but it does carry
# `config.yml.example` and `module_component_map.csv`, and the component
# lookup needs that CSV whether or not anyone wrote a config.
_CONFIG_DIR_MARKERS = ("config.yml", "config.yml.example",
                       "module_component_map.csv")


@lru_cache(maxsize=1)
def locate_config_file() -> Path | None:
    """The config file, or None when there is no config at all.

    None is a supported state, not a failure: a Jenkins job can supply every
    setting through the environment, and requiring a file there would mean
    committing one or provisioning a secret file for values that are not
    secret. `load_config` decides what is genuinely required.
    """
    override = os.environ.get("TRIAGE_CONFIG")
    if override:
        path = Path(override).expanduser().resolve()
        if not path.exists():
            # An explicit override that does not exist is a mistake, not an
            # invitation to fall back — silently ignoring it would point the
            # run at whatever config happened to be lying around.
            raise FileNotFoundError(
                f"$TRIAGE_CONFIG points at {path}, which does not exist.")
        return path

    here = Path(__file__).resolve()
    for parent in here.parents:
        for d in _CONFIG_DIRS:
            candidate = parent / d / "config.yml"
            if candidate.exists():
                return candidate
    return None


def find_config_file() -> Path:
    """Absolute path to config.yml. Raises FileNotFoundError if none found.

    Kept for callers that genuinely cannot proceed without a file.
    """
    path = locate_config_file()
    if path is None:
        raise FileNotFoundError(
            "config.yml not found. Looked for config/config.yml or "
            "configs/config.yml walking up from this package. "
            "Set $TRIAGE_CONFIG to point at it explicitly."
        )
    return path


@lru_cache(maxsize=1)
def config_dir() -> Path:
    """Directory holding config.yml — also where module_component_map.csv lives.

    Resolved by MARKER rather than by config.yml alone, so the component map is
    still found on an env-configured run with no config file.
    """
    path = locate_config_file()
    if path is not None:
        return path.parent

    here = Path(__file__).resolve()
    for parent in here.parents:
        for d in _CONFIG_DIRS:
            candidate = parent / d
            if candidate.is_dir() and any((candidate / m).exists()
                                          for m in _CONFIG_DIR_MARKERS):
                return candidate

    raise FileNotFoundError(
        "No config directory found. Looked for config/ or configs/ carrying "
        f"one of {', '.join(_CONFIG_DIR_MARKERS)}.")


def cli_command() -> str:
    """How to invoke this tool, as the reader would have to type it.

    Printed hints used to say a bare `testray-analysis`, which only works
    inside an activated virtualenv — so every "finish it with:" line in the
    output failed when pasted, which is exactly when a person is most likely to
    paste one. The entry point sits beside the running interpreter, so ask
    that instead of assuming a PATH.
    """
    import sys as _sys

    candidate = Path(_sys.executable).with_name("testray-analysis")
    if not candidate.exists():
        return "testray-analysis"

    # Relative to the project root when it is inside it (`.venv/bin/…`), which
    # is shorter to read and correct to paste from the repo.
    try:
        return str(candidate.relative_to(project_root()))
    except ValueError:
        return str(candidate)


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
