"""无第三方依赖的水下干预闭环 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import InterventionError, ValidationFailed
from .service import InterventionService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(self, service: InterventionService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized_headers)
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/wells":
                return Response(201, self.service.register_well(
                    actor, payload["well_id"], payload["name"], payload["field_name"], payload["water_depth_m"]))
            if method == "POST" and path == "/equipment":
                return Response(201, self.service.register_equipment(
                    actor, payload["well_id"], payload["equipment_id"], payload["kind"], payload["serial_no"]))
            if method == "POST" and path == "/jobs":
                return Response(201, self.service.create_job(
                    actor, payload["job_id"], payload["well_id"], payload["title"],
                    payload["anomaly_summary"], int(payload.get("priority", 100))))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "evidence":
                return Response(201, self.service.attach_evidence(
                    actor, parts[1], payload["evidence_id"], payload["kind"], payload["summary"],
                    payload["content_sha256"], payload["observed_at"], payload["source"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "freeze":
                return Response(200, self.service.freeze_job(
                    actor, parts[1], payload["plan"], int(payload["expected_revision"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "isolation":
                return Response(200, self.service.confirm_isolation(
                    actor, parts[1], int(payload["expected_revision"]), payload["idempotency_key"],
                    payload.get("barrier_confirmations", []), payload["note"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "start":
                return Response(200, self.service.start_operation(
                    actor, parts[1], int(payload["expected_revision"]), payload["idempotency_key"], payload["note"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "pause":
                return Response(200, self.service.pause_operation(
                    actor, parts[1], int(payload["expected_revision"]), payload["idempotency_key"], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "resume":
                return Response(200, self.service.resume_operation(
                    actor, parts[1], int(payload["expected_revision"]), payload["idempotency_key"], payload["note"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "complete":
                return Response(200, self.service.complete_operation(
                    actor, parts[1], int(payload["expected_revision"]), payload["idempotency_key"], payload["summary"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                return Response(200, self.service.cancel_job(
                    actor, parts[1], int(payload["expected_revision"]), payload["reason"]))
            if method == "GET" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "report":
                return Response(200, self.service.job_report(actor, parts[1]))
            if method == "POST" and len(parts) == 4 and parts[0] == "jobs" and parts[2] == "recovery":
                return Response(200, self.service.complete_recovery_action(
                    actor, parts[1], parts[3], payload["note"], payload["evidence_ref"]))
            if method == "POST" and path == "/resources":
                return Response(201, self.service.register_resource(
                    actor, payload["resource_id"], payload["kind"], payload["name"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "resources":
                return Response(201, self.service.commit_resource(actor, parts[1], payload["resource_id"]))
            if method == "POST" and len(parts) == 4 and parts[0] == "jobs" and parts[2] == "resources" and parts[3] == "release":
                return Response(200, self.service.release_resource(
                    actor, parts[1], payload["resource_id"], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "wells" and parts[2] == "telemetry":
                key = normalized_headers.get("idempotency-key", "").strip() or payload.get("idempotency_key", "")
                return Response(200, self.service.record_telemetry(
                    actor, parts[1], payload["metric"], payload["value"], payload["unit"],
                    payload["observed_at"], key))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except InterventionError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SubseaIntervention/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动水下井干预闭环 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("subsea_intervention.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(InterventionService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
