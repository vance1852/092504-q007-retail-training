import unittest
from datetime import datetime, timezone

from skills_workspace.clock import FixedClock
from skills_workspace.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from skills_workspace.inventory_service import InventoryService
from skills_workspace.storage import Database


class InventoryServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = InventoryService(
            self.database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="赛训机构")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                    display_name="操作员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="supervisor", actor_id="a1", new_actor_id="sv1",
                                    display_name="主管", role="supervisor", organization_id="o1")
        self.service.register_actor(request_id="auditor", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="模拟门店", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def _receive(self, batch_id, expires_on, shelf=0, backroom=0, sku="MILK"):
        return self.service.receive_batch(
            request_id=f"recv-{batch_id}", actor_id="op1", site_id="s1", batch_id=batch_id,
            sku=sku, expires_on=expires_on, shelf_quantity=shelf, backroom_quantity=backroom)

    def _commit(self, request_id, quantity, priority=100, sku="MILK", kind="order"):
        return self.service.create_commitment(
            request_id=request_id, actor_id="op1", site_id="s1", sku=sku,
            quantity=quantity, kind=kind, priority=priority)

    def _plan(self, commitment_id):
        explanation = self.service.commitment_explanation(commitment_id)
        proposed = [plan for plan in explanation["plans"] if plan["status"] == "proposed"]
        self.assertEqual(1, len(proposed))
        return proposed[0]

    def _position(self, batch_id, location, sku="MILK"):
        inventory = self.service.get_inventory("s1", sku)
        for position in inventory["positions"]:
            if position["batch_id"] == batch_id and position["location"] == location:
                return position["quantity"]
        return None

    def test_plan_follows_fefo_and_shelf_first(self):
        self._receive("B1", "2026-10-01", shelf=2)
        self._receive("B2", "2026-09-28", shelf=2)
        self._receive("B3", "2026-09-27", backroom=5)
        receipt = self._commit("c1", 3)
        plan = self._plan(receipt.resource_id)
        self.assertEqual([("B2", 2), ("B1", 1)],
                         [(line["batch_id"], line["quantity"]) for line in plan["lines"]])
        self.assertEqual(0, plan["delayed_quantity"])
        rule_ids = [entry["rule_id"] for entry in plan["trace"]]
        self.assertIn("R2", rule_ids)
        self.assertIn("R3", rule_ids)

    def test_expired_batches_are_never_allocated(self):
        self._receive("B-OLD", "2026-09-24", shelf=4)
        self._receive("B-OK", "2026-09-30", shelf=2)
        receipt = self._commit("c1", 3)
        plan = self._plan(receipt.resource_id)
        self.assertEqual([("B-OK", 2)], [(line["batch_id"], line["quantity"]) for line in plan["lines"]])
        self.assertEqual(1, plan["delayed_quantity"])
        excluded = [entry for entry in plan["trace"] if entry["rule_id"] == "R1"]
        self.assertEqual("B-OLD", excluded[0]["detail"]["excluded"][0]["batch_id"])

    def test_closed_store_rolls_to_next_business_day(self):
        self.service.set_store_calendar(request_id="cal", actor_id="op1", site_id="s1",
                                        open_time="08:00", close_time="22:00")
        late = InventoryService(self.database, FixedClock(datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)))
        self._receive("B1", "2026-09-25", shelf=3)  # 当天到期，结转到次日即过期
        receipt = late.create_commitment(request_id="night", actor_id="op1", site_id="s1",
                                         sku="MILK", quantity=2, kind="order", priority=100)
        explanation = late.commitment_explanation(receipt.resource_id)
        self.assertEqual("2026-09-26", explanation["commitment"]["effective_day"])
        plan = explanation["plans"][0]
        self.assertEqual(0, plan["allocated_quantity"])
        self.assertEqual(2, plan["delayed_quantity"])
        rolled = [entry for entry in plan["trace"] if entry["rule_id"] == "R6"]
        self.assertIn("结转", rolled[0]["summary"])

    def test_replan_lets_higher_priority_win_over_earlier_receipt(self):
        self._receive("B1", "2026-10-01", shelf=5)
        low = self._commit("c-low", 4, priority=10)
        high = self._commit("c-high", 4, priority=900)
        self.service.replan_pending(request_id="replan", actor_id="op1", site_id="s1", sku="MILK")
        high_plan = self._plan(high.resource_id)
        low_plan = self._plan(low.resource_id)
        self.assertEqual(4, high_plan["allocated_quantity"])
        self.assertEqual(1, low_plan["allocated_quantity"])
        self.assertEqual(3, low_plan["delayed_quantity"])
        events = self.service.commitment_explanation(low.resource_id)["events"]
        replanned = [event for event in events if event["event_type"] == "replanned"]
        self.assertEqual("lost", replanned[-1]["detail"]["outcome"])

    def test_confirm_checks_version_and_posts_atomically(self):
        self._receive("B1", "2026-10-01", shelf=5)
        first = self._commit("c1", 3)
        second = self._commit("c2", 2)
        first_plan = self._plan(first.resource_id)
        self.service.confirm_plan(request_id="ok", actor_id="op1",
                                  plan_id=first_plan["plan_id"], expected_version=1)
        self.assertEqual(2, self._position("B1", "shelf"))
        stale_plan = self._plan(second.resource_id)
        with self.assertRaises(ConflictError):
            self.service.confirm_plan(request_id="stale", actor_id="op1",
                                      plan_id=stale_plan["plan_id"], expected_version=1)
        # 版本冲突后不得留下部分扣减
        self.assertEqual(2, self._position("B1", "shelf"))
        self.assertEqual("proposed", self._plan(second.resource_id)["status"])

    def test_confirm_replay_does_not_deduct_twice(self):
        self._receive("B1", "2026-10-01", shelf=5)
        receipt = self._commit("c1", 3)
        plan = self._plan(receipt.resource_id)
        first = self.service.confirm_plan(request_id="ok", actor_id="op1",
                                          plan_id=plan["plan_id"], expected_version=1)
        replay = self.service.confirm_plan(request_id="ok", actor_id="op1",
                                           plan_id=plan["plan_id"], expected_version=1)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(2, self._position("B1", "shelf"))

    def test_same_message_replays_and_changed_payload_conflicts(self):
        self._receive("B1", "2026-10-01", shelf=5)
        first = self._commit("c1", 2)
        replay = self._commit("c1", 2)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        with self.assertRaises(ConflictError):
            self._commit("c1", 3)

    def test_cancel_releases_only_unfulfilled_quantity(self):
        self._receive("B1", "2026-10-01", shelf=3)
        self._receive("B2", "2026-10-05", shelf=2)
        receipt = self._commit("c1", 5)
        plan = self._plan(receipt.resource_id)
        self.service.confirm_plan(request_id="ok", actor_id="op1",
                                  plan_id=plan["plan_id"], expected_version=2)
        self.service.fulfill_commitment(request_id="f1", actor_id="op1",
                                        commitment_id=receipt.resource_id, line_numbers=[1])
        self.service.cancel_commitment(request_id="x1", actor_id="op1",
                                       commitment_id=receipt.resource_id)
        self.assertEqual(0, self._position("B1", "shelf"))  # 已履约，保持扣减
        self.assertEqual(2, self._position("B2", "shelf"))  # 未履约，退回库存
        with self.assertRaises(ConflictError):
            self.service.cancel_commitment(request_id="x2", actor_id="op1",
                                           commitment_id=receipt.resource_id)

    def test_fulfilled_commitment_cannot_be_cancelled(self):
        self._receive("B1", "2026-10-01", shelf=2)
        receipt = self._commit("c1", 2)
        plan = self._plan(receipt.resource_id)
        self.service.confirm_plan(request_id="ok", actor_id="op1",
                                  plan_id=plan["plan_id"], expected_version=1)
        self.service.fulfill_commitment(request_id="f1", actor_id="op1",
                                        commitment_id=receipt.resource_id, line_numbers=[1])
        with self.assertRaises(ConflictError):
            self.service.cancel_commitment(request_id="x1", actor_id="op1",
                                           commitment_id=receipt.resource_id)

    def test_supervisor_lock_requires_reason_and_role(self):
        self._receive("B1", "2026-10-01", shelf=4)
        receipt = self._commit("c1", 2)
        with self.assertRaises(PermissionDenied):
            self.service.lock_commitment(request_id="l0", actor_id="op1",
                                         commitment_id=receipt.resource_id, reason="操作员越权")
        with self.assertRaises(ValidationError):
            self.service.lock_commitment(request_id="l1", actor_id="sv1",
                                         commitment_id=receipt.resource_id, reason="  ")
        self.service.lock_commitment(request_id="l2", actor_id="sv1",
                                     commitment_id=receipt.resource_id, reason="紧急顾客承诺")
        explanation = self.service.commitment_explanation(receipt.resource_id)
        self.assertTrue(explanation["commitment"]["locked"])
        self.assertEqual("紧急顾客承诺", explanation["overrides"][0]["reason"])

    def test_locked_commitment_keeps_stock_during_replan(self):
        self._receive("B1", "2026-10-01", shelf=4)
        vip = self._commit("c-vip", 3, priority=50)
        self.service.lock_commitment(request_id="l1", actor_id="sv1",
                                     commitment_id=vip.resource_id, reason="紧急承诺锁定")
        other = self._commit("c-other", 3, priority=900)
        self.service.replan_pending(request_id="replan", actor_id="op1", site_id="s1", sku="MILK")
        self.assertEqual(3, self._plan(vip.resource_id)["allocated_quantity"])
        self.assertEqual(1, self._plan(other.resource_id)["allocated_quantity"])

    def test_priority_change_is_recorded_with_reason(self):
        self._receive("B1", "2026-10-01", shelf=4)
        receipt = self._commit("c1", 2, priority=10)
        self.service.set_commitment_priority(request_id="p1", actor_id="sv1",
                                             commitment_id=receipt.resource_id,
                                             priority=800, reason="顾客升级为紧急单")
        explanation = self.service.commitment_explanation(receipt.resource_id)
        self.assertEqual(800, explanation["commitment"]["priority"])
        event = [item for item in explanation["events"] if item["event_type"] == "priority_changed"]
        self.assertEqual({"old_priority": 10, "new_priority": 800, "reason": "顾客升级为紧急单"},
                         {key: event[0]["detail"][key] for key in
                          ("old_priority", "new_priority", "reason")})

    def test_replenishment_moves_stock_or_fails_without_partial_moves(self):
        self._receive("B1", "2026-10-01", backroom=5)
        task = self.service.create_replenishment_task(
            request_id="t1", actor_id="op1", site_id="s1", sku="MILK", quantity=3)
        self.service.execute_replenishment_task(request_id="t1-exec", actor_id="op1",
                                                task_id=task.resource_id)
        self.assertEqual(3, self._position("B1", "shelf"))
        self.assertEqual(2, self._position("B1", "backroom"))
        too_big = self.service.create_replenishment_task(
            request_id="t2", actor_id="op1", site_id="s1", sku="MILK", quantity=10)
        with self.assertRaises(ConflictError):
            self.service.execute_replenishment_task(request_id="t2-exec", actor_id="op1",
                                                    task_id=too_big.resource_id)
        self.assertEqual(3, self._position("B1", "shelf"))
        self.assertEqual(2, self._position("B1", "backroom"))

    def test_explanation_contains_full_calculation_and_audit_basis(self):
        self._receive("B1", "2026-10-01", shelf=3)
        receipt = self._commit("c1", 5)
        plan = self._plan(receipt.resource_id)
        self.service.confirm_plan(request_id="ok", actor_id="op1",
                                  plan_id=plan["plan_id"], expected_version=1)
        explanation = self.service.commitment_explanation(receipt.resource_id)
        self.assertEqual("retail-allocation-v1", explanation["rules"]["version"])
        self.assertEqual(8, len(explanation["rules"]["rules"]))
        self.assertEqual(2, explanation["plans"][0]["delayed_quantity"])
        self.assertTrue(explanation["audit"])
        self.assertTrue(any("落账" in line for line in explanation["narrative"]))
        self.assertTrue(any("延迟" in line for line in explanation["narrative"]))
        valid, _ = self.service.verify_audit()
        self.assertTrue(valid)

    def test_missing_objects_raise_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.commitment_explanation("missing")
        with self.assertRaises(NotFoundError):
            self.service.get_plan("missing")
        with self.assertRaises(NotFoundError):
            self.service.get_inventory("missing-site", "MILK")

    def test_auditor_cannot_write_inventory(self):
        with self.assertRaises(PermissionDenied):
            self.service.receive_batch(request_id="b1", actor_id="au1", site_id="s1",
                                       batch_id="B1", sku="MILK", expires_on="2026-10-01",
                                       shelf_quantity=1)


if __name__ == "__main__":
    unittest.main()
