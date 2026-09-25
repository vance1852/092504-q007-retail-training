"""定义库存与承诺协调服务在模块边界使用的读模型。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StockPosition:
    """表示某批次在货架或后仓的可用数量。"""

    site_id: str
    batch_id: str
    sku: str
    location: str
    quantity: int
    expires_on: str
    expired: bool


@dataclass(frozen=True)
class Commitment:
    """表示一笔顾客预留或订单承诺。"""

    commitment_id: str
    site_id: str
    sku: str
    kind: str
    quantity: int
    priority: int
    locked: bool
    promised_at: str
    effective_day: str
    status: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class ReplenishmentTask:
    """表示一条货架与后仓之间的补货任务。"""

    task_id: str
    site_id: str
    sku: str
    quantity: int
    source_location: str
    target_location: str
    status: str
    created_by: str
    created_at: str
