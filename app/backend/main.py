"""Local FastAPI application. Account arithmetic is delegated to AccountEngine only."""
from __future__ import annotations

import csv
import io
import json
import os
import pickle
import shutil
import sqlite3
import uuid
from calendar import monthrange
from datetime import datetime, timedelta, time
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

try:
    from .account_engine import AccountEngine, dec, solve_target_after_fee
    from .account_engine_v4 import AccountEngineV4, V4_CANCELLABLE, V4_PENDING_OPEN
    from .multi_user import (
        MultiUserSecurityError, account_connection, bind_account_context,
        current_account_context, ensure_no_tenant_parameters, get_runtime,
        http_error, shared_connection,
    )
    from .multi_user_admin import initialize_empty_v4_account, provision_user
    from .path_config import resource_root, release_mode
    from .fund_nav_store import ensure_fund_nav_schema, latest_fund_nav
    from . import account_maintenance as maintenance
    from . import order_ocr as order_ocr_service
except ImportError:  # direct execution by the local test runner
    from account_engine import AccountEngine, dec, solve_target_after_fee
    from account_engine_v4 import AccountEngineV4, V4_CANCELLABLE, V4_PENDING_OPEN
    from multi_user import (
        MultiUserSecurityError, account_connection, bind_account_context,
        current_account_context, ensure_no_tenant_parameters, get_runtime,
        http_error, shared_connection,
    )
    from multi_user_admin import initialize_empty_v4_account, provision_user
    from path_config import resource_root, release_mode
    from fund_nav_store import ensure_fund_nav_schema, latest_fund_nav
    import account_maintenance as maintenance
    import order_ocr as order_ocr_service

ROOT = resource_root()
DATA = ROOT / "data"
LEGACY_DB_PATH = DATA / "dashboard.sqlite3"
CONTRACT = json.loads((Path(__file__).with_name("formula_contract.json")).read_text(encoding="utf-8"))
DATA_SOURCE_CONTRACT = json.loads((ROOT / "data_source_contract.json").read_text(encoding="utf-8"))
FUND_FEE_CONFIG = json.loads((ROOT / "config" / "fund_fee_config.json").read_text(encoding="utf-8"))
APP_TITLE = "中证红利指数股债利差策略仪表盘"

FUND_REGISTRY = json.loads((ROOT / "config" / "fund_whitelist.json").read_text(encoding="utf-8"))
FUNDS = [{"code": x["fund_code"], "name": x["fund_name"], **x} for x in FUND_REGISTRY["funds"]]
DEFAULTS = {"selected_fund": "007801", "profit_lock": "0", "dividend_route": "REINVEST", "fee_rate": "0.003", "settlement_days": "2", "beginner_mode": "true", "account_initialized": "false"}
SHANGHAI = ZoneInfo("Asia/Shanghai")


def now() -> datetime:
    """Shanghai wall-clock; an explicit QA clock is accepted only in QA."""
    qa_clock=os.environ.get("DASHBOARD_QA_CLOCK")
    if qa_clock and os.environ.get("S2_ENV","").upper()=="QA":return datetime.fromisoformat(qa_clock).replace(tzinfo=None,microsecond=0)
    return datetime.now(SHANGHAI).replace(tzinfo=None, microsecond=0)
def local_time(value: datetime) -> datetime:
    """Normalize API ISO timestamps to the contract's Shanghai wall-clock time."""
    if value.tzinfo is None:
        return value.replace(microsecond=0)
    return value.astimezone(SHANGHAI).replace(tzinfo=None, microsecond=0)
def dstr(x: Decimal | float | str) -> str: return str(x)

def connect():
    return account_connection()

def connect_shared(read_only: bool = False):
    return shared_connection(read_only=read_only)

