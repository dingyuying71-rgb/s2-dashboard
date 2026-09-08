"""Shared-store helpers for fund NAV observations."""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any


TABLE_SQL = """
CREATE TABLE IF NOT EXISTS fund_nav_observations(
    nav_date TEXT NOT NULL,
    fund_code TEXT NOT NULL,
    nav REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    raw_hash TEXT NOT NULL,
    validation_status TEXT NOT NULL,
    PRIMARY KEY(fund_code, nav_date)
)
"""


def ensure_fund_nav_schema(con: sqlite3.Connection) -> None:
    """Create the multi-fund schema or migrate the legacy date-only key."""
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fund_nav_observations'"
    ).fetchone()
    if not exists:
        con.execute(TABLE_SQL)
        return

    info = con.execute("PRAGMA table_info(fund_nav_observations)").fetchall()
    primary_key = [row[1] for row in sorted((row for row in info if row[5]), key=lambda row: row[5])]
    if primary_key == ["fund_code", "nav_date"]:
        return

    con.execute("ALTER TABLE fund_nav_observations RENAME TO fund_nav_observations_legacy")
    con.execute(TABLE_SQL)
    con.execute(
        """
        INSERT OR IGNORE INTO fund_nav_observations(
            nav_date,fund_code,nav,fetched_at,raw_hash,validation_status
        )
        SELECT nav_date,fund_code,nav,fetched_at,raw_hash,validation_status
        FROM fund_nav_observations_legacy
        """
    )
    con.execute("DROP TABLE fund_nav_observations_legacy")


def latest_fund_nav(
    con: sqlite3.Connection, fund_code: str, as_of: date | str | None = None
) -> dict[str, Any] | None:
    cutoff = as_of.isoformat() if isinstance(as_of, date) else as_of
    sql = """
        SELECT nav_date,fund_code,nav,fetched_at,raw_hash,validation_status
        FROM fund_nav_observations
        WHERE fund_code=? AND validation_status='VALID'
    """
    parameters: list[Any] = [fund_code]
    if cutoff:
        sql += " AND nav_date<=?"
        parameters.append(cutoff)
    sql += " ORDER BY nav_date DESC LIMIT 1"
    cursor = con.execute(sql, parameters)
    row = cursor.fetchone()
    return dict(row) if isinstance(row, sqlite3.Row) else dict(zip((column[0] for column in cursor.description), row)) if row else None
