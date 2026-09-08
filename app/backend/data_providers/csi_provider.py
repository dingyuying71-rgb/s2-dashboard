from __future__ import annotations

from datetime import datetime
from pathlib import Path
import hashlib
import pandas as pd

def fetch_csi_000922(raw_dir: Path) -> dict:
    import akshare as ak
    fetched = datetime.now().astimezone().isoformat()
    frame = ak.stock_zh_index_value_csindex(symbol="000922")
    frame.columns = ["date", "index_code", "index_name", "index_short", "index_en", "index_en_short", "pe1", "pe2", "dy1", "dy2"]
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame["dy2"] = pd.to_numeric(frame["dy2"], errors="coerce")
    frame = frame[["date", "dy2"]].dropna().sort_values("date").drop_duplicates("date")
    if frame.empty:
        return {"status": "NOT_PUBLISHED", "provider": "CSI", "fetched_at": fetched, "rows": 0}
    source_latest_date = frame["date"].max().isoformat()
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{datetime.now():%Y%m%d_%H%M%S}_CSI_000922.csv"
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return {"status": "SUCCESS", "provider": "CSI", "field": "DY2", "frame": frame, "fetched_at": fetched, "source_latest_date": source_latest_date, "raw_file": str(path), "raw_hash": hashlib.sha256(path.read_bytes()).hexdigest()}
