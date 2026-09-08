from __future__ import annotations

from datetime import datetime
from pathlib import Path
import hashlib
import pandas as pd

def fetch_chinabond_10y(raw_dir: Path) -> dict:
    import akshare as ak
    fetched = datetime.now().astimezone().isoformat()
    frame = ak.bond_china_yield(start_date="20260701", end_date=datetime.now().strftime("%Y%m%d"))
    frame.columns = [f"c{i}" for i in range(len(frame.columns))]
    frame = frame.rename(columns={"c0": "curve", "c1": "date", "c8": "cn10y"})
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame["cn10y"] = pd.to_numeric(frame["cn10y"], errors="coerce")
    names = list(frame["curve"].dropna().drop_duplicates())
    if names:
        frame = frame[frame["curve"] == names[-1]]
    frame = frame[["date", "cn10y"]].dropna().sort_values("date").drop_duplicates("date")
    if frame.empty:
        return {"status": "NOT_PUBLISHED", "provider": "ChinaBond", "fetched_at": fetched, "rows": 0}
    source_latest_date = frame["date"].max().isoformat()
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{datetime.now():%Y%m%d_%H%M%S}_CHINABOND_10Y.csv"
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return {"status": "SUCCESS", "provider": "ChinaBond", "field": "10Y", "unit": "percentage_points", "frame": frame, "fetched_at": fetched, "source_latest_date": source_latest_date, "raw_file": str(path), "raw_hash": hashlib.sha256(path.read_bytes()).hexdigest()}

def fetch_backup_10y(raw_dir: Path) -> dict:
    """EastMoney-backed AKShare fallback; never presented as ChinaBond."""
    from datetime import datetime
    import akshare as ak
    frame = ak.bond_zh_us_rate(start_date="20260701")
    date_col = frame.columns[0]
    value_col = frame.columns[3]  # China treasury 10Y in this endpoint
    out = pd.DataFrame({"date": pd.to_datetime(frame[date_col], errors="coerce").dt.date, "cn10y": pd.to_numeric(frame[value_col], errors="coerce")}).dropna().drop_duplicates("date").sort_values("date")
    path = raw_dir / f"{datetime.now():%Y%m%d_%H%M%S}_EASTMONEY_BACKUP_10Y.csv"
    out.to_csv(path, index=False, encoding="utf-8-sig")
    return {"status": "SUCCESS", "provider": "EastMoney_BACKUP", "frame": out, "fetched_at": datetime.now().astimezone().isoformat(), "raw_file": str(path), "raw_hash": hashlib.sha256(path.read_bytes()).hexdigest()}
