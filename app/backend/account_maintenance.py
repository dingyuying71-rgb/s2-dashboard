"""Account Maintenance V1 service layer.

This is intentionally separate from S2-account-v4.0-frozen.  It adds
auditable maintenance metadata, constrained provisional-order replacement,
and account catalog/archive management without changing any frozen reducer
method or reinterpreting legacy events.
"""
from __future__ import annotations

import hashlib
import json
import os
import pickle
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .account_engine import PendingCashLot, UnsettledLot, D0, D1, EPS, dec
from .account_engine_v4 import AccountEngineV4
from .multi_user import AccountContext, MultiUserRuntime, MultiUserSecurityError, DbResolver
from .multi_user_admin import DEFAULT_USER_SETTINGS, PRIVATE_SCHEMA

VERSION = "1.0"
WINDOW_DAYS = 30
EXECUTION_LATE_WINDOW_DAYS = 365


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def read_engine(con: sqlite3.Connection) -> AccountEngineV4:
    engine = pickle.loads(con.execute("SELECT engine FROM account_state WHERE id=1").fetchone()[0])
    if not isinstance(engine, AccountEngineV4):
        raise MultiUserSecurityError("MIGRATION_REQUIRED", 503)
    return engine


def ensure_private_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS maintenance_late_entries(
          event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,occurred_at TEXT NOT NULL,
          entered_at TEXT NOT NULL,accounting_effective_at TEXT NOT NULL,
          late_entry_mode TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS maintenance_corrections(
          correction_event_id TEXT PRIMARY KEY,revision_chain_id TEXT NOT NULL,
          revision_no INTEGER NOT NULL,references_event_id TEXT NOT NULL,
          supersedes_event_id TEXT NOT NULL,replacement_event_id TEXT NOT NULL UNIQUE,
          reason TEXT NOT NULL,corrected_at TEXT NOT NULL,status TEXT NOT NULL,
          payload_json TEXT NOT NULL,UNIQUE(revision_chain_id,revision_no)
        );
        CREATE TABLE IF NOT EXISTS maintenance_metadata_corrections(
          event_id TEXT PRIMARY KEY,references_event_id TEXT NOT NULL,reason TEXT NOT NULL,
          occurred_at TEXT,platform_reference TEXT,entered_at TEXT NOT NULL,status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS maintenance_execution_records(
          event_id TEXT PRIMARY KEY,signal_id TEXT NOT NULL,recommendation_id TEXT NOT NULL,
          fund_code TEXT NOT NULL,side TEXT NOT NULL,recommended_action TEXT NOT NULL,
          recommended_amount TEXT NOT NULL,cash_debited TEXT,reported_fee TEXT,
          redemption_units TEXT,estimated_redemption_amount TEXT,status TEXT NOT NULL,
          reported_at TEXT NOT NULL,parent_event_id TEXT,created_at TEXT NOT NULL,payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_maintenance_execution_signal ON maintenance_execution_records(signal_id,reported_at DESC);
        """
    )
    # V1.1 adds evidence fields without rewriting any existing maintenance
    # event.  SQLite has no IF NOT EXISTS for ALTER COLUMN, so inspect first.
    columns = {row[1] for row in con.execute("PRAGMA table_info(maintenance_late_entries)")}
    for name, ddl in {
        "platform_reference": "TEXT NOT NULL DEFAULT ''",
        "ocr_image_sha256": "TEXT",
        "ocr_filename": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in columns:
            con.execute(f"ALTER TABLE maintenance_late_entries ADD COLUMN {name} {ddl}")


def _store_execution_projection(con: sqlite3.Connection, event_id: str, order: dict[str, Any], parent_event_id: str | None = None) -> None:
    con.execute(
        "INSERT INTO maintenance_execution_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id,order["signal_id"],order["recommendation_id"],order["fund_code"],order["side"],order["recommended_action"],order["recommended_amount"],order.get("cash_debited"),order.get("reported_fee"),order.get("redemption_units"),order.get("estimated_redemption_amount"),order["status"],order["reported_at"],parent_event_id,order["reported_at"],json.dumps(order,ensure_ascii=False,default=str)),
    )


def _store_canonical_execution(
    con: sqlite3.Connection,
    event_id: str,
    order: dict[str, Any],
    source: sqlite3.Row,
    effective: datetime,
) -> None:
    """Expose a corrected provisional order through the normal V4 workflow."""
    source_keys = set(source.keys())
    fund_name = source["fund_name"] if "fund_name" in source_keys else order["fund_code"]
    schema_version = source["schema_version"] if "schema_version" in source_keys else "4.1"
    con.execute(
        """INSERT INTO v4_executions(
            event_id,signal_id,recommendation_id,contract_version,schema_version,fund_code,fund_name,
            side,reported_at,recommended_action,recommended_amount,actual_order_type,cash_debited,
            reported_fee,fee_status,redemption_units,estimated_redemption_amount,execution_deviation,
            user_note,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id, order["signal_id"], order["recommendation_id"], order["contract_version"],
            schema_version, order["fund_code"], fund_name, order["side"], order["reported_at"],
            order["recommended_action"], order["recommended_amount"],
            "SUBSCRIPTION_CASH_DEBIT" if order["side"] == "BUY" else "REDEMPTION_UNITS",
            order.get("cash_debited"), order.get("reported_fee"), order["fee_status"],
            order.get("redemption_units"), order.get("estimated_redemption_amount"),
            order["execution_deviation"], order.get("note", ""), order["status"], effective.isoformat(),
        ),
    )


def _store_event(con: sqlite3.Connection, engine: AccountEngineV4, event_id: str, event_type: str, result: dict, effective: datetime, note: str = "", causation: str | None = None) -> None:
    con.execute("UPDATE account_state SET engine=?,updated_at=? WHERE id=1", (pickle.dumps(engine), effective.isoformat()))
    sequence = con.execute("SELECT COALESCE(MAX(event_sequence),0)+1 FROM events").fetchone()[0]
    con.execute(
        "INSERT INTO events(event_id,event_type,timestamp,status,payload,contract_version,schema_version,event_sequence,effective_at,correlation_id,causation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, event_type, effective.isoformat(), result.get("status", "APPLIED"), json.dumps(result, ensure_ascii=False, default=str), engine.contract_version, "maintenance-1.0", sequence, effective.isoformat(), None, causation),
    )
    amount = result.get("amount", result.get("cash_debited", result.get("requested", "0"))) or "0"
    con.execute("INSERT INTO transactions(event_id,timestamp,type,amount,status,note) VALUES(?,?,?,?,?,?)", (event_id, effective.isoformat(), event_type, str(amount), result.get("status", "APPLIED"), note))
    for row in reversed(engine.audit_log):
        if row["event_id"] == event_id:
            con.execute("INSERT INTO audit_log(event_id,payload) VALUES(?,?)", (event_id, json.dumps(row, ensure_ascii=False)))
            break


def _persist_pending(con: sqlite3.Connection, engine: AccountEngineV4) -> None:
    for lot in engine.pending_lots:
        con.execute(
            "INSERT OR REPLACE INTO pending_lots(lot_id,event_id,amount,status,cleared_at,batch_id,original_contribution_event_id,contract_version) VALUES(?,?,?,?,?,?,?,?)",
            (lot.lot_id, lot.event_id, str(lot.remaining_amount), lot.status, lot.cleared_at.isoformat(), lot.eligible_batch_id, lot.event_id, engine.contract_version),
        )


def merge_eligible_pending_for_execution(engine: AccountEngineV4, signal_id: str, cutoff: datetime) -> dict[str, Any]:
    """Apply the existing V4 Pending->StrategyCash transition for late BUYs."""
    batch_id = f"BATCH-{signal_id}"
    eligible = engine.freeze_pending(batch_id, cutoff)
    amount = D0
    lot_ids: list[str] = []
    for lot in eligible:
        amount += lot.remaining_amount
        lot_ids.append(lot.lot_id)
        engine.C += lot.remaining_amount
        lot.remaining_amount = D0
        lot.status = "MERGED"
    return {"batch_id": batch_id, "amount": amount, "lot_ids": lot_ids, "count": len(lot_ids)}


def late_entry(
    con: sqlite3.Connection,
    body: dict[str, Any],
    effective: datetime,
    allowed_funds: set[str],
    execution_cutoff: datetime | None = None,
) -> dict:
    ensure_private_schema(con)
    event_id = body["event_id"]
    existing = con.execute("SELECT * FROM maintenance_late_entries WHERE event_id=?", (event_id,)).fetchone()
    if existing:
        return {"status": "ALREADY_PROCESSED", "event_id": event_id}
    occurred = body["occurred_at"]
    window_days = EXECUTION_LATE_WINDOW_DAYS if body["kind"] == "EXECUTION_REPORTED" else WINDOW_DAYS
    if effective - occurred > timedelta(days=window_days):
        raise MultiUserSecurityError("LATE_ENTRY_WINDOW_EXCEEDED", 422)
    if occurred > effective + timedelta(minutes=1):
        raise MultiUserSecurityError("INVALID_OCCURRED_AT", 422)
    engine = read_engine(con)
    kind = body["kind"]
    if kind == "CONTRIBUTION":
        result = engine.contribution(event_id, effective, body["amount"], f"LATE-{event_id}", effective, f"LATE-{effective.date().isoformat()}")
        event_type = "EXTERNAL_CONTRIBUTION"
    elif kind == "WITHDRAWAL":
        result = engine.withdrawal(event_id, effective, body["amount"], body.get("fee_rate", D0), effective)
        event_type = "EXTERNAL_WITHDRAWAL_REQUEST"
    elif kind == "EXECUTION_REPORTED":
        if body.get("reported_status") != "PENDING_CONFIRMATION":
            raise MultiUserSecurityError("COMPLEX_HISTORICAL_REPAIR_NOT_SUPPORTED", 422)
        if body["fund_code"] not in allowed_funds:
            raise MultiUserSecurityError("FUND_NOT_ALLOWED", 422)
        if execution_cutoff is not None and body.get("actual_action") == "BUY":
            # Projection is read-only until this late-entry is confirmed.  On
            # commit, consume only this account's eligible signal batch so the
            # frozen reducer sees the same StrategyCash as a normal report.
            merge_eligible_pending_for_execution(engine, body["signal_id"], execution_cutoff)
        result = engine.report_execution(event_id, effective, body["signal_id"], body["recommendation_id"], body["recommended_action"], body["recommended_amount"], body["actual_action"], body["fund_code"], cash_debited=body.get("cash_debited"), fee=body.get("reported_fee"), redemption_units=body.get("redemption_units"), estimated_redemption_amount=body.get("estimated_redemption_amount"), note=body.get("note", ""))
        event_type = "EXECUTION_REPORTED"
    else:
        raise MultiUserSecurityError("LATE_ENTRY_KIND_UNSUPPORTED", 422)
    if result.get("status") != "APPLIED":
        return result
    _persist_pending(con, engine)
    _store_event(con, engine, event_id, event_type, result, effective, body.get("note", ""))
    if kind == "EXECUTION_REPORTED":
        _store_execution_projection(con, event_id, engine.v4_orders[event_id])
    columns = [row[1] for row in con.execute("PRAGMA table_info(maintenance_late_entries)")]
    values = {
        "event_id": event_id, "kind": kind, "occurred_at": occurred.isoformat(),
        "entered_at": effective.isoformat(), "accounting_effective_at": effective.isoformat(),
        "late_entry_mode": "AS_OF_NOW", "reason": body.get("reason", ""),
        "created_at": effective.isoformat(), "platform_reference": body.get("platform_reference", ""),
        "ocr_image_sha256": body.get("ocr_image_sha256"), "ocr_filename": body.get("ocr_filename", ""),
    }
    con.execute(
        f"INSERT INTO maintenance_late_entries ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        tuple(values.get(column) for column in columns),
    )
    return {**result, "occurred_at": occurred.isoformat(), "entered_at": effective.isoformat(), "accounting_effective_at": effective.isoformat(), "late_entry_mode": "AS_OF_NOW"}


def correction(con: sqlite3.Connection, body: dict[str, Any], effective: datetime) -> dict:
    ensure_private_schema(con)
    correction_id = body["correction_event_id"]
    existing = con.execute("SELECT * FROM maintenance_corrections WHERE correction_event_id=?", (correction_id,)).fetchone()
    if existing:
        return {"status": "ALREADY_PROCESSED", "correction_event_id": correction_id, "replacement_event_id": existing["replacement_event_id"]}
    original_id = body["references_event_id"]
    prior = con.execute("SELECT * FROM maintenance_corrections WHERE replacement_event_id=?", (original_id,)).fetchone()
    source_id = original_id
    source = con.execute("SELECT * FROM v4_executions WHERE event_id=?", (source_id,)).fetchone()
    if source is None:
        source = con.execute("SELECT * FROM maintenance_execution_records WHERE event_id=?", (source_id,)).fetchone()
    if source is None:
        raise MultiUserSecurityError("OBJECT_NOT_FOUND", 404)
    engine = read_engine(con)
    order = engine.v4_orders.get(source_id)
    if not order or order.get("status") != "PENDING_CONFIRMATION":
        raise MultiUserSecurityError("CORRECTION_NOT_SUPPORTED_FOR_FINALIZED_EVENT", 422)
    if con.execute("SELECT 1 FROM maintenance_corrections WHERE supersedes_event_id=?", (source_id,)).fetchone():
        raise MultiUserSecurityError("CORRECTION_NOT_LATEST_REVISION", 409)
    side = source["side"]
    replacement_id = body.get("replacement_event_id") or f"MNT-REPL-{uuid.uuid4().hex[:16]}"
    # Reverse the provisional projection only. The immutable event remains in
    # the audit log; the mutable order projection is marked SUPERSEDED.
    if side == "BUY":
        original_cash = dec(order["cash_debited"])
        lot = next((lot for lot in engine.unsettled_lots if lot.event_id == source_id and lot.status == "V4_PENDING_SUBSCRIPTION"), None)
        if lot is None:
            raise MultiUserSecurityError("CORRECTION_NOT_SUPPORTED_FOR_FINALIZED_EVENT", 422)
        engine.C += original_cash
        lot.status = "SUPERSEDED"
    else:
        original_cash = D0
    engine.v4_orders[source_id]["status"] = "SUPERSEDED"
    engine.signal_executions.pop(order["signal_id"], None)
    replacement = engine.report_execution(
        replacement_id, effective, order["signal_id"], order["recommendation_id"], order["recommended_action"], order["recommended_amount"], side,
        body.get("fund_code") or order["fund_code"],
        cash_debited=body.get("cash_debited") if side == "BUY" else None,
        fee=body.get("reported_fee"),
        redemption_units=body.get("redemption_units") if side == "SELL" else None,
        estimated_redemption_amount=body.get("estimated_redemption_amount") if side == "SELL" else None,
        note=body.get("note", ""),
    )
    if replacement.get("status") != "APPLIED":
        raise MultiUserSecurityError("CORRECTION_REPLACEMENT_REJECTED", 422)
    root = prior["revision_chain_id"] if prior else source_id
    next_revision = con.execute("SELECT COALESCE(MAX(revision_no),0)+1 FROM maintenance_corrections WHERE revision_chain_id=?", (root,)).fetchone()[0]
    details = {"original_event_id": source_id, "reversed_provisional_cash": str(original_cash), "replacement": replacement}
    _persist_pending(con, engine)
    _store_event(con, engine, replacement_id, "EXECUTION_REPORTED", replacement, effective, body.get("note", ""), source_id)
    _store_execution_projection(con, replacement_id, engine.v4_orders[replacement_id], source_id)
    con.execute("UPDATE v4_executions SET status='SUPERSEDED' WHERE event_id=?", (source_id,))
    con.execute("UPDATE maintenance_execution_records SET status='SUPERSEDED' WHERE event_id=?", (source_id,))
    _store_canonical_execution(con, replacement_id, engine.v4_orders[replacement_id], source, effective)
    correction_result = {"status": "APPLIED", "correction_event_id": correction_id, "references_event_id": source_id, "replacement_event_id": replacement_id, "revision_chain_id": root, "revision_no": next_revision}
    _store_event(con, engine, correction_id, "EXECUTION_CORRECTION", correction_result, effective, body.get("reason", ""), source_id)
    con.execute(
        "INSERT INTO maintenance_corrections VALUES(?,?,?,?,?,?,?,?,?,?)",
        (correction_id, root, next_revision, original_id, source_id, replacement_id, body["reason"], effective.isoformat(), "APPLIED", json.dumps(details, ensure_ascii=False, default=str)),
    )
    return correction_result


def validate_snapshot(snapshot: dict[str, Any], funds: set[str], baseline: datetime) -> dict[str, Decimal | str]:
    if not snapshot.get("evidence_note", "").strip():
        raise MultiUserSecurityError("OPENING_SNAPSHOT_EVIDENCE_REQUIRED", 422)
    if baseline > datetime.now().astimezone().replace(tzinfo=None) + timedelta(minutes=1):
        raise MultiUserSecurityError("INVALID_BASELINE_DATE", 422)
    values = {key: dec(snapshot.get(key, D0)) for key in ("fund_units", "fund_nav", "fund_value", "strategy_cash", "reserve_cash", "pending_cash", "subscription_pending", "redemption_receivable")}
    if any(value < D0 for value in values.values()):
        raise MultiUserSecurityError("NEGATIVE_OPENING_BUCKET", 422)
    if values["fund_value"] > D0:
        if snapshot.get("fund_code") not in funds or values["fund_units"] <= D0 or values["fund_nav"] <= D0:
            raise MultiUserSecurityError("INVALID_FUND_OPENING_SNAPSHOT", 422)
        if abs(values["fund_units"] * values["fund_nav"] - values["fund_value"]) > Decimal("0.01"):
            raise MultiUserSecurityError("FUND_VALUE_NAV_MISMATCH", 422)
    elif values["fund_units"] > D0 or values["fund_nav"] > D0:
        raise MultiUserSecurityError("INVALID_FUND_OPENING_SNAPSHOT", 422)
    total = sum(values[key] for key in values if key != "fund_nav")
    if total <= D0:
        raise MultiUserSecurityError("OPENING_SNAPSHOT_EMPTY", 422)
    values["total"] = total
    return values


def create_opening_account(db_path: Path, account_id: str, snapshot: dict[str, Any], baseline: datetime, funds: set[str]) -> str:
    values = validate_snapshot(snapshot, funds, baseline)
    db_path.parent.mkdir(parents=True, exist_ok=False)
    engine = AccountEngineV4(values["fund_value"], values["strategy_cash"], values["reserve_cash"], baseline, initial_nav="1.000000", fund_shares=values["fund_units"])
    engine.pending_lots = []
    if values["pending_cash"] > D0:
        engine.pending_lots.append(PendingCashLot(f"OPEN-P-{account_id}", f"OPEN-{account_id}", values["pending_cash"], values["pending_cash"], baseline, baseline, "OPENING", "CLEARED_NOT_ELIGIBLE", "OPENING_SNAPSHOT"))
    engine.unsettled_lots = []
    if values["subscription_pending"] > D0:
        engine.unsettled_lots.append(UnsettledLot(f"OPEN-S-{account_id}", f"OPEN-{account_id}", "V4_PENDING_SUBSCRIPTION", values["subscription_pending"], baseline, baseline, "OPENING", "V4_PENDING_SUBSCRIPTION"))
    if values["redemption_receivable"] > D0:
        engine.unsettled_lots.append(UnsettledLot(f"OPEN-R-{account_id}", f"OPEN-{account_id}", "V4_REDEMPTION_RECEIVABLE", values["redemption_receivable"], baseline, baseline, "OPENING", "V4_REDEMPTION_RECEIVABLE"))
    engine.units = values["total"]
    engine.nav_hwm = D1
    engine.cumulative_contributions = values["total"]
    engine.external_cashflows = [(baseline, -values["total"], "OPENING_SNAPSHOT")]
    engine._validate()
    with sqlite3.connect(db_path) as con:
        con.executescript(PRIVATE_SCHEMA)
        ensure_private_schema(con)
        con.execute("INSERT INTO account_state VALUES(1,?,?)", (pickle.dumps(engine), baseline.isoformat()))
        con.executemany("INSERT INTO settings VALUES(?,?)", DEFAULT_USER_SETTINGS.items())
        con.executemany("INSERT INTO account_metadata VALUES(?,?)", (("account_contract_version", engine.contract_version), ("multi_user_schema_version", "1"), ("migration_status", "CURRENT"), ("account_id", account_id), ("maintenance_version", VERSION)))
        payload = {"account_id": account_id, "opening_snapshot": {key: str(value) for key, value in values.items()}, "evidence_note": snapshot["evidence_note"]}
        con.execute("INSERT INTO events(event_id,event_type,timestamp,status,payload,contract_version,schema_version,event_sequence,effective_at) VALUES(?,?,?,?,?,?,?,?,?)", (f"OPEN-{account_id}", "OPENING_SNAPSHOT", baseline.isoformat(), "APPLIED", json.dumps(payload, ensure_ascii=False), engine.contract_version, "maintenance-1.0", 1, baseline.isoformat()))
    DbResolver.verify_schema(db_path)
    return sha256(db_path)


def restart(runtime: MultiUserRuntime, context: AccountContext, confirmation: str, snapshot: dict[str, Any], reason: str, funds: set[str], effective: datetime, operation_id: str) -> dict:
    if confirmation != "重新开始":
        raise MultiUserSecurityError("RESTART_CONFIRMATION_REQUIRED", 422)
    runtime.registry.initialize()
    old = runtime.registry.active_account(context.internal_user_id, context.account_id)
    if old is None:
        raise MultiUserSecurityError("OBJECT_NOT_FOUND", 404)
    with runtime.registry.session() as registry:
        previous = registry.execute("SELECT * FROM maintenance_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if previous:
            return {"status": "ALREADY_PROCESSED", "operation_id": operation_id, "new_account_id": previous["new_account_id"]}
        new_id, archive_id = f"acct_{uuid.uuid4().hex}", old["account_id"]
        registry.execute("INSERT INTO maintenance_operations(operation_id,user_id,old_account_id,new_account_id,stage,status,started_at,updated_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?)", (operation_id, context.internal_user_id, old["account_id"], new_id, "PREPARED", "RUNNING", effective.isoformat(), effective.isoformat(), json.dumps({"archive_id": archive_id, "reason": reason}, ensure_ascii=False)))
    old_path = context.account_db
    user_root = runtime.resolver.users_root / context.internal_user_id
    archive_dir = user_root / "archives" / archive_id
    new_dir = user_root / "active" / new_id
    backup_dir = runtime.backups_root.parent / "account_restart" / context.internal_user_id / f"{effective:%Y%m%d_%H%M%S}_{operation_id[-8:]}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup_path = backup_dir / "old_account.db"
    source = sqlite3.connect(old_path); target = sqlite3.connect(backup_path)
    try: source.backup(target)
    finally: target.close(); source.close()
    old_hash = sha256(old_path)
    archive_dir.mkdir(parents=True, exist_ok=False)
    staged_archive = archive_dir / ".account.db.staging"
    shutil.copy2(old_path, staged_archive)
    if sha256(staged_archive) != old_hash:
        raise MultiUserSecurityError("ARCHIVE_HASH_FAILED", 503)
    new_hash = create_opening_account(new_dir / "account.db", new_id, snapshot, effective, funds)
    manifest = {"account_id": archive_id, "source_account_id": old["account_id"], "archived_at": effective.isoformat(), "reason": reason, "sha256": old_hash, "read_only": True}
    (archive_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (new_dir / "manifest.json").write_text(json.dumps({"account_id": new_id, "created_at": effective.isoformat(), "maintenance_version": VERSION}, ensure_ascii=False, indent=2), encoding="utf-8")
    # Make the immutable copy durable before the registry pointer changes.  A
    # failed registry commit can leave an orphan copy, but never loses the
    # still-active source account or points an account to a missing archive.
    os.replace(staged_archive, archive_dir / "account.db")
    try: os.chmod(archive_dir / "account.db", 0o444)
    except OSError: pass
    with runtime.registry.session() as registry:
        registry.execute("BEGIN IMMEDIATE")
        with sqlite3.connect(f"file:{old_path.as_posix()}?mode=ro", uri=True) as old_con:
            ending_wealth = str(read_engine(old_con).total_wealth)
        registry.execute("INSERT INTO account_catalog(account_id,user_id,status,db_relative_path,display_name,account_type,lineage_id,is_default,updated_at,created_at,manifest_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (new_id, context.internal_user_id, "STAGING", f"active/{new_id}/account.db", old["display_name"], old["account_type"], old["lineage_id"], old["is_default"], effective.isoformat(), effective.isoformat(), (new_dir / "manifest.json").read_text(encoding="utf-8")))
        registry.execute("UPDATE account_catalog SET status='ARCHIVED',db_relative_path=?,archived_at=?,archive_reason=?,ending_wealth=?,manifest_json=?,is_default=0,updated_at=? WHERE account_id=?", (f"archives/{archive_id}/account.db", effective.isoformat(), reason, ending_wealth, json.dumps(manifest, ensure_ascii=False), effective.isoformat(), old["account_id"]))
        registry.execute("UPDATE account_catalog SET status='ACTIVE' WHERE account_id=?", (new_id,))
        if old["is_default"]:
            registry.execute("UPDATE users SET current_account_id=? WHERE user_id=?", (new_id, context.internal_user_id))
        registry.execute("UPDATE maintenance_operations SET stage='POINTER_SWITCHED',status='COMPLETED',updated_at=?,old_hash=?,new_hash=?,backup_path_ref=? WHERE operation_id=?", (effective.isoformat(), old_hash, new_hash, str(backup_dir), operation_id))
    # Windows may retain a short-lived read handle from the request context,
    # so the verified copy becomes the immutable archive.  The former legacy
    # path is no longer resolvable by the account catalog and is made read-only
    # as a retired source; it is never a second ACTIVE account.
    try: os.chmod(old_path, 0o444)
    except OSError: pass
    backup_manifest = {"operation_id": operation_id, "old_account_id": old["account_id"], "archive_id": archive_id, "old_hash": old_hash, "new_account_id": new_id, "new_hash": new_hash, "pre_restart_summary": "preserved in archive"}
    (backup_dir / "manifest.json").write_text(json.dumps(backup_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "APPLIED", "operation_id": operation_id, "archive_id": archive_id, "new_account_id": new_id, "backup_path_ref": str(backup_dir)}