def init_shared_db():
    runtime=get_runtime()
    # The Tester Portal may consume the production strategy store, but it
    # never initializes or writes that authoritative file.
    if runtime.shared_db_read_only:
        return
    runtime.shared_db.parent.mkdir(parents=True,exist_ok=True)
    with connect_shared() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS monthly_signals(signal_date TEXT PRIMARY KEY, spread REAL, percentile REAL, target_exposure REAL, action TEXT);
        CREATE TABLE IF NOT EXISTS market_observations(date TEXT PRIMARY KEY, dividend_yield REAL, cn10y REAL, spread REAL, spread_percentile REAL, target_exposure REAL, source_dy TEXT, source_bond TEXT, fetched_at TEXT, raw_hash TEXT, validation_status TEXT);
        CREATE TABLE IF NOT EXISTS data_update_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, status TEXT, data_date TEXT, details TEXT);
        CREATE TABLE IF NOT EXISTS official_signals(signal_date TEXT PRIMARY KEY, dividend_yield REAL, cn10y REAL, spread REAL, spread_percentile REAL, target_exposure REAL, source_dy TEXT, source_bond TEXT, generated_at TEXT, validation_status TEXT);
        """)
        ensure_fund_nav_schema(con)
        if con.execute("SELECT 1 FROM monthly_signals LIMIT 1").fetchone() is None: import_signals(con)

def import_signals(con):
    with (DATA / "s2_v2_monthly.csv").open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("signal_used"):
                # `signal_used` is the percentile. V2's frozen position mapping
                # is represented by the audited `position` field, not by the
                # raw percentile itself.
                pos = float(r["position"])
                con.execute("INSERT OR REPLACE INTO monthly_signals VALUES(?,?,?,?,?)", (r["date"], float(r["spread"]), float(r["s2_score"]), pos, r["trade_action"] or "HOLD"))

def get_engine() -> AccountEngine:
    with connect() as con: return pickle.loads(con.execute("SELECT engine FROM account_state WHERE id=1").fetchone()["engine"])

def backup_db():
    if get_runtime().disable_event_backup:return None
    context=current_account_context();source=context.account_db
    if source.exists() and source.stat().st_size:
        target_dir=get_runtime().backups_root/context.internal_user_id/context.account_id
        target_dir.mkdir(parents=True,exist_ok=True)
        target=target_dir/f"account_{now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.sqlite3"
        src=sqlite3.connect(source);dst=sqlite3.connect(target)
        try:src.backup(dst)
        finally:dst.close();src.close()
        return target

def settings(con=None):
    close = con is None; con = con or connect()
    result = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM settings")}
    if close: con.close()
    return result

def selected_fund(con=None):
    code = settings(con).get("selected_fund", "007801")
    return next((x for x in FUNDS if x["code"] == code), FUNDS[0])


def selected_fund_nav(con=None):
    fund = selected_fund(con)
    with connect_shared(read_only=True) as shared:
        observation = latest_fund_nav(shared, fund["code"], now().date())
    return fund, observation


def fund_market_value(engine, nav_observation=None) -> Decimal:
    shares = dec(getattr(engine, "fund_shares", 0))
    if shares > 0 and nav_observation:
        return shares * dec(nav_observation["nav"])
    return dec(engine.F)

def persist(engine: AccountEngine, event_id: str | None = None, event_type: str = "", result: dict | None = None, note: str = ""):
    backup_db()
    with connect() as con:
        con.execute("UPDATE account_state SET engine=?,updated_at=? WHERE id=1", (pickle.dumps(engine), now().isoformat()))
        fund = selected_fund(con)
        con.execute("INSERT OR REPLACE INTO fund_holdings VALUES(1,?,?,?,?)", (fund["code"], fund["name"], str(engine.F), now().isoformat()))
        for lot in engine.pending_lots:
            pending_cols={r[1] for r in con.execute("PRAGMA table_info(pending_lots)")}
            if "contract_version" in pending_cols:
                con.execute("INSERT OR REPLACE INTO pending_lots(lot_id,event_id,amount,status,cleared_at,batch_id,original_contribution_event_id,contract_version) VALUES(?,?,?,?,?,?,?,?)", (lot.lot_id, lot.event_id, str(lot.remaining_amount), lot.status, lot.cleared_at.isoformat(), lot.eligible_batch_id, lot.event_id, getattr(engine,"contract_version",CONTRACT["contract_version"])))
            else:
                con.execute("INSERT OR REPLACE INTO pending_lots(lot_id,event_id,amount,status,cleared_at,batch_id) VALUES(?,?,?,?,?,?)", (lot.lot_id, lot.event_id, str(lot.remaining_amount), lot.status, lot.cleared_at.isoformat(), lot.eligible_batch_id))
        for lot in engine.unsettled_lots:
            con.execute("INSERT INTO settlements(lot_id,event_id,kind,amount,available_at,status) VALUES(?,?,?,?,?,?)", (lot.lot_id, lot.event_id, lot.kind, str(lot.amount), lot.available_at.isoformat(), lot.status))
        if event_id:
            con.execute("INSERT OR REPLACE INTO events(event_id,event_type,timestamp,status,payload) VALUES(?,?,?,?,?)", (event_id, event_type, now().isoformat(), (result or {}).get("status", "APPLIED"), json.dumps(result or {}, ensure_ascii=False, default=str)))
            con.execute("INSERT INTO transactions(event_id,timestamp,type,amount,status,note) VALUES(?,?,?,?,?,?)", (event_id, now().isoformat(), event_type, str((result or {}).get("amount", (result or {}).get("requested", "0"))), (result or {}).get("status", "APPLIED"), note))
            for row in engine.audit_log[-2:]:
                if row["event_id"] == event_id: con.execute("INSERT INTO audit_log(event_id,payload) VALUES(?,?)", (event_id, json.dumps(row, ensure_ascii=False)))

def db_contract_version(con=None) -> str:
    close=con is None;con=con or connect()
    try:
        exists=con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_metadata'").fetchone()
        row=con.execute("SELECT value FROM account_metadata WHERE key='account_contract_version'").fetchone() if exists else None
        return row["value"] if row else CONTRACT["contract_version"]
    finally:
        if close:con.close()

def persist_v4_tx(con, engine: AccountEngineV4, event_id: str, event_type: str, result: dict, note: str = ""):
    """Persist engine, projections and immutable event in the caller transaction."""
    con.execute("UPDATE account_state SET engine=?,updated_at=? WHERE id=1",(pickle.dumps(engine),now().isoformat()))
    fund=selected_fund(con)
    con.execute("INSERT OR REPLACE INTO fund_holdings VALUES(1,?,?,?,?)",(fund["code"],fund["name"],str(engine.F),now().isoformat()))
    for lot in engine.pending_lots:
        con.execute("INSERT OR REPLACE INTO pending_lots(lot_id,event_id,amount,status,cleared_at,batch_id,original_contribution_event_id,contract_version) VALUES(?,?,?,?,?,?,?,?)",
                    (lot.lot_id,lot.event_id,str(lot.remaining_amount),lot.status,lot.cleared_at.isoformat(),lot.eligible_batch_id,lot.event_id,engine.contract_version))
    sequence=con.execute("SELECT COALESCE(MAX(event_sequence),0)+1 FROM events").fetchone()[0]
    con.execute("INSERT INTO events(event_id,event_type,timestamp,status,payload,contract_version,schema_version,event_sequence,effective_at,correlation_id,causation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (event_id,event_type,now().isoformat(),result.get("status","APPLIED"),json.dumps(result,ensure_ascii=False,default=str),engine.contract_version,"4.1",sequence,now().isoformat(),result.get("signal_id"),result.get("execution_event_id")))
    amount=result.get("amount",result.get("cash_debited",result.get("settled_cash_amount","0"))) or "0"
    con.execute("INSERT INTO transactions(event_id,timestamp,type,amount,status,note) VALUES(?,?,?,?,?,?)",(event_id,now().isoformat(),event_type,str(amount),result.get("status","APPLIED"),note))
    for row in reversed(engine.audit_log):
        if row["event_id"]==event_id:
            con.execute("INSERT INTO audit_log(event_id,payload) VALUES(?,?)",(event_id,json.dumps(row,ensure_ascii=False)));break

def v4_execution_for_signal(signal_date: str | None, con=None):
    if not signal_date:return None
    close=con is None;con=con or connect()
    try:
        exists=con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='v4_executions'").fetchone()
        row=con.execute("SELECT * FROM v4_executions WHERE signal_id=? AND status NOT IN ('REJECTED','CANCELLED','SUPERSEDED') ORDER BY created_at DESC LIMIT 1",(signal_date,)).fetchone() if exists else None
        return dict(row) if row else None
    finally:
        if close:con.close()

V4_REASON_ZH={
    "ACTION_MISMATCH":"实际操作方向与本次策略建议不一致。V4当前版本不支持反向手动执行。",
    "INVALID_ACTUAL_AMOUNT":"实际执行金额必须大于0。",
    "INSUFFICIENT_STRATEGY_CASH":"实际平台扣款超过可用策略现金。",
    "INSUFFICIENT_REDEEMABLE_UNITS":"实际赎回份额超过当前可赎回份额。",
    "SIGNAL_ALREADY_EXECUTED":"本次正式信号已经存在执行记录。",
    "BUY_CONFIRMATION_DOES_NOT_MATCH_CASH_DEBIT":"确认本金与已记录的平台实际扣款不一致。请先在“设置 → 账户维护 → 修正错误记录”中修正订单扣款金额，再录入确认结果。",
    "PENDING_NOT_CANCELLABLE":"该笔资金已经进入调仓或交易流程，如需取出请使用提款功能。",
    "PENDING_NOT_TRANSFERABLE":"该笔资金已经进入调仓或交易流程，不能再转入备用现金。",
    "CANCELLATION_EXCEEDS_REMAINING":"撤销金额超过该笔资金当前剩余金额。",
    "TRANSFER_EXCEEDS_REMAINING":"转入金额超过该笔资金当前剩余金额。",
}

def latest_signal():
    cutoff = now().date().isoformat()
    with connect_shared(read_only=True) as con:
        r = con.execute("SELECT * FROM monthly_signals WHERE signal_date<=? ORDER BY signal_date DESC LIMIT 1", (cutoff,)).fetchone()
    return dict(r) if r else None

def latest_market_observation():
    cutoff = now().date().isoformat()
    with connect_shared(read_only=True) as con:
        r = con.execute("SELECT * FROM market_observations WHERE validation_status='VALID' AND date<=? ORDER BY date DESC LIMIT 1", (cutoff,)).fetchone()
    return dict(r) if r else None

def latest_official_signal():
    cutoff = now().date().isoformat()
    with connect_shared(read_only=True) as con:
        r = con.execute("SELECT * FROM official_signals WHERE validation_status='VALID' AND signal_date<=? ORDER BY signal_date DESC LIMIT 1", (cutoff,)).fetchone()
    return dict(r) if r else None

def market_view():
    obs = latest_market_observation(); official = latest_official_signal()
    return {"latest_observation": obs, "latest_official_signal": official, "source_contract": {"migration_status": "PROVISIONAL_PASS", "historical_compatibility": "PARTIAL", "production_enabled": True, "cross_validation_required": True}}

def last_weekday(year, month):
    date = datetime(year, month, monthrange(year, month)[1]).date()
    while date.weekday() >= 5: date -= timedelta(days=1)
    return date

def next_rebalance_date(ref=None):
    ref = ref or now().date(); candidate = last_weekday(ref.year, ref.month)
    if ref > candidate:
        year, month = (ref.year + 1, 1) if ref.month == 12 else (ref.year, ref.month + 1)
        candidate = last_weekday(year, month)
    return candidate.isoformat()


def next_business_day(value):
    """Return the next weekday used by the frozen T+1 execution window."""
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def execution_window(signal_date: str, current=None, execution=None) -> dict:
    """Project the T+1 window without changing any account or signal state."""
    signal_day = datetime.fromisoformat(str(signal_date)).date()
    execution_day = next_business_day(signal_day)
    current_day = (current or now()).date()
    cutoff = datetime.combine(execution_day, time(14, 30))
    deadline = datetime.combine(execution_day, time(15, 0))
    if execution:
        status = execution.get("status")
        state = {"PENDING_CONFIRMATION": "CONFIRMATION_PENDING", "CONFIRMED": "EXECUTED",
                 "WAITING_SETTLEMENT": "EXECUTED", "SETTLED": "EXECUTED"}.get(status, "EXECUTED")
    elif current_day < signal_day:
        state = "SIGNAL_READY"
    elif current_day <= execution_day:
        state = "WAITING_FOR_EXECUTION" if current_day == signal_day else "EXECUTION_DAY"
    else:
        state = "EXECUTION_DAY"
    return {"signal_date": signal_date, "execution_date": execution_day.isoformat(),
            "cutoff": cutoff.isoformat(), "deadline": deadline.isoformat(),
            "state": state, "is_execution_day": current_day == execution_day}


def projected_eligible_pending(engine, signal_date: str, cutoff: datetime) -> dict:
    """Read-only eligibility projection; never mutates lot statuses."""
    batch_id = f"BATCH-{signal_date}"
    lots = []
    for lot in getattr(engine, "pending_lots", []):
        if (lot.status in V4_PENDING_OPEN and lot.remaining_amount > 0
                and lot.eligible_batch_id == batch_id and lot.cleared_at <= cutoff):
            lots.append(lot)
    return {"batch_id": batch_id, "amount": sum((lot.remaining_amount for lot in lots), Decimal(0)),
            "lot_ids": [lot.lot_id for lot in lots], "count": len(lots),
            "will_be_eligible": bool(lots)}


def merge_eligible_pending_for_execution(engine, signal_date: str, cutoff: datetime) -> dict:
    """Apply the frozen Pending->StrategyCash transition for a real order report."""
    batch_id = f"BATCH-{signal_date}"
    eligible_lots = engine.freeze_pending(batch_id, cutoff)
    amount = Decimal(0)
    lot_ids = []
    for lot in eligible_lots:
        amount += lot.remaining_amount
        lot_ids.append(lot.lot_id)
        engine.C += lot.remaining_amount
        lot.remaining_amount = Decimal(0)
        lot.status = "MERGED"
    return {"batch_id": batch_id, "amount": amount, "lot_ids": lot_ids,
            "count": len(lot_ids)}


def current_account_type() -> str:
    context = current_account_context()
    row = get_runtime().registry.active_account(context.internal_user_id, context.account_id)
    return row["account_type"] if row else "UNKNOWN"


def fund_fee_profile(fund: dict, cfg: dict, account_type: str) -> dict:
    profile = FUND_FEE_CONFIG.get("funds", {}).get(fund["code"])
    if not profile:
        return {"fee_status": "FEE_PARTIAL", "product_status": "OPEN",
                "configured_product_status": "UNKNOWN", "account_type_eligible": None,
                "effective_buy_fee_rate": None,
                "source": "未找到该基金的可靠费率配置", "profile": None}
    status = profile.get("fee_status", "FEE_PARTIAL")
    # 009051's verified channel rate is deliberately account-local. Other
    # classes use only their own product profile and never inherit this value.
    if profile.get("subscription_fee", {}).get("rate_source") == "ACCOUNT_SETTING_OVERRIDE":
        rate = Decimal(str(cfg.get("fee_rate", "0")))
    else:
        tiers = profile.get("subscription_fee", {}).get("tiers", [])
        rate = Decimal(str(tiers[0].get("rate"))) if tiers and tiers[0].get("rate") is not None else None
    configured_product_status = profile.get("product_status", "OPEN")
    account_type_eligible = True
    product_status = configured_product_status
    if configured_product_status == "PENSION_ONLY":
        account_type_eligible = account_type == "PENSION"
        product_status = "OPEN" if account_type_eligible else "PRODUCT_RESTRICTION"
    return {"fee_status": status, "product_status": product_status,
            "configured_product_status": configured_product_status,
            "account_type_eligible": account_type_eligible,
            "effective_buy_fee_rate": rate, "source": profile.get("source", ""), "profile": profile}

def signal_label(q):
    if q >= .9: return "极度便宜"
    if q >= .8: return "明显便宜"
    if q >= .6: return "偏便宜"
    if q >= .4: return "中性"
    if q >= .2: return "偏贵"
    if q >= .1: return "明显偏贵"
    return "极度偏贵"

def initialized() -> bool:
    return settings().get("account_initialized", "false") == "true"

def account_view(engine=None):
    engine = engine or get_engine(); fund, nav_observation = selected_fund_nav()
    subscription=getattr(engine,"subscription_pending",Decimal(0));receivable=getattr(engine,"redemption_receivable",Decimal(0))
    marked_fund = fund_market_value(engine, nav_observation)
    active = marked_fund + engine.C + subscription + receivable
    economic_fund = marked_fund + subscription
    total_wealth = dec(engine.total_wealth) - dec(engine.F) + marked_fund
    fund={**fund,"shares":str(getattr(engine,"fund_shares",0)),"nav":nav_observation["nav"] if nav_observation else None,"nav_date":nav_observation["nav_date"] if nav_observation else None,"nav_status":"VALID" if nav_observation else "UNAVAILABLE"}
    state={**engine.state(),"LedgerFundValue":str(engine.F),"MarkedFundValue":str(marked_fund)}
    return {"is_initialized": initialized(), "contract_version":getattr(engine,"contract_version",CONTRACT["contract_version"]), "total_wealth": dstr(total_wealth), "fund_value": dstr(marked_fund), "ledger_fund_value":dstr(engine.F), "economic_fund_exposure":dstr(economic_fund), "strategy_cash": dstr(engine.C), "pending_cash": dstr(engine.pending_total), "reserve_cash": dstr(engine.R), "subscription_pending":dstr(subscription), "redemption_receivable":dstr(receivable), "unsettled": dstr(engine.unsettled_total), "cumulative_contributions": dstr(engine.cumulative_contributions), "cumulative_withdrawals": dstr(engine.cumulative_withdrawals), "actual_exposure": float(economic_fund / active) if active else 0, "twr": float(engine.twr()), "xirr": engine.xirr(now()), "fund": fund, "state": state}

def action_view(engine=None):
    engine = engine or get_engine(); official = latest_official_signal(); s = official or latest_signal()
    if not s: raise HTTPException(503, "缺少已导入策略数据")
    cfg = settings(); signal_percentile = float(s.get("spread_percentile", s.get("percentile", 0)))
    p = Decimal(str(s["target_exposure"])); fund, nav_observation = selected_fund_nav()
    execution = None
    if official:
        with connect() as con:
            execution = v4_execution_for_signal(official["signal_date"], con)
            if not execution:
                row = con.execute("SELECT * FROM execution_records WHERE signal_date=? AND status='EXECUTED'", (official["signal_date"],)).fetchone()
                execution = dict(row) if row else None
    window = execution_window(s["signal_date"], now(), execution)
    cutoff = datetime.fromisoformat(window["cutoff"])
    projected = projected_eligible_pending(engine, s["signal_date"], cutoff)
    profile = fund_fee_profile(fund, cfg, current_account_type())
    marked_fund = fund_market_value(engine, nav_observation)
    # Frozen V4 investable assets are F + C after merging only projected
    # eligible Pending. Subscription/receivable remain outside this domain.
    active = marked_fund + engine.C + projected["amount"]
    current_active = marked_fund + engine.C
    actual = float(marked_fund / current_active) if current_active > 0 else 0.0
    fee_rate = profile["effective_buy_fee_rate"]
    fee_complete = (profile["fee_status"] == "FEE_COMPLETE"
                    and profile["product_status"] == "OPEN"
                    and fee_rate is not None)
    solved = solve_target_after_fee(marked_fund, active, p, fee_rate or Decimal(0)) if active else {"trade": Decimal(0), "final_fund": Decimal(0), "fee": Decimal(0)}
    no_fee = solve_target_after_fee(marked_fund, active, p, Decimal(0)) if active else {"trade": Decimal(0), "final_fund": Decimal(0), "fee": Decimal(0)}
    trade = solved["trade"] if fee_complete else no_fee["trade"]
    observation = latest_market_observation()
    current_day = now().date()
    stale = (current_day - datetime.fromisoformat((observation or s)["date" if observation else "signal_date"]).date()).days > 5
    signal_complete = bool(observation and observation["date"] == s["signal_date"] and official and official["signal_date"] == s["signal_date"] and current_day >= datetime.fromisoformat(s["signal_date"]).date())
    if not initialized(): action = "UNINITIALIZED"
    elif execution: action = {"PENDING_CONFIRMATION":"EXECUTION_PENDING", "CONFIRMED":"CONFIRMED", "WAITING_SETTLEMENT":"WAITING_SETTLEMENT", "SETTLED":"EXECUTED"}.get(execution.get("status"), "EXECUTED")
    elif stale: action = "DATA_STALE"
    elif not signal_complete: action = "SIGNAL_BLOCKED"
    else: action = "BUY" if trade > 0 else "SELL" if trade < 0 else "HOLD"
    direction = "增加" if trade > 0 else "降低" if trade < 0 else "保持"
    if action == "UNINITIALIZED": headline, explanation = "尚未建立账户", "你目前尚未建立策略账户。请先输入准备投入本策略的资金。"
    elif action == "DATA_STALE": headline, explanation = "数据需要更新", "策略数据尚未更新，请先更新数据；系统不会使用旧数据生成正式买卖建议。"
    elif action == "SIGNAL_BLOCKED": headline, explanation = "月末数据不完整，暂不操作", "本月关键市场数据或正式月度信号尚未完整生成，系统暂不输出买卖指令。"
    elif execution: headline, explanation = ("已提交待确认", "实际基金订单已记录，正在等待基金公司确认净值和份额。") if action == "EXECUTION_PENDING" else ("本月已执行", "本月正式执行记录已保存。")
    elif window["state"] == "WAITING_FOR_EXECUTION":
        headline = "次交易日待执行"
        explanation = f"{s['signal_date']}正式信号已生成，次交易日{window['execution_date']} 15:00前执行；当前仓位{round(actual*100)}%，目标仓位{round(float(p)*100)}%。"
    elif window["state"] == "EXECUTION_DAY":
        headline = "今天是执行日"
        explanation = f"正式信号{ s['signal_date'] }进入执行窗口；当前仓位{round(actual*100)}%，目标仓位{round(float(p)*100)}%。"
    elif trade != 0: headline, explanation = ("需要买入" if trade > 0 else "需要卖出"), f"当前股债利差处于{round(signal_percentile * 100)}%历史分位，目标仓位{round(float(p)*100)}%，建议{direction}基金仓位。"
    elif engine.pending_total > 0: headline, explanation = "等待月度调仓", f"账户有¥{engine.pending_total}待配置资金，等待正式月度调仓。"
    else: headline, explanation = "平时无需操作", "当前没有新的月度执行单。"
    if profile["product_status"] == "PRODUCT_RESTRICTION":
        explanation += f" 执行基金 {fund['code']} 为个人养老金专属，当前账户类型 {current_account_type()} 不满足产品资格，状态 PRODUCT_RESTRICTION。"
    elif profile["fee_status"] != "FEE_COMPLETE":
        explanation += f" 执行基金 {fund['code']} 费率配置不完整，金额仅为不含费用理论值，状态 FEE_PARTIAL。"
    observation_spread = float(observation["spread"]) if observation else float(s["spread"])
    observation_percentile = float(observation["spread_percentile"]) if observation else signal_percentile
    redemption_units = abs(trade) / dec(nav_observation["nav"]) if trade < 0 and nav_observation else None
    # Keep the execution-state gate separate from the fee-aware projection.
    # A missed-day candidate can be SIGNAL_BLOCKED while still requiring the
    # exact same solver fields for a later, explicitly reported order.
    is_buy = trade > 0 and execution is None
    is_sell = trade < 0 and execution is None
    theoretical_delta = abs(no_fee["trade"]) if trade != 0 else Decimal(0)
    estimated_fee = solved["fee"] if fee_complete and is_buy else None
    net_subscription = solved["trade"] if fee_complete and is_buy else None
    gross_order = (solved["trade"] + solved["fee"]) if fee_complete and is_buy else None
    post_trade_wealth = solved["active_after_cost"] if fee_complete and (is_buy or is_sell) else None
    post_trade_fund = solved["final_fund"] if fee_complete and (is_buy or is_sell) else None
    post_trade_exposure = solved["achieved_exposure"] if fee_complete and (is_buy or is_sell) else None
    # BUY is entered on the platform as cash debited (principal plus fee);
    # SELL remains a redemption-units instruction under the frozen contract.
    display_amount = gross_order if is_buy else abs(trade) if is_sell else None
    final_amount = display_amount if fee_complete and action in {"BUY", "SELL"} else None
    if action == "BUY" and fee_complete:
        explanation += f" 已按申购费进行费率校正，预计扣费后实际基金仓位仍约为{float(post_trade_exposure) * 100:.2f}%。"
    return {"is_rebalance_day": window["is_execution_day"], "latest_signal_date": s["signal_date"], "latest_observation_date": observation["date"] if observation else None, "next_rebalance_date": window["execution_date"], "execution_state": window["state"], "execution_date": window["execution_date"], "execution_cutoff": window["cutoff"], "execution_deadline": window["deadline"], "spread": observation_spread, "spread_percentile": observation_percentile, "signal_status": signal_label(observation_percentile), "target_exposure": float(p), "actual_exposure": actual, "investable_assets": dstr(active), "projected_eligible_pending": dstr(projected["amount"]), "projected_eligible_lot_ids": projected["lot_ids"], "projected_eligible": projected["will_be_eligible"], "target_fund_value": dstr(post_trade_fund) if post_trade_fund is not None else dstr(no_fee["final_fund"]), "net_trade": dstr(trade), "theoretical_amount_before_fees": dstr(theoretical_delta), "theoretical_target_delta": dstr(theoretical_delta), "gross_order_amount": dstr(gross_order) if gross_order is not None else None, "cash_debited": dstr(gross_order) if gross_order is not None else None, "estimated_subscription_fee": dstr(estimated_fee) if estimated_fee is not None else None, "net_subscription_amount": dstr(net_subscription) if net_subscription is not None else None, "projected_post_trade_wealth": dstr(post_trade_wealth) if post_trade_wealth is not None else None, "projected_post_trade_fund_value": dstr(post_trade_fund) if post_trade_fund is not None else None, "projected_post_trade_exposure": float(post_trade_exposure) if post_trade_exposure is not None else None, "final_recommended_amount": dstr(final_amount) if final_amount is not None else None, "trade_fee": dstr(estimated_fee) if estimated_fee is not None else None, "action": action, "amount": dstr(display_amount) if display_amount is not None else "0", "recommended_redemption_units": dstr(redemption_units) if redemption_units is not None else None, "fund_nav": nav_observation["nav"] if nav_observation else None, "fund_nav_date": nav_observation["nav_date"] if nav_observation else None, "selected_fund": fund, "fee_status": profile["fee_status"], "fee_profile": profile["profile"], "fee_source": profile["source"], "product_status": profile["product_status"], "account_type": current_account_type(), "headline": headline, "explanation": explanation, "data_stale": stale, "official_signal_date": s["signal_date"], "execution": execution}

class TenantSafeModel(BaseModel):
    model_config=ConfigDict(extra="forbid",str_strip_whitespace=True)

class ContributionIn(TenantSafeModel):
    event_id: str = Field(default_factory=lambda: f"CONTRIB-{uuid.uuid4().hex[:12]}")
    amount: Decimal = Field(gt=0); cleared_at: datetime; note: str = ""

class WithdrawalIn(TenantSafeModel):
    event_id: str = Field(default_factory=lambda: f"WITHDRAW-{uuid.uuid4().hex[:12]}")
    amount: Decimal = Field(gt=0); note: str = ""

class InitializeIn(TenantSafeModel):
    event_id: str = Field(default_factory=lambda: f"INIT-{uuid.uuid4().hex[:12]}")
    strategy_capital: Decimal = Field(ge=0)
    reserve_cash: Decimal = Field(default=Decimal(0), ge=0)
    note: str = ""

class AccountContainerCreateIn(TenantSafeModel):
    display_name: str = Field(min_length=1, max_length=80)
    account_type: Literal["NORMAL", "PENSION", "OTHER"] = "NORMAL"

class AccountContainerRenameIn(TenantSafeModel):
    display_name: str = Field(min_length=1, max_length=80)

class AccountTypeChangeIn(TenantSafeModel):
    new_type: Literal["NORMAL", "PENSION"]
    user_confirmed: Literal[True]
    note: str = Field(default="", max_length=500)

class ImportAccountIn(TenantSafeModel):
    event_id: str = Field(default_factory=lambda: f"IMPORT-{uuid.uuid4().hex[:12]}")
    fund_code: str = "007801"
    fund_value: Decimal = Field(default=Decimal(0), ge=0)
    fund_shares: Decimal | None = Field(default=None, ge=0)
    strategy_cash: Decimal = Field(default=Decimal(0), ge=0)
    pending_cash: Decimal = Field(default=Decimal(0), ge=0)
    reserve_cash: Decimal = Field(default=Decimal(0), ge=0)
    note: str = ""

class SettingsIn(TenantSafeModel):
    selected_fund: str; profit_lock: Literal["0", "0.25", "0.5", "0.75"]; dividend_route: Literal["REINVEST", "STRATEGY_CASH", "RESERVE", "EXTERNAL"]; fee_rate: Decimal = Field(ge=0, le=.05); settlement_days: int = Field(ge=0, le=10); beginner_mode: bool = True

class ExecutionRecordIn(TenantSafeModel):
    signal_date: str
    actual_amount: Decimal = Field(ge=0)
    executed_at: datetime
    fund_code: str
    note: str = ""

class V4ExecutionReportIn(TenantSafeModel):
    event_id: str = Field(default_factory=lambda: f"V4-EXEC-{uuid.uuid4().hex[:12]}")
    signal_date: str
    actual_action: Literal["BUY", "SELL"]
    cash_debited: Decimal | None = Field(default=None, ge=0)
    reported_fee: Decimal | None = Field(default=None, ge=0)
    redemption_units: Decimal | None = Field(default=None, ge=0)
    estimated_redemption_amount: Decimal | None = Field(default=None, ge=0)
    fund_code: str
    reported_at: datetime
    note: str = ""

class V4ConfirmationIn(TenantSafeModel):
    confirmation_event_id: str = Field(default_factory=lambda: f"V4-CONF-{uuid.uuid4().hex[:12]}")
    execution_event_id: str
    confirmed_at: datetime
    confirmed_nav: Decimal = Field(gt=0)
    confirmed_units: Decimal = Field(gt=0)
    confirmed_principal: Decimal = Field(ge=0)
    confirmed_fee: Decimal = Field(ge=0)
    confirmed_value: Decimal = Field(ge=0)
    platform_reference: str = ""

class V4SettlementIn(TenantSafeModel):
    settlement_event_id: str = Field(default_factory=lambda: f"V4-SETTLE-{uuid.uuid4().hex[:12]}")
    execution_event_id: str
    settled_at: datetime
    destination: Literal["STRATEGY_CASH"] = "STRATEGY_CASH"

class V4PendingActionIn(TenantSafeModel):
    event_id: str = Field(default_factory=lambda: f"V4-PENDING-{uuid.uuid4().hex[:12]}")
    lot_id: str
    amount: Decimal = Field(gt=0)
    effective_at: datetime
    reason: str = ""

class MaintenanceLateEntryIn(TenantSafeModel):
    event_id: str = Field(default_factory=lambda: f"MNT-LATE-{uuid.uuid4().hex[:12]}")
    kind: Literal["CONTRIBUTION", "WITHDRAWAL", "EXECUTION_REPORTED"]
    occurred_at: datetime
    amount: Decimal | None = Field(default=None, gt=0)
    fee_rate: Decimal = Field(default=Decimal(0), ge=0, lt=1)
    reported_status: Literal["PENDING_CONFIRMATION"] | None = None
    signal_id: str | None = None
    recommendation_id: str | None = None
    recommended_action: Literal["BUY", "SELL"] | None = None
    recommended_amount: Decimal | None = Field(default=None, ge=0)
    actual_action: Literal["BUY", "SELL"] | None = None
    cash_debited: Decimal | None = Field(default=None, gt=0)
    reported_fee: Decimal | None = Field(default=None, ge=0)
    redemption_units: Decimal | None = Field(default=None, gt=0)
    estimated_redemption_amount: Decimal | None = Field(default=None, ge=0)
    fund_code: str | None = None
    platform_reference: str = Field(default="", max_length=500)
    ocr_image_sha256: str | None = Field(default=None, max_length=128)
    ocr_filename: str = Field(default="", max_length=255)
    reason: str = Field(min_length=1, max_length=500)
    note: str = ""

class OrderOCRIn(TenantSafeModel):
    image_data: str = Field(min_length=1)
    filename: str = Field(default="", max_length=255)

class MaintenanceCorrectionIn(TenantSafeModel):
    correction_event_id: str = Field(default_factory=lambda: f"MNT-CORR-{uuid.uuid4().hex[:12]}")
    references_event_id: str
    replacement_event_id: str | None = None
    cash_debited: Decimal | None = Field(default=None, gt=0)
    reported_fee: Decimal | None = Field(default=None, ge=0)
    redemption_units: Decimal | None = Field(default=None, gt=0)
    estimated_redemption_amount: Decimal | None = Field(default=None, ge=0)
    fund_code: str | None = None
    reason: str = Field(min_length=1, max_length=500)
    note: str = ""

class MaintenanceRestartIn(TenantSafeModel):
    operation_id: str = Field(default_factory=lambda: f"MNT-RESTART-{uuid.uuid4().hex[:12]}")
    confirmation: str
    reason: str = Field(min_length=1, max_length=500)
    baseline_date: datetime
    fund_code: str | None = None
    fund_units: Decimal = Field(default=Decimal(0), ge=0)
    fund_nav: Decimal = Field(default=Decimal(0), ge=0)
    fund_value: Decimal = Field(default=Decimal(0), ge=0)
    strategy_cash: Decimal = Field(default=Decimal(0), ge=0)
    reserve_cash: Decimal = Field(default=Decimal(0), ge=0)
    pending_cash: Decimal = Field(default=Decimal(0), ge=0)
    subscription_pending: Decimal = Field(default=Decimal(0), ge=0)
    redemption_receivable: Decimal = Field(default=Decimal(0), ge=0)
    evidence_note: str = Field(min_length=1, max_length=1000)

app = FastAPI(title=APP_TITLE)


def ensure_release_owner(runtime) -> dict:
    """Create one empty local OWNER on a clean packaged installation.

    This path is opt-in and only runs when S2_RELEASE_MODE is enabled. It
    never copies the development OWNER or any existing account database.
    """
    with runtime.registry.session() as con:
        row = con.execute("SELECT user_id FROM users WHERE access_sub=?", (runtime.owner_sub,)).fetchone()
        any_user = con.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    if row:
        runtime.registry.initialize()
        return {"status": "EXISTING", "user_id": row["user_id"]}
    if any_user:
        # A non-empty release data root belongs to an existing installation;
        # do not silently add or replace an OWNER.
        return {"status": "UNCHANGED"}
    result = provision_user(runtime, runtime.owner_email, runtime.owner_sub, "我的投资", "OWNER")
    runtime.registry.initialize()
    return {"status": "CREATED", "user_id": result["user_id"]}

@app.exception_handler(sqlite3.DatabaseError)
async def sqlite_error_handler(request:Request,exc:sqlite3.DatabaseError):
    return JSONResponse({"detail":"数据暂时不可用，系统不会生成新的交易指令。","error_code":"DATA_UNAVAILABLE"},status_code=503,headers={"Cache-Control":"no-store, private"})

@app.exception_handler(MultiUserSecurityError)
async def multi_user_error_handler(request:Request,exc:MultiUserSecurityError):
    error=http_error(exc)
    return JSONResponse({"detail":error.detail,"error_code":exc.code},status_code=error.status_code,headers={"Cache-Control":"no-store, private"})

@app.on_event("startup")
def startup():
    runtime=get_runtime();runtime.initialize(require_accounts=True)
    if release_mode():
        ensure_release_owner(runtime)
    init_shared_db()

@app.middleware("http")
async def multi_user_security(request: Request, call_next):
    path=request.url.path
    if not path.startswith("/api/") or path=="/api/health":
        return await call_next(request)
    runtime=get_runtime()
    try:
        ensure_no_tenant_parameters(request)
        identity=runtime.identity_provider.identity(request)
        selected_account_id=request.headers.get("x-s2-account-id", "").strip() or None
        context=runtime.account_context(identity, selected_account_id)
        if request.method.upper() in {"POST","PUT","PATCH","DELETE"}:
            runtime.validate_write_request(request,context)
        with bind_account_context(context):
            response=await call_next(request)
        response.headers["Cache-Control"]="no-store, private"
        response.headers["Pragma"]="no-cache"
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        return response
    except MultiUserSecurityError as exc:
        error=http_error(exc)
        return JSONResponse({"detail":error.detail,"error_code":exc.code},status_code=error.status_code,headers={"Cache-Control":"no-store, private"})

@app.get("/api/session")
def session_info():
    context=current_account_context();runtime=get_runtime();runtime.registry.touch_login(context.internal_user_id)
    return {"user":{"id":context.internal_user_id,"display_name":context.display_name,"role":context.role},"account":{"account_id":context.account_id},"csrf_token":runtime.csrf_token(context.internal_user_id),"identity_provider":context.identity.provider}

class ProvisionUserIn(TenantSafeModel):
    invite_email:str
    access_sub:str=Field(min_length=3,max_length=200)
    display_name:str=Field(min_length=1,max_length=80)
    role:Literal["TESTER"]="TESTER"

class DisableUserIn(TenantSafeModel):
    target_internal_user_id:str

def require_owner():
    context=current_account_context()
    if context.role!="OWNER":raise HTTPException(403,"仅OWNER可执行系统管理操作。")
    return context

def account_container_view(row):
    return {"account_id":row["account_id"],"display_name":row["display_name"],"account_type":row["account_type"],"is_default":bool(row["is_default"]),"status":row["status"]}

@app.get("/api/accounts")
def owner_accounts():
    context=require_owner();runtime=get_runtime()
    return {"selected_account_id":context.account_id,"accounts":[account_container_view(row) for row in runtime.registry.active_accounts(context.internal_user_id)]}

@app.post("/api/accounts")
def owner_create_account(body:AccountContainerCreateIn):
    context=require_owner();runtime=get_runtime()
    row=runtime.registry.create_account_container(context.internal_user_id,body.display_name,body.account_type)
    db_path=runtime.resolver.users_root/context.internal_user_id/row["db_relative_path"]
    try:
        initialize_empty_v4_account(db_path)
        runtime.resolver.resolve_relative(context.internal_user_id,row["db_relative_path"])
        runtime.registry.activate_account_container(context.internal_user_id,row["account_id"])
        active=runtime.registry.active_account(context.internal_user_id,row["account_id"])
        return {"account":account_container_view(active),"owner_data_copied":False}
    except Exception:
        runtime.registry.mark_account_container_error(context.internal_user_id,row["account_id"])
        raise

@app.post("/api/accounts/{account_id}/rename")
def owner_rename_account(account_id:str,body:AccountContainerRenameIn):
    context=require_owner();runtime=get_runtime();runtime.registry.rename_account(context.internal_user_id,account_id,body.display_name)
    return {"account":account_container_view(runtime.registry.active_account(context.internal_user_id,account_id))}

@app.post("/api/accounts/{account_id}/type")
def owner_change_account_type(account_id:str,body:AccountTypeChangeIn):
    context=require_owner();runtime=get_runtime()
    if account_id != context.account_id:
        raise HTTPException(409,"当前选中账户已变化，请刷新后重试。")
    current=runtime.registry.active_account(context.internal_user_id,account_id)
    if current is None:
        raise HTTPException(404,"未找到当前账户。")
    old_type=current["account_type"]
    row=runtime.registry.change_account_type(
        context.internal_user_id,account_id,body.new_type,body.user_confirmed,body.note
    )
    return {"account":account_container_view(row),"audit":{
        "account_id":account_id,"old_type":old_type,"new_type":row["account_type"],
        "user_confirmed":True
    }}

@app.post("/api/accounts/{account_id}/default")
def owner_default_account(account_id:str):
    context=require_owner();get_runtime().registry.set_default_account(context.internal_user_id,account_id)
    return {"status":"DEFAULT_SET","account_id":account_id}

@app.post("/api/accounts/{account_id}/archive")
def owner_archive_account(account_id:str):
    context=require_owner();get_runtime().registry.archive_account(context.internal_user_id,account_id)
    return {"status":"ARCHIVED","account_id":account_id}

@app.get("/api/admin/users")
def admin_users():
    require_owner();runtime=get_runtime()
    users=runtime.registry.list_safe()
    for user in users:
        try:
            runtime.resolver.resolve(user["user_id"]);user["db_health"]="OK"
        except MultiUserSecurityError:
            user["db_health"]="UNAVAILABLE"
    return {"users":users,"privacy":"FINANCIAL_DETAILS_HIDDEN"}

@app.post("/api/admin/users/provision")
def admin_provision(body:ProvisionUserIn):
    require_owner();return provision_user(get_runtime(),body.invite_email,body.access_sub,body.display_name,body.role)

@app.post("/api/admin/users/disable")
def admin_disable(body:DisableUserIn):
    require_owner();get_runtime().registry.disable(body.target_internal_user_id)
    return {"status":"DISABLED","retention_days":90}

@app.get("/api/health")
def health():
    s = latest_signal()
    obs = latest_market_observation()
    official=latest_official_signal()
    return {"status": "OK", "engine_status": "OK", "formula_contract_version": CONTRACT["contract_version"], "account_contract_version":"S2-account-v4.0-frozen", "account_maintenance_version":maintenance.VERSION, "database_status": "OK", "latest_data_date": obs["date"] if obs else (s["signal_date"] if s else None), "latest_official_signal_date": official["signal_date"] if official else None,"multi_user":"M2","cloudflare":"NOT_DEPLOYED"}

@app.get("/api/dashboard")
def dashboard():
    signal = latest_official_signal() or latest_signal(); execution = None
    if signal:
        with connect() as con:
            execution=v4_execution_for_signal(signal["signal_date"],con)
            if not execution:
                row = con.execute("SELECT * FROM execution_records WHERE signal_date=?", (signal["signal_date"],)).fetchone(); execution = dict(row) if row else None
    with (DATA / "s2_v2_monthly.csv").open(encoding="utf-8-sig") as f:
        history_points = [{"date": r["date"], "spread": float(r["spread"]), "percentile": float(r["s2_score"]), "target_exposure": float(r["position"]), "status": signal_label(float(r["s2_score"]))} for r in csv.DictReader(f) if r.get("s2_score")]
    return {"account": account_view(), "today": action_view(), "performance": performance(), "data_status": data_status(), "market": market_view(), "execution": execution, "history_points": history_points}

@app.get("/api/today-action")
def today_action(): return action_view()

@app.get("/api/account")
def account(): return account_view()

@app.get("/api/data-status")
def data_status_api(): return {"data_status": data_status(), "market": market_view()}

@app.post("/api/data-update")
def data_update(retry: bool = False):
    if get_runtime().shared_db_read_only:
        raise HTTPException(403, "测试环境只读取最新已发布数据，不能运行行情更新。")
    require_owner()
    try:
        from .data_updater import run_update
    except ImportError:
        from data_updater import run_update
    fund_codes=tuple(dict.fromkeys((*DATA_SOURCE_CONTRACT["execution_funds"],selected_fund()["code"])))
    result = run_update(get_runtime().shared_db, retry=retry, trigger="manual_api", task_name="S2 Dashboard API",fund_codes=fund_codes)
    return result

@app.get("/api/cashflows")
def cashflows():
    with connect() as con: rows = [dict(r) for r in con.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 200")]
    return {"summary": account_view(), "rows": rows}

@app.post("/api/contribution")
def contribution(body: ContributionIn):
    if not initialized(): raise HTTPException(409, "请先建立账户")
    e = get_engine(); cleared_at = local_time(body.cleared_at); batch = f"BATCH-{next_rebalance_date(cleared_at.date())}"; result = e.contribution(body.event_id, now(), body.amount, f"LOT-{body.event_id}", cleared_at, batch); persist(e, body.event_id, "新增资金", result, body.note); return {"result": result, "account": account_view(e)}

@app.post("/api/initialize")
def initialize(body: InitializeIn):
    e = get_engine(); timestamp = now(); batch = f"BATCH-{next_rebalance_date(timestamp.date())}"
    result = e.initialize_account(body.event_id, timestamp, body.strategy_capital, body.reserve_cash, f"LOT-{body.event_id}", timestamp, batch)
    if result["status"] == "APPLIED":
        with connect() as con: con.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", ("account_initialized", "true"))
    persist(e, body.event_id, "初始投入", result, body.note)
    return {"result": result, "account": account_view(e)}

@app.post("/api/import-existing-account")
def import_existing_account(body: ImportAccountIn):
    if body.fund_code not in {x["code"] for x in FUNDS}: raise HTTPException(400, "基金不在已确认白名单")
    if body.fund_value > 0 and (body.fund_shares is None or body.fund_shares <= 0): raise HTTPException(422, "导入基金持仓时必须填写真实持有份额")
    if body.fund_value == 0 and body.fund_shares not in {None, Decimal(0)}: raise HTTPException(422, "基金市值为0时不能填写持有份额")
    e = get_engine(); timestamp = now(); batch = f"BATCH-{next_rebalance_date(timestamp.date())}"
    result = e.initialize_account(body.event_id, timestamp, body.pending_cash, body.reserve_cash, f"LOT-{body.event_id}", timestamp, batch, body.fund_value, body.strategy_cash)
    if result["status"] == "APPLIED":
        e.fund_shares=body.fund_shares or Decimal(0)
        with connect() as con:
            con.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", ("account_initialized", "true"))
            con.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", ("selected_fund", body.fund_code))
    persist(e, body.event_id, "存量账户导入", result, body.note)
    return {"result": result, "account": account_view(e)}

@app.post("/api/withdrawal")
def withdrawal(body: WithdrawalIn):
    e = get_engine(); cfg = settings(); available = now() + timedelta(days=int(cfg["settlement_days"])); result = e.withdrawal(body.event_id, now(), body.amount, cfg["fee_rate"], available); persist(e, body.event_id, "提款", result, body.note); return {"result": result, "account": account_view(e)}

@app.post("/api/record-trade")
def record_trade():
    if db_contract_version().startswith("S2-account-v4"):
        raise HTTPException(409,"V4账户必须填写真实平台订单，不能按策略建议金额直接记账。")
    return execute_signal_record(latest_official_signal() or latest_signal(), None)

def execute_signal_record(signal: dict | None, body: ExecutionRecordIn | None):
    if db_contract_version().startswith("S2-account-v4"):
        raise HTTPException(409,"V4账户必须使用实际订单事件，recommended_amount不得驱动账户会计。")
    if not signal: raise HTTPException(409, "当前没有正式月度执行单")
    signal_date = signal["signal_date"]
    with connect() as con:
        existing = con.execute("SELECT * FROM execution_records WHERE signal_date=?", (signal_date,)).fetchone()
    if existing:
        return {"result": {"status": "ALREADY_PROCESSED", "reason": "本月执行记录已经存在"}, "execution": dict(existing), "account": account_view()}
    e = get_engine(); cfg = settings(); event_id = f"REB-{signal_date}"; execution = local_time(body.executed_at) if body else now(); cutoff = execution.replace(hour=14, minute=30, second=0)
    result = e.monthly_execute(event_id, execution, f"BATCH-{next_rebalance_date(execution.date())}", cutoff, signal["target_exposure"], cfg["fee_rate"], cfg["profit_lock"], execution + timedelta(days=int(cfg["settlement_days"])), execution + timedelta(days=int(cfg["settlement_days"])))
    if result.get("status") not in {"APPLIED", "ALREADY_PROCESSED"}: raise HTTPException(409, result.get("reason", "执行单无法完成"))
    payload = result.get("payload", result); direction = payload.get("direction", "HOLD")
    recommended = abs(Decimal(str(e.audit_log[-1].get("trade", 0)))) if e.audit_log else Decimal(0)
    # AccountEngine remains authoritative for the recommended rebalance. The execution row separately records what the user actually entered on the fund platform.
    actual = body.actual_amount if body else recommended; fund = body.fund_code if body else selected_fund()["code"]
    if fund not in {x["code"] for x in FUNDS}: raise HTTPException(400, "基金不在已确认白名单")
    persist(e, event_id, "月度调仓", result, "用户确认已执行月度建议")
    with connect() as con:
        con.execute("INSERT INTO execution_records(signal_date,event_id,recommended_action,recommended_amount,actual_action,actual_amount,fund_code,executed_at,note,status) VALUES(?,?,?,?,?,?,?,?,?,?)", (signal_date,event_id,direction,str(recommended),direction,str(actual),fund,execution.isoformat(),body.note if body else "", "EXECUTED"))
    return {"result": result, "execution": {"signal_date": signal_date, "event_id": event_id, "recommended_action": direction, "recommended_amount": str(recommended), "actual_action": direction, "actual_amount": str(actual), "fund_code": fund, "executed_at": execution.isoformat(), "note": body.note if body else "", "status": "EXECUTED"}, "account": account_view(e)}

@app.get("/api/execution-status")
def execution_status(signal_date: str | None = None):
    signal = latest_signal() if not signal_date else {"signal_date": signal_date}
    with connect() as con:
        row = con.execute("SELECT * FROM execution_records WHERE signal_date=?", (signal["signal_date"],)).fetchone() if signal else None
    return {"signal_date": signal["signal_date"] if signal else None, "execution": dict(row) if row else None}

@app.post("/api/execution-record")
def execution_record(body: ExecutionRecordIn):
    if db_contract_version().startswith("S2-account-v4"):
        current=action_view();side="BUY" if current["net_trade"] and Decimal(current["net_trade"])>0 else "SELL"
        if side=="SELL":raise HTTPException(409,"V4赎回必须填写实际赎回份额，请使用新版记录入口。")
        return v4_report_execution(V4ExecutionReportIn(signal_date=body.signal_date,actual_action=side,cash_debited=body.actual_amount,fund_code=body.fund_code,reported_at=body.executed_at,note=body.note))
    signal = latest_official_signal() or latest_signal()
    if not signal or signal["signal_date"] != body.signal_date: raise HTTPException(409, "只能记录最近正式月度信号")
    return execute_signal_record(signal, body)

def _v4_engine_from_con(con) -> AccountEngineV4:
    engine=pickle.loads(con.execute("SELECT engine FROM account_state WHERE id=1").fetchone()["engine"])
    if not isinstance(engine,AccountEngineV4):raise HTTPException(409,"账户尚未完成V4迁移")
    engine._ensure_v4();return engine

def _v4_raise(result):
    reason=result.get("reason","V4_EVENT_REJECTED")
    if reason in {"PENDING_LOT_NOT_FOUND","EXECUTION_NOT_FOUND"}:raise HTTPException(404,"未找到该记录。")
    raise HTTPException(409,V4_REASON_ZH.get(reason,reason))

@app.get("/api/v4/pending")
def v4_pending_lots():
    e=get_engine()
    if not isinstance(e,AccountEngineV4):raise HTTPException(409,"账户尚未完成V4迁移")
    return {"contract_version":e.contract_version,"lots":[{
        "lot_id":lot.lot_id,"original_contribution_event_id":lot.event_id,"original_amount":str(lot.amount),
        "remaining_amount":str(lot.remaining_amount),"status":lot.status,"cancellable":lot.status in V4_CANCELLABLE and lot.remaining_amount>0,
        "created_at":lot.created_at.isoformat(),"cleared_at":lot.cleared_at.isoformat(),"batch_id":lot.eligible_batch_id,
    } for lot in e.pending_lots if lot.remaining_amount>0 or lot.status in {"CANCELLED","MERGED","CONSUMED"}]}

@app.post("/api/v4/execution/report")
def v4_report_execution(body: V4ExecutionReportIn):
    backup_db();reported=local_time(body.reported_at)
    with connect_shared(read_only=True) as shared:
        signal=shared.execute("SELECT * FROM official_signals WHERE signal_date=? AND validation_status='VALID'",(body.signal_date,)).fetchone()
        signal=dict(signal) if signal else None
    if not signal:raise HTTPException(409,"只能记录最近有效的正式月度信号")
    with connect() as con:
        try:
            con.execute("BEGIN IMMEDIATE");e=_v4_engine_from_con(con)
            existing=con.execute("SELECT * FROM v4_executions WHERE signal_id=? AND status NOT IN ('REJECTED','CANCELLED','SUPERSEDED')",(body.signal_date,)).fetchone()
            if existing:
                con.commit();return {"result":{"status":"ALREADY_PROCESSED","reason":"SIGNAL_ALREADY_EXECUTED"},"execution":dict(existing),"account":account_view(e)}
            cfg=settings(con);subscription=e.subscription_pending;receivable=e.redemption_receivable
            if body.fund_code != cfg.get("selected_fund"):
                raise HTTPException(409,"实际订单基金必须与账户设置中的执行基金一致。")
            _,nav_observation=selected_fund_nav(con)
            execution_cutoff = datetime.fromisoformat(execution_window(body.signal_date, reported)["cutoff"])
            # The report endpoint is the real execution transition. Pending
            # is merged only when this account has an eligible lot for this
            # signal; accounts without Pending retain the original V4 path.
            pending_projection = projected_eligible_pending(e, body.signal_date, execution_cutoff)
            if pending_projection["amount"] > 0:
                if reported < execution_cutoff:
                    raise HTTPException(409, f"订单记录时间早于本次正式执行截止时间 {execution_cutoff.isoformat()}，请填写实际执行时间。")
                # Freeze and merge only this account's eligible batch so V4's
                # original StrategyCash validation can accept a BUY funded by
                # Pending.
                merge_eligible_pending_for_execution(e, body.signal_date, execution_cutoff)
            marked_fund=fund_market_value(e,nav_observation)
            active=marked_fund+e.C+subscription+receivable;economic_fund=marked_fund+subscription
            p=Decimal(str(signal["target_exposure"]));fee=Decimal(cfg["fee_rate"])
            solved=solve_target_after_fee(economic_fund,active,p,fee) if active else {"trade":Decimal(0)}
            trade=Decimal(str(solved["trade"]));recommended_action="BUY" if trade>0 else "SELL" if trade<0 else "HOLD"
            if recommended_action not in {"BUY","SELL"}:raise HTTPException(409,"本次正式信号无需交易")
            recommended=abs(trade);recommendation_id=f"REC-{body.signal_date}"
            fingerprint=e.deterministic_fingerprint()
            con.execute("INSERT OR IGNORE INTO v4_recommendations VALUES(?,?,?,?,?,?,?,?,?,?)",(recommendation_id,body.signal_date,body.signal_date,recommended_action,str(recommended),str(p),fingerprint,reported.isoformat(),e.contract_version,"4.1"))
            rec=con.execute("SELECT * FROM v4_recommendations WHERE recommendation_id=?",(recommendation_id,)).fetchone()
            fund=next((x for x in FUNDS if x["code"]==body.fund_code),None)
            if not fund:raise HTTPException(400,"基金不在已确认白名单")
            result=e.report_execution(body.event_id,reported,body.signal_date,recommendation_id,rec["recommended_action"],rec["recommended_amount"],body.actual_action,body.fund_code,cash_debited=body.cash_debited,fee=body.reported_fee,redemption_units=body.redemption_units,estimated_redemption_amount=body.estimated_redemption_amount,note=body.note)
            if result["status"]=="ALREADY_PROCESSED":
                row=con.execute("SELECT * FROM v4_executions WHERE event_id=?",(body.event_id,)).fetchone();con.commit();return {"result":result,"execution":dict(row) if row else None,"account":account_view(e)}
            if result["status"]!="APPLIED":_v4_raise(result)
            order=e.v4_orders[body.event_id]
            con.execute("""INSERT INTO v4_executions(
                event_id,signal_id,recommendation_id,contract_version,schema_version,fund_code,fund_name,
                side,reported_at,recommended_action,recommended_amount,actual_order_type,cash_debited,
                reported_fee,fee_status,redemption_units,estimated_redemption_amount,execution_deviation,
                user_note,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                body.event_id,body.signal_date,recommendation_id,e.contract_version,"4.1",fund["code"],fund["name"],body.actual_action,reported.isoformat(),rec["recommended_action"],rec["recommended_amount"],"SUBSCRIPTION_CASH_DEBIT" if body.actual_action=="BUY" else "REDEMPTION_UNITS",order["cash_debited"],order["reported_fee"],order["fee_status"],order["redemption_units"],order["estimated_redemption_amount"],order["execution_deviation"],body.note,"PENDING_CONFIRMATION",now().isoformat(),))
            persist_v4_tx(con,e,body.event_id,"EXECUTION_REPORTED",result,body.note);con.commit()
            row=con.execute("SELECT * FROM v4_executions WHERE event_id=?",(body.event_id,)).fetchone()
            return {"result":result,"recommendation":dict(rec),"execution":dict(row),"account":account_view(e)}
        except HTTPException:
            con.rollback();raise
        except sqlite3.IntegrityError:
            con.rollback();raise HTTPException(409,"本次正式信号已经存在执行记录。")
        except Exception:
            con.rollback();raise

