from __future__ import annotations

from datetime import datetime
from pathlib import Path
import hashlib
import pandas as pd

def fetch_fund_nav(fund_code: str, raw_dir: Path) -> dict:
    import akshare as ak
    fetched = datetime.now().astimezone().isoformat()
    if not (fund_code.isdigit() and len(fund_code) == 6):
        raise ValueError("fund_code must contain exactly six digits")
    frame = ak.fund_open_fund_info_em(symbol=fund_code, indicator="单位净值走势")
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{datetime.now():%Y%m%d_%H%M%S}_{fund_code}_NAV.csv"
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    date_col = frame.columns[0]
    nav_col = next((c for c in frame.columns if "单位净值" in str(c)), frame.columns[1])
    frame["nav_date"] = pd.to_datetime(frame[date_col], errors="coerce").dt.date
    frame["nav"] = pd.to_numeric(frame[nav_col], errors="coerce")
    frame = frame[["nav_date", "nav"]].dropna().sort_values("nav_date").drop_duplicates("nav_date")
    if frame.empty:
        return {"status": "NOT_PUBLISHED", "provider": "FundNAV", "fund_code": fund_code, "fetched_at": fetched, "rows": 0, "raw_file": str(path), "raw_hash": hashlib.sha256(path.read_bytes()).hexdigest()}
    return {"status": "SUCCESS", "provider": "FundNAV", "fund_code": fund_code, "frame": frame, "fetched_at": fetched, "source_latest_date": frame["nav_date"].max().isoformat(), "raw_file": str(path), "raw_hash": hashlib.sha256(path.read_bytes()).hexdigest()}


def fetch_007801_nav(raw_dir: Path) -> dict:
    """Backward-compatible entry point for older QA scripts."""
    return fetch_fund_nav("007801", raw_dir)
