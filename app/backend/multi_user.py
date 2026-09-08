"""Multi-user Isolation V1 infrastructure.

This module is intentionally outside AccountEngine. It owns identity,
registry, account context, fixed-root database resolution and HTTP security.
It never changes S2-account-v4.0-frozen arithmetic.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from fastapi import HTTPException, Request

try:
    from .path_config import resource_root, release_mode, user_data_root
except ImportError:  # direct execution by local test runners
    from path_config import resource_root, release_mode, user_data_root


CONTRACT_VERSION = "S2-account-v4.0-frozen"
ACCOUNT_SCHEMA_VERSION = 1
ACCOUNT_MAINTENANCE_VERSION = "1.0"
USER_ID_RE = re.compile(r"^usr_[0-9a-f]{32}$")
ACCOUNT_ID_RE = re.compile(r"^acct_(?:legacy_)?[0-9a-f]{32}$")
SAFE_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")
FORBIDDEN_TENANT_FIELDS = frozenset({"user_id", "tenant_id", "account_id", "db_path"})
VALID_ROLES = frozenset({"OWNER", "TESTER"})
VALID_STATUSES = frozenset({"PROVISIONING", "ACTIVE", "DISABLED", "ERROR"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testserver"})


class MultiUserSecurityError(RuntimeError):
    def __init__(self, code: str, status_code: int = 403):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class ClosingConnection(sqlite3.Connection):
    """SQLite connection whose context manager also releases the file handle.

    The stdlib connection context manager commits or rolls back but does not
    close.  Multi-user isolation performs frequent resolver/integrity checks,
    so retaining those handles can block safe rename checks on Windows.
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


@dataclass(frozen=True)
class IdentityContext:
    access_sub: str
    normalized_email: str
    provider: str


@dataclass(frozen=True)
class AccountContext:
    internal_user_id: str
    role: str
    display_name: str
    account_db: Path
    identity: IdentityContext
    account_id: str = ""


_account_context: contextvars.ContextVar[AccountContext | None] = contextvars.ContextVar(
    "s2_account_context", default=None
)


def normalize_email(value: str) -> str:
    normalized = value.strip().casefold()
    if not SAFE_EMAIL_RE.fullmatch(normalized) or any(ch in normalized for ch in "\r\n\x00"):
        raise MultiUserSecurityError("INVALID_EMAIL", 400)
    return normalized


def opaque_user_id() -> str:
    return f"usr_{uuid.uuid4().hex}"


def _is_reparse_point(path: Path) -> bool:
    try:
        attrs = path.lstat().st_file_attributes
    except AttributeError:
        return path.is_symlink()
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


