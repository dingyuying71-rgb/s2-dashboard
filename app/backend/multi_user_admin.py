"""Provisioning and migration helpers for Multi-user Isolation V1.

All account arithmetic remains inside the frozen AccountEngineV4. These
helpers create storage and compare immutable account state; they never replay
or recompute account history.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .account_engine_v4 import AccountEngineV4
from .multi_user import (
    ACCOUNT_SCHEMA_VERSION,
    CONTRACT_VERSION,
    DbResolver,
    MultiUserRuntime,
    MultiUserSecurityError,
)


DEFAULT_USER_SETTINGS = {
    "selected_fund": "007801",
    "profit_lock": "0",
    "dividend_route": "REINVEST",
    "fee_rate": "0.003",
    "settlement_days": "2",
    "beginner_mode": "true",
    "account_initialized": "false",
}

SHARED_TABLES = (
    "market_observations",
    "fund_nav_observations",
    "monthly_signals",
    "official_signals",
    "data_update_runs",
)

PRIVATE_SCHEMA = """
CREATE TABLE account_state(id INTEGER PRIMARY KEY CHECK(id=1), engine BLOB NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE events(
  event_id TEXT PRIMARY KEY,event_type TEXT,timestamp TEXT,status TEXT,payload TEXT,
  contract_version TEXT,schema_version TEXT,event_sequence INTEGER,effective_at TEXT,
  correlation_id TEXT,causation_id TEXT);
CREATE TABLE pending_lots(
  lot_id TEXT PRIMARY KEY,event_id TEXT,amount TEXT,status TEXT,cleared_at TEXT,batch_id TEXT,
  original_contribution_event_id TEXT,contract_version TEXT);
CREATE TABLE fund_holdings(id INTEGER PRIMARY KEY CHECK(id=1),fund_code TEXT,fund_name TEXT,value TEXT,updated_at TEXT);
CREATE TABLE transactions(id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT,timestamp TEXT,type TEXT,amount TEXT,status TEXT,note TEXT);
CREATE TABLE settlements(id INTEGER PRIMARY KEY AUTOINCREMENT,lot_id TEXT,event_id TEXT,kind TEXT,amount TEXT,available_at TEXT,status TEXT);
CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT,payload TEXT NOT NULL);
CREATE TABLE execution_records(
  id INTEGER PRIMARY KEY AUTOINCREMENT,signal_date TEXT NOT NULL UNIQUE,event_id TEXT NOT NULL UNIQUE,
  recommended_action TEXT NOT NULL,recommended_amount TEXT NOT NULL,actual_action TEXT NOT NULL,
  actual_amount TEXT NOT NULL,fund_code TEXT NOT NULL,executed_at TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,contract_version TEXT,account_effect_mode TEXT);
CREATE TABLE account_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE migration_runs(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL,details TEXT NOT NULL);
CREATE TABLE v4_recommendations(
  recommendation_id TEXT PRIMARY KEY,signal_id TEXT NOT NULL,signal_date TEXT NOT NULL,
  recommended_action TEXT NOT NULL,recommended_amount TEXT NOT NULL,target_exposure TEXT NOT NULL,
  account_fingerprint TEXT NOT NULL,created_at TEXT NOT NULL,contract_version TEXT NOT NULL,schema_version TEXT NOT NULL);
CREATE TABLE v4_executions(
  event_id TEXT PRIMARY KEY,signal_id TEXT NOT NULL,recommendation_id TEXT NOT NULL,
  contract_version TEXT NOT NULL,schema_version TEXT NOT NULL,fund_code TEXT NOT NULL,fund_name TEXT NOT NULL,
  side TEXT NOT NULL,reported_at TEXT NOT NULL,recommended_action TEXT NOT NULL,recommended_amount TEXT NOT NULL,
  actual_order_type TEXT NOT NULL,cash_debited TEXT,reported_fee TEXT,fee_status TEXT NOT NULL,
  redemption_units TEXT,estimated_redemption_amount TEXT,execution_deviation TEXT NOT NULL,user_note TEXT NOT NULL,
  status TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(recommendation_id) REFERENCES v4_recommendations(recommendation_id));
CREATE UNIQUE INDEX ux_v4_execution_active_signal ON v4_executions(signal_id)
  WHERE status NOT IN ('REJECTED','CANCELLED','SUPERSEDED');
CREATE TABLE v4_confirmations(
  confirmation_event_id TEXT PRIMARY KEY,execution_event_id TEXT NOT NULL UNIQUE,confirmed_at TEXT NOT NULL,
  confirmed_nav TEXT NOT NULL,confirmed_units TEXT NOT NULL,confirmed_principal TEXT NOT NULL,
  confirmed_fee TEXT NOT NULL,confirmed_value TEXT NOT NULL,confirmation_status TEXT NOT NULL,
  platform_reference TEXT NOT NULL DEFAULT '',contract_version TEXT NOT NULL,schema_version TEXT NOT NULL,
  FOREIGN KEY(execution_event_id) REFERENCES v4_executions(event_id));
