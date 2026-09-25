"""运行零售库存与承诺协调服务的离线端到端验收。

场景：同一模拟门店的多组订单共用一套库存。高优先级承诺在打烊后到达，
按所在地营业日结转；分配方案按已发布规则排序，过期批次被跳过；确认时
核对库存版本，过期方案整体冲突且不留部分扣减；取消只释放未履约数量；
主管锁定与调优先级均记录理由。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import ConflictError
from .retail import RetailService
from .storage import Database


def run() -> dict[str, object]:
    """执行完整零售协同流程并返回关键结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "retail_acceptance.sqlite3")
        service = RetailService(database, FixedClock(datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)))
        service.register_organization(request_id="rq-org", actor_id="bootstrap",
                                      organization_id="org-retail", name="零售赛训机构")
        service.register_actor(request_id="rq-admin", actor_id="bootstrap", new_actor_id="admin-1",
                               display_name="系统管理员", role="admin", organization_id="org-retail")
        service.register_actor(request_id="rq-operator", actor_id="admin-1", new_actor_id="op-1",
                               display_name="门店操作员", role="operator", organization_id="org-retail")
        service.register_actor(request_id="rq-supervisor", actor_id="admin-1", new_actor_id="sup-1",
                               display_name="值班主管", role="supervisor", organization_id="org-retail")
        service.register_site(request_id="rq-site", actor_id="op-1", site_id="store-1",
                              organization_id="org-retail", name="一号模拟门店",
                              timezone_name="Asia/Shanghai")
        service.set_schedule(request_id="rq-schedule", actor_id="op-1", site_id="store-1",
                             open_time="09:00", close_time="21:00", closed_weekdays=[5, 6])
        service.publish_rules(request_id="rq-rules", actor_id="admin-1", site_id="store-1",
                              rules={"candidate_order": ["locked_desc", "priority_desc",
                                                         "effective_at_asc", "promise_id_asc"],
                                     "batch_order": ["expires_on_asc", "batch_id_asc"],
                                     "location_order": ["shelf", "backroom"],
                                     "allow_partial": True,
                                     "min_remaining_days_default": 0})
        service.receive_batch(request_id="rq-b1", actor_id="op-1", site_id="store-1",
                              batch_id="b1", sku="SKU-001", shelf_qty=5, expires_on="2026-09-26")
        service.receive_batch(request_id="rq-b2", actor_id="op-1", site_id="store-1",
                              batch_id="b2", sku="SKU-001", shelf_qty=10, backroom_qty=10,
                              expires_on="2026-10-05")
        service.receive_batch(request_id="rq-b3", actor_id="op-1", site_id="store-1",
                              batch_id="b3", sku="SKU-001", shelf_qty=4, expires_on="2026-09-24")
        service.create_promise(request_id="rq-p-low", actor_id="op-1", site_id="store-1",
                               promise_id="p-low", sku="SKU-001", qty=6, priority=10,
                               requested_at="2026-09-25T02:00:00Z")
        high = service.create_promise(request_id="rq-p-high", actor_id="op-1", site_id="store-1",
                                      promise_id="p-high", sku="SKU-001", qty=6, priority=90,
                                      requested_at="2026-09-25T13:30:00Z")
        service.create_promise(request_id="rq-p-win", actor_id="op-1", site_id="store-1",
                               promise_id="p-win", sku="SKU-001", qty=8, priority=50,
                               min_remaining_days=5, requested_at="2026-09-25T03:00:00Z")
        service.create_promise(request_id="rq-p-late", actor_id="op-1", site_id="store-1",
                               promise_id="p-late", sku="SKU-001", qty=30, priority=5,
                               requested_at="2026-09-25T04:00:00Z")

        plan_one = service.generate_plan(request_id="rq-plan-1", actor_id="op-1",
                                         site_id="store-1", sku="SKU-001")
        detail_one = service.get_plan(plan_one.resource_id)["detail"]
        order = [item["promise_id"] for item in detail_one["candidates"]]
        lines = {line["promise_id"]: line for line in detail_one["lines"]}
        service.confirm_plan(request_id="rq-confirm-1", actor_id="op-1",
                             plan_id=plan_one.resource_id)

        # 方案生成后库存版本被新写入改变：旧方案确认必须整体冲突且不留部分扣减。
        service.receive_batch(request_id="rq-b4", actor_id="op-1", site_id="store-1",
                              batch_id="b4", sku="SKU-001", backroom_qty=30,
                              expires_on="2026-10-10")
        stale_plan = service.generate_plan(request_id="rq-plan-2", actor_id="op-1",
                                           site_id="store-1", sku="SKU-001")
        stale_lines = {line["promise_id"]: line
                       for line in service.get_plan(stale_plan.resource_id)["detail"]["lines"]}
        service.create_promise(request_id="rq-p-extra", actor_id="op-1", site_id="store-1",
                               promise_id="p-extra", sku="SKU-001", qty=1, priority=1,
                               requested_at="2026-09-25T05:00:00Z")
        stale_conflict = False
        try:
            service.confirm_plan(request_id="rq-confirm-2", actor_id="op-1",
                                 plan_id=stale_plan.resource_id)
        except ConflictError:
            stale_conflict = True
        stock_after_conflict = service.get_stock("store-1", "SKU-001")["items"][0]

        service.fulfill_promise(request_id="rq-fulfill-1", actor_id="op-1",
                                promise_id="p-high", qty=2)
        service.cancel_promise(request_id="rq-cancel-1", actor_id="op-1",
                               promise_id="p-high", reason="顾客取消剩余部分")
        service.set_lock(request_id="rq-lock-1", actor_id="sup-1",
                         promise_id="p-low", locked=True, reason="紧急顾客承诺，优先保障")
        service.adjust_priority(request_id="rq-prio-1", actor_id="sup-1",
                                promise_id="p-late", priority=80, reason="主管调整：大客户订单")

        plan_three = service.generate_plan(request_id="rq-plan-3", actor_id="op-1",
                                           site_id="store-1", sku="SKU-001")
        service.confirm_plan(request_id="rq-confirm-3", actor_id="op-1",
                             plan_id=plan_three.resource_id)
        task = service.create_replenishment(request_id="rq-rep-1", actor_id="op-1",
                                            site_id="store-1", sku="SKU-001", qty=4)
        service.complete_replenishment(request_id="rq-rep-done", actor_id="op-1",
                                       task_id=task.resource_id)

        final_stock = service.get_stock("store-1", "SKU-001")["items"][0]
        batches = {item["batch_id"]: item for item in final_stock["batches"]}
        explained = service.explain_promise("p-high")
        promise_high = service.get_promise("p-high")
        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "candidate_order": order,
            "carried_forward": {
                "effective_at": service.get_promise("p-high")["effective_at"],
                "business_date": service.get_promise("p-high")["business_date"],
            },
            "plan_one": {
                "p-high": lines["p-high"]["allocated_qty"],
                "p-win": lines["p-win"]["allocated_qty"],
                "p-low": lines["p-low"]["allocated_qty"],
                "p-late_allocated": lines["p-late"]["allocated_qty"],
                "p-late_shortfall": lines["p-late"]["shortfall"],
                "expired_skipped": any("expired_batch_skipped:b3" in line["reasons"]
                                       for line in detail_one["lines"]),
                "window_filtered": any("window_too_short:b1" in line["reasons"]
                                       for line in detail_one["lines"]),
            },
            "stale_confirm_conflict": stale_conflict,
            "stale_plan_allocated": stale_lines["p-late"]["allocated_qty"],
            "no_partial_deduction": stock_after_conflict["backroom_qty"] == 30,
            "p_high_final": {
                "fulfilled": promise_high["fulfilled_qty"],
                "cancelled": promise_high["cancelled_qty"],
                "status": promise_high["status"],
            },
            "final_batches": {key: {"shelf": item["shelf_qty"], "backroom": item["backroom_qty"],
                                    "status": item["status"]}
                              for key, item in batches.items()},
            "explain": {
                "plans": len(explained["plans"]),
                "allocations": len(explained["allocations"]),
                "audit_events": len(explained["audit_events"]),
                "outcome": explained["plans"][0]["outcome"] if explained["plans"] else None,
            },
            "stock_version": final_stock["version"],
            "audit_events": event_count,
            "audit_valid": valid,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["stale_confirm_conflict"] and result["no_partial_deduction"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
