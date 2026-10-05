"""无第三方依赖的水下干预闭环 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import InterventionError, ValidationFailed
from .service import InterventionService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: InterventionService) -> None:
        self.service = service
        # 同一进程内串行化写请求：避免多线程在单连接上交错事务；
        # 数据库层 BEGIN IMMEDIATE 仍保证多进程/多连接的幂等与资源竞争语义。
        self._write_lock = threading.Lock()

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

    @staticmethod
    def _key(payload: dict[str, Any], name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailed(f"{name} 不能为空")
        return value.strip()

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        # 单连接按请求串行使用，杜绝跨线程并发访问；跨进程由数据库事务隔离。
        with self._write_lock:
            return self._handle_locked(method, target, headers, body)

    def _handle_locked(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        service = self.service
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(201, service.create_user(payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/resources":
                return Response(201, service.register_resource(
                    actor, payload["resource_ref"], payload["resource_kind"], payload["name"]))
            if method == "POST" and path == "/jobs":
                return Response(201, service.create_job(actor, payload))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "draft":
                return Response(200, service.update_draft(actor, parts[1], payload))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "seal":
                return Response(200, service.seal_job(
                    actor, parts[1], int(payload["expected_revision"]), self._key(payload, "idempotency_key")))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "revise":
                telemetry = payload.get("include_telemetry_ids", [])
                if not isinstance(telemetry, list):
                    raise ValidationFailed("include_telemetry_ids 必须是数组")
                return Response(200, service.revise_job(actor, parts[1], payload, [int(x) for x in telemetry]))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "isolation":
                return Response(200, service.confirm_isolation(
                    actor, parts[1], payload["verifications"], payload["note"],
                    int(payload["expected_revision"]), self._key(payload, "idempotency_key")))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "start":
                return Response(200, service.start_job(
                    actor, parts[1], self._key(payload, "window_id"), payload["note"],
                    int(payload["expected_revision"]), self._key(payload, "idempotency_key")))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "pause":
                return Response(200, service.pause_job(
                    actor, parts[1], payload["reason"], payload["note"],
                    int(payload["expected_revision"]), self._key(payload, "idempotency_key")))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "resume":
                return Response(200, service.resume_job(
                    actor, parts[1], self._key(payload, "window_id"), payload["note"],
                    int(payload["expected_revision"]), self._key(payload, "idempotency_key")))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "complete":
                return Response(200, service.complete_job(
                    actor, parts[1], payload["note"],
                    int(payload["expected_revision"]), self._key(payload, "idempotency_key")))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                return Response(200, service.cancel_job(
                    actor, parts[1], payload["reason"],
                    int(payload["expected_revision"]), self._key(payload, "idempotency_key")))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "telemetry":
                return Response(201, service.record_telemetry(
                    actor, parts[1], self._key(payload, "equipment_id"), payload["observed_at"],
                    payload["metric"], payload["value"], payload["unit"], payload["source_ref"],
                    self._key(payload, "idempotency_key")))
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "sea-state":
                return Response(201, service.record_sea_state(
                    actor, parts[1], payload["observed_at"], payload["wave_height_m"],
                    payload["current_ms"], payload["wind_ms"], self._key(payload, "idempotency_key")))
            if len(parts) == 5 and parts[0] == "jobs" and parts[2] == "barriers" and parts[4] == "verify":
                return Response(200, service.verify_barrier(
                    actor, parts[1], parts[3], payload["evidence_ref"],
                    self._key(payload, "idempotency_key")))
            if len(parts) == 5 and parts[0] == "jobs" and parts[2] == "barriers" and parts[4] == "establish":
                return Response(200, service.establish_barrier(
                    actor, parts[1], parts[3], payload["evidence_ref"],
                    self._key(payload, "idempotency_key")))
            if len(parts) == 5 and parts[0] == "jobs" and parts[2] == "recovery" and parts[4] == "complete":
                return Response(200, service.complete_recovery_action(
                    actor, parts[1], parts[3], payload["note"], self._key(payload, "idempotency_key")))
            if method == "GET" and len(parts) == 2 and parts[0] == "jobs":
                return Response(200, service.job(parts[1]))
            if method == "GET" and len(parts) == 4 and parts[0] == "jobs" and parts[2] == "versions":
                return Response(200, service.job_version(parts[1], int(parts[3])))
            if method == "GET" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "barriers":
                return Response(200, {"job_id": parts[1], "barriers": service.barrier_evidence(parts[1])})
            if method == "GET" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "resources":
                return Response(200, {"job_id": parts[1], "resources": service.resources_status(parts[1])})
            if method == "GET" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "recovery":
                return Response(200, service.recovery_status(parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "telemetry":
                return Response(200, {"job_id": parts[1], "telemetry": service.late_telemetry(parts[1])})
            if method == "GET" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "reconstruction":
                return Response(200, service.reconstruction(actor, parts[1]))
            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(actor))
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
    parser = argparse.ArgumentParser(description="启动水下干预闭环服务")
    parser.add_argument("--database", type=Path, default=Path("subsea-intervention.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database, check_same_thread=False)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(InterventionService(connection))))
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