@app.post("/api/v4/execution/confirm")
def v4_confirm_execution(body: V4ConfirmationIn):
    backup_db();confirmed=local_time(body.confirmed_at)
    with connect() as con:
        try:
            con.execute("BEGIN IMMEDIATE");e=_v4_engine_from_con(con)
            if e.fund_shares > 0 and e.F > 0:
                marked_value=e.fund_shares*body.confirmed_nav
                market_return=marked_value/e.F-Decimal(1)
                mark=e.apply_market_return(f"NAV-{body.confirmation_event_id}",confirmed,market_return)
                if mark.get("status") not in {"APPLIED","ALREADY_PROCESSED"}:_v4_raise(mark)
            result=e.confirm_order(body.confirmation_event_id,confirmed,body.execution_event_id,body.confirmed_nav,body.confirmed_units,body.confirmed_principal,body.confirmed_fee,body.confirmed_value,body.platform_reference)
            if result["status"]=="ALREADY_PROCESSED":
                row=con.execute("SELECT * FROM v4_confirmations WHERE confirmation_event_id=?",(body.confirmation_event_id,)).fetchone();con.commit();return {"result":result,"confirmation":dict(row) if row else None,"account":account_view(e)}
            if result["status"]!="APPLIED":_v4_raise(result)
            order=e.v4_orders[body.execution_event_id]
            con.execute("""INSERT INTO v4_confirmations(
                confirmation_event_id,execution_event_id,confirmed_at,confirmed_nav,confirmed_units,
                confirmed_principal,confirmed_fee,confirmed_value,confirmation_status,platform_reference,
                contract_version,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (body.confirmation_event_id,body.execution_event_id,confirmed.isoformat(),str(body.confirmed_nav),str(body.confirmed_units),str(body.confirmed_principal),str(body.confirmed_fee),str(body.confirmed_value),"CONFIRMED",body.platform_reference,e.contract_version,"4.1"))
            con.execute("UPDATE v4_executions SET status=? WHERE event_id=?",(order["status"],body.execution_event_id))
            persist_v4_tx(con,e,body.confirmation_event_id,"FUND_ORDER_CONFIRMED",result);con.commit()
            return {"result":result,"execution":dict(con.execute("SELECT * FROM v4_executions WHERE event_id=?",(body.execution_event_id,)).fetchone()),"account":account_view(e)}
        except HTTPException:con.rollback();raise
        except sqlite3.IntegrityError:con.rollback();raise HTTPException(409,"该订单已经确认。")
        except Exception:con.rollback();raise

@app.post("/api/v4/execution/settle")
def v4_settle_execution(body: V4SettlementIn):
    backup_db();settled=local_time(body.settled_at)
    with connect() as con:
        try:
            con.execute("BEGIN IMMEDIATE");e=_v4_engine_from_con(con)
            result=e.settle_execution(body.settlement_event_id,settled,body.execution_event_id,body.destination)
            if result["status"]=="ALREADY_PROCESSED":
                row=con.execute("SELECT * FROM v4_settlement_events WHERE settlement_event_id=?",(body.settlement_event_id,)).fetchone();con.commit();return {"result":result,"settlement":dict(row) if row else None,"account":account_view(e)}
            if result["status"]!="APPLIED":_v4_raise(result)
            order=e.v4_orders[body.execution_event_id]
            con.execute("""INSERT INTO v4_settlement_events(
                settlement_event_id,execution_event_id,settled_at,settled_cash_amount,destination,status,
                contract_version,schema_version) VALUES(?,?,?,?,?,?,?,?)""",
                (body.settlement_event_id,body.execution_event_id,settled.isoformat(),order.get("settled_cash_amount","0"),body.destination,"SETTLED",e.contract_version,"4.1"))
            con.execute("UPDATE v4_executions SET status='SETTLED' WHERE event_id=?",(body.execution_event_id,))
            persist_v4_tx(con,e,body.settlement_event_id,"SETTLEMENT",result);con.commit()
            return {"result":result,"execution":dict(con.execute("SELECT * FROM v4_executions WHERE event_id=?",(body.execution_event_id,)).fetchone()),"account":account_view(e)}
        except HTTPException:con.rollback();raise
        except sqlite3.IntegrityError:con.rollback();raise HTTPException(409,"该订单已经完成结算。")
        except Exception:con.rollback();raise

def _v4_pending_action(body: V4PendingActionIn, destination: str):
    backup_db();effective=local_time(body.effective_at)
    with connect() as con:
        try:
            con.execute("BEGIN IMMEDIATE");e=_v4_engine_from_con(con)
            result=e.pending_cancellation(body.event_id,effective,body.lot_id,body.amount,body.reason) if destination=="EXTERNAL" else e.pending_to_reserve(body.event_id,effective,body.lot_id,body.amount,body.reason)
            if result["status"]=="ALREADY_PROCESSED":
                row=con.execute("SELECT * FROM v4_pending_events WHERE event_id=?",(body.event_id,)).fetchone();con.commit();return {"result":result,"pending_event":dict(row) if row else None,"account":account_view(e)}
            if result["status"]!="APPLIED":_v4_raise(result)
            lot=e._find_pending(body.lot_id)
            con.execute("""INSERT INTO v4_pending_events(
                event_id,event_type,original_pending_lot_id,original_contribution_event_id,amount,effective_at,
                destination,reason,nav_before,units_cancelled,remaining_pending_amount,status,created_at,
                contract_version,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (body.event_id,"PENDING_CANCELLATION" if destination=="EXTERNAL" else "PENDING_TO_RESERVE_TRANSFER",body.lot_id,lot.event_id,str(body.amount),effective.isoformat(),destination,body.reason,result.get("nav_before",result["before_state"]["NAV"] if "before_state" in result else ""),result.get("units_cancelled","0"),str(lot.remaining_amount),lot.status,now().isoformat(),e.contract_version,"4.1"))
            persist_v4_tx(con,e,body.event_id,"PENDING_CANCELLATION" if destination=="EXTERNAL" else "PENDING_TO_RESERVE_TRANSFER",result,body.reason);con.commit()
            return {"result":result,"pending_event":dict(con.execute("SELECT * FROM v4_pending_events WHERE event_id=?",(body.event_id,)).fetchone()),"account":account_view(e)}
        except HTTPException:con.rollback();raise
        except sqlite3.IntegrityError:con.rollback();raise HTTPException(409,"该资金操作已经处理。")
        except Exception:con.rollback();raise

@app.post("/api/v4/pending/cancel")
def v4_cancel_pending(body: V4PendingActionIn):return _v4_pending_action(body,"EXTERNAL")

@app.post("/api/v4/pending/to-reserve")
def v4_pending_to_reserve(body: V4PendingActionIn):return _v4_pending_action(body,"RESERVE")

def maintenance_funds() -> set[str]:
    return {fund["code"] for fund in FUNDS}

@app.get("/api/account-maintenance/execution-candidates")
def maintenance_execution_candidates():
    """Read-only candidate for a missed fund order in the selected account."""
    signal = latest_official_signal()
    if not signal:
        return {"candidate": None, "reason": "NO_OFFICIAL_SIGNAL"}
    with connect() as con:
        execution = v4_execution_for_signal(signal["signal_date"], con)
    if execution and execution.get("status") not in {"REJECTED", "CANCELLED", "SUPERSEDED"}:
        return {"candidate": None, "reason": "SIGNAL_ALREADY_EXECUTED", "execution": execution}
    view = action_view()
    trade = Decimal(str(view.get("net_trade", "0")))
    action = "BUY" if trade > 0 else "SELL" if trade < 0 else "HOLD"
    if action == "HOLD":
        return {"candidate": None, "reason": "NO_REBALANCE"}
    return {"candidate": {
        "signal_id": signal["signal_date"],
        "recommendation_id": f"REC-{signal['signal_date']}",
        "recommended_action": action,
        "recommended_amount": str(abs(trade)),
        "target_exposure": view.get("target_exposure"),
        "current_exposure": view.get("actual_exposure"),
        "selected_fund": view.get("selected_fund"),
        "execution_date": view.get("execution_date"),
        "execution_cutoff": view.get("execution_cutoff"),
        "execution_deadline": view.get("execution_deadline"),
        "execution_state": view.get("execution_state"),
        "fee_status": view.get("fee_status"),
        "product_status": view.get("product_status"),
        "gross_order_amount": view.get("gross_order_amount"),
        "theoretical_target_delta": view.get("theoretical_target_delta"),
    }}

@app.post("/api/account-maintenance/order-ocr")
def maintenance_order_ocr(body: OrderOCRIn):
    """Parse an uploaded screenshot without writing account state."""
    try:
        return order_ocr_service.parse_image(body.image_data, FUNDS, body.filename)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

@app.post("/api/account-maintenance/late-entry")
def maintenance_late_entry(body: MaintenanceLateEntryIn):
    payload=body.model_dump();payload["occurred_at"]=local_time(payload["occurred_at"])
    if payload["kind"] in {"CONTRIBUTION","WITHDRAWAL"} and payload["amount"] is None:
        raise HTTPException(422,"补录资金操作必须填写金额。")
    if payload["kind"]=="EXECUTION_REPORTED":
        required=("reported_status","signal_id","recommendation_id","recommended_action","recommended_amount","actual_action","fund_code")
        if any(payload.get(key) is None for key in required):raise HTTPException(422,"补录订单必须填写完整的真实订单事实。")
        if payload["fund_code"] not in maintenance_funds():raise HTTPException(422,"基金不在已确认白名单")
        cfg = settings()
        if payload["fund_code"] != cfg.get("selected_fund"):
            raise HTTPException(409,"实际订单基金必须与当前账户设置中的执行基金一致。")
        signal = None
        with connect_shared(read_only=True) as shared:
            row = shared.execute("SELECT * FROM official_signals WHERE signal_date=? AND validation_status='VALID'", (payload["signal_id"],)).fetchone()
            signal = dict(row) if row else None
        if not signal:
            raise HTTPException(409,"只能补录有效的正式月度信号。")
        with connect() as check:
            refs = [x.strip() for x in (payload.get("platform_reference") or "").split(";") if x.strip()]
            if refs:
                for table, column in (("maintenance_late_entries", "platform_reference"), ("v4_confirmations", "platform_reference")):
                    exists = check.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                    if not exists:
                        continue
                    for raw in check.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL AND {column}<>''"):
                        known = {x.strip() for x in str(raw[0]).split(";") if x.strip()}
                        if known.intersection(refs):
                            raise HTTPException(409,"该平台流水号已经录入，疑似重复订单。")
        window = execution_window(payload["signal_id"], now())
        execution_cutoff = datetime.fromisoformat(window["cutoff"])
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        result=maintenance.late_entry(con,payload,now(),maintenance_funds(),execution_cutoff if payload["kind"]=="EXECUTION_REPORTED" else None);con.commit()
    return {"result":result,"account":account_view()}

@app.post("/api/account-maintenance/correction")
def maintenance_correction(body: MaintenanceCorrectionIn):
    payload=body.model_dump()
    if payload.get("fund_code") and payload["fund_code"] not in maintenance_funds():raise HTTPException(422,"基金不在已确认白名单")
    with connect() as con:
        con.execute("BEGIN IMMEDIATE")
        result=maintenance.correction(con,payload,now());con.commit()
    return {"result":result,"account":account_view()}

@app.post("/api/account-maintenance/restart/prepare")
def maintenance_restart_prepare():
    context=current_account_context();runtime=get_runtime();row=runtime.registry.active_account(context.internal_user_id,context.account_id)
    return {"account_id":row["account_id"],"summary":account_view(),"confirmation_required":"重新开始","warning":"旧账本将永久只读保存；新账本从新的真实资产基线开始统计。"}

@app.post("/api/account-maintenance/restart/commit")
def maintenance_restart_commit(body: MaintenanceRestartIn):
    context=current_account_context();payload=body.model_dump();payload["baseline_date"]=local_time(payload["baseline_date"])
    baseline=payload["baseline_date"]
    result=maintenance.restart(get_runtime(),context,payload.pop("confirmation"),payload,payload.pop("reason"),maintenance_funds(),baseline,payload.pop("operation_id"))
    return {"result":result}

@app.get("/api/account-maintenance/archives")
def maintenance_archives():
    context=current_account_context();runtime=get_runtime();current=runtime.registry.active_account(context.internal_user_id,context.account_id)
    rows=runtime.registry.archives(context.internal_user_id,current["lineage_id"])
    return {"archives":[{"account_id":row["account_id"],"created_at":row["created_at"],"archived_at":row["archived_at"],"reason":row["archive_reason"],"ending_wealth":row["ending_wealth"],"read_only":True} for row in rows]}

@app.get("/api/account-maintenance/history")
def maintenance_history():
    with connect() as con:
        late=[dict(row) for row in con.execute("SELECT event_id,kind,occurred_at,entered_at,accounting_effective_at,late_entry_mode,reason FROM maintenance_late_entries ORDER BY entered_at DESC")] if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_late_entries'").fetchone() else []
        corrections=[dict(row) for row in con.execute("SELECT correction_event_id,revision_chain_id,revision_no,references_event_id,supersedes_event_id,replacement_event_id,reason,corrected_at,status FROM maintenance_corrections ORDER BY corrected_at DESC")] if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_corrections'").fetchone() else []
    return {"late_entries":late,"corrections":corrections}

@app.get("/api/account-maintenance/archives/{archive_id}")
def maintenance_archive(archive_id:str):
    context=current_account_context();runtime=get_runtime();current=runtime.registry.active_account(context.internal_user_id,context.account_id);row=runtime.registry.account_by_archive(context.internal_user_id,archive_id,current["lineage_id"])
    if row is None:raise HTTPException(404,"未找到历史账本")
    path=get_runtime().resolver.resolve_relative(context.internal_user_id,row["db_relative_path"])
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro",uri=True) as con:
        engine=pickle.loads(con.execute("SELECT engine FROM account_state WHERE id=1").fetchone()[0])
    return {"account_id":archive_id,"read_only":True,"created_at":row["created_at"],"archived_at":row["archived_at"],"reason":row["archive_reason"],"state":engine.state()}

@app.post("/api/account-maintenance/archives/{archive_id}/export")
def maintenance_archive_export(archive_id:str):
    context=current_account_context();runtime=get_runtime();current=runtime.registry.active_account(context.internal_user_id,context.account_id);row=runtime.registry.account_by_archive(context.internal_user_id,archive_id,current["lineage_id"])
    if row is None:raise HTTPException(404,"未找到历史账本")
    source=get_runtime().resolver.resolve_relative(context.internal_user_id,row["db_relative_path"])
    target_dir=get_runtime().exports_root/context.internal_user_id;target_dir.mkdir(parents=True,exist_ok=True)
    target=target_dir/f"archive_{archive_id}_{now():%Y%m%d_%H%M%S}.sqlite3"
    src=sqlite3.connect(f"file:{source.as_posix()}?mode=ro",uri=True);dst=sqlite3.connect(target)
    try:src.backup(dst)
    finally:dst.close();src.close()
    return FileResponse(target,filename=target.name,headers={"Cache-Control":"no-store, private"})

@app.get("/api/history")
def history():
    with (DATA / "s2_v2_monthly.csv").open(encoding="utf-8-sig") as f: data = list(csv.DictReader(f))
    points = [{"date": x["date"], "spread": float(x["spread"]), "percentile": float(x["s2_score"]), "target_exposure": float(x["position"]), "nav": float(x["nav"]), "status": signal_label(float(x["s2_score"]))} for x in data if x.get("s2_score")]
    with connect_shared(read_only=True) as shared:
        signals = [dict(r) for r in shared.execute("SELECT * FROM monthly_signals ORDER BY signal_date DESC LIMIT 60")]
        official_rows = [dict(r) for r in shared.execute("SELECT * FROM official_signals WHERE validation_status='VALID' ORDER BY signal_date DESC")]
    with connect() as con:
        executions = {r["signal_date"]: dict(r) for r in con.execute("SELECT * FROM execution_records")}
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='v4_executions'").fetchone():
            for raw in con.execute("SELECT * FROM v4_executions"):
                row=dict(raw)
                row.update({
                    "signal_date":row["signal_id"],"actual_action":row["side"],
                    "actual_amount":row["cash_debited"] if row["side"]=="BUY" else row["estimated_redemption_amount"],
                    "executed_at":row["reported_at"],
                })
                executions[row["signal_id"]]=row
    known = {row["signal_date"] for row in signals}
    for row in official_rows:
        if row["signal_date"] not in known:
            ex = executions.get(row["signal_date"])
            signals.append({"signal_date": row["signal_date"], "spread": row["spread"], "percentile": row["spread_percentile"], "target_exposure": row["target_exposure"], "action": ex["recommended_action"] if ex else "HOLD"})
    signals.sort(key=lambda row: row["signal_date"], reverse=True)
    for row in signals:
        ex = executions.get(row["signal_date"]); row["execution"] = ex; row["execution_status"] = ex["status"] if ex else ("无需操作" if row.get("action") in {"HOLD", "NONE"} else "未执行")
    return {"signals": signals, "points": points, "crisis": crisis(), "executions": list(executions.values())}

@app.get("/api/strategy")
def strategy(): return {"signal_index": {"code": "000922", "name": "中证红利指数"}, "return_index": {"code": "H00922", "name": "中证红利全收益指数"}, "funds": FUNDS, "rules": [[">=90%", "100%"], ["80%-90%", "80%"], ["60%-80%", "60%"], ["40%-60%", "50%"], ["20%-40%", "40%"], ["10%-20%", "20%"], ["<10%", "0%"]]}

@app.get("/api/settings")
def get_settings():
    cfg=settings();context=current_account_context();runtime=get_runtime();account_row=runtime.registry.active_account(context.internal_user_id,context.account_id);account_type=account_row["account_type"] if account_row else "UNKNOWN";fund=next((item for item in FUNDS if item["code"]==cfg.get("selected_fund")),FUNDS[0]);profile=fund_fee_profile(fund,cfg,account_type)
    return {**cfg, "funds": FUNDS, "account":account_container_view(account_row), "selected_fund_fee": {"fee_status": profile["fee_status"], "product_status": profile["product_status"], "configured_product_status":profile["configured_product_status"], "account_type_eligible":profile["account_type_eligible"], "source": profile["source"], "profile": profile["profile"]}, "cutoff": "T+1 14:30", "data_sources": {"historical": "RESSET DYR_TTM (2013-07-01 至 2026-08-03)", "current": "中证指数有限公司 000922 DY2（2026-08-04起）", "bond": "ChinaBond 中国10年期国债", "fund_nav": f"{fund['code']} {fund['name']} NAV接口", "migration_status": "PROVISIONAL PASS"}}

@app.post("/api/settings")
def save_settings(body: SettingsIn):
    if body.selected_fund not in {x["code"] for x in FUNDS}: raise HTTPException(400, "基金不在已确认白名单")
    current=settings().get("selected_fund","007801")
    engine=get_engine()
    if body.selected_fund != current and (dec(engine.F)>0 or dec(getattr(engine,"fund_shares",0))>0 or dec(getattr(engine,"subscription_pending",0))>0 or dec(getattr(engine,"redemption_receivable",0))>0):
        raise HTTPException(409,"当前账户仍有基金持仓或待确认订单，不能仅通过设置切换基金代码；请先完成真实账户迁移。")
    backup_db()
    payload = body.model_dump(); payload["fee_rate"] = str(payload["fee_rate"]); payload["settlement_days"] = str(payload["settlement_days"]); payload["beginner_mode"] = str(payload["beginner_mode"]).lower()
    with connect() as con:
        for k, v in payload.items(): con.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v))
    return get_settings()

