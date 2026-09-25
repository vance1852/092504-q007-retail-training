"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .inventory_service import InventoryService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        if method == "POST" and parsed.path == "/store-calendars":
            receipt = service.set_store_calendar(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/inventory/batches":
            receipt = service.receive_batch(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/inventory/replenishment-tasks":
            receipt = service.create_replenishment_task(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/inventory/replenishment-executions":
            receipt = service.execute_replenishment_task(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/inventory/replans":
            receipt = service.replan_pending(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/commitments":
            receipt = service.create_commitment(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/plan-confirmations":
            receipt = service.confirm_plan(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/commitment-cancellations":
            receipt = service.cancel_commitment(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/commitment-fulfillments":
            receipt = service.fulfill_commitment(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/commitment-overrides":
            body = dict(body)
            action = body.pop("action", "")
            if action == "lock":
                receipt = service.lock_commitment(actor_id=actor_id, **body)
            elif action == "unlock":
                receipt = service.unlock_commitment(actor_id=actor_id, **body)
            elif action == "set_priority":
                receipt = service.set_commitment_priority(actor_id=actor_id, **body)
            else:
                raise ValidationError("action 必须是 lock、unlock 或 set_priority")
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/inventory":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            sku = query.get("sku", [""])[0]
            if not site_id or not sku:
                raise ValidationError("site_id 和 sku 不能为空")
            return 200, service.get_inventory(site_id, sku)
        if method == "GET" and parsed.path == "/commitments":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            status = query.get("status", [None])[0]
            return 200, {"items": [item.__dict__ for item in
                                   service.list_commitments(site_id, status)]}
        if method == "GET" and parsed.path == "/commitment-explanation":
            query = parse_qs(parsed.query)
            commitment_id = query.get("commitment_id", [""])[0]
            if not commitment_id:
                raise ValidationError("commitment_id 不能为空")
            return 200, service.commitment_explanation(commitment_id)
        if method == "GET" and parsed.path == "/plans":
            query = parse_qs(parsed.query)
            plan_id = query.get("plan_id", [""])[0]
            if not plan_id:
                raise ValidationError("plan_id 不能为空")
            return 200, service.get_plan(plan_id)
        if method == "GET" and parsed.path == "/allocation-rules":
            return 200, service.list_allocation_rules()
        if method == "GET" and parsed.path == "/replenishment-tasks":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            status = query.get("status", [None])[0]
            return 200, {"items": [item.__dict__ for item in
                                   service.list_replenishment_tasks(site_id, status)]}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = InventoryService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