class UserRegistry:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10, factory=ClosingConnection)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    @contextlib.contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            with con:
                yield con
        finally:
            con.close()

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.session() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    user_id TEXT PRIMARY KEY,
                    invite_email TEXT NOT NULL UNIQUE,
                    current_access_email TEXT,
                    access_sub TEXT UNIQUE,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('OWNER','TESTER')),
                    status TEXT NOT NULL CHECK(status IN ('PROVISIONING','ACTIVE','DISABLED','ERROR')),
                    created_at TEXT NOT NULL,
                    last_login_at TEXT,
                    schema_version INTEGER NOT NULL,
                    account_contract_version TEXT NOT NULL,
                    migration_status TEXT NOT NULL,
                    disabled_at TEXT,
                    retention_until TEXT,
                    identity_version INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS registry_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    actor_user_id TEXT,
                    event_type TEXT NOT NULL,
                    target_user_id TEXT,
                    result TEXT NOT NULL,
                    error_code TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_registry_status ON users(status);
                CREATE TABLE IF NOT EXISTS account_catalog(
                    account_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('ACTIVE','ARCHIVED','STAGING','ERROR')),
                    db_relative_path TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    account_type TEXT NOT NULL DEFAULT 'NORMAL',
                    lineage_id TEXT NOT NULL DEFAULT '',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    created_at TEXT NOT NULL,
                    archived_at TEXT,
                    archive_reason TEXT NOT NULL DEFAULT '',
                    ending_wealth TEXT,
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                );
                CREATE TABLE IF NOT EXISTS account_type_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    old_type TEXT NOT NULL,
                    new_type TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    user_confirmed INTEGER NOT NULL CHECK(user_confirmed=1),
                    note TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(account_id) REFERENCES account_catalog(account_id),
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_account_type_audit_account
                    ON account_type_audit(account_id,changed_at);
                CREATE TABLE IF NOT EXISTS maintenance_operations(
                    operation_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    old_account_id TEXT NOT NULL,
                    new_account_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    old_hash TEXT,
                    new_hash TEXT,
                    backup_path_ref TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    error_text TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                );
                """
            )
            user_columns = {row[1] for row in con.execute("PRAGMA table_info(users)")}
            if "current_account_id" not in user_columns:
                con.execute("ALTER TABLE users ADD COLUMN current_account_id TEXT")
            catalog_columns = {row[1] for row in con.execute("PRAGMA table_info(account_catalog)")}
            for name, definition in (
                ("display_name", "TEXT NOT NULL DEFAULT ''"),
                ("account_type", "TEXT NOT NULL DEFAULT 'NORMAL'"),
                ("lineage_id", "TEXT NOT NULL DEFAULT ''"),
                ("is_default", "INTEGER NOT NULL DEFAULT 0"),
                ("updated_at", "TEXT"),
            ):
                if name not in catalog_columns:
                    con.execute(f"ALTER TABLE account_catalog ADD COLUMN {name} {definition}")
            con.execute("DROP INDEX IF EXISTS ux_account_catalog_one_active")
            rows = list(con.execute("SELECT user_id,current_account_id FROM users WHERE status IN ('ACTIVE','DISABLED')"))
            for row in rows:
                # Preserve the legacy current pointer where it is still valid.
                # On later restarts this must not silently change an OWNER's
                # chosen default merely because multiple containers now exist.
                existing = con.execute(
                    """SELECT account_id FROM account_catalog
                       WHERE user_id=? AND status='ACTIVE'
                       ORDER BY CASE WHEN account_id=? THEN 0
                                     WHEN is_default=1 THEN 1 ELSE 2 END,
                                created_at
                       LIMIT 1""",
                    (row["user_id"], row["current_account_id"]),
                ).fetchone()
                if existing is None:
                    legacy_id = f"acct_legacy_{row['user_id'][4:]}"
                    con.execute(
                    "INSERT OR IGNORE INTO account_catalog(account_id,user_id,status,db_relative_path,display_name,account_type,lineage_id,is_default,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (legacy_id, row["user_id"], "ACTIVE", "account.db", "我的投资", "NORMAL", legacy_id, 1, datetime.now().astimezone().isoformat(), datetime.now().astimezone().isoformat()),
                    )
                    existing = con.execute(
                        "SELECT account_id FROM account_catalog WHERE user_id=? AND status='ACTIVE' ORDER BY created_at LIMIT 1",
                        (row["user_id"],),
                    ).fetchone()
                legacy_prefix = f"{row['user_id']}/"
                con.execute("UPDATE account_catalog SET db_relative_path=substr(db_relative_path, ?) WHERE user_id=? AND db_relative_path LIKE ?", (len(legacy_prefix) + 1, row["user_id"], legacy_prefix + "%"))
                con.execute("UPDATE account_catalog SET display_name=CASE WHEN trim(display_name)='' THEN '我的投资' ELSE display_name END, account_type=CASE WHEN account_type NOT IN ('NORMAL','PENSION','OTHER') THEN 'NORMAL' ELSE account_type END, lineage_id=CASE WHEN trim(lineage_id)='' THEN account_id ELSE lineage_id END, updated_at=COALESCE(updated_at,created_at) WHERE user_id=?", (row["user_id"],))
                if row["current_account_id"] != existing["account_id"]:
                    con.execute("UPDATE users SET current_account_id=? WHERE user_id=?", (existing["account_id"], row["user_id"]))
                con.execute("UPDATE account_catalog SET is_default=CASE WHEN account_id=? THEN 1 ELSE 0 END WHERE user_id=? AND status='ACTIVE'", (existing["account_id"], row["user_id"]))
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_account_catalog_active_name ON account_catalog(user_id, lower(display_name)) WHERE status='ACTIVE'")
            # A restart operation only switches the account pointer in its
            # final registry transaction.  Anything left RUNNING therefore
            # has not made an ambiguous current-account change; mark it for
            # review and keep the previous ACTIVE account fail-closed.
            con.execute(
                "UPDATE maintenance_operations SET status='ERROR',stage='RECOVERY_REQUIRED',updated_at=?,error_text=CASE WHEN error_text='' THEN 'startup recovery: pointer was not committed' ELSE error_text END WHERE status='RUNNING'",
                (datetime.now().astimezone().isoformat(),),
            )

    def validate_integrity(self, resolver: "DbResolver", require_accounts: bool = True) -> None:
        try:
            with self.session() as con:
                rows = list(con.execute("SELECT * FROM users WHERE status IN ('ACTIVE','DISABLED')"))
                duplicate_sub = con.execute(
                    "SELECT access_sub,COUNT(*) n FROM users WHERE access_sub IS NOT NULL GROUP BY access_sub HAVING n>1"
                ).fetchall()
                duplicate_email = con.execute(
                    "SELECT invite_email,COUNT(*) n FROM users GROUP BY invite_email HAVING n>1"
                ).fetchall()
                duplicate_default = con.execute(
                    "SELECT user_id,COUNT(*) n FROM account_catalog WHERE status='ACTIVE' AND is_default=1 GROUP BY user_id HAVING n<>1"
                ).fetchall()
                tester_multiple_active = con.execute(
                    "SELECT c.user_id,COUNT(*) n FROM account_catalog c JOIN users u ON u.user_id=c.user_id WHERE c.status='ACTIVE' AND u.role='TESTER' GROUP BY c.user_id HAVING n>1"
                ).fetchall()
            if duplicate_sub or duplicate_email or duplicate_default or tester_multiple_active:
                raise MultiUserSecurityError("DUPLICATE_IDENTITY_MAPPING", 503)
            seen: dict[tuple[int, int], str] = {}
            for row in rows:
                if not USER_ID_RE.fullmatch(row["user_id"]):
                    raise MultiUserSecurityError("INVALID_REGISTRY_USER_ID", 503)
                if row["account_contract_version"] != CONTRACT_VERSION:
                    raise MultiUserSecurityError("MIGRATION_REQUIRED", 503)
                if require_accounts:
                    for account in self.active_accounts(row["user_id"]):
                        path = resolver.resolve_relative(row["user_id"], account["db_relative_path"], require_schema=False)
                        info = path.stat()
                        identity = (info.st_dev, info.st_ino)
                        mapping = f"{row['user_id']}:{account['account_id']}"
                        if identity in seen and seen[identity] != mapping:
                            raise MultiUserSecurityError("DUPLICATE_DB_MAPPING", 503)
                        seen[identity] = mapping
        except sqlite3.DatabaseError as exc:
            raise MultiUserSecurityError("REGISTRY_UNAVAILABLE", 503) from exc

    def get_by_identity(self, identity: IdentityContext) -> sqlite3.Row:
        try:
            with self.session() as con:
                row = con.execute("SELECT * FROM users WHERE access_sub=?", (identity.access_sub,)).fetchone()
        except sqlite3.DatabaseError as exc:
            raise MultiUserSecurityError("REGISTRY_UNAVAILABLE", 503) from exc
        if row is None:
            raise MultiUserSecurityError("USER_NOT_INVITED", 403)
        if row["status"] != "ACTIVE":
            raise MultiUserSecurityError("USER_DISABLED", 403)
        if row["current_access_email"] and normalize_email(identity.normalized_email) != row["current_access_email"]:
            raise MultiUserSecurityError("IDENTITY_REBIND_REQUIRED", 403)
        if row["account_contract_version"] != CONTRACT_VERSION or row["schema_version"] != ACCOUNT_SCHEMA_VERSION:
            raise MultiUserSecurityError("MIGRATION_REQUIRED", 503)
        return row

    def get(self, user_id: str) -> sqlite3.Row | None:
        with self.session() as con:
            return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def current_account(self, user_id: str) -> sqlite3.Row:
        with self.session() as con:
            row = con.execute(
                "SELECT c.* FROM users u JOIN account_catalog c ON c.account_id=u.current_account_id WHERE u.user_id=? AND c.user_id=? AND c.status='ACTIVE'",
                (user_id, user_id),
            ).fetchone()
        if row is None:
            raise MultiUserSecurityError("ACCOUNT_UNAVAILABLE", 503)
        return row

    def active_accounts(self, user_id: str) -> list[sqlite3.Row]:
        with self.session() as con:
            return list(con.execute(
                "SELECT * FROM account_catalog WHERE user_id=? AND status='ACTIVE' ORDER BY is_default DESC,created_at",
                (user_id,),
            ))

    def active_account(self, user_id: str, account_id: str) -> sqlite3.Row | None:
        if not ACCOUNT_ID_RE.fullmatch(account_id):
            return None
        with self.session() as con:
            return con.execute(
                "SELECT * FROM account_catalog WHERE user_id=? AND account_id=? AND status='ACTIVE'",
                (user_id, account_id),
            ).fetchone()

    def create_account_container(self, user_id: str, display_name: str, account_type: str) -> sqlite3.Row:
        if account_type not in {"NORMAL", "PENSION", "OTHER"}:
            raise MultiUserSecurityError("INVALID_ACCOUNT_TYPE", 400)
        name = display_name.strip()
        if not name or len(name) > 80:
            raise MultiUserSecurityError("INVALID_ACCOUNT_NAME", 400)
        account_id = f"acct_{uuid.uuid4().hex}"
        created = datetime.now().astimezone().isoformat()
        with self.session() as con:
            try:
                con.execute(
                    "INSERT INTO account_catalog(account_id,user_id,status,db_relative_path,display_name,account_type,lineage_id,is_default,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (account_id, user_id, "STAGING", f"active/{account_id}/account.db", name, account_type, account_id, 0, created, created),
                )
            except sqlite3.IntegrityError as exc:
                raise MultiUserSecurityError("DUPLICATE_ACCOUNT_NAME", 409) from exc
            return con.execute("SELECT * FROM account_catalog WHERE account_id=?", (account_id,)).fetchone()

    def activate_account_container(self, user_id: str, account_id: str) -> None:
        with self.session() as con:
            con.execute("UPDATE account_catalog SET status='ACTIVE',updated_at=? WHERE user_id=? AND account_id=? AND status='STAGING'", (datetime.now().astimezone().isoformat(), user_id, account_id))
            if con.total_changes != 1:
                raise MultiUserSecurityError("ACCOUNT_CONTAINER_STATE_INVALID", 409)

    def mark_account_container_error(self, user_id: str, account_id: str) -> None:
        with self.session() as con:
            con.execute("UPDATE account_catalog SET status='ERROR',updated_at=? WHERE user_id=? AND account_id=? AND status='STAGING'", (datetime.now().astimezone().isoformat(), user_id, account_id))

    def rename_account(self, user_id: str, account_id: str, display_name: str) -> None:
        name = display_name.strip()
        if not name or len(name) > 80:
            raise MultiUserSecurityError("INVALID_ACCOUNT_NAME", 400)
        with self.session() as con:
            try:
                con.execute("UPDATE account_catalog SET display_name=?,updated_at=? WHERE user_id=? AND account_id=? AND status='ACTIVE'", (name, datetime.now().astimezone().isoformat(), user_id, account_id))
            except sqlite3.IntegrityError as exc:
                raise MultiUserSecurityError("DUPLICATE_ACCOUNT_NAME", 409) from exc
            if con.total_changes != 1:
                raise MultiUserSecurityError("OBJECT_NOT_FOUND", 404)

    def change_account_type(self, user_id: str, account_id: str, new_type: str,
                            user_confirmed: bool, note: str = "") -> sqlite3.Row:
        if new_type not in {"NORMAL", "PENSION"}:
            raise MultiUserSecurityError("INVALID_ACCOUNT_TYPE", 400)
        if user_confirmed is not True:
            raise MultiUserSecurityError("ACCOUNT_TYPE_CONFIRMATION_REQUIRED", 409)
        audit_note = note.strip()
        if len(audit_note) > 500:
            raise MultiUserSecurityError("ACCOUNT_TYPE_NOTE_TOO_LONG", 400)
        changed_at = datetime.now().astimezone().isoformat()
        with self.session() as con:
            row = con.execute(
                "SELECT * FROM account_catalog WHERE user_id=? AND account_id=? AND status='ACTIVE'",
                (user_id, account_id),
            ).fetchone()
            if row is None:
                raise MultiUserSecurityError("OBJECT_NOT_FOUND", 404)
            old_type = row["account_type"]
            if old_type == new_type:
                return row
            con.execute(
                "UPDATE account_catalog SET account_type=?,updated_at=? WHERE user_id=? AND account_id=? AND status='ACTIVE'",
                (new_type, changed_at, user_id, account_id),
            )
            con.execute(
                """INSERT INTO account_type_audit(
                       account_id,user_id,old_type,new_type,changed_at,user_confirmed,note
                   ) VALUES(?,?,?,?,?,1,?)""",
                (account_id, user_id, old_type, new_type, changed_at, audit_note),
            )
            return con.execute(
                "SELECT * FROM account_catalog WHERE user_id=? AND account_id=?",
                (user_id, account_id),
            ).fetchone()

    def account_type_audit(self, user_id: str, account_id: str) -> list[sqlite3.Row]:
        with self.session() as con:
            return list(con.execute(
                """SELECT account_id,old_type,new_type,changed_at,user_confirmed,note
                   FROM account_type_audit
                   WHERE user_id=? AND account_id=? ORDER BY id DESC""",
                (user_id, account_id),
            ))

    def set_default_account(self, user_id: str, account_id: str) -> None:
        with self.session() as con:
            row = con.execute("SELECT 1 FROM account_catalog WHERE user_id=? AND account_id=? AND status='ACTIVE'", (user_id, account_id)).fetchone()
            if row is None:
                raise MultiUserSecurityError("OBJECT_NOT_FOUND", 404)
            con.execute("UPDATE account_catalog SET is_default=CASE WHEN account_id=? THEN 1 ELSE 0 END,updated_at=? WHERE user_id=? AND status='ACTIVE'", (account_id, datetime.now().astimezone().isoformat(), user_id))
            con.execute("UPDATE users SET current_account_id=? WHERE user_id=?", (account_id, user_id))

    def archive_account(self, user_id: str, account_id: str) -> None:
        with self.session() as con:
            active = list(con.execute("SELECT account_id,is_default FROM account_catalog WHERE user_id=? AND status='ACTIVE' ORDER BY created_at", (user_id,)))
            if len(active) < 2 or not any(row["account_id"] == account_id for row in active):
                raise MultiUserSecurityError("ACCOUNT_ARCHIVE_FORBIDDEN", 409)
            replacement = next(row["account_id"] for row in active if row["account_id"] != account_id)
            now = datetime.now().astimezone().isoformat()
            con.execute("UPDATE account_catalog SET status='ARCHIVED',archived_at=?,archive_reason='OWNER_ARCHIVED',is_default=0,updated_at=? WHERE user_id=? AND account_id=?", (now, now, user_id, account_id))
            if con.execute("SELECT current_account_id FROM users WHERE user_id=?", (user_id,)).fetchone()[0] == account_id:
                con.execute("UPDATE account_catalog SET is_default=CASE WHEN account_id=? THEN 1 ELSE is_default END,updated_at=? WHERE user_id=? AND status='ACTIVE'", (replacement, now, user_id))
                con.execute("UPDATE users SET current_account_id=? WHERE user_id=?", (replacement, user_id))

    def account_by_archive(self, user_id: str, archive_id: str, lineage_id: str) -> sqlite3.Row | None:
        with self.session() as con:
            return con.execute(
                "SELECT * FROM account_catalog WHERE user_id=? AND account_id=? AND lineage_id=? AND status='ARCHIVED'",
                (user_id, archive_id, lineage_id),
            ).fetchone()

    def archives(self, user_id: str, lineage_id: str) -> list[sqlite3.Row]:
        with self.session() as con:
            return list(con.execute("SELECT * FROM account_catalog WHERE user_id=? AND lineage_id=? AND status='ARCHIVED' ORDER BY archived_at DESC", (user_id, lineage_id)))

    def list_safe(self) -> list[dict]:
        """Privacy-preserving OWNER view: no account balances or notes."""
        with self.session() as con:
            rows = con.execute(
                """SELECT user_id,display_name,role,status,created_at,last_login_at,
                          schema_version,account_contract_version,migration_status,
                          disabled_at,retention_until
                   FROM users ORDER BY created_at"""
            ).fetchall()
        return [dict(row) for row in rows]

    def create_provisioning(
        self,
        invite_email: str,
        access_sub: str,
        display_name: str,
        role: str,
        user_id: str | None = None,
    ) -> tuple[sqlite3.Row, bool]:
        if role not in VALID_ROLES:
            raise MultiUserSecurityError("INVALID_ROLE", 400)
        email = normalize_email(invite_email)
        user_id = user_id or opaque_user_id()
        if not USER_ID_RE.fullmatch(user_id):
            raise MultiUserSecurityError("INVALID_INTERNAL_USER_ID", 400)
        created = datetime.now().astimezone().isoformat()
        with self.session() as con:
            existing = con.execute(
                "SELECT * FROM users WHERE invite_email=? OR access_sub=?", (email, access_sub)
            ).fetchone()
            if existing:
                same = existing["invite_email"] == email and existing["access_sub"] == access_sub
                if not same:
                    raise MultiUserSecurityError("DUPLICATE_IDENTITY_MAPPING", 409)
                return existing, False
            con.execute(
                """INSERT INTO users(
                    user_id,invite_email,current_access_email,access_sub,display_name,
                    role,status,created_at,schema_version,account_contract_version,migration_status
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    user_id,
                    email,
                    email,
                    access_sub,
                    display_name.strip()[:80] or role.title(),
                    role,
                    "PROVISIONING",
                    created,
                    ACCOUNT_SCHEMA_VERSION,
                    CONTRACT_VERSION,
                    "INITIALIZING",
                ),
            )
            return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone(), True

    def activate(self, user_id: str) -> None:
        with self.session() as con:
            con.execute(
                "UPDATE users SET status='ACTIVE',migration_status='CURRENT' WHERE user_id=? AND status='PROVISIONING'",
                (user_id,),
            )
            if con.total_changes != 1:
                raise MultiUserSecurityError("PROVISIONING_STATE_INVALID", 409)

    def mark_error(self, user_id: str) -> None:
        with self.session() as con:
            con.execute("UPDATE users SET status='ERROR',migration_status='ERROR' WHERE user_id=?", (user_id,))

    def disable(self, user_id: str) -> None:
        when = datetime.now().astimezone()
        retention = when + timedelta(days=90)
        with self.session() as con:
            row = con.execute("SELECT role,status FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                raise MultiUserSecurityError("USER_NOT_FOUND", 404)
            if row["role"] == "OWNER":
                raise MultiUserSecurityError("OWNER_DISABLE_FORBIDDEN", 409)
            con.execute(
                "UPDATE users SET status='DISABLED',disabled_at=?,retention_until=? WHERE user_id=?",
                (when.isoformat(), retention.isoformat(), user_id),
            )

    def touch_login(self, user_id: str) -> None:
        with self.session() as con:
            con.execute("UPDATE users SET last_login_at=? WHERE user_id=?", (datetime.now().astimezone().isoformat(), user_id))


class DbResolver:
    def __init__(self, users_root: Path):
        self.users_root = users_root.absolute()

    def resolve(self, internal_user_id: str, require_schema: bool = True) -> Path:
        if not USER_ID_RE.fullmatch(internal_user_id):
            raise MultiUserSecurityError("INVALID_INTERNAL_USER_ID", 403)
        root = self.users_root.resolve()
        user_dir = root / internal_user_id
        if not user_dir.exists() or not user_dir.is_dir():
            raise MultiUserSecurityError("ACCOUNT_UNAVAILABLE", 503)
        if _is_reparse_point(user_dir):
            raise MultiUserSecurityError("ACCOUNT_PATH_REPARSE_POINT", 503)
        resolved_dir = user_dir.resolve()
        if resolved_dir.parent != root:
            raise MultiUserSecurityError("ACCOUNT_PATH_ESCAPE", 503)
        db = resolved_dir / "account.db"
        if not db.exists() or not db.is_file() or _is_reparse_point(db):
            raise MultiUserSecurityError("ACCOUNT_UNAVAILABLE", 503)
        resolved_db = db.resolve()
        if resolved_db.parent != resolved_dir or resolved_db.name != "account.db":
            raise MultiUserSecurityError("ACCOUNT_PATH_ESCAPE", 503)
        if require_schema:
            self.verify_schema(resolved_db)
        return resolved_db

    def resolve_relative(self, internal_user_id: str, relative_path: str, require_schema: bool = True) -> Path:
        if not USER_ID_RE.fullmatch(internal_user_id):
            raise MultiUserSecurityError("INVALID_INTERNAL_USER_ID", 403)
        root = self.users_root.resolve()
        user_root = root / internal_user_id
        target = user_root / relative_path
        if not user_root.exists() or _is_reparse_point(user_root) or not target.exists() or _is_reparse_point(target):
            raise MultiUserSecurityError("ACCOUNT_UNAVAILABLE", 503)
        resolved_root, resolved_target = user_root.resolve(), target.resolve()
        if resolved_root.parent != root or resolved_target.name != "account.db" or resolved_root not in resolved_target.parents:
            raise MultiUserSecurityError("ACCOUNT_PATH_ESCAPE", 503)
        if require_schema:
            self.verify_schema(resolved_target)
        return resolved_target

    @staticmethod
    def verify_schema(db: Path) -> None:
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            meta_exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_metadata'"
            ).fetchone()
            if not meta_exists:
                raise MultiUserSecurityError("MIGRATION_REQUIRED", 503)
            metadata = dict(con.execute("SELECT key,value FROM account_metadata"))
            if metadata.get("account_contract_version") != CONTRACT_VERSION:
                raise MultiUserSecurityError("MIGRATION_REQUIRED", 503)
            if metadata.get("multi_user_schema_version") != str(ACCOUNT_SCHEMA_VERSION):
                raise MultiUserSecurityError("MIGRATION_REQUIRED", 503)
            integrity = con.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise MultiUserSecurityError("ACCOUNT_CORRUPT", 503)
        except MultiUserSecurityError:
            raise
        except sqlite3.DatabaseError as exc:
            raise MultiUserSecurityError("ACCOUNT_CORRUPT", 503) from exc
        finally:
            if "con" in locals():
                con.close()


