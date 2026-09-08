"""Validated market-data pipeline: RAW -> STAGING -> VALIDATE -> COMMIT."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from decimal import Decimal
import os
import pandas as pd
import sys
import time
from zoneinfo import ZoneInfo

try:
    from .path_config import resource_root, release_mode, user_data_root, writable_logs_root, writable_runtime_root
except ImportError:  # direct execution by local test runners
    from path_config import resource_root, release_mode, user_data_root, writable_logs_root, writable_runtime_root

from .data_providers.csi_provider import fetch_csi_000922
from .data_providers.chinabond_provider import fetch_chinabond_10y, fetch_backup_10y
from .data_providers.fund_nav_provider import fetch_fund_nav
from .fund_nav_store import ensure_fund_nav_schema

APP = resource_root()
DATA = APP / "data"  # read-only release seed data
_WRITABLE_DATA = user_data_root() if release_mode() else DATA
RAW = _WRITABLE_DATA / "raw"
LOG = writable_logs_root() / "data_update"
RUNTIME = writable_runtime_root()
UPDATE_LOCK_PATH = RUNTIME / "s2_update.lock"
LOCK_LOG = LOG / "update_lock.jsonl"
SHANGHAI = ZoneInfo("Asia/Shanghai")
EXECUTION_FUND_CODES = ("007801", "009051", "022925")


class UpdateLocked(RuntimeError):
    """Another live S2 update owns the process-level update lock."""


def business_now() -> datetime:
    return datetime.now(SHANGHAI)


def to_business_time(value: datetime | None = None) -> datetime:
    if value is None:
        return business_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _write_lock_event(event: str, payload: dict) -> None:
    LOG.mkdir(parents=True, exist_ok=True)
    record = {"at": business_now().isoformat(), "event": event, **payload}
    with LOCK_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class S2UpdateLock:
    """Atomic file lock shared by every in-process and CLI update entry."""

    def __init__(self, trigger: str, task_name: str, business_date: date):
        self.trigger = trigger
        self.task_name = task_name
        self.business_date = business_date
        self.acquired = False

    def acquire(self) -> None:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "trigger": self.trigger,
            "task_name": self.task_name,
            "started_at": business_now().isoformat(),
            "business_date": self.business_date.isoformat(),
        }
        for _ in range(2):
            try:
                fd = os.open(str(UPDATE_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False)
                self.acquired = True
                _write_lock_event("LOCK_ACQUIRED", payload)
                return
            except FileExistsError:
                try:
                    owner = json.loads(UPDATE_LOCK_PATH.read_text(encoding="utf-8"))
                    pid = int(owner.get("pid", 0))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    owner, pid = {}, 0
                if _pid_alive(pid):
                    _write_lock_event("SKIPPED_LOCKED", {"owner": owner, **payload})
                    raise UpdateLocked(f"S2 update lock held by PID {pid}")
                UPDATE_LOCK_PATH.unlink(missing_ok=True)
                _write_lock_event("STALE_LOCK_RECOVERED", {"owner": owner, **payload})
        raise UpdateLocked("S2 update lock is unavailable")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            owner = json.loads(UPDATE_LOCK_PATH.read_text(encoding="utf-8"))
            if int(owner.get("pid", -1)) == os.getpid():
                UPDATE_LOCK_PATH.unlink(missing_ok=True)
                _write_lock_event("LOCK_RELEASED", {"pid": os.getpid()})
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            UPDATE_LOCK_PATH.unlink(missing_ok=True)
        finally:
            self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()

def is_trading_day(day: date) -> bool:
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        values = set(pd.to_datetime(cal.iloc[:, 0], errors="coerce").dt.date.dropna())
        return day in values
    except Exception:
        # Conservative fallback: weekday only; a failed calendar lookup never
        # turns a market-data failure into fabricated data.
        return day.weekday() < 5

def bucket(p: float) -> float:
    return 1.0 if p >= .9 else .8 if p >= .8 else .6 if p >= .6 else .5 if p >= .4 else .4 if p >= .2 else .2 if p >= .1 else 0.0

def historical_seed_spreads() -> list[float]:
    frame = pd.read_csv(DATA / "s2_v2_monthly.csv")
    return pd.to_numeric(frame["spread"], errors="coerce").dropna().tolist()

def percentile(value: float, history: list[float]) -> float:
    return sum(float(x) <= value for x in history) / len(history) if history else 0.0

def validate(name: str, frame: pd.DataFrame, value_col: str, upper: float) -> tuple[bool, str]:
    if frame.empty or frame[value_col].isna().any(): return False, f"{name}_EMPTY_OR_NULL"
    if not frame["date" if "date" in frame else "nav_date"].is_monotonic_increasing: return False, f"{name}_DATE_ORDER"
    if (frame[value_col] <= 0).any() or (frame[value_col] >= upper).any(): return False, f"{name}_RANGE"
    return True, "PASS"


def public_provider_result(value):
    if isinstance(value, dict):
        return {key: public_provider_result(item) for key, item in value.items() if key != "frame"}
    return value


def _annotate_target_date(result: dict, target_date: date, date_column: str) -> dict:
    """Record source freshness and reject non-empty frames missing the target day."""
    frame = result.get("frame")
    values = set()
    latest = result.get("source_latest_date")
    if isinstance(frame, pd.DataFrame) and not frame.empty and date_column in frame:
        values = set(frame[date_column].dropna())
        if values:
            latest = max(values).isoformat()
    result["target_date"] = target_date.isoformat()
    result["source_latest_date"] = latest
    result["target_date_available"] = target_date in values
    if result.get("status") == "SUCCESS" and not result["target_date_available"]:
        result["status"] = "STALE"
        result["error_code"] = "TARGET_DATE_NOT_AVAILABLE"
    return result


def _fetch_until_target(fetcher, target_date: date, date_column: str, attempts: int = 2) -> dict:
    """Retry briefly when a provider returns a valid but not-yet-current frame."""
    last = {"status": "NETWORK_ERROR", "error": "provider_not_called"}
    for attempt in range(1, attempts + 1):
        try:
            last = fetcher()
        except Exception as exc:
            last = {"status": "NETWORK_ERROR", "error": repr(exc)}
        last = _annotate_target_date(last, target_date, date_column)
        last["fetch_attempts"] = attempt
        if last.get("target_date_available"):
            return last
        if attempt < attempts:
            time.sleep(1)
    return last

def _run_update_unlocked(db_path: Path, as_of: datetime | None = None, retry: bool = False,
                         fund_codes: tuple[str, ...] | None = None) -> dict:
    business_as_of = to_business_time(as_of)
    business_date = business_as_of.date()
    run_at = business_now().isoformat()
    if not is_trading_day(business_date):
        return {"status": "SKIP_NON_TRADING_DAY", "run_at": run_at, "data_date": None}
    results = {}
    results["csi"] = _fetch_until_target(lambda: fetch_csi_000922(RAW / "csi"), business_date, "date")
    def fetch_bond_with_approved_fallback():
        try:
            return fetch_chinabond_10y(RAW / "chinabond")
        except Exception as primary_error:
            try:
                result = fetch_backup_10y(RAW / "chinabond")
                result["fallback_used"] = True
                result["primary_error"] = repr(primary_error)
                return result
            except Exception as fallback_error:
                return {"status": "NETWORK_ERROR", "error": repr(primary_error), "fallback_error": repr(fallback_error)}

    results["bond"] = _fetch_until_target(fetch_bond_with_approved_fallback, business_date, "date")
    results["funds"] = {}
    for fund_code in fund_codes or EXECUTION_FUND_CODES:
        results["funds"][fund_code] = _fetch_until_target(
            lambda code=fund_code: fetch_fund_nav(code, RAW / "fund_nav"), business_date, "nav_date"
        )
        results["funds"][fund_code].setdefault("fund_code", fund_code)
    csi, bond = results.get("csi", {}).get("frame", pd.DataFrame()), results.get("bond", {}).get("frame", pd.DataFrame())
    if not csi.empty: csi = csi[(csi["date"] >= date(2026, 8, 4)) & (csi["date"] <= business_date)]
    if not bond.empty: bond = bond[bond["date"] <= business_date]
    merged = csi.merge(bond, on="date", how="inner") if not csi.empty and not bond.empty else pd.DataFrame()
    history = historical_seed_spreads(); rows=[]
    for _, row in merged.iterrows():
        spread = float(row.dy2 - row.cn10y); q = percentile(spread, history); rows.append({"date": str(row.date), "dividend_yield": float(row.dy2), "cn10y": float(row.cn10y), "spread": spread, "spread_percentile": q, "target_exposure": bucket(q), "source_dy": "CSI_DY2", "source_bond": "ChinaBond_10Y", "fetched_at": run_at, "raw_hash": f"{results['csi'].get('raw_hash','')}|{results['bond'].get('raw_hash','')}", "validation_status": "VALID"})
        history.append(spread)
    today_published = bool(not csi.empty and not bond.empty and business_date in set(csi["date"]) and business_date in set(bond["date"]))
    valid_market = bool(rows)
    for key, col, upper in [("csi", "dy2", 100), ("bond", "cn10y", 20)]:
        if results.get(key, {}).get("frame") is not None and not results[key].get("frame", pd.DataFrame()).empty:
            frame = results[key]["frame"]; ok, reason = validate(key, frame, col, upper); results[key]["validation_status"] = reason if not ok else "PASS"
    status = "SUCCESS" if today_published and valid_market and results.get("csi", {}).get("validation_status") == "PASS" and results.get("bond", {}).get("validation_status") == "PASS" else "PARTIAL" if valid_market and today_published else "NOT_PUBLISHED"
    LOG.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE IF NOT EXISTS market_observations(date TEXT PRIMARY KEY, dividend_yield REAL, cn10y REAL, spread REAL, spread_percentile REAL, target_exposure REAL, source_dy TEXT, source_bond TEXT, fetched_at TEXT, raw_hash TEXT, validation_status TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS data_update_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, run_at TEXT, status TEXT, data_date TEXT, details TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS official_signals(signal_date TEXT PRIMARY KEY, dividend_yield REAL, cn10y REAL, spread REAL, spread_percentile REAL, target_exposure REAL, source_dy TEXT, source_bond TEXT, generated_at TEXT, validation_status TEXT)")
        ensure_fund_nav_schema(con)
        # Historical rows are never touched. Future rows are append-only; a
        # revised upstream value is retained in raw files and reported, not
        # silently overwritten.
        for item in rows:
            exists = con.execute("SELECT 1 FROM market_observations WHERE date=?", (item["date"],)).fetchone()
            if not exists: con.execute("INSERT INTO market_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(item.values()))
        for fund_code, fund_result in results["funds"].items():
            fund_frame = fund_result.get("frame", pd.DataFrame())
            if fund_frame.empty:
                continue
            for _, item in fund_frame.iterrows():
                if float(item.nav) <= 0:
                    continue
                con.execute(
                    "INSERT OR IGNORE INTO fund_nav_observations VALUES(?,?,?,?,?,?)",
                    (str(item.nav_date), fund_code, float(item.nav), fund_result.get("fetched_at", run_at), fund_result.get("raw_hash", ""), "VALID"),
                )
        latest = max((x["date"] for x in rows), default=None)
        if latest and date.fromisoformat(latest).day == 1: pass
        con.execute("INSERT INTO data_update_runs(run_at,status,data_date,details) VALUES(?,?,?,?)", (run_at,status,latest,json.dumps(public_provider_result(results),ensure_ascii=False,default=str)))
        if rows:
            last = rows[-1]
            # Official monthly signal is generated only on the last trading
            # day according to the same calendar used by the updater.
            next_day = last["date"]
            d = date.fromisoformat(next_day) + timedelta(days=1)
            while not is_trading_day(d): d += timedelta(days=1)
            if d.month != date.fromisoformat(next_day).month:
                con.execute("INSERT OR REPLACE INTO official_signals VALUES(?,?,?,?,?,?,?,?,?,?)", (last["date"],last["dividend_yield"],last["cn10y"],last["spread"],last["spread_percentile"],last["target_exposure"],last["source_dy"],last["source_bond"],run_at,"VALID"))
    fund_statuses = {code: result.get("status") for code, result in results["funds"].items()}
    log_path = LOG / f"{business_now():%Y%m%d}.jsonl"; log_path.open("a",encoding="utf-8").write(json.dumps({"run_at":run_at,"status":status,"retry":retry,"data_date":latest,"csi":results.get("csi",{}).get("status"),"bond":results.get("bond",{}).get("status"),"funds":fund_statuses,"committed":valid_market},ensure_ascii=False,default=str)+"\n")
    return {"status": status, "run_at": run_at, "data_date": latest, "rows": rows, "providers": public_provider_result(results)}


def run_update(db_path: Path, as_of: datetime | None = None, retry: bool = False,
               trigger: str | None = None, task_name: str | None = None,
               fund_codes: tuple[str, ...] | None = None) -> dict:
    business_as_of = to_business_time(as_of)
    trigger = trigger or os.environ.get("S2_UPDATE_TRIGGER") or ("scheduled_retry" if retry else "scheduled_or_manual")
    task_name = task_name or os.environ.get("S2_UPDATE_TASK_NAME") or "unknown"
    try:
        with S2UpdateLock(trigger, task_name, business_as_of.date()):
            return _run_update_unlocked(db_path, as_of=business_as_of, retry=retry, fund_codes=fund_codes)
    except UpdateLocked as exc:
        return {
            "status": "SKIPPED_LOCKED",
            "run_at": business_now().isoformat(),
            "data_date": None,
            "retry": retry,
            "message": str(exc),
        }

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    shared_db = Path(os.environ.get("S2_SHARED_DB", str(user_data_root() / "shared" / "strategy.db" if release_mode() else DATA / "multi_user" / "shared" / "strategy.db")))
    result = run_update(shared_db, retry=args.retry)
    print(json.dumps(result, ensure_ascii=False, default=str))
