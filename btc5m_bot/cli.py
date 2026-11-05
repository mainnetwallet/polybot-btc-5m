"""Operator CLI. All live-affecting commands require an explicit typed phrase."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import secrets
import sys
from pathlib import Path

from .clock import RealClock
from .config import ConfigStore, Settings, load_settings
from .db.engine import backup_database, init_db, make_engine, make_session_factory, single_instance_lock
from .db.repo import Repository
from .security import hash_password


def _settings() -> Settings:
    return load_settings()


def _repo(settings: Settings) -> tuple[Repository, object]:
    db = make_engine(settings.db_path)
    init_db(db)
    return Repository(make_session_factory(db)), db


def cmd_init(args: argparse.Namespace) -> int:
    settings = _settings()
    repo, db = _repo(settings)
    store = ConfigStore(settings.runtime_config_file)
    cfg = store.snapshot()
    db.dispose()
    print(f"initialised database: {settings.db_path}")
    print(f"runtime config:       {settings.runtime_config_file} (mode={cfg.mode.value}, capital={cfg.capital_usd})")
    print("next steps:")
    print("  1. python -m btc5m_bot hash-password > hash.txt      # set POLYBOT_DASHBOARD_PASSWORD_HASH")
    print("  2. set POLYBOT_SESSION_SECRET to a long random value")
    print("  3. start PAPER: start.bat   (dashboard http://127.0.0.1:%d)" % settings.port)
    return 0


def cmd_hash_password(args: argparse.Namespace) -> int:
    password = getpass.getpass("Dashboard password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("passwords do not match", file=sys.stderr)
        return 1
    if len(password) < 12:
        print("password must be at least 12 characters", file=sys.stderr)
        return 1
    print("Add these to your .env (never commit them):")
    print(f"POLYBOT_DASHBOARD_PASSWORD_HASH={hash_password(password)}")
    print(f"POLYBOT_SESSION_SECRET={secrets.token_urlsafe(48)}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings()
    from .logging_setup import setup_logging

    setup_logging(settings.logs_path, component="engine")
    from .engine import run_engine

    with single_instance_lock(settings.data_path / settings.single_instance_lock):
        asyncio.run(run_engine(settings, start_api=not args.no_api))
    return 0


def cmd_supervisor(args: argparse.Namespace) -> int:
    from .supervisor import main as supervisor_main

    return supervisor_main()


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings()
    repo, db = _repo(settings)
    try:
        state_path = settings.data_path / "supervisor.json"
        supervisor = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
        risk = repo.get_risk_state()
        position = repo.open_position("LIVE") or repo.open_position("PAPER")
        payload = {
            "mode": ConfigStore(settings.runtime_config_file).snapshot().mode.value,
            "live_activation": repo.active_live_activation() is not None,
            "supervisor": supervisor,
            "recovery": repo.get_state("recovery"),
            "risk": {
                "kill_switch": risk.kill_switch,
                "kill_reason": risk.kill_reason,
                "consecutive_losses": risk.consecutive_losses,
                "cooldown_until": risk.cooldown_until.isoformat() if risk.cooldown_until else None,
            },
            "open_position": {
                "market_id": position.market_id,
                "outcome": position.outcome,
                "shares": str(position.shares),
                "status": position.status,
                "mode": position.mode,
            }
            if position
            else None,
            "open_orders": len(repo.open_orders()),
            "paper_trades": len(repo.list_trades(mode="PAPER", limit=100_000)),
            "live_trades": len(repo.list_trades(mode="LIVE", limit=100_000)),
            "heartbeat": {
                "armed": repo.get_heartbeat_state().armed,
                "consecutive_failures": repo.get_heartbeat_state().consecutive_failures,
            },
        }
        print(json.dumps(payload, indent=2))
    finally:
        db.dispose()
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    settings = _settings()
    db = make_engine(settings.db_path)
    try:
        dest = settings.data_path / "backups" / f"manual-{__import__('datetime').datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        path = backup_database(db, dest)
        print(f"backup written: {path}")
    finally:
        db.dispose()
    return 0


def cmd_approve_rule(args: argparse.Namespace) -> int:
    settings = _settings()
    repo, db = _repo(settings)
    try:
        rule = repo.get_resolution_rule(args.fingerprint)
        if rule is None:
            print("rule not found", file=sys.stderr)
            return 1
        actor = os.environ.get("USERNAME") or os.environ.get("USER") or "operator"
        repo.approve_resolution_rule(args.fingerprint, actor)
        repo.audit("RESOLUTION_RULE_APPROVED", actor=actor, detail={"fingerprint": args.fingerprint, "via": "cli"})
        print(f"approved {args.fingerprint} as {actor}")
    finally:
        db.dispose()
    return 0


def cmd_activate_live(args: argparse.Namespace) -> int:
    settings = _settings()
    phrase = args.phrase or input("Type 'ACTIVATE LIVE TRADING': ").strip()
    from .engine import build_default_engine

    engine, db = build_default_engine(settings, start_api=False)
    try:
        report = asyncio.run(engine.activate_live(typed_phrase=phrase, actor="cli"))
        print(json.dumps(report.as_dict(), indent=2))
        return 0 if report.passed else 2
    finally:
        asyncio.run(engine.adapter.close())
        db.dispose()


def cmd_revoke_live(args: argparse.Namespace) -> int:
    settings = _settings()
    phrase = args.phrase or input("Type 'REVOKE LIVE': ").strip()
    if phrase.strip() != "REVOKE LIVE":
        print("typed confirmation does not match 'REVOKE LIVE'", file=sys.stderr)
        return 1
    from .engine import build_default_engine

    engine, db = build_default_engine(settings, start_api=False)
    try:
        asyncio.run(engine.revoke_live(actor="cli", reason=args.reason or "cli revoke"))
        print("LIVE revoked; cancel-all requested")
    finally:
        asyncio.run(engine.adapter.close())
        db.dispose()
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    settings = _settings()
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{settings.db_path.as_posix()}")
    command.upgrade(config, "head")
    print(f"database migrated to head: {settings.db_path}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    settings = _settings()
    path = settings.data_path / "STOP"
    path.write_text("stop requested at operator CLI\n", encoding="utf-8")
    print(f"STOP file written: {path}")
    print("a supervised run will shut down within a few seconds; run.bat will exit")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m btc5m_bot", description="POLYBOT operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create directories, database, and default runtime config")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("hash-password", help="generate dashboard password hash and session secret")
    p.set_defaults(func=cmd_hash_password)

    p = sub.add_parser("run", help="run the engine in the foreground (dashboard enabled)")
    p.add_argument("--no-api", action="store_true", help="disable the dashboard API")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("supervisor", help="run the supervised engine + optional heartbeat")
    p.set_defaults(func=cmd_supervisor)

    p = sub.add_parser("status", help="print current state as JSON")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("backup", help="write a consistent SQLite backup")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("migrate", help="apply Alembic migrations to the configured database")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("approve-rule", help="approve a resolution rule fingerprint")
    p.add_argument("fingerprint")
    p.set_defaults(func=cmd_approve_rule)

    p = sub.add_parser("activate-live", help="evaluate the 23-condition gate and activate LIVE")
    p.add_argument("--phrase", default="", help="typed confirmation phrase")
    p.set_defaults(func=cmd_activate_live)

    p = sub.add_parser("revoke-live", help="revoke LIVE activation and request cancel-all")
    p.add_argument("--phrase", default="", help="typed confirmation phrase")
    p.add_argument("--reason", default="", help="revocation reason")
    p.set_defaults(func=cmd_revoke_live)

    p = sub.add_parser("stop", help="request a graceful supervisor shutdown")
    p.set_defaults(func=cmd_stop)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
