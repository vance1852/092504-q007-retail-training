"""运行库存与承诺协调服务的离线端到端验收。

场景覆盖：批次入库与保质窗口、货架/后仓分配、优先级重算挤占、
版本核对落账、取消只释放未履约数量、主管锁定、幂等重放与冲突、
承诺解释查询和审计链校验。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import ConflictError
from .inventory_service import InventoryService
from .storage import Database


def _active_plan_id(service: InventoryService, commitment_id: str) -> str:
    explanation = service.commitment_explanation(commitment_id)
    for plan in explanation["plans"]:
        if plan["status"] == "proposed":
            return plan["plan_id"]
    raise AssertionError("缺少待确认方案")


def run() -> dict[str, object]:
    """执行一条完整库存承诺链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "inventory_acceptance.sqlite3")
        service = InventoryService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="零售赛训机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="门店操作员", role="operator", organization_id="org-001")
        service.register_actor(request_id="req-supervisor", actor_id="admin-001", new_actor_id="supervisor-001",
                               display_name="值班主管", role="supervisor", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="store-001",
                              organization_id="org-001", name="一号模拟门店", timezone_name="Asia/Shanghai")
        service.set_store_calendar(request_id="req-calendar", actor_id="operator-001",
                                   site_id="store-001", open_time="08:00", close_time="22:00")
        service.receive_batch(request_id="req-batch-exp", actor_id="operator-001", site_id="store-001",
                              batch_id="B-EXP", sku="MILK", expires_on="2026-09-26", shelf_quantity=5)
        service.receive_batch(request_id="req-batch-late", actor_id="operator-001", site_id="store-001",
                              batch_id="B-LATE", sku="MILK", expires_on="2026-10-10", backroom_quantity=3)
        service.receive_batch(request_id="req-batch-old", actor_id="operator-001", site_id="store-001",
                              batch_id="B-OLD", sku="MILK", expires_on="2026-09-20", shelf_quantity=2)

        low = service.create_commitment(request_id="req-c-low", actor_id="operator-001", site_id="store-001",
                                        sku="MILK", quantity=6, kind="order", priority=10)
        high = service.create_commitment(request_id="req-c-high", actor_id="operator-001", site_id="store-001",
                                         sku="MILK", quantity=4, kind="reservation", priority=900)
        service.replan_pending(request_id="req-replan-1", actor_id="operator-001",
                               site_id="store-001", sku="MILK")
        high_plan = _active_plan_id(service, high.resource_id)
        service.confirm_plan(request_id="req-confirm-high", actor_id="operator-001",
                             plan_id=high_plan, expected_version=3)
        version_conflict = False
        try:
            service.confirm_plan(request_id="req-confirm-low-stale", actor_id="operator-001",
                                 plan_id=_active_plan_id(service, low.resource_id),
                                 expected_version=3)
        except ConflictError:
            version_conflict = True
        service.replan_pending(request_id="req-replan-2", actor_id="operator-001",
                               site_id="store-001", sku="MILK")
        low_plan = _active_plan_id(service, low.resource_id)
        service.confirm_plan(request_id="req-confirm-low", actor_id="operator-001",
                             plan_id=low_plan, expected_version=4)
        service.fulfill_commitment(request_id="req-fulfill-low", actor_id="operator-001",
                                   commitment_id=low.resource_id, line_numbers=[1])
        service.cancel_commitment(request_id="req-cancel-low", actor_id="operator-001",
                                  commitment_id=low.resource_id)
        service.fulfill_commitment(request_id="req-fulfill-high", actor_id="operator-001",
                                   commitment_id=high.resource_id, line_numbers=[1])

        vip = service.create_commitment(request_id="req-c-vip", actor_id="operator-001",
                                        site_id="store-001", sku="MILK", quantity=2,
                                        kind="reservation", priority=50)
        service.lock_commitment(request_id="req-lock-vip", actor_id="supervisor-001",
                                commitment_id=vip.resource_id, reason="紧急顾客承诺，需优先保障")
        service.replan_pending(request_id="req-replan-3", actor_id="operator-001",
                               site_id="store-001", sku="MILK")
        replay = service.create_commitment(request_id="req-c-vip", actor_id="operator-001",
                                           site_id="store-001", sku="MILK", quantity=2,
                                           kind="reservation", priority=50)
        payload_conflict = False
        try:
            service.create_commitment(request_id="req-c-vip", actor_id="operator-001",
                                      site_id="store-001", sku="MILK", quantity=3,
                                      kind="reservation", priority=50)
        except ConflictError:
            payload_conflict = True

        inventory = service.get_inventory("store-001", "MILK")
        explanation = service.commitment_explanation(low.resource_id)
        vip_explanation = service.commitment_explanation(vip.resource_id)
        valid, event_count = service.verify_audit()
        positions = {(item["batch_id"], item["location"]): item["quantity"]
                     for item in inventory["positions"]}
        checks = {
            "version_conflict": version_conflict,
            "payload_conflict": payload_conflict,
            "replayed": replay.replayed,
            "shelf_empty": positions.get(("B-EXP", "shelf")) == 0,
            "expired_untouched": positions.get(("B-OLD", "shelf")) == 2,
            "released_back": positions.get(("B-LATE", "backroom")) == 3,
            "low_delayed": explanation["plans"][-1]["delayed_quantity"] == 2,
            "vip_locked": vip_explanation["commitment"]["locked"],
            "narrative": any("释放未履约" in line for line in explanation["narrative"]),
        }
        result = {"status": "ok" if valid and all(checks.values()) else "failed",
                  "checks": checks, "audit_valid": valid, "audit_events": event_count,
                  "inventory_version": inventory["inventory_version"]}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
