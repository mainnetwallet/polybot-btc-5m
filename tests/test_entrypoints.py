"""Entry-point wiring tests: logging arguments, locks, and CLI dispatch."""
from __future__ import annotations

import argparse

from btc5m_bot import cli, engine, logging_setup, supervisor


def _record(monkeypatch):
    calls: list[tuple[object, str]] = []

    def fake_setup(logs_dir, *, level=0, component="engine"):  # noqa: ANN001, ANN202
        calls.append((logs_dir, component))

    # Entry points import setup_logging lazily, so patch the source module.
    monkeypatch.setattr(logging_setup, "setup_logging", fake_setup)
    return calls


async def _noop(*args, **kwargs):
    return None


def test_engine_main_wires_logging_and_runs(monkeypatch, settings):
    calls = _record(monkeypatch)
    monkeypatch.setattr(engine, "load_settings", lambda: settings)
    monkeypatch.setattr(engine, "run_engine", _noop)

    assert engine.main() == 0
    assert calls == [(settings.logs_path, "engine")]


def test_supervisor_main_wires_logging(monkeypatch, settings):
    calls = _record(monkeypatch)
    monkeypatch.setattr(supervisor, "load_settings", lambda: settings)
    monkeypatch.setattr(supervisor.Supervisor, "run", lambda self: 0)

    assert supervisor.main() == 0
    assert calls == [(settings.logs_path, "supervisor")]


def test_cli_run_wires_logging(monkeypatch, settings):
    calls = _record(monkeypatch)
    monkeypatch.setattr(cli, "_settings", lambda: settings)
    monkeypatch.setattr(engine, "run_engine", _noop)

    assert cli.cmd_run(argparse.Namespace(no_api=True)) == 0
    assert calls == [(settings.logs_path, "engine")]


def test_cli_stop_writes_stop_file(settings, monkeypatch):
    monkeypatch.setattr(cli, "_settings", lambda: settings)

    assert cli.cmd_stop(argparse.Namespace()) == 0
    assert (settings.data_path / "STOP").exists()