CREATE TABLE v4_settlement_events(
  settlement_event_id TEXT PRIMARY KEY,execution_event_id TEXT NOT NULL UNIQUE,settled_at TEXT NOT NULL,
  settled_cash_amount TEXT NOT NULL,destination TEXT NOT NULL,status TEXT NOT NULL,
  contract_version TEXT NOT NULL,schema_version TEXT NOT NULL,
  FOREIGN KEY(execution_event_id) REFERENCES v4_executions(event_id));
CREATE TABLE v4_pending_events(
  event_id TEXT PRIMARY KEY,event_type TEXT NOT NULL,original_pending_lot_id TEXT NOT NULL,
  original_contribution_event_id TEXT NOT NULL,amount TEXT NOT NULL,effective_at TEXT NOT NULL,
  destination TEXT NOT NULL,reason TEXT NOT NULL,nav_before TEXT NOT NULL,units_cancelled TEXT NOT NULL,
  remaining_pending_amount TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,
  contract_version TEXT NOT NULL,schema_version TEXT NOT NULL);
CREATE TABLE v4_execution_corrections(
  event_id TEXT PRIMARY KEY,references_event_id TEXT NOT NULL,delta_payload TEXT NOT NULL,reason TEXT NOT NULL,
  corrected_at TEXT NOT NULL,status TEXT NOT NULL,contract_version TEXT NOT NULL,schema_version TEXT NOT NULL);
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def initialize_empty_v4_account(db_path: Path) -> dict[str, Any]:
    if db_path.exists():
        raise MultiUserSecurityError("ACCOUNT_ALREADY_EXISTS", 409)
    db_path.parent.mkdir(parents=True, exist_ok=False)
    # Account events use Shanghai wall-clock time throughout backend.main.
    # Using the host-local clock here can otherwise make a fresh account's
    # initial timestamp later than its first user action.
    opening_at = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None, microsecond=0)
    engine = AccountEngineV4(0, 0, 0, opening_at, audit_enabled=True)
    try:
        with sqlite3.connect(db_path) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.executescript(PRIVATE_SCHEMA)
            con.execute("INSERT INTO account_state VALUES(1,?,?)", (pickle.dumps(engine), datetime.now().isoformat()))
            con.executemany("INSERT INTO settings VALUES(?,?)", DEFAULT_USER_SETTINGS.items())
            con.executemany(
                "INSERT INTO account_metadata VALUES(?,?)",
                (
                    ("account_contract_version", CONTRACT_VERSION),
                    ("multi_user_schema_version", str(ACCOUNT_SCHEMA_VERSION)),
                    ("migration_status", "CURRENT"),
                ),
            )
            con.execute(
                "INSERT INTO migration_runs VALUES(?,?,?)",
                (
                    "multi-user-v1-clean",
                    datetime.now().isoformat(),
                    json.dumps({"source": "clean-empty-v4", "owner_data_copied": False}),
                ),
            )
        DbResolver.verify_schema(db_path)
        return {"status": "PASS", "database": str(db_path), "sha256": sha256(db_path)}
    except Exception:
        if db_path.exists():
            db_path.unlink()
        if db_path.parent.exists() and not any(db_path.parent.iterdir()):
            db_path.parent.rmdir()
        raise


def provision_user(
    runtime: MultiUserRuntime,
    invite_email: str,
    access_sub: str,
    display_name: str,
    role: str = "TESTER",
) -> dict[str, Any]:
    row, created = runtime.registry.create_provisioning(invite_email, access_sub, display_name, role)
    user_id = row["user_id"]
    db_path = runtime.resolver.users_root / user_id / "account.db"
    if not created:
        if row["status"] == "ACTIVE":
            runtime.resolver.resolve(user_id)
            return {"status": "ALREADY_PROVISIONED", "user_id": user_id, "database": str(db_path)}
        if row["status"] not in {"PROVISIONING", "ERROR"}:
            raise MultiUserSecurityError("PROVISIONING_STATE_INVALID", 409)
    try:
        if not db_path.exists():
            initialize_empty_v4_account(db_path)
        runtime.resolver.resolve(user_id)
        if row["status"] == "PROVISIONING":
            runtime.registry.activate(user_id)
            # Account catalog/current pointer is a maintenance-layer concern;
            # materialize it immediately for newly provisioned isolated users.
            runtime.registry.initialize()
        return {"status": "ACTIVE", "user_id": user_id, "database": str(db_path), "owner_data_copied": False}
    except Exception:
        runtime.registry.mark_error(user_id)
        raise


def add_multi_user_metadata(db_path: Path) -> None:
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE IF NOT EXISTS account_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        con.execute(
            "INSERT OR REPLACE INTO account_metadata VALUES('account_contract_version',?)",
            (CONTRACT_VERSION,),
        )
        con.execute(
            "INSERT OR REPLACE INTO account_metadata VALUES('multi_user_schema_version',?)",
            (str(ACCOUNT_SCHEMA_VERSION),),
        )
        con.execute("INSERT OR REPLACE INTO account_metadata VALUES('migration_status','CURRENT')")


