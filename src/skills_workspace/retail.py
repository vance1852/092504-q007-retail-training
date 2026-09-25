"""零售赛训库存与承诺协调服务。

在基础协作服务之上管理商品批次、货架与后仓数量、保质窗口、顾客承诺与
补货任务，并按已发布的服务规则为候选承诺生成可解释的分配方案。确认方案时
核对库存版本并在单个事务内一次性落账；失败整体回滚，不留下部分扣减。

关键约定：

- 库存版本：每个 ``(场所, SKU)`` 一行计数。任何改变库存数量或候选承诺集合
  的写命令（收货、承诺创建/取消/履约、锁定、调优先级、补货完成、方案确认）
  都会递增版本。方案记录生成时读到的版本，确认时必须一致，否则整体冲突。
- 营业日结转：承诺到达时若门店处于关闭时段（或休息 weekday），生效时间
  结转到所在地下一营业时段开始，营业日取生效时间的当地日期。
- 保质窗口：批次在 ``expires_on`` 当天及之前可分配；承诺可声明
  ``min_remaining_days``，剩余保质天数不足的批次对该承诺不可分配。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService

DEFAULT_RULES: dict[str, Any] = {
    "candidate_order": ["locked_desc", "priority_desc", "effective_at_asc", "promise_id_asc"],
    "batch_order": ["expires_on_asc", "batch_id_asc"],
    "location_order": ["shelf", "backroom"],
    "allow_partial": True,
    "min_remaining_days_default": 0,
}
CANDIDATE_ORDER_KEYS = frozenset({"locked_desc", "priority_desc", "effective_at_asc", "promise_id_asc"})
BATCH_ORDER_KEYS = frozenset({"expires_on_asc", "batch_id_asc"})
LOCATIONS = ("shelf", "backroom")
PROMISE_TERMINAL = frozenset({"cancelled", "fulfilled", "closed"})


def _fmt(moment: datetime) -> str:
    """把带时区时间规范为秒级 UTC 文本，保证字典序即时间序。"""

    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class RetailService(DomainService):
    """零售赛训库存与承诺协调的领域服务。"""

    # ---------- 基础校验与读取助手 ----------

    def _int(self, value: Any, field: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数")
        if not minimum <= value <= maximum:
            raise ValidationError(f"{field} 超出允许范围 [{minimum}, {maximum}]")
        return value

    def _date(self, value: Any, field: str) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期")
        try:
            parsed = date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是有效的 YYYY-MM-DD 日期") from exc
        return parsed.isoformat()

    def _moment(self, value: Any, field: str) -> datetime:
        if not isinstance(value, str):
            raise ValidationError(f"{field} 必须是 ISO 8601 时间")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 时间格式无效") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc)

    def _site_row(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _check_site_access(self, actor, site) -> None:
        if actor.role != "admin" and actor.organization_id != site["organization_id"]:
            raise PermissionDenied("不能操作其他组织的场所")

    def _site_tz(self, site) -> ZoneInfo:
        try:
            return ZoneInfo(site["timezone_name"])
        except ZoneInfoNotFoundError as exc:
            raise ValidationError("场所时区无效") from exc

    def _promise_row(self, connection, promise_id: str):
        row = connection.execute("SELECT * FROM promises WHERE promise_id=?", (promise_id,)).fetchone()
        if row is None:
            raise NotFoundError("承诺不存在")
        return row

    def _task_row(self, connection, task_id: str):
        row = connection.execute("SELECT * FROM replenishment_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("补货任务不存在")
        return row

    # ---------- 库存版本 ----------

    def _version(self, connection, site_id: str, sku: str) -> int:
        row = connection.execute(
            "SELECT version FROM stock_versions WHERE site_id=? AND sku=?", (site_id, sku)
        ).fetchone()
        return row["version"] if row else 0

    def _bump(self, connection, site_id: str, sku: str) -> int:
        new_version = self._version(connection, site_id, sku) + 1
        connection.execute(
            "INSERT INTO stock_versions(site_id,sku,version) VALUES(?,?,?) "
            "ON CONFLICT(site_id,sku) DO UPDATE SET version=excluded.version",
            (site_id, sku, new_version),
        )
        return new_version

    # ---------- 营业日程与结转 ----------

    def _schedule(self, connection, site_id: str) -> dict[str, Any] | None:
        row = connection.execute("SELECT * FROM store_schedules WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            return None
        return {
            "open_time": row["open_time"],
            "close_time": row["close_time"],
            "closed_weekdays": json.loads(row["closed_weekdays_json"]),
        }

    def _effective_time(self, site, schedule: dict[str, Any] | None,
                        arrival: datetime) -> tuple[datetime, str]:
        """把到达时间按所在地营业日程结转，返回 (生效时间 UTC, 营业日)。"""

        tz = self._site_tz(site)
        local = arrival.astimezone(tz).replace(microsecond=0)
        if schedule is None:
            return arrival.replace(microsecond=0), local.date().isoformat()
        open_time = time.fromisoformat(schedule["open_time"])
        close_time = time.fromisoformat(schedule["close_time"])
        closed_weekdays = set(schedule["closed_weekdays"])
        cursor = local
        for _ in range(400):
            if cursor.weekday() in closed_weekdays:
                cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0)
                continue
            open_dt = cursor.replace(hour=open_time.hour, minute=open_time.minute, second=0)
            close_dt = cursor.replace(hour=close_time.hour, minute=close_time.minute, second=0)
            if cursor < open_dt:
                cursor = open_dt
                break
            if cursor < close_dt:
                break
            cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0)
        else:
            raise ValidationError("营业日程无法结转到达时间")
        return cursor.astimezone(timezone.utc), cursor.date().isoformat()

    def set_schedule(self, *, request_id: str, actor_id: str, site_id: str,
                     open_time: str, close_time: str,
                     closed_weekdays: list[int] | None = None) -> WriteReceipt:
        site_id = self._identifier(site_id, "site_id")
        for value, field in ((open_time, "open_time"), (close_time, "close_time")):
            if not isinstance(value, str):
                raise ValidationError(f"{field} 必须是 HH:MM 文本")
            try:
                time.fromisoformat(value.strip())
            except ValueError as exc:
                raise ValidationError(f"{field} 必须是 HH:MM 格式") from exc
        open_time = open_time.strip()
        close_time = close_time.strip()
        if not open_time < close_time:
            raise ValidationError("open_time 必须早于 close_time（不支持跨零点营业）")
        closed = closed_weekdays or []
        if not isinstance(closed, list) or any(
                isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in closed):
            raise ValidationError("closed_weekdays 必须是 0-6 的整数列表")
        closed = sorted(set(closed))
        payload = {"actor_id": actor_id, "site_id": site_id, "open_time": open_time,
                   "close_time": close_time, "closed_weekdays": closed}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_access(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO store_schedules(site_id,open_time,close_time,closed_weekdays_json,updated_by,updated_at) "
                    "VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(site_id) DO UPDATE SET open_time=excluded.open_time, "
                    "close_time=excluded.close_time, closed_weekdays_json=excluded.closed_weekdays_json, "
                    "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (site_id, open_time, close_time, canonical_json(closed), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="retail.schedule.set",
                             resource_type="site", resource_id=site_id,
                             detail={"open_time": open_time, "close_time": close_time,
                                     "closed_weekdays": closed},
                             occurred_at=self._now())
                return "site", site_id, {"site_id": site_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="retail.set_schedule", payload=payload, create=create)

    # ---------- 服务规则 ----------

    def _normalize_rules(self, rules: Any) -> dict[str, Any]:
        if not isinstance(rules, dict) or not rules:
            raise ValidationError("rules 必须是非空对象")
        unknown = sorted(set(rules) - set(DEFAULT_RULES))
        if unknown:
            raise ValidationError(f"rules 包含未知字段: {', '.join(unknown)}")
        normalized = dict(DEFAULT_RULES)
        if "candidate_order" in rules:
            value = rules["candidate_order"]
            if (not isinstance(value, list) or not value
                    or any(key not in CANDIDATE_ORDER_KEYS for key in value)):
                raise ValidationError("candidate_order 含有未知排序键")
            normalized["candidate_order"] = list(dict.fromkeys(value))
        if "batch_order" in rules:
            value = rules["batch_order"]
            if (not isinstance(value, list) or not value
                    or any(key not in BATCH_ORDER_KEYS for key in value)):
                raise ValidationError("batch_order 含有未知排序键")
            normalized["batch_order"] = list(dict.fromkeys(value))
        if "location_order" in rules:
            value = rules["location_order"]
            if (not isinstance(value, list) or not value
                    or any(key not in LOCATIONS for key in value)):
                raise ValidationError("location_order 只能包含 shelf/backroom")
            ordered = list(dict.fromkeys(value))
            normalized["location_order"] = ordered + [loc for loc in LOCATIONS if loc not in ordered]
        if "allow_partial" in rules:
            if not isinstance(rules["allow_partial"], bool):
                raise ValidationError("allow_partial 必须是布尔值")
            normalized["allow_partial"] = rules["allow_partial"]
        if "min_remaining_days_default" in rules:
            normalized["min_remaining_days_default"] = self._int(
                rules["min_remaining_days_default"], "min_remaining_days_default", 0, 3650)
        return normalized

    def publish_rules(self, *, request_id: str, actor_id: str, site_id: str,
                      rules: dict[str, Any]) -> WriteReceipt:
        site_id = self._identifier(site_id, "site_id")
        normalized = self._normalize_rules(rules)
        payload = {"actor_id": actor_id, "site_id": site_id, "rules": normalized}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            site = self._site_row(connection, site_id)
            self._check_site_access(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM rule_sets WHERE site_id=?",
                    (site_id,),
                ).fetchone()
                version = row["version"] + 1
                connection.execute(
                    "UPDATE rule_sets SET status='superseded' WHERE site_id=? AND status='published'",
                    (site_id,),
                )
                rule_set_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO rule_sets(rule_set_id,site_id,version,rules_json,status,published_by,published_at) "
                    "VALUES(?,?,?,?,'published',?,?)",
                    (rule_set_id, site_id, version, canonical_json(normalized), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="retail.rules.published",
                             resource_type="rule_set", resource_id=rule_set_id,
                             detail={"site_id": site_id, "version": version, "rules": normalized},
                             occurred_at=self._now())
                return "rule_set", rule_set_id, {"rule_set_id": rule_set_id, "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="retail.publish_rules", payload=payload, create=create)

    def _published_rules(self, connection, site_id: str) -> tuple[str, int, dict[str, Any]]:
        row = connection.execute(
            "SELECT * FROM rule_sets WHERE site_id=? AND status='published'", (site_id,)
        ).fetchone()
        if row is None:
            return "default", 0, dict(DEFAULT_RULES)
        return row["rule_set_id"], row["version"], json.loads(row["rules_json"])

    def get_rules(self, site_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            self._site_row(connection, site_id)
            rule_set_id, version, rules = self._published_rules(connection, site_id)
        return {"site_id": site_id, "rule_set_id": rule_set_id, "version": version, "rules": rules}

    # ---------- 批次与库存 ----------

    def receive_batch(self, *, request_id: str, actor_id: str, site_id: str, batch_id: str,
                      sku: str, expires_on: str, shelf_qty: int = 0,
                      backroom_qty: int = 0) -> WriteReceipt:
        site_id = self._identifier(site_id, "site_id")
        batch_id = self._identifier(batch_id, "batch_id")
        sku = self._identifier(sku, "sku")
        expires_on = self._date(expires_on, "expires_on")
        shelf_qty = self._int(shelf_qty, "shelf_qty", 0, 1_000_000)
        backroom_qty = self._int(backroom_qty, "backroom_qty", 0, 1_000_000)
        if shelf_qty + backroom_qty <= 0:
            raise ValidationError("货架与后仓数量不能同时为零")
        content = {"site_id": site_id, "sku": sku, "expires_on": expires_on,
                   "shelf_qty": shelf_qty, "backroom_qty": backroom_qty}
        content_hash = digest(content)
        payload = {"actor_id": actor_id, **content}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_access(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM stock_batches WHERE batch_id=?", (batch_id,)).fetchone()
                if existing is not None:
                    if existing["content_hash"] != content_hash:
                        raise ConflictError("批次编号已被不同内容使用")
                    return "stock_batch", batch_id, {"batch_id": batch_id}
                connection.execute(
                    "INSERT INTO stock_batches(batch_id,site_id,sku,shelf_qty,backroom_qty,expires_on,"
                    "content_hash,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (batch_id, site_id, sku, shelf_qty, backroom_qty, expires_on,
                     content_hash, actor_id, self._now()),
                )
                version = self._bump(connection, site_id, sku)
                append_event(connection, actor_id=actor_id, action="retail.batch.received",
                             resource_type="stock_batch", resource_id=batch_id,
                             detail={**content, "stock_version": version},
                             occurred_at=self._now())
                return "stock_batch", batch_id, {"batch_id": batch_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="retail.receive_batch", payload=payload, create=create)

    def get_stock(self, site_id: str, sku: str | None = None) -> dict[str, Any]:
        with self.database.transaction() as connection:
            site = self._site_row(connection, site_id)
            tz = self._site_tz(site)
            today = self.clock.now().astimezone(tz).date()
            parameters: list[Any] = [site_id]
            query = "SELECT * FROM stock_batches WHERE site_id=?"
            if sku:
                query += " AND sku=?"
                parameters.append(sku)
            query += " ORDER BY sku, expires_on, batch_id"
            grouped: dict[str, dict[str, Any]] = {}
            for row in connection.execute(query, parameters):
                entry = grouped.setdefault(row["sku"], {
                    "sku": row["sku"], "version": self._version(connection, site_id, row["sku"]),
                    "shelf_qty": 0, "backroom_qty": 0, "usable_qty": 0, "expired_qty": 0,
                    "batches": [],
                })
                total = row["shelf_qty"] + row["backroom_qty"]
                remaining_days = (date.fromisoformat(row["expires_on"]) - today).days
                status = "empty" if total == 0 else ("expired" if remaining_days < 0 else "usable")
                entry["shelf_qty"] += row["shelf_qty"]
                entry["backroom_qty"] += row["backroom_qty"]
                if status == "usable":
                    entry["usable_qty"] += total
                elif status == "expired":
                    entry["expired_qty"] += total
                entry["batches"].append({
                    "batch_id": row["batch_id"], "expires_on": row["expires_on"],
                    "shelf_qty": row["shelf_qty"], "backroom_qty": row["backroom_qty"],
                    "remaining_days": remaining_days, "status": status,
                })
            return {"site_id": site_id, "business_date": today.isoformat(),
                    "items": list(grouped.values())}

    # ---------- 顾客承诺 ----------

    def create_promise(self, *, request_id: str, actor_id: str, site_id: str, promise_id: str,
                       sku: str, qty: int, priority: int = 0,
                       min_remaining_days: int | None = None,
                       requested_at: str | None = None) -> WriteReceipt:
        site_id = self._identifier(site_id, "site_id")
        promise_id = self._identifier(promise_id, "promise_id")
        sku = self._identifier(sku, "sku")
        qty = self._int(qty, "qty", 1, 1_000_000)
        priority = self._int(priority, "priority", -1_000_000, 1_000_000)
        if min_remaining_days is not None:
            min_remaining_days = self._int(min_remaining_days, "min_remaining_days", 0, 3650)
        if requested_at is not None:
            requested = self._moment(requested_at, "requested_at")
            requested_text = _fmt(requested)
        else:
            requested = self.clock.now()
            requested_text = None
        payload = {"actor_id": actor_id, "site_id": site_id, "promise_id": promise_id, "sku": sku,
                   "qty": qty, "priority": priority, "min_remaining_days": min_remaining_days,
                   "requested_at": requested_text}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_access(actor, site)
            effective, business_date = self._effective_time(site, self._schedule(connection, site_id), requested)
            content = {"site_id": site_id, "sku": sku, "qty": qty, "priority": priority,
                       "min_remaining_days": min_remaining_days, "requested_at": requested_text}
            content_hash = digest(content)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM promises WHERE promise_id=?", (promise_id,)).fetchone()
                if existing is not None:
                    if existing["content_hash"] != content_hash:
                        raise ConflictError("承诺编号已被不同内容使用")
                    return "promise", promise_id, {"promise_id": promise_id}
                connection.execute(
                    "INSERT INTO promises(promise_id,site_id,sku,qty,priority,min_remaining_days,locked,"
                    "status,requested_at,effective_at,business_date,allocated_qty,fulfilled_qty,"
                    "cancelled_qty,content_hash,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,0,'pending',?,?,?,0,0,0,?,?,?)",
                    (promise_id, site_id, sku, qty, priority, min_remaining_days,
                     _fmt(requested), _fmt(effective), business_date, content_hash,
                     actor_id, self._now()),
                )
                version = self._bump(connection, site_id, sku)
                append_event(connection, actor_id=actor_id, action="retail.promise.created",
                             resource_type="promise", resource_id=promise_id,
                             detail={**content, "effective_at": _fmt(effective),
                                     "business_date": business_date,
                                     "carried_forward": _fmt(effective) != _fmt(requested),
                                     "stock_version": version},
                             occurred_at=self._now())
                return "promise", promise_id, {"promise_id": promise_id,
                                               "effective_at": _fmt(effective),
                                               "business_date": business_date}

            return self._idempotent(connection, request_id=request_id,
                                    action="retail.create_promise", payload=payload, create=create)

    def _refresh_promise_status(self, connection, promise_id: str) -> str:
        row = self._promise_row(connection, promise_id)
        qty = row["qty"]
        allocated = row["allocated_qty"]
        fulfilled = row["fulfilled_qty"]
        cancelled = row["cancelled_qty"]
        pending = qty - allocated - fulfilled - cancelled
        if cancelled == qty:
            status = "cancelled"
        elif fulfilled == qty:
            status = "fulfilled"
        elif pending == 0 and allocated == 0:
            status = "closed"
        elif allocated > 0 and pending == 0:
            status = "allocated"
        elif allocated > 0:
            status = "partially_allocated"
        elif fulfilled > 0:
            status = "partially_fulfilled"
        else:
            status = "pending"
        if status != row["status"]:
            connection.execute("UPDATE promises SET status=? WHERE promise_id=?", (status, promise_id))
        return status

    def cancel_promise(self, *, request_id: str, actor_id: str, promise_id: str,
                       qty: int | None = None, reason: str | None = None) -> WriteReceipt:
        promise_id = self._identifier(promise_id, "promise_id")
        if qty is not None:
            qty = self._int(qty, "qty", 1, 1_000_000)
        if reason is not None:
            reason = self._text(reason, "reason")
        payload = {"actor_id": actor_id, "promise_id": promise_id, "qty": qty, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            row = self._promise_row(connection, promise_id)
            site = self._site_row(connection, row["site_id"])
            self._check_site_access(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                fresh = self._promise_row(connection, promise_id)
                if fresh["status"] in PROMISE_TERMINAL:
                    raise ConflictError("承诺已终结，无法再取消")
                pending = fresh["qty"] - fresh["allocated_qty"] - fresh["fulfilled_qty"] - fresh["cancelled_qty"]
                unfulfilled = pending + fresh["allocated_qty"]
                cancel_qty = qty if qty is not None else unfulfilled
                if cancel_qty > unfulfilled:
                    raise ValidationError("可取消数量不足：已履约部分不能取消")
                # 先冲销未分配部分，再释放已分配库存，已履约数量不受影响。
                release_qty = max(0, cancel_qty - pending)
                released: list[dict[str, Any]] = []
                if release_qty:
                    left = release_qty
                    allocations = connection.execute(
                        "SELECT * FROM allocations WHERE promise_id=? AND qty > released_qty + fulfilled_qty "
                        "ORDER BY created_at, allocation_id",
                        (promise_id,),
                    ).fetchall()
                    for allocation in allocations:
                        if left == 0:
                            break
                        active = allocation["qty"] - allocation["released_qty"] - allocation["fulfilled_qty"]
                        take = min(left, active)
                        column = "shelf_qty" if allocation["location"] == "shelf" else "backroom_qty"
                        connection.execute(
                            f"UPDATE stock_batches SET {column}={column}+? WHERE batch_id=?",
                            (take, allocation["batch_id"]),
                        )
                        connection.execute(
                            "UPDATE allocations SET released_qty=released_qty+? WHERE allocation_id=?",
                            (take, allocation["allocation_id"]),
                        )
                        released.append({"batch_id": allocation["batch_id"],
                                         "location": allocation["location"], "qty": take})
                        left -= take
                    if left:
                        raise ConflictError("已分配数量与台账不一致，取消失败")
                connection.execute(
                    "UPDATE promises SET cancelled_qty=cancelled_qty+?, allocated_qty=allocated_qty-? "
                    "WHERE promise_id=?",
                    (cancel_qty, release_qty, promise_id),
                )
                status = self._refresh_promise_status(connection, promise_id)
                version = self._bump(connection, fresh["site_id"], fresh["sku"])
                append_event(connection, actor_id=actor_id, action="retail.promise.cancelled",
                             resource_type="promise", resource_id=promise_id,
                             detail={"cancel_qty": cancel_qty, "released_qty": release_qty,
                                     "released": released, "reason": reason, "status": status,
                                     "stock_version": version},
                             occurred_at=self._now())
                return "promise", promise_id, {"promise_id": promise_id, "cancelled": cancel_qty,
                                               "released": release_qty}

            return self._idempotent(connection, request_id=request_id,
                                    action="retail.cancel_promise", payload=payload, create=create)

    def fulfill_promise(self, *, request_id: str, actor_id: str, promise_id: str,
                        qty: int) -> WriteReceipt:
        promise_id = self._identifier(promise_id, "promise_id")
        qty = self._int(qty, "qty", 1, 1_000_000)
        payload = {"actor_id": actor_id, "promise_id": promise_id, "qty": qty}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            row = self._promise_row(connection, promise_id)
            site = self._site_row(connection, row["site_id"])
            self._check_site_access(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                fresh = self._promise_row(connection, promise_id)
                if qty > fresh["allocated_qty"]:
                    raise ValidationError("可履约数量不足：只能履约已分配且未取消的数量")
                left = qty
                allocations = connection.execute(
                    "SELECT * FROM allocations WHERE promise_id=? AND qty > released_qty + fulfilled_qty "
                    "ORDER BY created_at, allocation_id",
                    (promise_id,),
                ).fetchall()
                for allocation in allocations:
                    if left == 0:
                        break
                    active = allocation["qty"] - allocation["released_qty"] - allocation["fulfilled_qty"]
                    take = min(left, active)
                    connection.execute(
                        "UPDATE allocations SET fulfilled_qty=fulfilled_qty+? WHERE allocation_id=?",
                        (take, allocation["allocation_id"]),
                    )
                    left -= take
                if left:
                    raise ConflictError("已分配数量与台账不一致，履约失败")
                connection.execute(
                    "UPDATE promises SET fulfilled_qty=fulfilled_qty+?, allocated_qty=allocated_qty-? "
                    "WHERE promise_id=?",
                    (qty, qty, promise_id),
                )
                status = self._refresh_promise_status(connection, promise_id)
                version = self._bump(connection, fresh["site_id"], fresh["sku"])
                append_event(connection, actor_id=actor_id, action="retail.promise.fulfilled",
                             resource_type="promise", resource_id=promise_id,
                             detail={"qty": qty, "status": status, "stock_version": version},
                             occurred_at=self._now())
                return "promise", promise_id, {"promise_id": promise_id, "fulfilled": qty}

            return self._idempotent(connection, request_id=request_id,
                                    action="retail.fulfill_promise", payload=payload, create=create)

    # ---------- 主管干预 ----------

    def set_lock(self, *, request_id: str, actor_id: str, promise_id: str, locked: bool,
                 reason: str) -> WriteReceipt:
        promise_id = self._identifier(promise_id, "promise_id")
        if not isinstance(locked, bool):
            raise ValidationError("locked 必须是布尔值")
        reason = self._text(reason, "reason")
        payload = {"actor_id": actor_id, "promise_id": promise_id, "locked": locked, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "supervisor")
            row = self._promise_row(connection, promise_id)
            site = self._site_row(connection, row["site_id"])
            self._check_site_access(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute("UPDATE promises SET locked=? WHERE promise_id=?",
                                   (1 if locked else 0, promise_id))
                version = self._bump(connection, row["site_id"], row["sku"])
                append_event(connection, actor_id=actor_id,
                             action="retail.promise.locked" if locked else "retail.promise.unlocked",
                             resource_type="promise", resource_id=promise_id,
                             detail={"locked": locked, "reason": reason, "stock_version": version},
                             occurred_at=self._now())
                return "promise", promise_id, {"promise_id": promise_id, "locked": locked}

            return self._idempotent(connection, request_id=request_id,
                                    action="retail.set_lock", payload=payload, create=create)

    def adjust_priority(self, *, request_id: str, actor_id: str, promise_id: str,
                        priority: int, reason: str) -> WriteReceipt:
        promise_id = self._identifier(promise_id, "promise_id")
        priority = self._int(priority, "priority", -1_000_000, 1_000_000)
        reason = self._text(reason, "reason")
        payload = {"actor_id": actor_id, "promise_id": promise_id, "priority": priority, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "supervisor")
            row = self._promise_row(connection, promise_id)
            site = self._site_row(connection, row["site_id"])
            self._check_site_access(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                old_priority = row["priority"]
                connection.execute("UPDATE promises SET priority=? WHERE promise_id=?",
                                   (priority, promise_id))
                version = self._bump(connection, row["site_id"], row["sku"])
                append_event(connection, actor_id=actor_id, action="retail.promise.priority_adjusted",
                             resource_type="promise", resource_id=promise_id,
                             detail={"old_priority": old_priority, "new_priority": priority,
                                     "reason": reason, "stock_version": version},
                             occurred_at=self._now())
                return "promise", promise_id, {"promise_id": promise_id, "priority": priority}

            return self._idempotent(connection, request_id=request_id,
                                    action="retail.adjust_priority", payload=payload, create=create)

    # ---------- 分配方案 ----------

    def _candidate_key(self, row, order: list[str]) -> tuple:
        parts: list[Any] = []
        for key in order:
            if key == "locked_desc":
                parts.append(0 if row["locked"] else 1)
            elif key == "priority_desc":
                parts.append(-row["priority"])
            elif key == "effective_at_asc":
                parts.append(row["effective_at"])
            elif key == "promise_id_asc":
                parts.append(row["promise_id"])
        parts.append(row["promise_id"])
        return tuple(parts)

    def _build_plan(self, connection, site_id: str, sku: str,
                    rules: dict[str, Any], business_date: str) -> dict[str, Any]:
        today = date.fromisoformat(business_date)
        batch_rows = connection.execute(
            "SELECT * FROM stock_batches WHERE site_id=? AND sku=? ORDER BY batch_id",
            (site_id, sku),
        ).fetchall()
        batches_view = []
        usable = []
        expired_ids = []
        for batch in batch_rows:
            total = batch["shelf_qty"] + batch["backroom_qty"]
            remaining_days = (date.fromisoformat(batch["expires_on"]) - today).days
            status = "empty" if total == 0 else ("expired" if remaining_days < 0 else "usable")
            batches_view.append({"batch_id": batch["batch_id"], "expires_on": batch["expires_on"],
                                 "shelf_qty": batch["shelf_qty"], "backroom_qty": batch["backroom_qty"],
                                 "remaining_days": remaining_days, "status": status})
            if status == "usable":
                usable.append(batch)
            elif status == "expired":
                expired_ids.append(batch["batch_id"])
        batch_order = rules["batch_order"]

        def batch_key(batch) -> tuple:
            parts: list[Any] = []
            for key in batch_order:
                if key == "expires_on_asc":
                    parts.append(batch["expires_on"])
                elif key == "batch_id_asc":
                    parts.append(batch["batch_id"])
            parts.append(batch["batch_id"])
            return tuple(parts)

        usable.sort(key=batch_key)
        remaining = {batch["batch_id"]: {"shelf": batch["shelf_qty"], "backroom": batch["backroom_qty"]}
                     for batch in usable}
        promise_rows = connection.execute(
            "SELECT * FROM promises WHERE site_id=? AND sku=?", (site_id, sku)
        ).fetchall()
        candidates = []
        for row in promise_rows:
            pending = row["qty"] - row["allocated_qty"] - row["fulfilled_qty"] - row["cancelled_qty"]
            if pending > 0:
                candidates.append((row, pending))
        candidates.sort(key=lambda item: self._candidate_key(item[0], rules["candidate_order"]))
        location_order = rules["location_order"]
        lines = []
        candidates_view = []
        for rank, (row, pending) in enumerate(candidates, start=1):
            candidates_view.append({"promise_id": row["promise_id"], "rank": rank,
                                    "locked": bool(row["locked"]), "priority": row["priority"],
                                    "effective_at": row["effective_at"], "pending_qty": pending})
            min_days = (row["min_remaining_days"]
                        if row["min_remaining_days"] is not None
                        else rules["min_remaining_days_default"])
            reasons: list[str] = []
            if row["locked"]:
                reasons.append("locked_first")
            eligible = []
            for batch in usable:
                if (date.fromisoformat(batch["expires_on"]) - today).days >= min_days:
                    eligible.append(batch)
                else:
                    reasons.append(f"window_too_short:{batch['batch_id']}")
            need = pending
            allocations = []
            total_available = sum(remaining[batch["batch_id"]][location]
                                  for batch in eligible for location in LOCATIONS)
            if not rules["allow_partial"] and total_available < need:
                reasons.append("partial_not_allowed")
                reasons.append("insufficient_usable_stock")
            else:
                for batch in eligible:
                    if need == 0:
                        break
                    for location in location_order:
                        if need == 0:
                            break
                        available = remaining[batch["batch_id"]][location]
                        if available <= 0:
                            continue
                        take = min(need, available)
                        remaining[batch["batch_id"]][location] -= take
                        need -= take
                        allocations.append({
                            "batch_id": batch["batch_id"], "location": location, "qty": take,
                            "reasons": [f"batch_order:{batch_order[0]}",
                                        f"location_order:{location}"],
                        })
            if need > 0 and "insufficient_usable_stock" not in reasons:
                reasons.append("insufficient_usable_stock")
            if expired_ids:
                reasons.append("expired_batch_skipped:" + ",".join(expired_ids))
            lines.append({"promise_id": row["promise_id"], "rank": rank,
                          "requested_qty": pending, "allocated_qty": pending - need,
                          "shortfall": need, "allocations": allocations, "reasons": reasons})
        return {"batches": batches_view, "candidates": candidates_view, "lines": lines}

    def generate_plan(self, *, request_id: str, actor_id: str, site_id: str,
                      sku: str) -> WriteReceipt:
        site_id = self._identifier(site_id, "site_id")
        sku = self._identifier(sku, "sku")
        payload = {"actor_id": actor_id, "site_id": site_id, "sku": sku}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_access(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                tz = self._site_tz(site)
                business_date = self.clock.now().astimezone(tz).date().isoformat()
                rule_set_id, rule_version, rules = self._published_rules(connection, site_id)
                stock_version = self._version(connection, site_id, sku)
                detail = self._build_plan(connection, site_id, sku, rules, business_date)
                detail.update({"site_id": site_id, "sku": sku, "business_date": business_date,
                               "stock_version": stock_version, "rule_set_id": rule_set_id,
                               "rule_version": rule_version, "rules": rules,
                               "generated_at": self._now()})
                plan_id = uuid.uuid4().hex
                connection.execute(
                    "UPDATE allocation_plans SET status='superseded' "
                    "WHERE site_id=? AND sku=? AND status='draft'",
                    (site_id, sku),
                )
                connection.execute(
                    "INSERT INTO allocation_plans(plan_id,site_id,sku,rule_set_id,rule_version,"
                    "stock_version,business_date,status,detail_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'draft',?,?,?)",
                    (plan_id, site_id, sku, rule_set_id, rule_version, stock_version,
                     business_date, canonical_json(detail), actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="retail.plan.generated",
                             resource_type="allocation_plan", resource_id=plan_id,
                             detail={"site_id": site_id, "sku": sku, "stock_version": stock_version,
                                     "rule_set_id": rule_set_id, "rule_version": rule_version,
                                     "candidates": len(detail["candidates"]),
                                     "allocated_total": sum(line["allocated_qty"]
                                                            for line in detail["lines"])},
                             occurred_at=self._now())
                return "allocation_plan", plan_id, {"plan_id": plan_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="retail.generate_plan", payload=payload, create=create)

    def confirm_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> WriteReceipt:
        plan_id = self._identifier(plan_id, "plan_id")
        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            plan = connection.execute(
                "SELECT * FROM allocation_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("分配计划不存在")
            site = self._site_row(connection, plan["site_id"])
            self._check_site_access(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                if plan["status"] != "draft":
                    raise ConflictError("分配计划已确认或已失效")
                tz = self._site_tz(site)
                business_date = self.clock.now().astimezone(tz).date().isoformat()
                if plan["business_date"] != business_date:
                    raise ConflictError("分配计划已跨营业日，请重新生成")
                current_version = self._version(connection, plan["site_id"], plan["sku"])
                if current_version != plan["stock_version"]:
                    raise ConflictError("库存版本已变化，请重新生成分配方案")
                detail = json.loads(plan["detail_json"])
                total = 0
                per_promise: dict[str, int] = {}
                for line in detail["lines"]:
                    if line["allocated_qty"] <= 0:
                        continue
                    per_promise[line["promise_id"]] = per_promise.get(line["promise_id"], 0) + line["allocated_qty"]
                    for allocation in line["allocations"]:
                        batch = connection.execute(
                            "SELECT * FROM stock_batches WHERE batch_id=?",
                            (allocation["batch_id"],),
                        ).fetchone()
                        if batch is None:
                            raise ConflictError("方案引用的批次已不存在，请重新生成")
                        column = "shelf_qty" if allocation["location"] == "shelf" else "backroom_qty"
                        if batch[column] < allocation["qty"]:
                            raise ConflictError("批次库存不足，方案已失效，请重新生成")
                        connection.execute(
                            f"UPDATE stock_batches SET {column}={column}-? WHERE batch_id=?",
                            (allocation["qty"], allocation["batch_id"]),
                        )
                        connection.execute(
                            "INSERT INTO allocations(allocation_id,plan_id,promise_id,batch_id,site_id,"
                            "sku,location,qty,released_qty,fulfilled_qty,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?,0,0,?)",
                            (uuid.uuid4().hex, plan_id, line["promise_id"], allocation["batch_id"],
                             plan["site_id"], plan["sku"], allocation["location"],
                             allocation["qty"], self._now()),
                        )
                        total += allocation["qty"]
                for promise_id, allocated in per_promise.items():
                    connection.execute(
                        "UPDATE promises SET allocated_qty=allocated_qty+? WHERE promise_id=?",
                        (allocated, promise_id),
                    )
                    self._refresh_promise_status(connection, promise_id)
                version = plan["stock_version"]
                if total:
                    version = self._bump(connection, plan["site_id"], plan["sku"])
                connection.execute(
                    "UPDATE allocation_plans SET status='confirmed' WHERE plan_id=?", (plan_id,))
                connection.execute(
                    "UPDATE allocation_plans SET status='superseded' "
                    "WHERE site_id=? AND sku=? AND status='draft'",
                    (plan["site_id"], plan["sku"]),
                )
                append_event(connection, actor_id=actor_id, action="retail.plan.confirmed",
                             resource_type="allocation_plan", resource_id=plan_id,
                             detail={"site_id": plan["site_id"], "sku": plan["sku"],
                                     "allocated_total": total, "promises": len(per_promise),
                                     "stock_version": version},
                             occurred_at=self._now())
                return "allocation_plan", plan_id, {"plan_id": plan_id, "allocated_total": total}

            return self._idempotent(connection, request_id=request_id,
                                    action="retail.confirm_plan", payload=payload, create=create)

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM allocation_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("分配计划不存在")
        return {"plan_id": row["plan_id"], "site_id": row["site_id"], "sku": row["sku"],
                "status": row["status"], "rule_set_id": row["rule_set_id"],
                "rule_version": row["rule_version"], "stock_version": row["stock_version"],
                "business_date": row["business_date"], "created_by": row["created_by"],
                "created_at": row["created_at"], "detail": json.loads(row["detail_json"])}

    def list_plans(self, site_id: str, sku: str | None = None) -> list[dict[str, Any]]:
        parameters: list[Any] = [site_id]
        query = "SELECT * FROM allocation_plans WHERE site_id=?"
        if sku:
            query += " AND sku=?"
            parameters.append(sku)
        query += " ORDER BY created_at, rowid"
        return [{"plan_id": row["plan_id"], "site_id": row["site_id"], "sku": row["sku"],
                 "status": row["status"], "rule_version": row["rule_version"],
                 "stock_version": row["stock_version"], "business_date": row["business_date"],
                 "created_at": row["created_at"]}
                for row in self.database.connection.execute(query, parameters)]

    # ---------- 补货任务 ----------

    def create_replenishment(self, *, request_id: str, actor_id: str, site_id: str,
                             sku: str, qty: int) -> WriteReceipt:
        site_id = self._identifier(site_id, "site_id")
        sku = self._identifier(sku, "sku")
        qty = self._int(qty, "qty", 1, 1_000_000)
        payload = {"actor_id": actor_id, "site_id": site_id, "sku": sku, "qty": qty}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_row(connection, site_id)
            self._check_site_access(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                task_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO replenishment_tasks(task_id,site_id,sku,qty,status,created_by,created_at) "
                    "VALUES(?,?,?,?,'open',?,?)",
                    (task_id, site_id, sku, qty, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="retail.replenishment.created",
                             resource_type="replenishment_task", resource_id=task_id,
                             detail={"site_id": site_id, "sku": sku, "qty": qty},
                             occurred_at=self._now())
                return "replenishment_task", task_id, {"task_id": task_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="retail.create_replenishment", payload=payload, create=create)

    def complete_replenishment(self, *, request_id: str, actor_id: str,
                               task_id: str) -> WriteReceipt:
        task_id = self._identifier(task_id, "task_id")
        payload = {"actor_id": actor_id, "task_id": task_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            task = self._task_row(connection, task_id)
            site = self._site_row(connection, task["site_id"])
            self._check_site_access(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                if task["status"] != "open":
                    raise ConflictError("补货任务已关闭")
                batches = connection.execute(
                    "SELECT * FROM stock_batches WHERE site_id=? AND sku=? AND backroom_qty > 0 "
                    "ORDER BY expires_on, batch_id",
                    (task["site_id"], task["sku"]),
                ).fetchall()
                available = sum(batch["backroom_qty"] for batch in batches)
                if available < task["qty"]:
                    raise ValidationError("后仓可用数量不足，无法完成补货")
                left = task["qty"]
                moves = []
                for batch in batches:
                    if left == 0:
                        break
                    take = min(left, batch["backroom_qty"])
                    connection.execute(
                        "UPDATE stock_batches SET backroom_qty=backroom_qty-?, shelf_qty=shelf_qty+? "
                        "WHERE batch_id=?",
                        (take, take, batch["batch_id"]),
                    )
                    moves.append({"batch_id": batch["batch_id"], "qty": take})
                    left -= take
                connection.execute(
                    "UPDATE replenishment_tasks SET status='completed', closed_at=? WHERE task_id=?",
                    (self._now(), task_id),
                )
                version = self._bump(connection, task["site_id"], task["sku"])
                append_event(connection, actor_id=actor_id, action="retail.replenishment.completed",
                             resource_type="replenishment_task", resource_id=task_id,
                             detail={"site_id": task["site_id"], "sku": task["sku"],
                                     "qty": task["qty"], "moves": moves, "stock_version": version},
                             occurred_at=self._now())
                return "replenishment_task", task_id, {"task_id": task_id, "moved": task["qty"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="retail.complete_replenishment", payload=payload, create=create)

    def cancel_replenishment(self, *, request_id: str, actor_id: str, task_id: str,
                             reason: str | None = None) -> WriteReceipt:
        task_id = self._identifier(task_id, "task_id")
        if reason is not None:
            reason = self._text(reason, "reason")
        payload = {"actor_id": actor_id, "task_id": task_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            task = self._task_row(connection, task_id)
            site = self._site_row(connection, task["site_id"])
            self._check_site_access(actor, site)

            def create() -> tuple[str, str, dict[str, Any]]:
                if task["status"] != "open":
                    raise ConflictError("补货任务已关闭")
                connection.execute(
                    "UPDATE replenishment_tasks SET status='cancelled', reason=?, closed_at=? "
                    "WHERE task_id=?",
                    (reason, self._now(), task_id),
                )
                append_event(connection, actor_id=actor_id, action="retail.replenishment.cancelled",
                             resource_type="replenishment_task", resource_id=task_id,
                             detail={"site_id": task["site_id"], "sku": task["sku"],
                                     "qty": task["qty"], "reason": reason},
                             occurred_at=self._now())
                return "replenishment_task", task_id, {"task_id": task_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="retail.cancel_replenishment", payload=payload, create=create)

    def list_replenishments(self, site_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM replenishment_tasks WHERE site_id=? ORDER BY created_at, rowid",
            (site_id,),
        ).fetchall()
        return [{"task_id": row["task_id"], "site_id": row["site_id"], "sku": row["sku"],
                 "qty": row["qty"], "status": row["status"], "reason": row["reason"],
                 "created_by": row["created_by"], "created_at": row["created_at"],
                 "closed_at": row["closed_at"]} for row in rows]

    # ---------- 查询与审计依据 ----------

    def _promise_json(self, row) -> dict[str, Any]:
        pending = row["qty"] - row["allocated_qty"] - row["fulfilled_qty"] - row["cancelled_qty"]
        return {"promise_id": row["promise_id"], "site_id": row["site_id"], "sku": row["sku"],
                "qty": row["qty"], "priority": row["priority"],
                "min_remaining_days": row["min_remaining_days"], "locked": bool(row["locked"]),
                "status": row["status"], "requested_at": row["requested_at"],
                "effective_at": row["effective_at"], "business_date": row["business_date"],
                "allocated_qty": row["allocated_qty"], "fulfilled_qty": row["fulfilled_qty"],
                "cancelled_qty": row["cancelled_qty"], "pending_qty": pending,
                "created_by": row["created_by"], "created_at": row["created_at"]}

    def get_promise(self, promise_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            return self._promise_json(self._promise_row(connection, promise_id))

    def list_promises(self, site_id: str, status: str | None = None) -> list[dict[str, Any]]:
        parameters: list[Any] = [site_id]
        query = "SELECT * FROM promises WHERE site_id=?"
        if status:
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY effective_at, promise_id"
        with self.database.transaction() as connection:
            return [self._promise_json(row) for row in connection.execute(query, parameters)]

    def explain_promise(self, promise_id: str) -> dict[str, Any]:
        """汇总一笔承诺获得、延迟或失去库存的完整计算与审计依据。"""

        with self.database.transaction() as connection:
            row = self._promise_row(connection, promise_id)
            promise = self._promise_json(row)
            stock_version = self._version(connection, row["site_id"], row["sku"])
            plans = []
            plan_ids = []
            plan_rows = connection.execute(
                "SELECT * FROM allocation_plans WHERE site_id=? AND sku=? ORDER BY created_at, rowid",
                (row["site_id"], row["sku"]),
            ).fetchall()
            for plan in plan_rows:
                detail = json.loads(plan["detail_json"])
                line = next((item for item in detail["lines"]
                             if item["promise_id"] == promise_id), None)
                candidate = next((item for item in detail["candidates"]
                                  if item["promise_id"] == promise_id), None)
                if line is None and candidate is None:
                    continue
                plan_ids.append(plan["plan_id"])
                outcome = None
                if line is not None:
                    if line["allocated_qty"] == line["requested_qty"] and line["requested_qty"] > 0:
                        outcome = "gained"
                    elif line["allocated_qty"] > 0:
                        outcome = "partial"
                    else:
                        outcome = "delayed"
                plans.append({"plan_id": plan["plan_id"], "status": plan["status"],
                              "rule_set_id": plan["rule_set_id"],
                              "rule_version": plan["rule_version"],
                              "stock_version": plan["stock_version"],
                              "business_date": plan["business_date"],
                              "created_at": plan["created_at"], "outcome": outcome,
                              "line": line, "candidate": candidate,
                              "candidates": detail["candidates"], "batches": detail["batches"]})
            allocations = []
            for allocation in connection.execute(
                    "SELECT a.*, b.expires_on FROM allocations a "
                    "JOIN stock_batches b ON b.batch_id=a.batch_id "
                    "WHERE a.promise_id=? ORDER BY a.created_at, a.allocation_id",
                    (promise_id,)):
                active = allocation["qty"] - allocation["released_qty"] - allocation["fulfilled_qty"]
                allocations.append({"allocation_id": allocation["allocation_id"],
                                    "plan_id": allocation["plan_id"],
                                    "batch_id": allocation["batch_id"],
                                    "expires_on": allocation["expires_on"],
                                    "location": allocation["location"], "qty": allocation["qty"],
                                    "released_qty": allocation["released_qty"],
                                    "fulfilled_qty": allocation["fulfilled_qty"],
                                    "active_qty": active})
            events = []
            query = ("SELECT * FROM audit_events WHERE (resource_type='promise' AND resource_id=?)")
            parameters: list[Any] = [promise_id]
            if plan_ids:
                placeholders = ",".join("?" for _ in plan_ids)
                query += (f" OR (resource_type='allocation_plan' AND resource_id IN ({placeholders}))")
                parameters.extend(plan_ids)
            query += " ORDER BY sequence"
            for event in connection.execute(query, parameters):
                events.append({"sequence": event["sequence"], "event_id": event["event_id"],
                               "actor_id": event["actor_id"], "action": event["action"],
                               "resource_type": event["resource_type"],
                               "resource_id": event["resource_id"],
                               "detail": json.loads(event["detail_json"]),
                               "previous_hash": event["previous_hash"],
                               "event_hash": event["event_hash"],
                               "occurred_at": event["occurred_at"]})
            return {"promise": promise, "stock_version": stock_version, "plans": plans,
                    "allocations": allocations, "audit_events": events}
