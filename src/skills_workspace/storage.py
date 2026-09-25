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
CREATE TABLE IF NOT EXISTS store_schedules (
    site_id TEXT PRIMARY KEY REFERENCES sites(site_id),
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL,
    closed_weekdays_json TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rule_sets (
    rule_set_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    version INTEGER NOT NULL,
    rules_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('published', 'superseded')),
    published_by TEXT NOT NULL,
    published_at TEXT NOT NULL,
    UNIQUE(site_id, version)
);
CREATE TABLE IF NOT EXISTS stock_batches (
    batch_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    sku TEXT NOT NULL,
    shelf_qty INTEGER NOT NULL CHECK(shelf_qty >= 0),
    backroom_qty INTEGER NOT NULL CHECK(backroom_qty >= 0),
    expires_on TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_site_sku ON stock_batches(site_id, sku);
CREATE TABLE IF NOT EXISTS stock_versions (
    site_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 0),
    PRIMARY KEY(site_id, sku)
);
CREATE TABLE IF NOT EXISTS promises (
    promise_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    sku TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK(qty > 0),
    priority INTEGER NOT NULL,
    min_remaining_days INTEGER,
    locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0, 1)),
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    business_date TEXT NOT NULL,
    allocated_qty INTEGER NOT NULL DEFAULT 0 CHECK(allocated_qty >= 0),
    fulfilled_qty INTEGER NOT NULL DEFAULT 0 CHECK(fulfilled_qty >= 0),
    cancelled_qty INTEGER NOT NULL DEFAULT 0 CHECK(cancelled_qty >= 0),
    content_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_promises_site_sku ON promises(site_id, sku);
CREATE TABLE IF NOT EXISTS allocation_plans (
    plan_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    sku TEXT NOT NULL,
    rule_set_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    stock_version INTEGER NOT NULL,
    business_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft', 'confirmed', 'superseded')),
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_site_sku ON allocation_plans(site_id, sku);
CREATE TABLE IF NOT EXISTS allocations (
    allocation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES allocation_plans(plan_id),
    promise_id TEXT NOT NULL REFERENCES promises(promise_id),
    batch_id TEXT NOT NULL REFERENCES stock_batches(batch_id),
    site_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    location TEXT NOT NULL CHECK(location IN ('shelf', 'backroom')),
    qty INTEGER NOT NULL CHECK(qty > 0),
    released_qty INTEGER NOT NULL DEFAULT 0 CHECK(released_qty >= 0),
    fulfilled_qty INTEGER NOT NULL DEFAULT 0 CHECK(fulfilled_qty >= 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_allocations_promise ON allocations(promise_id);
CREATE TABLE IF NOT EXISTS replenishment_tasks (
    task_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    sku TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK(qty > 0),
    status TEXT NOT NULL CHECK(status IN ('open', 'completed', 'cancelled')),
    reason TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT
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
