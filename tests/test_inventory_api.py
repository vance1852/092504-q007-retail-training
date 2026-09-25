import unittest
from datetime import datetime, timezone

from skills_workspace.api import route
from skills_workspace.clock import FixedClock
from skills_workspace.inventory_service import InventoryService
from skills_workspace.storage import Database


class InventoryApiTest(unittest.TestCase):
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
        self.service.register_site(request_id="site", actor_id="op1", site_id="s1",
                                   organization_id="o1", name="模拟门店", timezone_name="Asia/Shanghai")

    def tearDown(self):
        self.database.close()

    def test_full_flow_over_http_routes(self):
        status, batch = route(self.service, "POST", "/inventory/batches", {
            "request_id": "b1", "site_id": "s1", "batch_id": "B1", "sku": "MILK",
            "expires_on": "2026-10-01", "shelf_quantity": 5}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, commitment = route(self.service, "POST", "/commitments", {
            "request_id": "c1", "site_id": "s1", "sku": "MILK", "quantity": 3,
            "kind": "reservation", "priority": 500}, {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, explanation = route(
            self.service, "GET",
            f"/commitment-explanation?commitment_id={commitment['resource_id']}", None)
        self.assertEqual(200, status)
        plan_id = explanation["plans"][0]["plan_id"]
        status, confirmed = route(self.service, "POST", "/plan-confirmations", {
            "request_id": "ok1", "plan_id": plan_id, "expected_version": 1},
            {"X-Actor-Id": "op1"})
        self.assertEqual(201, status)
        status, inventory = route(self.service, "GET", "/inventory?site_id=s1&sku=MILK", None)
        self.assertEqual(200, status)
        shelf = [item for item in inventory["positions"] if item["location"] == "shelf"]
        self.assertEqual(2, shelf[0]["quantity"])
        self.assertEqual(2, inventory["inventory_version"])

    def test_replayed_post_returns_200(self):
        body = {"request_id": "b1", "site_id": "s1", "batch_id": "B1", "sku": "MILK",
                "expires_on": "2026-10-01", "shelf_quantity": 1}
        first = route(self.service, "POST", "/inventory/batches", body, {"X-Actor-Id": "op1"})
        second = route(self.service, "POST", "/inventory/batches", body, {"X-Actor-Id": "op1"})
        self.assertEqual(201, first[0])
        self.assertEqual(200, second[0])
        self.assertTrue(second[1]["replayed"])

    def test_override_route_dispatches_and_validates(self):
        route(self.service, "POST", "/inventory/batches", {
            "request_id": "b1", "site_id": "s1", "batch_id": "B1", "sku": "MILK",
            "expires_on": "2026-10-01", "shelf_quantity": 2}, {"X-Actor-Id": "op1"})
        _, commitment = route(self.service, "POST", "/commitments", {
            "request_id": "c1", "site_id": "s1", "sku": "MILK", "quantity": 1},
            {"X-Actor-Id": "op1"})
        status, _ = route(self.service, "POST", "/commitment-overrides", {
            "request_id": "o1", "commitment_id": commitment["resource_id"],
            "action": "lock", "reason": "紧急承诺"}, {"X-Actor-Id": "sv1"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "POST", "/commitment-overrides", {
            "request_id": "o2", "commitment_id": commitment["resource_id"],
            "action": "explode", "reason": "非法动作"}, {"X-Actor-Id": "sv1"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_query_routes_require_parameters(self):
        status, payload = route(self.service, "GET", "/inventory?site_id=s1", None)
        self.assertEqual(400, status)
        status, payload = route(self.service, "GET", "/commitment-explanation", None)
        self.assertEqual(400, status)
        status, payload = route(self.service, "GET", "/commitment-explanation?commitment_id=nope", None)
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_rules_and_lists_are_available(self):
        status, rules = route(self.service, "GET", "/allocation-rules", None)
        self.assertEqual(200, status)
        self.assertEqual("retail-allocation-v1", rules["version"])
        self.assertEqual(8, len(rules["rules"]))
        status, tasks = route(self.service, "GET", "/replenishment-tasks?site_id=s1", None)
        self.assertEqual(200, status)
        self.assertEqual([], tasks["items"])
        status, commitments = route(self.service, "GET", "/commitments?site_id=s1", None)
        self.assertEqual(200, status)
        self.assertEqual([], commitments["items"])


if __name__ == "__main__":
    unittest.main()