class LocalOwnerIdentityProvider:
    """Temporary localhost-only provider used before Cloudflare deployment.

    It is intentionally OWNER-only and refuses all non-loopback traffic. The
    future Cloudflare phase replaces it; it is not a public authentication
    mechanism.
    """

    def __init__(self, owner_sub: str, owner_email: str):
        self.owner_sub = owner_sub
        self.owner_email = normalize_email(owner_email)

    def identity(self, request: Request) -> IdentityContext:
        client = request.client.host if request.client else ""
        host = request.url.hostname or ""
        if client not in LOOPBACK_HOSTS or host not in LOOPBACK_HOSTS:
            raise MultiUserSecurityError("LOCAL_IDENTITY_LOOPBACK_ONLY", 403)
        return IdentityContext(self.owner_sub, self.owner_email, "LOCAL_OWNER_BOOTSTRAP")


class QaSignedIdentityProvider:
    HEADER = "x-s2-qa-identity"
    SIGNATURE = "x-s2-qa-signature"

    def __init__(self, secret: str):
        if len(secret) < 32:
            raise RuntimeError("QA identity secret must be at least 32 characters")
        self.secret = secret.encode("utf-8")

    def sign(self, access_sub: str) -> str:
        return hmac.new(self.secret, access_sub.encode("utf-8"), hashlib.sha256).hexdigest()

    def identity(self, request: Request) -> IdentityContext:
        sub = request.headers.get(self.HEADER, "")
        signature = request.headers.get(self.SIGNATURE, "")
        expected = self.sign(sub) if sub else ""
        if not sub or not hmac.compare_digest(signature, expected):
            raise MultiUserSecurityError("QA_IDENTITY_INVALID", 401)
        # Email is server-derived from the signed sub mapping, never supplied
        # as a trusted browser header. Registry consistency is checked later.
        runtime = get_runtime()
        try:
            with runtime.registry.session() as con:
                row = con.execute("SELECT current_access_email FROM users WHERE access_sub=?", (sub,)).fetchone()
        except sqlite3.DatabaseError as exc:
            raise MultiUserSecurityError("REGISTRY_UNAVAILABLE", 503) from exc
        email = row[0] if row else "unknown@invalid.local"
        return IdentityContext(sub, email, "QA_SIGNED")


