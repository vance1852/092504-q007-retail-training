import unittest
from datetime import datetime, timezone

from skills_workspace.api import route
from skills_workspace.clock import FixedClock
from skills_workspace.retail import RetailService
from skills_workspace.storage import Database


class RetailApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = RetailService(self.database, FixedClock(
            datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="零售赛训机构")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="operator", actor_id="a1", new_actor_id="op1",
                                    display_name="操作员", role="operator", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="模拟门店",
                                   timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def post(self, path, body, actor="op1"):
        return route(self.service, "POST", path, body, {"X-Actor-Id": actor})

    def get(self, path):
        return route(self.service, "GET", path, None)

    def test_full_retail_flow_over_http(self):
        status, _ = self.post("/retail/schedules", {
            "request_id": "sch", "site_id": "s1",
            "open_time": "09:00", "close_time": "21:00", "closed_weekdays": []})
        self.assertEqual(201, status)
        status, _ = self.post("/retail/rules", {
            "request_id": "rules", "site_id": "s1",
            "rules": {"allow_partial": True}}, actor="a1")
        self.assertEqual(201, status)
        status, _ = self.post("/retail/batches", {
            "request_id": "b1", "site_id": "s1", "batch_id": "b1", "sku": "SKU-1",
            "shelf_qty": 5, "expires_on": "2026-10-01"})
        self.assertEqual(201, status)
        status, body = self.post("/retail/promises", {
            "request_id": "p1", "site_id": "s1", "promise_id": "p1", "sku": "SKU-1",
            "qty": 3, "priority": 50, "requested_at": "2026-09-25T01:00:00Z"})
        self.assertEqual(201, status)
        self.assertEqual("p1", body["promise"]["promise_id"])
        self.assertEqual("pending", body["promise"]["status"])
        status, body = self.post("/retail/plans", {
            "request_id": "plan-1", "site_id": "s1", "sku": "SKU-1"})
        self.assertEqual(201, status)
        plan_id = body["plan"]["plan_id"]
        self.assertEqual(3, body["plan"]["detail"]["lines"][0]["allocated_qty"])
        status, body = self.post("/retail/plans/confirm", {
            "request_id": "confirm-1", "plan_id": plan_id})
        self.assertEqual(201, status)
        self.assertEqual("confirmed", body["plan"]["status"])
        status, body = self.get("/retail/stock?site_id=s1&sku=SKU-1")
        self.assertEqual(200, status)
        self.assertEqual(2, body["items"][0]["shelf_qty"])
        status, body = self.get("/retail/promises/explain?promise_id=p1")
        self.assertEqual(200, status)
        self.assertEqual("gained", body["plans"][0]["outcome"])
        self.assertEqual(3, body["promise"]["allocated_qty"])

    def test_replayed_post_returns_200(self):
        body = {"request_id": "b1", "site_id": "s1", "batch_id": "b1", "sku": "SKU-1",
                "shelf_qty": 5, "expires_on": "2026-10-01"}
        first = self.post("/retail/batches", dict(body))
        second = self.post("/retail/batches", dict(body))
        self.assertEqual(201, first[0])
        self.assertEqual(200, second[0])
        self.assertTrue(second[1]["replayed"])

    def test_changed_payload_same_request_id_returns_409(self):
        body = {"request_id": "b1", "site_id": "s1", "batch_id": "b1", "sku": "SKU-1",
                "shelf_qty": 5, "expires_on": "2026-10-01"}
        self.post("/retail/batches", dict(body))
        body["shelf_qty"] = 6
        status, payload = self.post("/retail/batches", body)
        self.assertEqual(409, status)
        self.assertEqual("conflict", payload["error"])

    def test_lock_without_reason_returns_400(self):
        self.post("/retail/promises", {
            "request_id": "p1", "site_id": "s1", "promise_id": "p1", "sku": "SKU-1", "qty": 1})
        status, payload = self.post("/retail/promises/lock", {
            "request_id": "lock-1", "promise_id": "p1", "locked": True, "reason": " "}, actor="a1")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_unknown_promise_explain_returns_404(self):
        status, payload = self.get("/retail/promises/explain?promise_id=missing")
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_missing_site_param_returns_400(self):
        status, payload = self.get("/retail/stock")
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_unknown_retail_route_returns_404(self):
        status, payload = self.get("/retail/unknown")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
