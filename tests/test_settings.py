"""Settings: clear errors for bad values, LOG_LEVEL in any case, paths relative to the project."""

from __future__ import annotations

import os

import pytest

from retail_agent import cli
from retail_agent.settings import ROOT, SettingsError, load_settings, log_level


@pytest.mark.parametrize(
    "name, value, message",
    [
        ("MAX_QUERIES_PER_TURN", "ten", "whole number"),
        ("TURN_TIMEOUT_S", "4m", "a number"),
        ("MAX_SQL_ATTEMPTS", "0", "at least 1"),
        ("WAREHOUSE", "postgres", "'bigquery' or 'duckdb'"),
    ],
)
def test_bad_values_name_the_setting(monkeypatch, name, value, message):
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError) as err:
        load_settings(env_file=os.devnull)
    assert name in str(err.value) and message in str(err.value)


def test_cli_reports_a_bad_setting_without_a_traceback(monkeypatch):
    monkeypatch.setenv("MAX_ROWS_TO_LLM", "lots")
    with cli.console.capture() as captured, pytest.raises(SystemExit) as exit_:
        cli.main(["stats"])
    assert exit_.value.code == 2
    assert "MAX_ROWS_TO_LLM" in captured.get()


def test_log_level_any_case(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert log_level() == "DEBUG"
    monkeypatch.setenv("LOG_LEVEL", "loud")
    assert log_level() == "WARNING"


def test_paths_are_relative_to_the_project(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "my-data")
    monkeypatch.setenv("PERSONA_FILE", "~/persona.md")
    monkeypatch.delenv("DUCKDB_PATH", raising=False)
    settings = load_settings(env_file=os.devnull)
    assert settings.data_dir == ROOT / "my-data"
    assert settings.duckdb_path == ROOT / "my-data" / "thelook_sample.duckdb"
    assert str(settings.persona_path) == os.path.expanduser("~/persona.md")