def create_shared_store(source_db: Path, shared_db: Path) -> dict[str, Any]:
    shared_db.parent.mkdir(parents=True, exist_ok=True)
    temp = shared_db.with_name(f".{shared_db.name}.{uuid.uuid4().hex}.tmp")
    source = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(temp)
    copied: dict[str, int] = {}
    try:
        for table in SHARED_TABLES:
            schema = source.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if table == "fund_nav_observations":
                target.execute(
                    """CREATE TABLE fund_nav_observations(
                    nav_date TEXT NOT NULL,fund_code TEXT NOT NULL,nav REAL NOT NULL,
                    fetched_at TEXT NOT NULL,raw_hash TEXT NOT NULL,validation_status TEXT NOT NULL,
                    PRIMARY KEY(fund_code,nav_date))"""
                )
                if schema is not None:
                    rows = source.execute(
                        "SELECT nav_date,fund_code,nav,fetched_at,raw_hash,validation_status FROM fund_nav_observations"
                    ).fetchall()
                    if rows:
                        target.executemany(
                            "INSERT OR IGNORE INTO fund_nav_observations VALUES(?,?,?,?,?,?)",
                            [tuple(row) for row in rows],
                        )
                    copied[table] = len(rows)
                else:
                    copied[table] = 0
                continue
            if schema is None:
                raise RuntimeError(f"SHARED_TABLE_MISSING:{table}")
            target.execute(schema[0])
            columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
            rows = source.execute(f"SELECT * FROM {table}").fetchall()
            if rows:
                target.executemany(
                    f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                    [tuple(row) for row in rows],
                )
            copied[table] = len(rows)
        target.execute("CREATE TABLE shared_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        target.executemany(
            "INSERT INTO shared_metadata VALUES(?,?)",
            (("schema_version", "1"), ("strategy_contract", "S2-account-v4.0-frozen"), ("source", "owner-production-copy")),
        )
        target.commit()
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SHARED_STORE_INTEGRITY_FAILED")
    except Exception:
        target.rollback()
        raise
    finally:
        target.close()
        source.close()
    os.replace(temp, shared_db)
    return {"status": "PASS", "database": str(shared_db), "sha256": sha256(shared_db), "rows": copied}


def account_snapshot(db_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        engine = pickle.loads(con.execute("SELECT engine FROM account_state WHERE id=1").fetchone()[0])
        terminal = datetime.now().replace(microsecond=0)
        return {
            "total_wealth": str(engine.total_wealth),
            "fund": str(engine.F),
            "strategy_cash": str(engine.C),
            "pending": str(engine.pending_total),
            "reserve": str(engine.R),
            "subscription_pending": str(getattr(engine, "subscription_pending", 0)),
            "receivable": str(getattr(engine, "redemption_receivable", 0)),
            "payable": str(getattr(engine, "payable", 0)),
            "units": str(engine.units),
            "nav": str(engine.nav),
            "nav_hwm": str(engine.nav_hwm),
            "twr": str(engine.twr()),
            "xirr": engine.xirr(terminal),
            "event_count": con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "contract_version": getattr(engine, "contract_version", "UNKNOWN"),
            "engine_state": engine.state(),
        }
    finally:
        con.close()


def verify_snapshots(before: dict[str, Any], after: dict[str, Any]) -> None:
    exact = (
        "total_wealth",
        "fund",
        "strategy_cash",
        "pending",
        "reserve",
        "subscription_pending",
        "receivable",
        "payable",
        "units",
        "nav",
        "nav_hwm",
        "twr",
        "event_count",
        "contract_version",
        "engine_state",
    )
    changed = {key: (before.get(key), after.get(key)) for key in exact if before.get(key) != after.get(key)}
    if changed:
        raise RuntimeError(f"OWNER_ACCOUNT_STATE_CHANGED:{changed}")


def simulate_owner_migration(source_db: Path, runtime: MultiUserRuntime, email: str, access_sub: str) -> dict[str, Any]:
    runtime.registry.initialize()
    row, created = runtime.registry.create_provisioning(email, access_sub, "OWNER", "OWNER")
    if not created:
        raise MultiUserSecurityError("OWNER_ALREADY_EXISTS", 409)
    destination = runtime.resolver.users_root / row["user_id"] / "account.db"
    destination.parent.mkdir(parents=True, exist_ok=False)
    before = account_snapshot(source_db)
    source_hash = sha256(source_db)
    shutil.copy2(source_db, destination)
    copied_hash = sha256(destination)
    if source_hash != copied_hash:
        raise RuntimeError("OWNER_COPY_HASH_MISMATCH")
    add_multi_user_metadata(destination)
    after = account_snapshot(destination)
    verify_snapshots(before, after)
    shared = create_shared_store(source_db, runtime.shared_db)
    runtime.registry.activate(row["user_id"])
    runtime.resolver.resolve(row["user_id"])
    return {
        "status": "PASS",
        "mode": "QA_COPY",
        "user_id": row["user_id"],
        "source_hash": source_hash,
        "copy_hash_before_metadata": copied_hash,
        "owner_db_hash": sha256(destination),
        "before": before,
        "after": after,
        "shared": shared,
        "single_writable_owner_db": True,
    }
