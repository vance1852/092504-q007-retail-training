"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .retail import RetailService
from .service import DomainService
from .storage import Database


def _receipt_response(receipt, extra: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    body = dict(receipt.__dict__)
    if extra:
        body.update(extra)
    return (200 if receipt.replayed else 201), body


def _retail_route(service: RetailService, method: str, parsed, body: dict[str, Any],
                  actor_id: str) -> tuple[int, dict[str, Any]] | None:
    """分派零售库存与承诺协调接口。"""

    path = parsed.path
    if method == "POST" and path == "/retail/schedules":
        return _receipt_response(service.set_schedule(actor_id=actor_id, **body))
    if method == "POST" and path == "/retail/rules":
        return _receipt_response(service.publish_rules(actor_id=actor_id, **body))
    if method == "GET" and path == "/retail/rules":
        query = parse_qs(parsed.query)
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("site_id 不能为空")
        return 200, service.get_rules(site_id)
    if method == "POST" and path == "/retail/batches":
        return _receipt_response(service.receive_batch(actor_id=actor_id, **body))
    if method == "GET" and path == "/retail/stock":
        query = parse_qs(parsed.query)
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("site_id 不能为空")
        return 200, service.get_stock(site_id, query.get("sku", [None])[0])
    if method == "POST" and path == "/retail/promises":
        receipt = service.create_promise(actor_id=actor_id, **body)
        return _receipt_response(receipt, {"promise": service.get_promise(receipt.resource_id)})
    if method == "GET" and path == "/retail/promises":
        query = parse_qs(parsed.query)
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("site_id 不能为空")
        return 200, {"items": service.list_promises(site_id, query.get("status", [None])[0])}
    if method == "GET" and path == "/retail/promises/detail":
        query = parse_qs(parsed.query)
        promise_id = query.get("promise_id", [""])[0]
        if not promise_id:
            raise ValidationError("promise_id 不能为空")
        return 200, service.get_promise(promise_id)
    if method == "GET" and path == "/retail/promises/explain":
        query = parse_qs(parsed.query)
        promise_id = query.get("promise_id", [""])[0]
        if not promise_id:
            raise ValidationError("promise_id 不能为空")
        return 200, service.explain_promise(promise_id)
    if method == "POST" and path == "/retail/promises/cancel":
        receipt = service.cancel_promise(actor_id=actor_id, **body)
        return _receipt_response(receipt, {"promise": service.get_promise(receipt.resource_id)})
    if method == "POST" and path == "/retail/promises/fulfill":
        receipt = service.fulfill_promise(actor_id=actor_id, **body)
        return _receipt_response(receipt, {"promise": service.get_promise(receipt.resource_id)})
    if method == "POST" and path == "/retail/promises/lock":
        receipt = service.set_lock(actor_id=actor_id, **body)
        return _receipt_response(receipt, {"promise": service.get_promise(receipt.resource_id)})
    if method == "POST" and path == "/retail/promises/priority":
        receipt = service.adjust_priority(actor_id=actor_id, **body)
        return _receipt_response(receipt, {"promise": service.get_promise(receipt.resource_id)})
    if method == "POST" and path == "/retail/plans":
        receipt = service.generate_plan(actor_id=actor_id, **body)
        return _receipt_response(receipt, {"plan": service.get_plan(receipt.resource_id)})
    if method == "GET" and path == "/retail/plans":
        query = parse_qs(parsed.query)
        plan_id = query.get("plan_id", [""])[0]
        if plan_id:
            return 200, service.get_plan(plan_id)
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("plan_id 或 site_id 不能为空")
        return 200, {"items": service.list_plans(site_id, query.get("sku", [None])[0])}
    if method == "POST" and path == "/retail/plans/confirm":
        receipt = service.confirm_plan(actor_id=actor_id, **body)
        return _receipt_response(receipt, {"plan": service.get_plan(receipt.resource_id)})
    if method == "POST" and path == "/retail/replenishments":
        return _receipt_response(service.create_replenishment(actor_id=actor_id, **body))
    if method == "GET" and path == "/retail/replenishments":
        query = parse_qs(parsed.query)
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("site_id 不能为空")
        return 200, {"items": service.list_replenishments(site_id)}
    if method == "POST" and path == "/retail/replenishments/complete":
        return _receipt_response(service.complete_replenishment(actor_id=actor_id, **body))
    if method == "POST" and path == "/retail/replenishments/cancel":
        return _receipt_response(service.cancel_replenishment(actor_id=actor_id, **body))
    return None


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
        if parsed.path.startswith("/retail/") and isinstance(service, RetailService):
            handled = _retail_route(service, method, parsed, body, actor_id)
            if handled is not None:
                return handled
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
    Handler.service = RetailService(database)
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