class CloudflareAccessIdentityProvider:
    """Future adapter contract. Deliberately not activated in this phase."""

    def identity(self, request: Request) -> IdentityContext:
        raise MultiUserSecurityError("CLOUDFLARE_NOT_CONFIGURED", 503)


class MultiUserRuntime:
    def __init__(self):
        self.environment = os.environ.get("S2_ENV", "PRODUCTION").upper()
        default_root = user_data_root()
        self.root = Path(os.environ.get("S2_MULTIUSER_ROOT", str(default_root))).absolute()
        self.registry = UserRegistry(self.root / "registry" / "users.db")
        self.resolver = DbResolver(self.root / "users")
        default_shared_db = self.root / "shared" / "strategy.db"
        self.shared_db = Path(os.environ.get("S2_SHARED_STRATEGY_DB", str(default_shared_db))).absolute()
        self.shared_db_read_only = os.environ.get("S2_SHARED_STRATEGY_DB_READ_ONLY", "false").casefold() == "true"
        if self.shared_db_read_only and self.environment != "QA":
            raise RuntimeError("External shared strategy store may be read-only only in QA")
        self.backups_root = Path(
            os.environ.get("S2_MULTIUSER_BACKUPS", str(user_data_root() / "backups" if release_mode() else resource_root() / "backups" / "users"))
        ).absolute()
        self.exports_root = Path(
            os.environ.get("S2_MULTIUSER_EXPORTS", str(user_data_root() / "exports" if release_mode() else resource_root() / "exports" / "users"))
        ).absolute()
        self.identity_mode = os.environ.get("S2_IDENTITY_MODE", "LOCAL_OWNER").upper()
        self.owner_sub = os.environ.get("S2_LOCAL_OWNER_SUB", "local-owner-v1")
        self.owner_email = os.environ.get("S2_LOCAL_OWNER_EMAIL", "owner@local.invalid")
        csrf_secret = os.environ.get("S2_CSRF_SECRET") or secrets.token_hex(32)
        self.csrf_secret = csrf_secret.encode("utf-8")
        dashboard_port = os.environ.get("S2_DASHBOARD_PORT", "8767")
        allowed = os.environ.get(
            "S2_ALLOWED_ORIGINS",
            f"http://127.0.0.1:{dashboard_port},http://localhost:{dashboard_port},http://testserver",
        )
        self.allowed_origins = frozenset(x.strip() for x in allowed.split(",") if x.strip())
        self.disable_event_backup = os.environ.get("S2_DISABLE_EVENT_BACKUP", "false").casefold() == "true"
        if self.disable_event_backup and self.environment != "QA":
            raise RuntimeError("Event backup may only be disabled in QA")
        if self.identity_mode == "QA_SIGNED":
            if self.environment != "QA" or "qa" not in str(self.root).casefold():
                raise RuntimeError("QA Identity Adapter is forbidden outside an isolated QA root")
            self.identity_provider = QaSignedIdentityProvider(os.environ.get("S2_QA_IDENTITY_SECRET", ""))
        elif self.identity_mode == "LOCAL_OWNER":
            if os.environ.get("S2_CLOUDFLARE_ENABLED", "false").casefold() == "true":
                raise RuntimeError("Local OWNER identity cannot run when Cloudflare mode is enabled")
            self.identity_provider = LocalOwnerIdentityProvider(self.owner_sub, self.owner_email)
        elif self.identity_mode == "CLOUDFLARE":
            self.identity_provider = CloudflareAccessIdentityProvider()
        else:
            raise RuntimeError("Unknown S2_IDENTITY_MODE")

    def initialize(self, require_accounts: bool = True) -> None:
        self.registry.initialize()
        self.resolver.users_root.mkdir(parents=True, exist_ok=True)
        if self.shared_db_read_only:
            if not self.shared_db.is_file():
                raise RuntimeError("Configured read-only shared strategy store is unavailable")
        else:
            self.shared_db.parent.mkdir(parents=True, exist_ok=True)
        self.backups_root.mkdir(parents=True, exist_ok=True)
        self.exports_root.mkdir(parents=True, exist_ok=True)
        self.registry.validate_integrity(self.resolver, require_accounts=require_accounts)

    def account_context(self, identity: IdentityContext, selected_account_id: str | None = None) -> AccountContext:
        row = self.registry.get_by_identity(identity)
        account = self.registry.active_account(row["user_id"], selected_account_id) if selected_account_id else self.registry.current_account(row["user_id"])
        if account is None:
            raise MultiUserSecurityError("OBJECT_NOT_FOUND", 404)
        db = self.resolver.resolve_relative(row["user_id"], account["db_relative_path"])
        return AccountContext(row["user_id"], row["role"], row["display_name"], db, identity, account["account_id"])

    def csrf_token(self, user_id: str) -> str:
        return hmac.new(self.csrf_secret, f"csrf:{user_id}".encode("utf-8"), hashlib.sha256).hexdigest()

    def validate_write_request(self, request: Request, context: AccountContext) -> None:
        origin = request.headers.get("origin")
        if origin not in self.allowed_origins:
            raise MultiUserSecurityError("CSRF_ORIGIN_REJECTED", 403)
        supplied = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(supplied, self.csrf_token(context.internal_user_id)):
            raise MultiUserSecurityError("CSRF_TOKEN_REJECTED", 403)


