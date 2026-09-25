"""零售赛训库存与承诺协调服务。

在基础服务的权限、幂等、事务和审计能力之上，管理商品批次、货架与
后仓数量、保质窗口、顾客预留和补货任务，按已发布的规则集为候选
请求生成可解释的分配方案；确认方案时核对库存版本并在同一事务内
一次性落账，失败不会留下部分扣减。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .audit import append_event, canonical_json
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .inventory_models import Commitment, ReplenishmentTask, StockPosition
from .inventory_rules import RULE_SET_VERSION, published_rules
from .models import Actor, WriteReceipt
from .service import DomainService

LOCATIONS = ("shelf", "backroom")
LOCATION_LABEL = {"shelf": "货架", "backroom": "后仓"}
COMMITMENT_KINDS = ("reservation", "order")
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
OUTCOME_LABEL = {
    "gained": "获得库存增加",
    "lost": "失去部分库存",
    "delayed": "更多数量被延迟",
    "unchanged": "分配结果不变",
}


def _hm(value: str) -> tuple[int, int]:
    return int(value[:2]), int(value[3:5])


def _business_moment(timezone_name: str, calendar, now: datetime):
    """按所在地营业时间把到达时刻折算为生效营业日。

    返回 (生效当地时间, 营业日, 是否结转)。未设置营业时间时按到达
    时刻所在营业日处理；关闭期间到达的请求结转到下一次开门。
    """

    try:
        zone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValidationError("场所时区无效") from exc
    local = now.astimezone(zone)
    if calendar is None:
        return local, local.date().isoformat(), False
    open_hm, close_hm = _hm(calendar["open_time"]), _hm(calendar["close_time"])
    now_hm = (local.hour, local.minute)
    if open_hm <= now_hm < close_hm:
        return local, local.date().isoformat(), False
    if now_hm < open_hm:
        effective = local.replace(hour=open_hm[0], minute=open_hm[1], second=0, microsecond=0)
    else:
        effective = (local + timedelta(days=1)).replace(
            hour=open_hm[0], minute=open_hm[1], second=0, microsecond=0)
    return effective, effective.date().isoformat(), True


class InventoryService(DomainService):
    """协调库存、承诺、分配方案和补货任务的领域服务。"""

    # ---------- 输入校验 ----------

    def _quantity(self, value: Any, field: str = "quantity", allow_zero: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数")
        if value < 0 or (value == 0 and not allow_zero):
            raise ValidationError(f"{field} 必须大于 0" if not allow_zero else f"{field} 不能为负数")
        return value

    def _priority(self, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 999:
            raise ValidationError("priority 必须是 0 到 999 的整数")
        return value

    def _expires_on(self, value: Any) -> str:
        value = str(value).strip()
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValidationError("expires_on 必须是 YYYY-MM-DD 日期") from exc
        return value

    def _promised_at(self, value: Any) -> str:
        if value is None:
            return self._now()
        text = str(value).strip()
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError("promised_at 必须是 ISO 8601 时间") from exc
        if moment.tzinfo is None:
            raise ValidationError("promised_at 必须包含时区")
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _location(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if value not in LOCATIONS:
            raise ValidationError(f"{field} 必须是 shelf 或 backroom")
        return value

    # ---------- 共享读取 ----------

    def _site_for_write(self, connection, actor: Actor, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        if actor.organization_id != row["organization_id"] and actor.role != "admin":
            raise PermissionDenied("不能写入其他组织的场所")
        return row

    def _calendar(self, connection, site_id: str):
        return connection.execute(
            "SELECT * FROM store_calendars WHERE site_id=?", (site_id,)).fetchone()

    def _version(self, connection, site_id: str, sku: str) -> int:
        row = connection.execute(
            "SELECT version FROM inventory_versions WHERE site_id=? AND sku=?",
            (site_id, sku)).fetchone()
        return row["version"] if row else 0

    def _bump_version(self, connection, site_id: str, sku: str) -> int:
        connection.execute(
            "INSERT INTO inventory_versions(site_id,sku,version) VALUES(?,?,1) "
            "ON CONFLICT(site_id,sku) DO UPDATE SET version=version+1",
            (site_id, sku))
        return self._version(connection, site_id, sku)

    def _commitment_row(self, connection, commitment_id: str):
        row = connection.execute(
            "SELECT * FROM commitments WHERE commitment_id=?", (commitment_id,)).fetchone()
        if row is None:
            raise NotFoundError("承诺不存在")
        return row

    def _commitment_event(self, connection, commitment_id: str,
                          event_type: str, detail: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO commitment_events(event_id,commitment_id,event_type,detail_json,occurred_at) "
            "VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex, commitment_id, event_type, canonical_json(detail), self._now()))

    def _current_availability(self, connection, site_id: str, sku: str) -> dict:
        rows = connection.execute(
            "SELECT p.batch_id, p.location, p.quantity, b.expires_on FROM stock_positions p "
            "JOIN inventory_batches b ON b.batch_id=p.batch_id "
            "WHERE p.site_id=? AND b.sku=? AND p.quantity>0",
            (site_id, sku)).fetchall()
        return {(row["batch_id"], row["location"]): {"quantity": row["quantity"],
                                                     "expires_on": row["expires_on"]}
                for row in rows}

    def _locked_holds(self, connection, site_id: str, sku: str, exclude_commitment: str = "") -> dict:
        rows = connection.execute(
            "SELECT l.batch_id, l.location, l.quantity FROM allocation_lines l "
            "JOIN allocation_plans p ON p.plan_id=l.plan_id "
            "JOIN commitments c ON c.commitment_id=p.commitment_id "
            "WHERE p.site_id=? AND p.sku=? AND p.status='proposed' "
            "AND c.locked=1 AND c.status='proposed' AND c.commitment_id<>?",
            (site_id, sku, exclude_commitment)).fetchall()
        holds: dict[tuple[str, str], int] = {}
        for row in rows:
            key = (row["batch_id"], row["location"])
            holds[key] = holds.get(key, 0) + row["quantity"]
        return holds

    # ---------- 方案生成 ----------

    def _build_plan(self, connection, *, site_id: str, commitment_id: str, sku: str,
                    quantity: int, effective_day: str, trace: list[dict[str, Any]],
                    availability: dict | None = None) -> tuple[str, int, int]:
        """按已发布规则生成方案并写入计划行，返回 (方案编号, 已分配, 延迟)。

        availability 为 None 时读取当前库存并扣除锁定承诺的保留；重算
        场景由调用方传入共享的模拟库存，本方法会就地消耗其中的数量。
        """

        if availability is None:
            availability = self._current_availability(connection, site_id, sku)
            holds = self._locked_holds(connection, site_id, sku, exclude_commitment=commitment_id)
            if holds:
                for key, held in holds.items():
                    info = availability.get(key)
                    if info:
                        info["quantity"] = max(0, info["quantity"] - held)
                trace.append({
                    "rule_id": "R4",
                    "summary": "为主管锁定的承诺保留库存",
                    "detail": {"held": [{"batch_id": batch, "location": location, "quantity": qty}
                                        for (batch, location), qty in sorted(holds.items())]},
                })
        candidates, excluded = [], []
        for (batch_id, location), info in availability.items():
            if info["quantity"] <= 0:
                continue
            if info["expires_on"] < effective_day:
                excluded.append({"batch_id": batch_id, "location": location,
                                 "expires_on": info["expires_on"]})
            else:
                candidates.append((batch_id, location, info["expires_on"], info["quantity"]))
        if excluded:
            trace.append({
                "rule_id": "R1",
                "summary": "排除保质窗口早于承诺营业日的批次",
                "detail": {"effective_day": effective_day, "excluded": excluded},
            })
        candidates.sort(key=lambda item: (0 if item[1] == "shelf" else 1, item[2], item[0]))
        trace.append({
            "rule_id": "R2",
            "summary": "同一位置内合格批次按保质截止日升序（先到期先出）",
            "detail": {"candidates": [{"batch_id": batch, "location": location,
                                       "expires_on": expires, "available": available}
                                      for batch, location, expires, available in candidates]},
        })
        trace.append({
            "rule_id": "R3",
            "summary": "货架库存优先于后仓库存",
            "detail": {"order": [f"{location}:{batch}" for batch, location, _, _ in candidates]},
        })
        remaining, lines = quantity, []
        for batch_id, location, expires_on, available in candidates:
            if remaining == 0:
                break
            take = min(available, remaining)
            if take <= 0:
                continue
            availability[(batch_id, location)]["quantity"] -= take
            remaining -= take
            lines.append({
                "batch_id": batch_id, "location": location, "quantity": take,
                "reason": f"{LOCATION_LABEL[location]}批次 {batch_id}（保质至 {expires_on}），"
                          f"按先到期先出分配 {take} 件",
            })
        allocated = quantity - remaining
        if remaining:
            trace.append({
                "rule_id": "R5",
                "summary": "可用库存不足，剩余数量延迟等待补货",
                "detail": {"requested": quantity, "allocated": allocated, "delayed": remaining},
            })
        plan_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO allocation_plans(plan_id,commitment_id,site_id,sku,rule_set_version,"
            "inventory_version,allocated_quantity,delayed_quantity,status,trace_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, commitment_id, site_id, sku, RULE_SET_VERSION,
             self._version(connection, site_id, sku), allocated, remaining,
             "proposed", canonical_json({"entries": trace}), self._now()))
        for line_no, line in enumerate(lines, start=1):
            connection.execute(
                "INSERT INTO allocation_lines(plan_id,line_no,batch_id,location,quantity,fulfilled,reason) "
                "VALUES(?,?,?,?,?,0,?)",
                (plan_id, line_no, line["batch_id"], line["location"], line["quantity"], line["reason"]))
        return plan_id, allocated, remaining

    def _active_plan(self, connection, commitment_id: str, status: str = "proposed"):
        return connection.execute(
            "SELECT * FROM allocation_plans WHERE commitment_id=? AND status=? "
            "ORDER BY rowid DESC LIMIT 1",
            (commitment_id, status)).fetchone()

    # ---------- 写入动作 ----------

    def set_store_calendar(self, *, request_id: str, actor_id: str, site_id: str,
                           open_time: str, close_time: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id,
                   "open_time": open_time, "close_time": close_time}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_write(connection, actor, site_id)
            open_time, close_time = str(open_time).strip(), str(close_time).strip()
            if not TIME_PATTERN.fullmatch(open_time) or not TIME_PATTERN.fullmatch(close_time):
                raise ValidationError("营业时间必须是 HH:MM 格式")
            if _hm(open_time) >= _hm(close_time):
                raise ValidationError("开门时间必须早于关门时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO store_calendars(site_id,open_time,close_time,updated_by,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(site_id) DO UPDATE SET "
                    "open_time=excluded.open_time, close_time=excluded.close_time, "
                    "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (site_id, open_time, close_time, actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="store_calendar.updated",
                             resource_type="site", resource_id=site_id,
                             detail={"open_time": open_time, "close_time": close_time},
                             occurred_at=self._now())
                return "store_calendar", site_id, {"site_id": site_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_store_calendar", payload=payload, create=create)

    def receive_batch(self, *, request_id: str, actor_id: str, site_id: str, batch_id: str,
                      sku: str, expires_on: str, shelf_quantity: int = 0,
                      backroom_quantity: int = 0) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "batch_id": batch_id, "sku": sku,
                   "expires_on": expires_on, "shelf_quantity": shelf_quantity,
                   "backroom_quantity": backroom_quantity}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_write(connection, actor, site_id)
            batch_id = self._identifier(batch_id, "batch_id")
            sku = self._text(sku, "sku", 80)
            expires_on = self._expires_on(expires_on)
            shelf_quantity = self._quantity(shelf_quantity, "shelf_quantity", allow_zero=True)
            backroom_quantity = self._quantity(backroom_quantity, "backroom_quantity", allow_zero=True)
            if shelf_quantity + backroom_quantity == 0:
                raise ValidationError("入库数量必须大于 0")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO inventory_batches(batch_id,site_id,sku,expires_on,received_at,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (batch_id, site_id, sku, expires_on, self._now(), actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("批次编号已经存在") from exc
                for location, quantity in (("shelf", shelf_quantity), ("backroom", backroom_quantity)):
                    connection.execute(
                        "INSERT INTO stock_positions(site_id,batch_id,location,quantity) VALUES(?,?,?,?)",
                        (site_id, batch_id, location, quantity))
                version = self._bump_version(connection, site_id, sku)
                append_event(connection, actor_id=actor_id, action="inventory.batch_received",
                             resource_type="batch", resource_id=batch_id,
                             detail={"site_id": site_id, "sku": sku, "expires_on": expires_on,
                                     "shelf_quantity": shelf_quantity,
                                     "backroom_quantity": backroom_quantity,
                                     "inventory_version": version},
                             occurred_at=self._now())
                return "batch", batch_id, {"batch_id": batch_id, "inventory_version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="receive_batch", payload=payload, create=create)

    def create_commitment(self, *, request_id: str, actor_id: str, site_id: str, sku: str,
                          quantity: int, kind: str = "order", priority: int = 100,
                          promised_at: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "sku": sku, "quantity": quantity,
                   "kind": kind, "priority": priority, "promised_at": promised_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            site = self._site_for_write(connection, actor, site_id)
            sku = self._text(sku, "sku", 80)
            quantity = self._quantity(quantity)
            if kind not in COMMITMENT_KINDS:
                raise ValidationError("kind 必须是 reservation 或 order")
            priority = self._priority(priority)
            promised = self._promised_at(promised_at)
            calendar = self._calendar(connection, site_id)
            effective, effective_day, rolled = _business_moment(
                site["timezone_name"], calendar, self.clock.now())

            def create() -> tuple[str, str, dict[str, Any]]:
                commitment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO commitments(commitment_id,site_id,sku,kind,quantity,priority,locked,"
                    "promised_at,effective_day,status,created_by,created_at) VALUES(?,?,?,?,?,?,0,?,?,?,?,?)",
                    (commitment_id, site_id, sku, kind, quantity, priority, promised,
                     effective_day, "proposed", actor_id, self._now()))
                if calendar is None:
                    trace = [{"rule_id": "R6", "summary": "未设置营业时间，按到达时间所在营业日处理",
                              "detail": {"effective_day": effective_day}}]
                elif rolled:
                    trace = [{"rule_id": "R6", "summary": "门店关闭，请求结转到下一营业日",
                              "detail": {"arrived_local": self.clock.now().astimezone(
                                             ZoneInfo(site["timezone_name"])).isoformat(),
                                         "open_time": calendar["open_time"],
                                         "close_time": calendar["close_time"],
                                         "effective_day": effective_day}}]
                else:
                    trace = [{"rule_id": "R6", "summary": "门店营业中，按到达营业日处理",
                              "detail": {"effective_day": effective_day}}]
                plan_id, allocated, delayed = self._build_plan(
                    connection, site_id=site_id, commitment_id=commitment_id, sku=sku,
                    quantity=quantity, effective_day=effective_day, trace=trace)
                self._commitment_event(connection, commitment_id, "planned",
                                       {"plan_id": plan_id, "allocated": allocated,
                                        "delayed": delayed, "effective_day": effective_day})
                append_event(connection, actor_id=actor_id, action="commitment.created",
                             resource_type="commitment", resource_id=commitment_id,
                             detail={"site_id": site_id, "sku": sku, "kind": kind,
                                     "quantity": quantity, "priority": priority,
                                     "promised_at": promised, "effective_day": effective_day,
                                     "rolled": rolled, "plan_id": plan_id},
                             occurred_at=self._now())
                return "commitment", commitment_id, {
                    "commitment_id": commitment_id, "plan_id": plan_id,
                    "effective_day": effective_day, "rolled": rolled,
                    "allocated": allocated, "delayed": delayed}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_commitment", payload=payload, create=create)

    def replan_pending(self, *, request_id: str, actor_id: str, site_id: str, sku: str) -> WriteReceipt:
        """按锁定、优先级、承诺时间重算某商品全部待确认承诺的方案。"""

        payload = {"actor_id": actor_id, "site_id": site_id, "sku": sku}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_write(connection, actor, site_id)
            sku = self._text(sku, "sku", 80)

            def create() -> tuple[str, str, dict[str, Any]]:
                rows = connection.execute(
                    "SELECT * FROM commitments WHERE site_id=? AND sku=? AND status='proposed' "
                    "ORDER BY locked DESC, priority DESC, promised_at ASC, created_at ASC, commitment_id ASC",
                    (site_id, sku)).fetchall()
                availability = self._current_availability(connection, site_id, sku)
                order = [{"commitment_id": row["commitment_id"], "locked": bool(row["locked"]),
                          "priority": row["priority"], "promised_at": row["promised_at"]}
                         for row in rows]
                results = []
                for row in rows:
                    commitment_id = row["commitment_id"]
                    old_plan = self._active_plan(connection, commitment_id)
                    if row["locked"]:
                        # 锁定承诺保留原方案，其数量先从模拟库存中扣除
                        for line in connection.execute(
                                "SELECT * FROM allocation_lines WHERE plan_id=?",
                                (old_plan["plan_id"],)).fetchall():
                            info = availability.get((line["batch_id"], line["location"]))
                            if info:
                                info["quantity"] = max(0, info["quantity"] - line["quantity"])
                        results.append({"commitment_id": commitment_id, "kept": True})
                        continue
                    trace = [{"rule_id": "R4",
                              "summary": "按主管锁定、优先级、承诺时间排序处理候选承诺",
                              "detail": {"order": order}}]
                    connection.execute(
                        "UPDATE allocation_plans SET status='superseded' WHERE plan_id=?",
                        (old_plan["plan_id"],))
                    plan_id, allocated, delayed = self._build_plan(
                        connection, site_id=site_id, commitment_id=commitment_id, sku=sku,
                        quantity=row["quantity"], effective_day=row["effective_day"],
                        trace=trace, availability=availability)
                    before, delayed_before = old_plan["allocated_quantity"], old_plan["delayed_quantity"]
                    if allocated > before:
                        outcome = "gained"
                    elif allocated < before:
                        outcome = "lost"
                    elif delayed > delayed_before:
                        outcome = "delayed"
                    else:
                        outcome = "unchanged"
                    self._commitment_event(connection, commitment_id, "replanned", {
                        "old_plan_id": old_plan["plan_id"], "new_plan_id": plan_id,
                        "allocated_before": before, "allocated_after": allocated,
                        "delayed_before": delayed_before, "delayed_after": delayed,
                        "outcome": outcome})
                    results.append({"commitment_id": commitment_id, "kept": False,
                                    "plan_id": plan_id, "outcome": outcome})
                resource_id = f"{site_id}:{sku}"
                append_event(connection, actor_id=actor_id, action="inventory.replanned",
                             resource_type="inventory", resource_id=resource_id,
                             detail={"site_id": site_id, "sku": sku, "processed": len(rows),
                                     "results": results},
                             occurred_at=self._now())
                return "inventory", resource_id, {"site_id": site_id, "sku": sku,
                                                  "processed": len(rows), "results": results}

            return self._idempotent(connection, request_id=request_id,
                                    action="replan_pending", payload=payload, create=create)

    def confirm_plan(self, *, request_id: str, actor_id: str, plan_id: str,
                     expected_version: int) -> WriteReceipt:
        """核对库存版本并在同一事务内一次性扣减全部方案行。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id, "expected_version": expected_version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if isinstance(expected_version, bool) or not isinstance(expected_version, int) \
                    or expected_version < 0:
                raise ValidationError("expected_version 必须是非负整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                plan = connection.execute(
                    "SELECT * FROM allocation_plans WHERE plan_id=?", (plan_id,)).fetchone()
                if plan is None:
                    raise NotFoundError("分配方案不存在")
                self._site_for_write(connection, actor, plan["site_id"])
                commitment = self._commitment_row(connection, plan["commitment_id"])
                if plan["status"] != "proposed" or commitment["status"] != "proposed":
                    raise ConflictError("方案已被取代或承诺已处理，请重新生成方案")
                if expected_version != plan["inventory_version"]:
                    raise ValidationError("期望版本与方案版本不一致，请重新获取方案")
                if self._version(connection, plan["site_id"], plan["sku"]) != plan["inventory_version"]:
                    raise ConflictError("库存版本已变化，方案失效，请重新生成")
                lines = connection.execute(
                    "SELECT * FROM allocation_lines WHERE plan_id=? ORDER BY line_no",
                    (plan_id,)).fetchall()
                if not lines:
                    raise ValidationError("方案没有可落账的分配行")
                for line in lines:
                    cursor = connection.execute(
                        "UPDATE stock_positions SET quantity=quantity-? "
                        "WHERE site_id=? AND batch_id=? AND location=? AND quantity>=?",
                        (line["quantity"], plan["site_id"], line["batch_id"],
                         line["location"], line["quantity"]))
                    if cursor.rowcount != 1:
                        raise ConflictError("可用库存不足，方案已失效")
                connection.execute("UPDATE allocation_plans SET status='confirmed' WHERE plan_id=?",
                                   (plan_id,))
                connection.execute("UPDATE commitments SET status='confirmed' WHERE commitment_id=?",
                                   (commitment["commitment_id"],))
                version = self._bump_version(connection, plan["site_id"], plan["sku"])
                posted = [{"batch_id": line["batch_id"], "location": line["location"],
                           "quantity": line["quantity"]} for line in lines]
                self._commitment_event(connection, commitment["commitment_id"], "confirmed",
                                       {"plan_id": plan_id, "lines": posted,
                                        "inventory_version": version})
                append_event(connection, actor_id=actor_id, action="inventory.plan_confirmed",
                             resource_type="commitment", resource_id=commitment["commitment_id"],
                             detail={"plan_id": plan_id, "site_id": plan["site_id"],
                                     "sku": plan["sku"], "lines": posted,
                                     "inventory_version": version},
                             occurred_at=self._now())
                return "plan", plan_id, {"plan_id": plan_id,
                                         "commitment_id": commitment["commitment_id"],
                                         "inventory_version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_plan", payload=payload, create=create)

    def cancel_commitment(self, *, request_id: str, actor_id: str, commitment_id: str) -> WriteReceipt:
        """取消承诺，只把尚未履约的数量退回库存。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")

            def create() -> tuple[str, str, dict[str, Any]]:
                commitment = self._commitment_row(connection, commitment_id)
                self._site_for_write(connection, actor, commitment["site_id"])
                if commitment["status"] == "cancelled":
                    raise ConflictError("承诺已取消")
                if commitment["status"] == "fulfilled":
                    raise ConflictError("承诺已全部履约，无法取消")
                released, retained = [], []
                if commitment["status"] == "confirmed":
                    plan = self._active_plan(connection, commitment_id, status="confirmed")
                    for line in connection.execute(
                            "SELECT * FROM allocation_lines WHERE plan_id=? ORDER BY line_no",
                            (plan["plan_id"],)).fetchall():
                        item = {"batch_id": line["batch_id"], "location": line["location"],
                                "quantity": line["quantity"]}
                        if line["fulfilled"]:
                            retained.append(item)
                        else:
                            connection.execute(
                                "UPDATE stock_positions SET quantity=quantity+? "
                                "WHERE site_id=? AND batch_id=? AND location=?",
                                (line["quantity"], commitment["site_id"],
                                 line["batch_id"], line["location"]))
                            released.append(item)
                    if released:
                        self._bump_version(connection, commitment["site_id"], commitment["sku"])
                else:
                    plan = self._active_plan(connection, commitment_id)
                    if plan is not None:
                        connection.execute(
                            "UPDATE allocation_plans SET status='superseded' WHERE plan_id=?",
                            (plan["plan_id"],))
                connection.execute("UPDATE commitments SET status='cancelled' WHERE commitment_id=?",
                                   (commitment_id,))
                detail = {"released": released, "retained": retained,
                          "released_total": sum(item["quantity"] for item in released),
                          "retained_total": sum(item["quantity"] for item in retained)}
                self._commitment_event(connection, commitment_id, "cancelled", detail)
                append_event(connection, actor_id=actor_id, action="commitment.cancelled",
                             resource_type="commitment", resource_id=commitment_id,
                             detail=detail, occurred_at=self._now())
                return "commitment", commitment_id, {"commitment_id": commitment_id, **detail}

            return self._idempotent(connection, request_id=request_id,
                                    action="cancel_commitment", payload=payload, create=create)

    def fulfill_commitment(self, *, request_id: str, actor_id: str, commitment_id: str,
                           line_numbers: list[int]) -> WriteReceipt:
        """把已确认承诺的指定分配行标记为履约。"""

        payload = {"actor_id": actor_id, "commitment_id": commitment_id,
                   "line_numbers": line_numbers}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if not isinstance(line_numbers, list) or not line_numbers \
                    or any(isinstance(no, bool) or not isinstance(no, int) for no in line_numbers):
                raise ValidationError("line_numbers 必须是非空整数数组")
            line_numbers = sorted(set(line_numbers))

            def create() -> tuple[str, str, dict[str, Any]]:
                commitment = self._commitment_row(connection, commitment_id)
                self._site_for_write(connection, actor, commitment["site_id"])
                if commitment["status"] != "confirmed":
                    raise ConflictError("承诺未确认或已处理完毕")
                plan = self._active_plan(connection, commitment_id, status="confirmed")
                lines = {row["line_no"]: row for row in connection.execute(
                    "SELECT * FROM allocation_lines WHERE plan_id=?", (plan["plan_id"],)).fetchall()}
                for line_no in line_numbers:
                    if line_no not in lines:
                        raise NotFoundError("分配行不存在")
                    if lines[line_no]["fulfilled"]:
                        raise ConflictError("分配行已履约")
                for line_no in line_numbers:
                    connection.execute(
                        "UPDATE allocation_lines SET fulfilled=1 WHERE plan_id=? AND line_no=?",
                        (plan["plan_id"], line_no))
                complete = all(row["fulfilled"] for row in connection.execute(
                    "SELECT fulfilled FROM allocation_lines WHERE plan_id=?",
                    (plan["plan_id"],)).fetchall())
                if complete:
                    connection.execute(
                        "UPDATE commitments SET status='fulfilled' WHERE commitment_id=?",
                        (commitment_id,))
                detail = {"plan_id": plan["plan_id"], "lines": line_numbers, "complete": complete}
                self._commitment_event(connection, commitment_id, "fulfilled", detail)
                append_event(connection, actor_id=actor_id, action="commitment.fulfilled",
                             resource_type="commitment", resource_id=commitment_id,
                             detail=detail, occurred_at=self._now())
                return "commitment", commitment_id, {"commitment_id": commitment_id, **detail}

            return self._idempotent(connection, request_id=request_id,
                                    action="fulfill_commitment", payload=payload, create=create)

    def _override(self, *, request_id: str, actor_id: str, commitment_id: str, action: str,
                  reason: str, priority: int | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "action": action,
                   "reason": reason, "priority": priority}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "supervisor")
            reason = self._text(reason, "reason")
            if priority is not None:
                priority = self._priority(priority)

            def create() -> tuple[str, str, dict[str, Any]]:
                commitment = self._commitment_row(connection, commitment_id)
                self._site_for_write(connection, actor, commitment["site_id"])
                if commitment["status"] != "proposed":
                    raise ConflictError("仅待确认的承诺可以调整")
                detail: dict[str, Any] = {"reason": reason}
                if action == "lock":
                    if commitment["locked"]:
                        raise ConflictError("承诺已锁定")
                    connection.execute(
                        "UPDATE commitments SET locked=1 WHERE commitment_id=?", (commitment_id,))
                    event_type = "locked"
                elif action == "unlock":
                    if not commitment["locked"]:
                        raise ConflictError("承诺未锁定")
                    connection.execute(
                        "UPDATE commitments SET locked=0 WHERE commitment_id=?", (commitment_id,))
                    event_type = "unlocked"
                else:
                    detail["old_priority"] = commitment["priority"]
                    detail["new_priority"] = priority
                    connection.execute(
                        "UPDATE commitments SET priority=? WHERE commitment_id=?",
                        (priority, commitment_id))
                    event_type = "priority_changed"
                connection.execute(
                    "INSERT INTO supervisor_overrides(override_id,commitment_id,action,reason,"
                    "actor_id,created_at) VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, commitment_id, action, reason, actor_id, self._now()))
                self._commitment_event(connection, commitment_id, event_type, detail)
                append_event(connection, actor_id=actor_id, action=f"commitment.{action}",
                             resource_type="commitment", resource_id=commitment_id,
                             detail=detail, occurred_at=self._now())
                return "commitment", commitment_id, {"commitment_id": commitment_id,
                                                     "action": action}

            return self._idempotent(connection, request_id=request_id,
                                    action=f"override_{action}", payload=payload, create=create)

    def lock_commitment(self, *, request_id: str, actor_id: str,
                        commitment_id: str, reason: str) -> WriteReceipt:
        return self._override(request_id=request_id, actor_id=actor_id,
                              commitment_id=commitment_id, action="lock", reason=reason)

    def unlock_commitment(self, *, request_id: str, actor_id: str,
                          commitment_id: str, reason: str) -> WriteReceipt:
        return self._override(request_id=request_id, actor_id=actor_id,
                              commitment_id=commitment_id, action="unlock", reason=reason)

    def set_commitment_priority(self, *, request_id: str, actor_id: str, commitment_id: str,
                                priority: int, reason: str) -> WriteReceipt:
        return self._override(request_id=request_id, actor_id=actor_id,
                              commitment_id=commitment_id, action="set_priority",
                              reason=reason, priority=priority)

    def create_replenishment_task(self, *, request_id: str, actor_id: str, site_id: str,
                                  sku: str, quantity: int, source_location: str = "backroom",
                                  target_location: str = "shelf") -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "sku": sku, "quantity": quantity,
                   "source_location": source_location, "target_location": target_location}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._site_for_write(connection, actor, site_id)
            sku = self._text(sku, "sku", 80)
            quantity = self._quantity(quantity)
            source_location = self._location(source_location, "source_location")
            target_location = self._location(target_location, "target_location")
            if source_location == target_location:
                raise ValidationError("来源与目标位置不能相同")

            def create() -> tuple[str, str, dict[str, Any]]:
                task_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO replenishment_tasks(task_id,site_id,sku,quantity,source_location,"
                    "target_location,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (task_id, site_id, sku, quantity, source_location, target_location,
                     "open", actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="replenishment.task_created",
                             resource_type="replenishment_task", resource_id=task_id,
                             detail={"site_id": site_id, "sku": sku, "quantity": quantity,
                                     "source_location": source_location,
                                     "target_location": target_location},
                             occurred_at=self._now())
                return "replenishment_task", task_id, {"task_id": task_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_replenishment_task", payload=payload, create=create)

    def execute_replenishment_task(self, *, request_id: str, actor_id: str,
                                   task_id: str) -> WriteReceipt:
        """按先到期先出把数量从来源位置移到目标位置，不足则整体失败。"""

        payload = {"actor_id": actor_id, "task_id": task_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")

            def create() -> tuple[str, str, dict[str, Any]]:
                task = connection.execute(
                    "SELECT * FROM replenishment_tasks WHERE task_id=?", (task_id,)).fetchone()
                if task is None:
                    raise NotFoundError("补货任务不存在")
                site = self._site_for_write(connection, actor, task["site_id"])
                if task["status"] != "open":
                    raise ConflictError("补货任务已处理")
                calendar = self._calendar(connection, task["site_id"])
                _, effective_day, _ = _business_moment(
                    site["timezone_name"], calendar, self.clock.now())
                rows = connection.execute(
                    "SELECT p.batch_id, p.quantity, b.expires_on FROM stock_positions p "
                    "JOIN inventory_batches b ON b.batch_id=p.batch_id "
                    "WHERE p.site_id=? AND b.sku=? AND p.location=? AND p.quantity>0 "
                    "AND b.expires_on>=? ORDER BY b.expires_on, p.batch_id",
                    (task["site_id"], task["sku"], task["source_location"], effective_day)
                ).fetchall()
                if sum(row["quantity"] for row in rows) < task["quantity"]:
                    raise ConflictError("来源位置合格库存不足，任务未执行")
                moves, remaining = [], task["quantity"]
                for row in rows:
                    if remaining == 0:
                        break
                    take = min(row["quantity"], remaining)
                    remaining -= take
                    connection.execute(
                        "UPDATE stock_positions SET quantity=quantity-? "
                        "WHERE site_id=? AND batch_id=? AND location=?",
                        (take, task["site_id"], row["batch_id"], task["source_location"]))
                    connection.execute(
                        "INSERT INTO stock_positions(site_id,batch_id,location,quantity) "
                        "VALUES(?,?,?,?) ON CONFLICT(site_id,batch_id,location) "
                        "DO UPDATE SET quantity=quantity+excluded.quantity",
                        (task["site_id"], row["batch_id"], task["target_location"], take))
                    moves.append({"batch_id": row["batch_id"], "quantity": take})
                version = self._bump_version(connection, task["site_id"], task["sku"])
                connection.execute("UPDATE replenishment_tasks SET status='done' WHERE task_id=?",
                                   (task_id,))
                append_event(connection, actor_id=actor_id, action="replenishment.task_executed",
                             resource_type="replenishment_task", resource_id=task_id,
                             detail={"site_id": task["site_id"], "sku": task["sku"],
                                     "source_location": task["source_location"],
                                     "target_location": task["target_location"],
                                     "moves": moves, "inventory_version": version},
                             occurred_at=self._now())
                return "replenishment_task", task_id, {"task_id": task_id, "moves": moves,
                                                       "inventory_version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="execute_replenishment_task", payload=payload, create=create)

    # ---------- 查询 ----------

    def get_inventory(self, site_id: str, sku: str) -> dict[str, Any]:
        connection = self.database.connection
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        calendar = self._calendar(connection, site_id)
        _, effective_day, _ = _business_moment(site["timezone_name"], calendar, self.clock.now())
        rows = connection.execute(
            "SELECT p.*, b.sku, b.expires_on FROM stock_positions p "
            "JOIN inventory_batches b ON b.batch_id=p.batch_id "
            "WHERE p.site_id=? AND b.sku=? ORDER BY b.expires_on, p.batch_id, p.location",
            (site_id, sku)).fetchall()
        positions = [StockPosition(row["site_id"], row["batch_id"], row["sku"], row["location"],
                                   row["quantity"], row["expires_on"],
                                   row["expires_on"] < effective_day).__dict__
                     for row in rows]
        return {"site_id": site_id, "sku": sku, "effective_day": effective_day,
                "inventory_version": self._version(connection, site_id, sku),
                "positions": positions}

    def list_commitments(self, site_id: str, status: str | None = None) -> list[Commitment]:
        parameters: list[Any] = [site_id]
        query = "SELECT * FROM commitments WHERE site_id=?"
        if status:
            if status not in ("proposed", "confirmed", "fulfilled", "cancelled"):
                raise ValidationError("status 不在允许范围内")
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY rowid"
        return [Commitment(row["commitment_id"], row["site_id"], row["sku"], row["kind"],
                           row["quantity"], row["priority"], bool(row["locked"]),
                           row["promised_at"], row["effective_day"], row["status"],
                           row["created_by"], row["created_at"])
                for row in self.database.connection.execute(query, parameters)]

    def list_replenishment_tasks(self, site_id: str, status: str | None = None) -> list[ReplenishmentTask]:
        parameters: list[Any] = [site_id]
        query = "SELECT * FROM replenishment_tasks WHERE site_id=?"
        if status:
            if status not in ("open", "done", "cancelled"):
                raise ValidationError("status 不在允许范围内")
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY rowid"
        return [ReplenishmentTask(row["task_id"], row["site_id"], row["sku"], row["quantity"],
                                  row["source_location"], row["target_location"], row["status"],
                                  row["created_by"], row["created_at"])
                for row in self.database.connection.execute(query, parameters)]

    def list_allocation_rules(self) -> dict[str, Any]:
        return published_rules()

    def _plan_view(self, connection, plan_row) -> dict[str, Any]:
        lines = connection.execute(
            "SELECT l.*, b.expires_on FROM allocation_lines l "
            "JOIN inventory_batches b ON b.batch_id=l.batch_id "
            "WHERE l.plan_id=? ORDER BY l.line_no", (plan_row["plan_id"],)).fetchall()
        return {"plan_id": plan_row["plan_id"], "commitment_id": plan_row["commitment_id"],
                "rule_set_version": plan_row["rule_set_version"],
                "inventory_version": plan_row["inventory_version"],
                "allocated_quantity": plan_row["allocated_quantity"],
                "delayed_quantity": plan_row["delayed_quantity"],
                "status": plan_row["status"],
                "trace": json.loads(plan_row["trace_json"])["entries"],
                "lines": [{"line_no": line["line_no"], "batch_id": line["batch_id"],
                           "location": line["location"], "quantity": line["quantity"],
                           "expires_on": line["expires_on"], "fulfilled": bool(line["fulfilled"]),
                           "reason": line["reason"]} for line in lines],
                "created_at": plan_row["created_at"]}

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        connection = self.database.connection
        plan = connection.execute(
            "SELECT * FROM allocation_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if plan is None:
            raise NotFoundError("分配方案不存在")
        return self._plan_view(connection, plan)

    def commitment_explanation(self, commitment_id: str) -> dict[str, Any]:
        """展示一笔承诺获得、延迟或失去库存的完整计算与审计依据。"""

        connection = self.database.connection
        row = connection.execute(
            "SELECT * FROM commitments WHERE commitment_id=?", (commitment_id,)).fetchone()
        if row is None:
            raise NotFoundError("承诺不存在")
        commitment = {"commitment_id": row["commitment_id"], "site_id": row["site_id"],
                      "sku": row["sku"], "kind": row["kind"], "quantity": row["quantity"],
                      "priority": row["priority"], "locked": bool(row["locked"]),
                      "promised_at": row["promised_at"], "effective_day": row["effective_day"],
                      "status": row["status"], "created_by": row["created_by"],
                      "created_at": row["created_at"]}
        plans = [self._plan_view(connection, plan) for plan in connection.execute(
            "SELECT * FROM allocation_plans WHERE commitment_id=? ORDER BY rowid",
            (commitment_id,)).fetchall()]
        events = [{"event_type": event["event_type"],
                   "detail": json.loads(event["detail_json"]),
                   "occurred_at": event["occurred_at"]}
                  for event in connection.execute(
                      "SELECT * FROM commitment_events WHERE commitment_id=? "
                      "ORDER BY rowid", (commitment_id,)).fetchall()]
        overrides = [{"action": item["action"], "reason": item["reason"],
                      "actor_id": item["actor_id"], "created_at": item["created_at"]}
                     for item in connection.execute(
                         "SELECT * FROM supervisor_overrides WHERE commitment_id=? "
                         "ORDER BY rowid", (commitment_id,)).fetchall()]
        audits = [{"sequence": event["sequence"], "action": event["action"],
                   "actor_id": event["actor_id"], "detail": json.loads(event["detail_json"]),
                   "event_hash": event["event_hash"], "occurred_at": event["occurred_at"]}
                  for event in connection.execute(
                      "SELECT * FROM audit_events WHERE resource_type='commitment' "
                      "AND resource_id=? ORDER BY sequence", (commitment_id,)).fetchall()]
        return {"commitment": commitment, "plans": plans, "events": events,
                "overrides": overrides, "audit": audits,
                "rules": published_rules(),
                "current_inventory_version": self._version(
                    connection, row["site_id"], row["sku"]),
                "narrative": self._narrative(events)}

    @staticmethod
    def _narrative(events: list[dict[str, Any]]) -> list[str]:
        """把承诺事件流水翻译成可读的获得、延迟、失去库存说明。"""

        lines = []
        for event in events:
            event_type, detail = event["event_type"], event["detail"]
            if event_type == "planned":
                lines.append(f"生成方案 {detail['plan_id']}：获得 {detail['allocated']} 件，"
                             f"延迟 {detail['delayed']} 件，按营业日 {detail['effective_day']} 计算")
            elif event_type == "replanned":
                outcome = OUTCOME_LABEL.get(detail["outcome"], detail["outcome"])
                lines.append(f"重新计算：获得 {detail['allocated_after']} 件"
                             f"（此前 {detail['allocated_before']} 件），延迟 "
                             f"{detail['delayed_after']} 件（此前 {detail['delayed_before']} 件），{outcome}")
            elif event_type == "confirmed":
                lines.append(f"方案 {detail['plan_id']} 按库存版本 "
                             f"{detail['inventory_version']} 一次性落账，库存正式获得")
            elif event_type == "cancelled":
                lines.append(f"取消：释放未履约 {detail['released_total']} 件，"
                             f"已履约 {detail['retained_total']} 件保持扣减")
            elif event_type == "fulfilled":
                state = "全部履约完成" if detail["complete"] else "部分履约"
                lines.append(f"分配行 {detail['lines']} {state}")
            elif event_type == "locked":
                lines.append(f"主管锁定紧急承诺，理由：{detail['reason']}")
            elif event_type == "unlocked":
                lines.append(f"主管解除锁定，理由：{detail['reason']}")
            elif event_type == "priority_changed":
                lines.append(f"主管将优先级 {detail['old_priority']} 调整为 "
                             f"{detail['new_priority']}，理由：{detail['reason']}")
        return lines
