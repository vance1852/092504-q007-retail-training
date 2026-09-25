import unittest
from datetime import datetime, timezone

from skills_workspace.clock import FixedClock
from skills_workspace.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from skills_workspace.retail import RetailService
from skills_workspace.storage import Database

NOW = datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc)  # 上海当地 10:00，营业中


class RetailTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = RetailService(self.database, FixedClock(NOW))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="零售赛训机构")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                    display_name="操作员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="supervisor", actor_id="a1", new_actor_id="sup1",
                                    display_name="主管", role="supervisor", organization_id="o1")
        self.service.register_actor(request_id="auditor", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="模拟门店",
                                   timezone_name="Asia/Shanghai")
        self.service.set_schedule(request_id="schedule", actor_id="op1", site_id="s1",
                                  open_time="09:00", close_time="21:00", closed_weekdays=[5, 6])
        self.service.publish_rules(request_id="rules", actor_id="a1", site_id="s1",
                                   rules={"candidate_order": ["locked_desc", "priority_desc",
                                                              "effective_at_asc", "promise_id_asc"],
                                          "batch_order": ["expires_on_asc", "batch_id_asc"],
                                          "location_order": ["shelf", "backroom"],
                                          "allow_partial": True,
                                          "min_remaining_days_default": 0})

    def tearDown(self):
        self.database.close()

    # ---------- 测试助手 ----------

    def batch(self, batch_id, expires_on, shelf=0, backroom=0, request_id=None):
        return self.service.receive_batch(
            request_id=request_id or f"batch-{batch_id}", actor_id="op1", site_id="s1",
            batch_id=batch_id, sku="SKU-1", shelf_qty=shelf, backroom_qty=backroom,
            expires_on=expires_on)

    def promise(self, promise_id, qty, priority=0, request_id=None, **kwargs):
        return self.service.create_promise(
            request_id=request_id or f"promise-{promise_id}", actor_id="op1", site_id="s1",
            promise_id=promise_id, sku="SKU-1", qty=qty, priority=priority, **kwargs)

    def plan(self, request_id="plan-1"):
        receipt = self.service.generate_plan(request_id=request_id, actor_id="op1",
                                             site_id="s1", sku="SKU-1")
        return self.service.get_plan(receipt.resource_id)

    def line_for(self, plan, promise_id):
        return next(line for line in plan["detail"]["lines"] if line["promise_id"] == promise_id)

    def stock(self):
        return self.service.get_stock("s1", "SKU-1")["items"][0]

    # ---------- 批次与库存 ----------

    def test_receive_batch_and_stock_summary(self):
        self.batch("b1", "2026-09-30", shelf=5, backroom=7)
        self.batch("b2", "2026-09-24", shelf=4)  # 已过期
        stock = self.stock()
        self.assertEqual(9, stock["shelf_qty"])
        self.assertEqual(7, stock["backroom_qty"])
        self.assertEqual(12, stock["usable_qty"])
        self.assertEqual(4, stock["expired_qty"])
        self.assertEqual(2, stock["version"])
        statuses = {b["batch_id"]: b["status"] for b in stock["batches"]}
        self.assertEqual({"b1": "usable", "b2": "expired"}, statuses)

    def test_batch_business_key_reuse(self):
        first = self.batch("b1", "2026-09-30", shelf=5)
        replay = self.service.receive_batch(request_id="other-request", actor_id="op1",
                                            site_id="s1", batch_id="b1", sku="SKU-1",
                                            shelf_qty=5, expires_on="2026-09-30")
        self.assertEqual(first.resource_id, replay.resource_id)
        with self.assertRaises(ConflictError):
            self.service.receive_batch(request_id="another", actor_id="op1", site_id="s1",
                                       batch_id="b1", sku="SKU-1", shelf_qty=6,
                                       expires_on="2026-09-30")

    def test_expired_batch_is_never_allocated(self):
        self.batch("b1", "2026-09-24", shelf=10)  # 营业日前一天已过期
        self.batch("b2", "2026-09-26", shelf=3)
        self.promise("p1", 10)
        plan = self.plan()
        line = self.line_for(plan, "p1")
        self.assertEqual(3, line["allocated_qty"])
        self.assertEqual(7, line["shortfall"])
        self.assertEqual([{"batch_id": "b2", "location": "shelf", "qty": 3,
                           "reasons": ["batch_order:expires_on_asc", "location_order:shelf"]}],
                         line["allocations"])
        self.assertIn("expired_batch_skipped:b1", line["reasons"])
        self.assertIn("insufficient_usable_stock", line["reasons"])

    def test_min_remaining_days_window_filter(self):
        self.batch("b1", "2026-09-26", shelf=10)  # 剩余 1 天
        self.batch("b2", "2026-10-10", shelf=10)
        self.promise("p1", 5, min_remaining_days=5)
        plan = self.plan()
        line = self.line_for(plan, "p1")
        self.assertEqual(5, line["allocated_qty"])
        self.assertEqual("b2", line["allocations"][0]["batch_id"])
        self.assertIn("window_too_short:b1", line["reasons"])

    # ---------- 分配排序 ----------

    def test_priority_beats_arrival_order(self):
        self.batch("b1", "2026-10-01", shelf=8)
        self.promise("p-early", 6, priority=10, requested_at="2026-09-25T01:00:00Z")
        self.promise("p-late", 6, priority=90, requested_at="2026-09-25T03:00:00Z")
        plan = self.plan()
        order = [c["promise_id"] for c in plan["detail"]["candidates"]]
        self.assertEqual(["p-late", "p-early"], order)
        self.assertEqual(6, self.line_for(plan, "p-late")["allocated_qty"])
        self.assertEqual(2, self.line_for(plan, "p-early")["allocated_qty"])
        self.assertEqual(4, self.line_for(plan, "p-early")["shortfall"])

    def test_locked_promise_ranks_first(self):
        self.batch("b1", "2026-10-01", shelf=5)
        self.promise("p1", 5, priority=90)
        self.promise("p2", 5, priority=10)
        self.service.set_lock(request_id="lock-1", actor_id="sup1", promise_id="p2",
                              locked=True, reason="紧急承诺")
        plan = self.plan()
        order = [c["promise_id"] for c in plan["detail"]["candidates"]]
        self.assertEqual(["p2", "p1"], order)
        self.assertIn("locked_first", self.line_for(plan, "p2")["reasons"])

    def test_allow_partial_false_rule(self):
        self.service.publish_rules(request_id="rules-2", actor_id="a1", site_id="s1",
                                   rules={"allow_partial": False})
        self.batch("b1", "2026-10-01", shelf=3)
        self.promise("p1", 5)
        plan = self.plan()
        line = self.line_for(plan, "p1")
        self.assertEqual(0, line["allocated_qty"])
        self.assertIn("partial_not_allowed", line["reasons"])
        self.assertIn("insufficient_usable_stock", line["reasons"])

    # ---------- 营业日结转 ----------

    def test_after_close_carries_to_next_business_day(self):
        # 2026-09-25 21:30 上海（周五打烊后）→ 下周一 09:00
        self.promise("p1", 1, requested_at="2026-09-25T13:30:00Z")
        promise = self.service.get_promise("p1")
        self.assertEqual("2026-09-28T01:00:00Z", promise["effective_at"])
        self.assertEqual("2026-09-28", promise["business_date"])

    def test_before_open_carries_to_same_day_open(self):
        self.promise("p1", 1, requested_at="2026-09-25T00:30:00Z")  # 上海 08:30
        promise = self.service.get_promise("p1")
        self.assertEqual("2026-09-25T01:00:00Z", promise["effective_at"])
        self.assertEqual("2026-09-25", promise["business_date"])

    def test_during_open_hours_no_carryover(self):
        self.promise("p1", 1, requested_at="2026-09-25T02:00:00Z")  # 上海 10:00
        promise = self.service.get_promise("p1")
        self.assertEqual("2026-09-25T02:00:00Z", promise["effective_at"])

    # ---------- 确认与原子性 ----------

    def test_confirm_deducts_atomically_and_bumps_version(self):
        self.batch("b1", "2026-10-01", shelf=5, backroom=10)
        self.promise("p1", 8)
        plan = self.plan()
        version_before = self.stock()["version"]
        receipt = self.service.confirm_plan(request_id="confirm-1", actor_id="op1",
                                            plan_id=plan["plan_id"])
        self.assertFalse(receipt.replayed)
        stock = self.stock()
        self.assertEqual(0, stock["shelf_qty"])
        self.assertEqual(7, stock["backroom_qty"])
        self.assertEqual(version_before + 1, stock["version"])
        promise = self.service.get_promise("p1")
        self.assertEqual(8, promise["allocated_qty"])
        self.assertEqual("allocated", promise["status"])
        self.assertEqual("confirmed", self.service.get_plan(plan["plan_id"])["status"])

    def test_confirm_replay_returns_same_receipt(self):
        self.batch("b1", "2026-10-01", shelf=5)
        self.promise("p1", 2)
        plan = self.plan()
        first = self.service.confirm_plan(request_id="confirm-1", actor_id="op1",
                                          plan_id=plan["plan_id"])
        second = self.service.confirm_plan(request_id="confirm-1", actor_id="op1",
                                           plan_id=plan["plan_id"])
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(3, self.stock()["shelf_qty"])

    def test_confirm_with_stale_version_conflicts_without_partial_deduction(self):
        self.batch("b1", "2026-10-01", shelf=10)
        self.promise("p1", 6)
        plan = self.plan()
        self.promise("p2", 1)  # 改变候选集合 → 库存版本 +1
        with self.assertRaises(ConflictError):
            self.service.confirm_plan(request_id="confirm-stale", actor_id="op1",
                                      plan_id=plan["plan_id"])
        stock = self.stock()
        self.assertEqual(10, stock["shelf_qty"])  # 没有部分扣减
        promise = self.service.get_promise("p1")
        self.assertEqual(0, promise["allocated_qty"])
        self.assertEqual("pending", promise["status"])
        self.assertEqual("draft", self.service.get_plan(plan["plan_id"])["status"])

    def test_confirm_across_business_day_conflicts(self):
        self.batch("b1", "2026-10-01", shelf=5)
        self.promise("p1", 2)
        plan = self.plan()
        self.service.clock = FixedClock(datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc))
        with self.assertRaises(ConflictError):
            self.service.confirm_plan(request_id="confirm-next-day", actor_id="op1",
                                      plan_id=plan["plan_id"])
        self.assertEqual(5, self.stock()["shelf_qty"])

    def test_new_plan_supersedes_previous_draft(self):
        self.batch("b1", "2026-10-01", shelf=5)
        self.promise("p1", 2)
        first = self.plan("plan-a")
        second = self.plan("plan-b")
        self.assertEqual("superseded", self.service.get_plan(first["plan_id"])["status"])
        self.assertEqual("draft", self.service.get_plan(second["plan_id"])["status"])
        with self.assertRaises(ConflictError):
            self.service.confirm_plan(request_id="confirm-old", actor_id="op1",
                                      plan_id=first["plan_id"])

    # ---------- 幂等与冲突 ----------

    def test_promise_replay_and_payload_conflict(self):
        first = self.promise("p1", 3, request_id="req-1")
        replay = self.promise("p1", 3, request_id="req-1")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        with self.assertRaises(ConflictError):
            self.promise("p1", 4, request_id="req-1")  # 相同 request_id 不同载荷
        with self.assertRaises(ConflictError):
            self.promise("p1", 4, request_id="req-2")  # 相同承诺编号不同内容

    def test_promise_without_requested_at_replays_safely(self):
        first = self.promise("p1", 3, request_id="req-1")
        self.service.clock = FixedClock(datetime(2026, 9, 25, 5, 0, tzinfo=timezone.utc))
        replay = self.promise("p1", 3, request_id="req-1")
        self.assertTrue(replay.replayed)

    def test_confirm_request_id_with_changed_payload_conflicts(self):
        self.batch("b1", "2026-10-01", shelf=5)
        self.promise("p1", 2)
        plan = self.plan("plan-a")
        self.service.confirm_plan(request_id="confirm-x", actor_id="op1", plan_id=plan["plan_id"])
        other = self.plan("plan-b")
        with self.assertRaises(ConflictError):
            self.service.confirm_plan(request_id="confirm-x", actor_id="op1",
                                      plan_id=other["plan_id"])

    # ---------- 取消与履约 ----------

    def test_cancel_releases_only_unfulfilled_quantities(self):
        self.batch("b1", "2026-10-01", shelf=10)
        self.promise("p1", 10)
        plan = self.plan()
        self.service.confirm_plan(request_id="confirm-1", actor_id="op1", plan_id=plan["plan_id"])
        self.service.fulfill_promise(request_id="fulfill-1", actor_id="op1",
                                     promise_id="p1", qty=4)
        self.service.cancel_promise(request_id="cancel-1", actor_id="op1", promise_id="p1")
        promise = self.service.get_promise("p1")
        self.assertEqual(4, promise["fulfilled_qty"])
        self.assertEqual(6, promise["cancelled_qty"])
        self.assertEqual(0, promise["allocated_qty"])
        self.assertEqual("closed", promise["status"])
        self.assertEqual(6, self.stock()["shelf_qty"])  # 只回补未履约的 6
        with self.assertRaises(ConflictError):
            self.service.cancel_promise(request_id="cancel-2", actor_id="op1", promise_id="p1")

    def test_cancel_pending_promise_releases_nothing(self):
        self.batch("b1", "2026-10-01", shelf=5)
        self.promise("p1", 3)
        self.service.cancel_promise(request_id="cancel-1", actor_id="op1", promise_id="p1")
        promise = self.service.get_promise("p1")
        self.assertEqual("cancelled", promise["status"])
        self.assertEqual(5, self.stock()["shelf_qty"])

    def test_cancel_rejects_fulfilled_quantities(self):
        self.batch("b1", "2026-10-01", shelf=5)
        self.promise("p1", 5)
        plan = self.plan()
        self.service.confirm_plan(request_id="confirm-1", actor_id="op1", plan_id=plan["plan_id"])
        self.service.fulfill_promise(request_id="fulfill-1", actor_id="op1",
                                     promise_id="p1", qty=5)
        with self.assertRaises(ConflictError):
            self.service.cancel_promise(request_id="cancel-1", actor_id="op1", promise_id="p1")

    def test_fulfill_requires_allocated_quantity(self):
        self.promise("p1", 3)
        with self.assertRaises(ValidationError):
            self.service.fulfill_promise(request_id="fulfill-1", actor_id="op1",
                                         promise_id="p1", qty=1)

    # ---------- 主管干预 ----------

    def test_lock_requires_supervisor_role_and_reason(self):
        self.promise("p1", 2)
        with self.assertRaises(PermissionDenied):
            self.service.set_lock(request_id="lock-1", actor_id="op1", promise_id="p1",
                                  locked=True, reason="操作员无权")
        with self.assertRaises(ValidationError):
            self.service.set_lock(request_id="lock-2", actor_id="sup1", promise_id="p1",
                                  locked=True, reason="  ")
        self.service.set_lock(request_id="lock-3", actor_id="sup1", promise_id="p1",
                              locked=True, reason="紧急顾客承诺")
        self.assertTrue(self.service.get_promise("p1")["locked"])
        events = [e for e in self.service.audit_events()
                  if e["action"] == "retail.promise.locked"]
        self.assertEqual("紧急顾客承诺", events[0]["detail"]["reason"])
        self.assertEqual("sup1", events[0]["actor_id"])

    def test_adjust_priority_records_reason_and_reorders(self):
        self.batch("b1", "2026-10-01", shelf=5)
        self.promise("p1", 5, priority=90)
        self.promise("p2", 5, priority=10)
        with self.assertRaises(PermissionDenied):
            self.service.adjust_priority(request_id="prio-1", actor_id="op1",
                                         promise_id="p2", priority=95, reason="越权")
        self.service.adjust_priority(request_id="prio-2", actor_id="sup1",
                                     promise_id="p2", priority=95, reason="大客户优先")
        plan = self.plan()
        order = [c["promise_id"] for c in plan["detail"]["candidates"]]
        self.assertEqual(["p2", "p1"], order)
        events = [e for e in self.service.audit_events()
                  if e["action"] == "retail.promise.priority_adjusted"]
        self.assertEqual({"old_priority": 10, "new_priority": 95},
                         {k: events[0]["detail"][k] for k in ("old_priority", "new_priority")})
        self.assertEqual("大客户优先", events[0]["detail"]["reason"])

    # ---------- 补货任务 ----------

    def test_replenishment_moves_backroom_to_shelf(self):
        self.batch("b1", "2026-10-01", backroom=10)
        task = self.service.create_replenishment(request_id="rep-1", actor_id="op1",
                                                 site_id="s1", sku="SKU-1", qty=4)
        self.service.complete_replenishment(request_id="rep-1-done", actor_id="op1",
                                            task_id=task.resource_id)
        stock = self.stock()
        self.assertEqual(4, stock["shelf_qty"])
        self.assertEqual(6, stock["backroom_qty"])
        tasks = self.service.list_replenishments("s1")
        self.assertEqual("completed", tasks[0]["status"])

    def test_replenishment_insufficient_backroom_is_atomic(self):
        self.batch("b1", "2026-10-01", backroom=3)
        task = self.service.create_replenishment(request_id="rep-1", actor_id="op1",
                                                 site_id="s1", sku="SKU-1", qty=5)
        with self.assertRaises(ValidationError):
            self.service.complete_replenishment(request_id="rep-1-done", actor_id="op1",
                                                task_id=task.resource_id)
        stock = self.stock()
        self.assertEqual(0, stock["shelf_qty"])
        self.assertEqual(3, stock["backroom_qty"])
        self.assertEqual("open", self.service.list_replenishments("s1")[0]["status"])

    def test_replenishment_cancel_and_double_close_conflict(self):
        self.batch("b1", "2026-10-01", backroom=5)
        task = self.service.create_replenishment(request_id="rep-1", actor_id="op1",
                                                 site_id="s1", sku="SKU-1", qty=2)
        self.service.cancel_replenishment(request_id="rep-1-cancel", actor_id="op1",
                                          task_id=task.resource_id, reason="不再需要")
        self.assertEqual("cancelled", self.service.list_replenishments("s1")[0]["status"])
        with self.assertRaises(ConflictError):
            self.service.complete_replenishment(request_id="rep-1-done", actor_id="op1",
                                                task_id=task.resource_id)

    # ---------- 查询与审计依据 ----------

    def test_explain_promise_shows_calculation_and_audit_basis(self):
        self.batch("b1", "2026-10-01", shelf=8)
        self.promise("p1", 6, priority=10)
        self.promise("p2", 6, priority=90)
        plan = self.plan()
        self.service.confirm_plan(request_id="confirm-1", actor_id="op1", plan_id=plan["plan_id"])
        explained = self.service.explain_promise("p1")
        self.assertEqual(1, len(explained["plans"]))
        entry = explained["plans"][0]
        self.assertEqual("partial", entry["outcome"])
        self.assertEqual(2, entry["line"]["allocated_qty"])
        self.assertEqual(4, entry["line"]["shortfall"])
        self.assertEqual("confirmed", entry["status"])
        ranks = {c["promise_id"]: c["rank"] for c in entry["candidates"]}
        self.assertEqual({"p2": 1, "p1": 2}, ranks)
        self.assertEqual(1, len(explained["allocations"]))
        self.assertEqual("b1", explained["allocations"][0]["batch_id"])
        actions = [e["action"] for e in explained["audit_events"]]
        self.assertIn("retail.promise.created", actions)
        self.assertIn("retail.plan.generated", actions)
        self.assertIn("retail.plan.confirmed", actions)
        self.assertEqual(explained["stock_version"], self.stock()["version"])

    def test_explain_unknown_promise_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.explain_promise("missing")

    def test_explain_delayed_outcome(self):
        self.batch("b1", "2026-10-01", shelf=4)
        self.promise("p1", 6, priority=10)
        self.promise("p2", 6, priority=90)
        plan = self.plan()
        explained = self.service.explain_promise("p1")
        entry = explained["plans"][0]
        self.assertEqual(plan["plan_id"], entry["plan_id"])
        self.assertEqual("delayed", entry["outcome"])
        self.assertEqual(0, entry["line"]["allocated_qty"])
        self.assertIn("insufficient_usable_stock", entry["line"]["reasons"])

    def test_explain_shows_lost_allocation_across_plans(self):
        self.batch("b1", "2026-10-01", shelf=5)
        self.promise("p1", 5, priority=10)
        first = self.plan("plan-a")  # p1 独占库存
        self.assertEqual(5, self.line_for(first, "p1")["allocated_qty"])
        self.promise("p2", 5, priority=90)  # 更高优先级到达，旧方案失效
        second = self.plan("plan-b")
        self.service.confirm_plan(request_id="confirm-b", actor_id="op1",
                                  plan_id=second["plan_id"])
        explained = self.service.explain_promise("p1")
        self.assertEqual(2, len(explained["plans"]))
        old, new = explained["plans"]
        self.assertEqual("superseded", old["status"])
        self.assertEqual(5, old["line"]["allocated_qty"])
        self.assertEqual("confirmed", new["status"])
        self.assertEqual(0, new["line"]["allocated_qty"])
        self.assertEqual("delayed", new["outcome"])
        self.assertEqual([], explained["allocations"])

    def test_audit_chain_remains_valid(self):
        self.batch("b1", "2026-10-01", shelf=5)
        self.promise("p1", 2)
        plan = self.plan()
        self.service.confirm_plan(request_id="confirm-1", actor_id="op1", plan_id=plan["plan_id"])
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)

    def test_auditor_cannot_write(self):
        with self.assertRaises(PermissionDenied):
            self.service.receive_batch(request_id="b1", actor_id="au1", site_id="s1",
                                       batch_id="b1", sku="SKU-1", shelf_qty=1,
                                       expires_on="2026-10-01")


if __name__ == "__main__":
    unittest.main()