_runtime: MultiUserRuntime | None = None


def get_runtime() -> MultiUserRuntime:
    global _runtime
    if _runtime is None:
        _runtime = MultiUserRuntime()
    return _runtime


def reset_runtime_for_tests() -> None:
    global _runtime
    _runtime = None


def current_account_context() -> AccountContext:
    context = _account_context.get()
    if context is None:
        raise MultiUserSecurityError("ACCOUNT_CONTEXT_MISSING", 503)
    return context


@contextlib.contextmanager
def bind_account_context(context: AccountContext) -> Iterator[AccountContext]:
    token = _account_context.set(context)
    try:
        yield context
    finally:
        _account_context.reset(token)


def account_connection(context: AccountContext | None = None) -> sqlite3.Connection:
    context = context or current_account_context()
    con = sqlite3.connect(context.account_db, timeout=15, factory=ClosingConnection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=15000")
    return con


def shared_connection(read_only: bool = False) -> sqlite3.Connection:
    runtime = get_runtime()
    path = runtime.shared_db
    if runtime.shared_db_read_only and not read_only:
        raise MultiUserSecurityError("SHARED_STORE_READ_ONLY", 403)
    if read_only:
        con = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10, factory=ClosingConnection
        )
    else:
        con = sqlite3.connect(path, timeout=15, factory=ClosingConnection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    return con


def ensure_no_tenant_parameters(request: Request) -> None:
    found = FORBIDDEN_TENANT_FIELDS.intersection(request.query_params.keys())
    if found:
        raise MultiUserSecurityError("TENANT_PARAMETER_FORBIDDEN", 400)


def http_error(exc: MultiUserSecurityError) -> HTTPException:
    safe_messages = {
        "USER_NOT_INVITED": "你的账户尚未被邀请使用本系统。",
        "USER_DISABLED": "该账户当前已停用。",
        "ACCOUNT_UNAVAILABLE": "账户暂时不可用。",
        "MIGRATION_REQUIRED": "账户需要维护升级，当前已阻止写入。",
        "OBJECT_NOT_FOUND": "未找到该记录。",
        "TENANT_PARAMETER_FORBIDDEN": "请求不得指定账户身份。",
        "CSRF_ORIGIN_REJECTED": "请求来源验证失败。",
        "CSRF_TOKEN_REJECTED": "请求安全令牌无效。",
        "LATE_ENTRY_WINDOW_EXCEEDED": "该操作距今超过30天。当前版本不会自动重算历史收益，请使用归档并重新开始。",
        "INVALID_OCCURRED_AT": "真实发生时间不能晚于当前时间。",
        "FUND_NOT_ALLOWED": "所选基金不在当前允许的基金列表中。",
        "CORRECTION_NOT_SUPPORTED_FOR_FINALIZED_EVENT": "该记录已完成确认或结算，当前版本不能安全修改其历史会计影响。",
        "CORRECTION_NOT_LATEST_REVISION": "只能修正当前最新的未确认订单版本。",
        "OPENING_SNAPSHOT_EVIDENCE_REQUIRED": "请填写各资产余额的事实依据说明。",
        "RESTART_CONFIRMATION_REQUIRED": "请输入“重新开始”后才能归档当前账户。",
    }
    return HTTPException(exc.status_code, safe_messages.get(exc.code, "请求无法完成。"))
