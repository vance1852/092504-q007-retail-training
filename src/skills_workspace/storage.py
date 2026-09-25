"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS store_calendars (
    site_id TEXT PRIMARY KEY REFERENCES sites(site_id),
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL,
    updated_by TEXT NOT NULL REFERENCES actors(actor_id),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_batches (
    batch_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    sku TEXT NOT NULL,
    expires_on TEXT NOT NULL,
    received_at TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_positions (
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    batch_id TEXT NOT NULL REFERENCES inventory_batches(batch_id),
    location TEXT NOT NULL CHECK(location IN ('shelf', 'backroom')),
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    PRIMARY KEY(site_id, batch_id, location)
);
CREATE TABLE IF NOT EXISTS inventory_versions (
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    sku TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 0),
    PRIMARY KEY(site_id, sku)
);
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    sku TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('reservation', 'order')),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 999),
    locked INTEGER NOT NULL CHECK(locked IN (0, 1)),
    promised_at TEXT NOT NULL,
    effective_day TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'confirmed', 'fulfilled', 'cancelled')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocation_plans (
    plan_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    site_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    rule_set_version TEXT NOT NULL,
    inventory_version INTEGER NOT NULL,
    allocated_quantity INTEGER NOT NULL CHECK(allocated_quantity >= 0),
    delayed_quantity INTEGER NOT NULL CHECK(delayed_quantity >= 0),
    status TEXT NOT NULL CHECK(status IN ('proposed', 'confirmed', 'superseded')),
    trace_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocation_lines (
    plan_id TEXT NOT NULL REFERENCES allocation_plans(plan_id),
    line_no INTEGER NOT NULL,
    batch_id TEXT NOT NULL REFERENCES inventory_batches(batch_id),
    location TEXT NOT NULL CHECK(location IN ('shelf', 'backroom')),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    fulfilled INTEGER NOT NULL CHECK(fulfilled IN (0, 1)),
    reason TEXT NOT NULL,
    PRIMARY KEY(plan_id, line_no)
);
CREATE TABLE IF NOT EXISTS replenishment_tasks (
    task_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    sku TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    source_location TEXT NOT NULL CHECK(source_location IN ('shelf', 'backroom')),
    target_location TEXT NOT NULL CHECK(target_location IN ('shelf', 'backroom')),
    status TEXT NOT NULL CHECK(status IN ('open', 'done', 'cancelled')),
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commitment_events (
    event_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    event_type TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supervisor_overrides (
    override_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    action TEXT NOT NULL CHECK(action IN ('lock', 'unlock', 'set_priority')),
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
