"""Idempotent forward-only migration to S2-account-v4.0-rc."""
from __future__ import annotations

import argparse
import hashlib
import json
import pickle
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from backend.account_engine_v4 import AccountEngineV4

VERSION = "account-v4-20260823-01"
CONTRACT = "S2-account-v4.0-rc"


def sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(1024*1024),b""):h.update(block)
    return h.hexdigest().upper()


def columns(con, table):
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def add_column(con, table, definition):
    name=definition.split()[0]
    if name not in columns(con,table):con.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def state_key(engine):
    return (str(engine.total_wealth),str(engine.units),str(engine.nav),str(engine.nav_hwm),str(engine.F),str(engine.C),str(engine.R),str(engine.pending_total),str(engine.unsettled_total))


def migrate(db_path: Path, backup_dir: Path | None = None) -> dict:
    db_path=Path(db_path); backup_path=None
    if backup_dir:
        backup_dir=Path(backup_dir);backup_dir.mkdir(parents=True,exist_ok=True)
        backup_path=backup_dir/f"{db_path.stem}_before_v4_{datetime.now():%Y%m%d_%H%M%S}{db_path.suffix}"
        src=sqlite3.connect(db_path);dst=sqlite3.connect(backup_path);src.backup(dst);dst.close();src.close()
    before_hash=sha256(db_path)
    con=sqlite3.connect(db_path);con.row_factory=sqlite3.Row
    try:
        con.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS account_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS migration_runs(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL,details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS v4_recommendations(
          recommendation_id TEXT PRIMARY KEY,signal_id TEXT NOT NULL,signal_date TEXT NOT NULL,
          recommended_action TEXT NOT NULL,recommended_amount TEXT NOT NULL,target_exposure TEXT NOT NULL,
          account_fingerprint TEXT NOT NULL,created_at TEXT NOT NULL,contract_version TEXT NOT NULL,schema_version TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS v4_executions(
          event_id TEXT PRIMARY KEY,signal_id TEXT NOT NULL,recommendation_id TEXT NOT NULL,
          contract_version TEXT NOT NULL,schema_version TEXT NOT NULL,fund_code TEXT NOT NULL,fund_name TEXT NOT NULL,
          side TEXT NOT NULL,reported_at TEXT NOT NULL,recommended_action TEXT NOT NULL,recommended_amount TEXT NOT NULL,
          actual_order_type TEXT NOT NULL,cash_debited TEXT,reported_fee TEXT,fee_status TEXT NOT NULL,
          redemption_units TEXT,estimated_redemption_amount TEXT,execution_deviation TEXT NOT NULL,user_note TEXT NOT NULL,
          status TEXT NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(recommendation_id) REFERENCES v4_recommendations(recommendation_id));
        CREATE UNIQUE INDEX IF NOT EXISTS ux_v4_execution_active_signal ON v4_executions(signal_id) WHERE status NOT IN ('REJECTED','CANCELLED','SUPERSEDED');
        CREATE TABLE IF NOT EXISTS v4_confirmations(
          confirmation_event_id TEXT PRIMARY KEY,execution_event_id TEXT NOT NULL UNIQUE,confirmed_at TEXT NOT NULL,
          confirmed_nav TEXT NOT NULL,confirmed_units TEXT NOT NULL,confirmed_principal TEXT NOT NULL,
          confirmed_fee TEXT NOT NULL,confirmed_value TEXT NOT NULL,confirmation_status TEXT NOT NULL,
          platform_reference TEXT NOT NULL DEFAULT '',contract_version TEXT NOT NULL,schema_version TEXT NOT NULL,
          FOREIGN KEY(execution_event_id) REFERENCES v4_executions(event_id));
        CREATE TABLE IF NOT EXISTS v4_settlement_events(
          settlement_event_id TEXT PRIMARY KEY,execution_event_id TEXT NOT NULL UNIQUE,settled_at TEXT NOT NULL,
          settled_cash_amount TEXT NOT NULL,destination TEXT NOT NULL,status TEXT NOT NULL,
          contract_version TEXT NOT NULL,schema_version TEXT NOT NULL,
          FOREIGN KEY(execution_event_id) REFERENCES v4_executions(event_id));
        CREATE TABLE IF NOT EXISTS v4_pending_events(
          event_id TEXT PRIMARY KEY,event_type TEXT NOT NULL,original_pending_lot_id TEXT NOT NULL,
          original_contribution_event_id TEXT NOT NULL,amount TEXT NOT NULL,effective_at TEXT NOT NULL,
          destination TEXT NOT NULL,reason TEXT NOT NULL,nav_before TEXT NOT NULL,units_cancelled TEXT NOT NULL,
          remaining_pending_amount TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,
          contract_version TEXT NOT NULL,schema_version TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS v4_execution_corrections(
          event_id TEXT PRIMARY KEY,references_event_id TEXT NOT NULL,delta_payload TEXT NOT NULL,reason TEXT NOT NULL,
          corrected_at TEXT NOT NULL,status TEXT NOT NULL,contract_version TEXT NOT NULL,schema_version TEXT NOT NULL);
        """)
        for definition in (
            "contract_version TEXT","schema_version TEXT","event_sequence INTEGER","effective_at TEXT",
            "correlation_id TEXT","causation_id TEXT"):
            add_column(con,"events",definition)
        add_column(con,"execution_records","contract_version TEXT")
        add_column(con,"execution_records","account_effect_mode TEXT")
        add_column(con,"pending_lots","original_contribution_event_id TEXT")
        add_column(con,"pending_lots","contract_version TEXT")
        row=con.execute("SELECT engine FROM account_state WHERE id=1").fetchone()
        if row is None:raise RuntimeError("ACCOUNT_STATE_MISSING")
        old=pickle.loads(row["engine"]);before_state=state_key(old)
        upgraded=AccountEngineV4.from_v3(old)
        after_state=state_key(upgraded)
        if before_state!=after_state:raise RuntimeError(f"MIGRATION_STATE_CHANGED:{before_state}!={after_state}")
        already_applied=con.execute("SELECT 1 FROM migration_runs WHERE version=?",(VERSION,)).fetchone() is not None
        if not already_applied:
            con.execute("UPDATE execution_records SET contract_version=COALESCE(contract_version,'S2-account-v3.0-frozen'),account_effect_mode=COALESCE(account_effect_mode,'RECOMMENDED_TARGET')")
            con.execute("UPDATE pending_lots SET original_contribution_event_id=COALESCE(original_contribution_event_id,event_id),contract_version=COALESCE(contract_version,'S2-account-v3.0-frozen')")
            con.execute("UPDATE account_state SET engine=?,updated_at=? WHERE id=1",(pickle.dumps(upgraded),datetime.now().isoformat()))
            con.execute("INSERT OR REPLACE INTO account_metadata VALUES('account_contract_version',?)",(CONTRACT,))
            con.execute("INSERT INTO migration_runs VALUES(?,?,?)",(VERSION,datetime.now().isoformat(),json.dumps({"before_state":before_state,"after_state":after_state,"legacy_history_recomputed":False})))
        con.commit()
    except Exception:
        con.rollback();raise
    finally:con.close()
    return {"status":"PASS","migration_version":VERSION,"contract_version":CONTRACT,"database":str(db_path),"backup":str(backup_path) if backup_path else None,"before_hash":before_hash,"after_hash":sha256(db_path),"before_state":before_state,"after_state":after_state,"wealth_unchanged":before_state[0]==after_state[0],"legacy_history_recomputed":False,"already_applied":already_applied}


if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("db");parser.add_argument("--backup-dir");parser.add_argument("--evidence")
    args=parser.parse_args();result=migrate(Path(args.db),Path(args.backup_dir) if args.backup_dir else None)
    if args.evidence:Path(args.evidence).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False))