@app.get("/api/audit/{event_id}")
def audit(event_id: str):
    with connect() as con: rows = [json.loads(x["payload"]) for x in con.execute("SELECT payload FROM audit_log WHERE event_id=?", (event_id,))]
    if not rows: raise HTTPException(404, "未找到审计记录")
    return {"event_id": event_id, "entries": rows}

@app.get("/api/export/backup")
def export_backup():
    context=current_account_context();target_dir=get_runtime().exports_root/context.internal_user_id/context.account_id;target_dir.mkdir(parents=True,exist_ok=True)
    target=target_dir/f"account_backup_{now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.sqlite3"
    src=sqlite3.connect(context.account_db);dst=sqlite3.connect(target)
    try:src.backup(dst)
    finally:dst.close();src.close()
    return FileResponse(target,filename=target.name,headers={"Cache-Control":"no-store, private"})

@app.get("/api/export/cashflows")
def export_cashflows():
    rows = cashflows()["rows"]; output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=["timestamp", "type", "amount", "status", "note", "event_id"]); writer.writeheader(); writer.writerows(rows)
    return StreamingResponse(iter([output.getvalue().encode("utf-8-sig")]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=cashflows.csv"})

def performance():
    with (DATA / "s2_performance.csv").open(encoding="utf-8-sig") as f: rows = {r["model"]: r for r in csv.DictReader(f)}
    v2, bh = rows["V2"], rows["BuyHold"]
    years = (datetime.fromisoformat(v2["end"]) - datetime.fromisoformat(v2["start"])).days / 365.2425
    return {"strategy": {"cagr": float(v2["cagr"]), "mdd": float(v2["max_drawdown"]), "sharpe": float(v2["sharpe"]), "annual_trades": float(v2["trades"]) / years}, "buyhold": {"cagr": float(bh["cagr"]), "mdd": float(bh["max_drawdown"]), "sharpe": float(bh["sharpe"])}}

def crisis():
    with (DATA / "s2_crisis_summary.csv").open(encoding="utf-8-sig") as f: rows = list(csv.DictReader(f))
    out = {}
    for r in rows:
        if r["model"] in {"BuyHold", "V2"}: out.setdefault(r["crisis"], {})[r["model"]] = float(r["max_drawdown"])
    return out

def data_status():
    obs = latest_market_observation(); s = latest_signal(); chosen = obs["date"] if obs else (s["signal_date"] if s else None)
    age = (now().date() - datetime.fromisoformat(chosen).date()).days if chosen else 9999
    latest_run = None
    provider_health = {}
    try:
        with connect_shared(read_only=True) as shared:
            run = shared.execute("SELECT run_at,status,data_date,details FROM data_update_runs ORDER BY id DESC LIMIT 1").fetchone()
        if run:
            latest_run = {"run_at": run[0], "status": run[1], "data_date": run[2]}
            details = json.loads(run[3] or "{}")
            for name, item in (("csi", details.get("csi", {})), ("bond", details.get("bond", {}))):
                provider_health[name] = {
                    "status": item.get("status"),
                    "target_date": item.get("target_date"),
                    "source_latest_date": item.get("source_latest_date"),
                    "target_date_available": item.get("target_date_available"),
                    "error_code": item.get("error_code"),
                    "fallback_used": item.get("fallback_used", False),
                }
            provider_health["funds"] = {
                code: {
                    "status": item.get("status"),
                    "target_date": item.get("target_date"),
                    "source_latest_date": item.get("source_latest_date"),
                    "target_date_available": item.get("target_date_available"),
                    "error_code": item.get("error_code"),
                }
                for code, item in details.get("funds", {}).items()
            }
    except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError):
        latest_run = None
    if latest_run and latest_run["status"] != "SUCCESS":
        status = "数据源未完整发布"
    else:
        status = "正常" if age <= 5 else "需要更新"
    fund,nav=selected_fund_nav()
    return {"latest_date": chosen, "latest_fetch_time": obs["fetched_at"] if obs else None, "status": status, "age_days": age, "market_status": "已更新" if obs else "等待今日数据", "official_signal_date": s["signal_date"] if s else None,"fund_code":fund["code"],"fund_nav":nav["nav"] if nav else None,"fund_nav_date":nav["nav_date"] if nav else None,"fund_nav_status":"正常" if nav else "缺少净值", "latest_update_run": latest_run, "provider_health": provider_health}

app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
