"""
Tests for configuring a run entirely from the environment.

A CI checkout has no `config/config.yml` — it is gitignored — and the values a
job needs are either secrets, which belong in credential bindings rather than a
file, or paths. So "no file, full environment" has to be a supported
configuration rather than an error, and the two failure modes it introduces
both have to be loud:

  - nothing configured at all must name BOTH ways out, not just the file;
  - a `$TRIAGE_CONFIG` that does not exist must fail, never silently fall back
    to whatever config happens to be lying around next to the package.
"""

import pytest

from testray_analytics.analysis import config as C
from testray_analytics.analysis import prepare as P

ENV_VARS = ("TRIAGE_CONFIG", "TESTRAY_BASE_URL", "TESTRAY_CLIENT_ID",
            "TESTRAY_CLIENT_SECRET", "TESTRAY_UI_URL", "TRIAGE_REPO_PATH",
            "TRIAGE_ROUTINE_REMOTES", "TRIAGE_SCAN_ROUTINES")


def _clear_caches():
    """Drop the memoised lookups.

    Tolerant of a monkeypatched stand-in: several tests replace these with
    plain lambdas, which have no cache to clear.
    """
    for fn in (C.locate_config_file, C.config_dir):
        getattr(fn, "cache_clear", lambda: None)()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No inherited settings, and no memoised lookup from another test."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture
def no_file(monkeypatch):
    """Stand in for a fresh checkout: config dir present, config.yml absent."""
    monkeypatch.setattr(C, "locate_config_file", lambda: None)
    monkeypatch.setattr(P, "locate_config_file", lambda: None)


def test_a_full_environment_needs_no_file(no_file, monkeypatch):
    monkeypatch.setenv("TESTRAY_BASE_URL", "https://testray.example")
    monkeypatch.setenv("TESTRAY_CLIENT_ID", "id")
    monkeypatch.setenv("TESTRAY_CLIENT_SECRET", "secret")
    monkeypatch.setenv("TESTRAY_UI_URL", "https://testray.example/web/testray")
    monkeypatch.setenv("TRIAGE_REPO_PATH", "/srv/liferay-portal")

    cfg = P.load_config()

    assert cfg["testray"]["base_url"] == "https://testray.example"
    assert cfg["testray"]["client_secret"] == "secret"
    assert cfg["testray"]["ui_url"] == "https://testray.example/web/testray"
    assert cfg["git"]["repo_path"] == "/srv/liferay-portal"


def test_nothing_configured_names_both_ways_out(no_file):
    """The old error pointed only at config.yml, which sent a CI operator
    looking for a file they were deliberately not going to have."""
    with pytest.raises(FileNotFoundError) as e:
        P.load_config()
    message = str(e.value)
    assert "config/config.yml" in message
    assert "TESTRAY_CLIENT_ID" in message


def test_an_explicit_config_that_does_not_exist_is_an_error(monkeypatch,
                                                            tmp_path):
    """Falling back would point the run at another instance's config."""
    monkeypatch.setenv("TRIAGE_CONFIG", str(tmp_path / "nope.yml"))
    with pytest.raises(FileNotFoundError, match="nope.yml"):
        C.locate_config_file()


def test_routine_remotes_parse_from_a_flat_string():
    """Jenkins bindings are flat strings; config.yml holds a nested map. Both
    have to produce the shape prepare reads."""
    assert P._routine_map("79529=bchan,590307=upstream") == {
        79529: "bchan", 590307: "upstream"}
    assert P._routine_map(" 79529 = bchan ") == {79529: "bchan"}
    assert P._routine_map("79529") == {}, "a pair with no remote is dropped"


def test_scan_routines_parse_into_the_nested_shape(no_file, monkeypatch):
    monkeypatch.setenv("TESTRAY_BASE_URL", "https://testray.example")
    monkeypatch.setenv("TRIAGE_SCAN_ROUTINES", "79529, 590307")

    cfg = P.load_config()

    assert cfg["triage"]["scan"]["routines"] == [79529, 590307]


def test_the_environment_wins_over_the_file(tmp_path, monkeypatch):
    (tmp_path / "config.yml").write_text(
        "testray:\n  base_url: https://from-file\n  client_id: file-id\n")
    monkeypatch.setenv("TRIAGE_CONFIG", str(tmp_path / "config.yml"))
    monkeypatch.setenv("TESTRAY_BASE_URL", "https://from-env")

    cfg = P.load_config()

    assert cfg["testray"]["base_url"] == "https://from-env"
    assert cfg["testray"]["client_id"] == "file-id", "file still supplies the rest"


def test_the_override_is_reported_not_inferred(tmp_path, monkeypatch):
    """A redirected run has to say so in its own output — the alternative is
    diagnosing it from a 401 twenty minutes later."""
    (tmp_path / "config.yml").write_text("testray:\n  base_url: https://f\n")
    monkeypatch.setenv("TRIAGE_CONFIG", str(tmp_path / "config.yml"))
    monkeypatch.setenv("TESTRAY_BASE_URL", "https://from-env")

    line = P.testray_target(P.load_config())

    assert "https://from-env" in line and "TESTRAY_BASE_URL" in line


def test_the_component_map_is_found_without_a_config_file(monkeypatch,
                                                          tmp_path):
    """config_dir() also locates module_component_map.csv, which the component
    lookup needs whether or not anyone wrote a config."""
    monkeypatch.setattr(C, "locate_config_file", lambda: None)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "module_component_map.csv").write_text("x\n")
    monkeypatch.setattr(C, "__file__",
                        str(tmp_path / "pkg" / "analysis" / "config.py"))

    C.config_dir.cache_clear()
    assert C.config_dir() == tmp_path / "config"
