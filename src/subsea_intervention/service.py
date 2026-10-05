"""水下干预闭环用例：冻结作业版本、顺序闸门、资源承诺与恢复跟踪。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    GATE_ORDER,
    GATE_PERMISSION,
    RESOURCE_KINDS,
    WorkPackage,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS: Mapping[str, set[str]] = {
    "isolation-engineer": {"gate.isolation", "barrier.verify"},
    "intervention-supervisor": {
        "job.write", "job.seal", "job.revise", "telemetry.write", "gate.start", "gate.resume",
    },
    "marine-supervisor": {"weather.write", "gate.pause"},
    "diving-supervisor": set(),
    "onscene-commander": {"job.cancel", "gate.complete", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

# 责任角色：恢复动作 owner_role 必须取自该表，闭环时强校验。
KNOWN_OWNER_ROLES = frozenset(ROLE_PERMISSIONS)

# 闸门成功后作业进入的状态。
GATE_TARGET_STATE = {
    "isolation": "isolated",
    "start": "active",
    "pause": "paused",
    "resume": "active",
    "complete": "completed",
}
# 闸门允许发起时作业所处的状态。
GATE_SOURCE_STATES = {
    "isolation": {"sealed"},
    "start": {"isolated"},
    "pause": {"active"},
    "resume": {"paused"},
    "complete": {"active"},
}
# 一次性阶段闸门在签署链中的固定序号。
GATE_SEQ = {"isolation": 1, "start": 2, "complete": 3}
PHASE_GATES = frozenset(GATE_SEQ)
REVISABLE_STATES = {"sealed", "isolated", "paused"}
OPEN_STATES = {"sealed", "isolated", "active", "paused"}


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def freeze_content(package: WorkPackage) -> dict[str, object]:
    """把作业包整理成确定顺序的冻结基线。"""

    data = asdict(package)
    for key in ("equipment", "evidence", "barriers", "steps", "qualifications", "components", "windows", "recovery"):
        order = {
            "equipment": "equipment_id",
            "evidence": "evidence_id",
            "barriers": "barrier_id",
            "steps": "step_no",
            "qualifications": "person_id",
            "components": "component_id",
            "windows": "window_id",
            "recovery": "action_id",
        }[key]
        data[key] = sorted(data[key], key=lambda item: item[order])
    return data


class InterventionService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM intervention_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM intervention_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO intervention_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    # ---------------------------------------------------------------- 幂等与事务

    def _replay_row(self, scope: str, key: str, request_hash: str) -> dict[str, Any] | None:
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM intervention_idempotency "
            "WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != request_hash:
            raise Conflict("幂等键对应不同的请求内容")
        return json.loads(stored["response_json"])

    def _idempotent(
        self,
        scope: str,
        key: str,
        request_obj: Mapping[str, Any],
        action: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """重复回调返回与首次完全相同的结果；action 在唯一的立即事务内执行。"""

        if not isinstance(key, str) or not key.strip():
            raise ValidationFailed("idempotency_key 不能为空")
        key = key.strip()
        request_hash = digest(request_obj)
        cached = self._replay_row(scope, key, request_hash)
        if cached is not None:
            return cached
        with transaction(self.connection, immediate=True):
            # 进入事务后再查一次，串行化并发回调。
            cached = self._replay_row(scope, key, request_hash)
            if cached is not None:
                return cached
            response = action()
            self.connection.execute(
                "INSERT INTO intervention_idempotency(scope,idempotency_key,request_sha256,response_json,"
                "created_at) VALUES(?,?,?,?,?)",
                (scope, key, request_hash, canonical_json(response), self._now()),
            )
        return response

    # ------------------------------------------------------------------ 用户/资源

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO intervention_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_resource(
        self, actor_id: str, resource_ref: str, resource_kind: str, name: str
    ) -> dict[str, Any]:
        self._require(actor_id, "job.write")
        ref = resource_ref.strip()
        kind = resource_kind.strip()
        if kind not in RESOURCE_KINDS or not ref or not name.strip():
            raise ValidationFailed("资源类型不受支持或字段为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO resource_registry(resource_ref,resource_kind,name,created_at) VALUES(?,?,?,?)",
                    (ref, kind, name.strip(), self._now()),
                )
                self._audit("resource", ref, "resource.registered", actor_id, {"resource_kind": kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict("资源已经登记") from exc
        return {"resource_ref": ref, "resource_kind": kind, "state": "registered"}

    # ------------------------------------------------------------------ 作业版本

    def _validate_certificates(self, package: WorkPackage) -> None:
        today = self.clock.now().date()
        for person in package.qualifications:
            expires = date.fromisoformat(person.valid_until)
            if expires < today:
                raise ValidationFailed(f"人员 {person.person_id} 的资格证书已于 {person.valid_until} 过期")

    def _validate_recovery_owners(self, package: WorkPackage) -> None:
        for action in package.recovery:
            if action.owner_role not in KNOWN_OWNER_ROLES:
                raise ValidationFailed(f"恢复动作 {action.action_id} 的 owner_role 不是已知职责")

    def create_job(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "job.write")
        package = WorkPackage.from_dict(raw)
        job_id = str(raw.get("job_id") or "").strip()
        if not job_id:
            raise ValidationFailed("job_id 不能为空")
        idem_key = str(raw.get("idempotency_key") or "").strip()
        if not idem_key:
            raise ValidationFailed("idempotency_key 不能为空")
        self._validate_certificates(package)
        self._validate_recovery_owners(package)

        def write() -> dict[str, Any]:
            try:
                self.connection.execute(
                    "INSERT INTO intervention_jobs(job_id,well_id,title,state,version,frozen_sha256,"
                    "trigger_evidence_id,created_by,created_at) VALUES(?,?,?,'draft',0,?,?,?,?)",
                    (job_id, package.well_id, package.title,
                     digest(freeze_content(package)), package.trigger_evidence_id, actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("作业编号已经存在") from exc
            self.connection.execute(
                "INSERT INTO job_drafts(job_id,package_json,updated_by,updated_at) VALUES(?,?,?,?)",
                (job_id, canonical_json(freeze_content(package)), actor_id, self._now()),
            )
            self._audit("job", job_id, "job.created", actor_id, {"well_id": package.well_id})
            return {"job_id": job_id, "state": "draft", "version": 0, "revision": 1}

        return self._idempotent("job.create", idem_key, raw, write)

    def _job_row(self, job_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM intervention_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise NotFound("作业不存在")
        return row

    def _load_draft_package(self, job_id: str) -> WorkPackage:
        row = self.connection.execute(
            "SELECT package_json FROM job_drafts WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise InvalidState("草稿作业包丢失")
        return WorkPackage.from_dict(json.loads(row["package_json"]))

    def update_draft(self, actor_id: str, job_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """密封前重新提交作业包：不产生签署版本，仅替换草稿基线。"""

        self._require(actor_id, "job.write")
        package = WorkPackage.from_dict(raw)
        self._validate_certificates(package)
        self._validate_recovery_owners(package)
        with transaction(self.connection, immediate=True):
            current = self._job_row(job_id)
            if current["state"] != "draft":
                raise InvalidState("只有草稿作业可以重新提交作业包")
            if package.well_id != current["well_id"]:
                raise ValidationFailed("不能改变作业井口")
            frozen = freeze_content(package)
            frozen_sha = digest(frozen)
            self.connection.execute(
                "UPDATE job_drafts SET package_json=?,updated_by=?,updated_at=? WHERE job_id=?",
                (canonical_json(frozen), actor_id, self._now(), job_id),
            )
            self.connection.execute(
                "UPDATE intervention_jobs SET frozen_sha256=? WHERE job_id=?", (frozen_sha, job_id)
            )
            self._audit("job", job_id, "job.draft_updated", actor_id, {"frozen_sha256": frozen_sha})
        return {"job_id": job_id, "state": "draft", "content_sha256": frozen_sha}

    def _persist_version_details(self, job_id: str, version: int, package: WorkPackage) -> None:
        for item in package.equipment:
            self.connection.execute(
                "INSERT INTO job_equipment(job_id,version,equipment_id,well_id,name,kind,design_pressure_bar) "
                "VALUES(?,?,?,?,?,?,?)",
                (job_id, version, item.equipment_id, item.well_id, item.name, item.kind,
                 str(item.design_pressure_bar)),
            )
        for item in package.evidence:
            self.connection.execute(
                "INSERT INTO job_evidence(job_id,version,evidence_id,kind,equipment_id,observed_at,received_at,"
                "metric,value,unit,source_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, version, item.evidence_id, item.kind, item.equipment_id, item.observed_at,
                 item.received_at, item.metric, str(item.value), item.unit, item.source_ref),
            )
        for item in package.barriers:
            self.connection.execute(
                "INSERT INTO job_barriers(job_id,version,barrier_id,kind,description,equipment_id,"
                "verification_method) VALUES(?,?,?,?,?,?,?)",
                (job_id, version, item.barrier_id, item.kind, item.description, item.equipment_id,
                 item.verification_method),
            )
        for item in package.steps:
            self.connection.execute(
                "INSERT INTO job_steps(job_id,version,step_no,title,instruction,estimated_minutes,"
                "requires_rov,requires_diving) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, version, item.step_no, item.title, item.instruction, item.estimated_minutes,
                 int(item.requires_rov), int(item.requires_diving)),
            )
        for item in package.qualifications:
            self.connection.execute(
                "INSERT INTO job_qualifications(job_id,version,person_id,display_name,qualification,"
                "certificate_ref,valid_until) VALUES(?,?,?,?,?,?,?)",
                (job_id, version, item.person_id, item.display_name, item.qualification,
                 item.certificate_ref, item.valid_until),
            )
        for item in package.components:
            self.connection.execute(
                "INSERT INTO job_components(job_id,version,component_id,resource_kind,name,resource_ref,quantity) "
                "VALUES(?,?,?,?,?,?,?)",
                (job_id, version, item.component_id, item.resource_kind, item.name, item.resource_ref,
                 item.quantity),
            )
        for item in package.windows:
            self.connection.execute(
                "INSERT INTO job_windows(job_id,version,window_id,opens_at,closes_at,max_wave_height_m,"
                "max_current_ms,max_wind_ms,forecast_ref) VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, version, item.window_id, item.opens_at, item.closes_at,
                 str(item.max_wave_height_m), str(item.max_current_ms), str(item.max_wind_ms),
                 item.forecast_ref),
            )
        for item in package.recovery:
            self.connection.execute(
                "INSERT INTO job_recovery_plan(job_id,version,action_id,kind,description,resource_ref,owner_role) "
                "VALUES(?,?,?,?,?,?,?)",
                (job_id, version, item.action_id, item.kind, item.description, item.resource_ref,
                 item.owner_role),
            )

    def _commit_components(
        self, job_id: str, components: Sequence[Any], now: str, held: frozenset[str] = frozenset()
    ) -> None:
        """资源竞争：active_resource_commitments 主键即资源，双方只有一个 INSERT 成功。"""

        for component in components:
            registry = self.connection.execute(
                "SELECT resource_kind FROM resource_registry WHERE resource_ref=?",
                (component.resource_ref,),
            ).fetchone()
            if registry is None:
                raise ValidationFailed(f"资源 {component.resource_ref} 未登记，不能承诺")
            if registry["resource_kind"] != component.resource_kind:
                raise ValidationFailed(f"组件 {component.component_id} 的资源类型与登记 {component.resource_ref} 不一致")
            if component.resource_ref in held:
                continue
            try:
                self.connection.execute(
                    "INSERT INTO active_resource_commitments(resource_ref,job_id,component_id,committed_at) "
                    "VALUES(?,?,?,?)",
                    (component.resource_ref, job_id, component.component_id, now),
                )
            except sqlite3.IntegrityError as exc:
                holder = self.connection.execute(
                    "SELECT job_id FROM active_resource_commitments WHERE resource_ref=?",
                    (component.resource_ref,),
                ).fetchone()
                raise Conflict(
                    f"资源 {component.resource_ref} 已被作业 {holder['job_id']} 承诺，资源竞争只允许一个作业占位"
                ) from exc
            self.connection.execute(
                "INSERT INTO resource_commitments(resource_ref,job_id,component_id,state,committed_at) "
                "VALUES(?,?,?,'committed',?)",
                (component.resource_ref, job_id, component.component_id, now),
            )

    def seal_job(
        self, actor_id: str, job_id: str, expected_revision: int, idempotency_key: str
    ) -> dict[str, Any]:
        self._require(actor_id, "job.seal")
        raw = {"job_id": job_id, "expected_revision": expected_revision, "idempotency_key": idempotency_key}

        def write() -> dict[str, Any]:
            current = self._job_row(job_id)
            if current["state"] != "draft":
                raise InvalidState("只有草稿作业可以密封")
            if current["revision"] != expected_revision:
                raise InvalidState("作业修订版本已变化，拒绝密封")
            package = self._load_draft_package(job_id)
            self._validate_certificates(package)
            frozen = freeze_content(package)
            frozen_sha = digest(frozen)
            version = 1
            now = self._now()
            self.connection.execute(
                "INSERT INTO job_version_snapshots(job_id,version,frozen_json,frozen_sha256,sealed_by,sealed_at) "
                "VALUES(?,?,?,?,?,?)",
                (job_id, version, canonical_json(frozen), frozen_sha, actor_id, now),
            )
            self._persist_version_details(job_id, version, package)
            for barrier in package.barriers:
                self.connection.execute(
                    "INSERT INTO barrier_states(job_id,barrier_id,version,state) VALUES(?,?,?,'pending')",
                    (job_id, barrier.barrier_id, version),
                )
            for action in package.recovery:
                self.connection.execute(
                    "INSERT INTO recovery_actions(job_id,action_id,version,kind,description,resource_ref,"
                    "owner_role,state) VALUES(?,?,?,?,?,?,?,'pending')",
                    (job_id, action.action_id, version, action.kind, action.description,
                     action.resource_ref, action.owner_role),
                )
            self._commit_components(job_id, package.components, now)
            cursor = self.connection.execute(
                "UPDATE intervention_jobs SET state='sealed',version=1,frozen_sha256=?,revision=revision+1,"
                "sealed_by=?,sealed_at=?,current_gate=?,responsible_user_id=? "
                "WHERE job_id=? AND state='draft' AND revision=?",
                (frozen_sha, actor_id, now, "sealed", actor_id, job_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("作业密封失败：状态已被其他事务修改")
            self._audit("job", job_id, "job.sealed", actor_id,
                        {"version": 1, "frozen_sha256": frozen_sha})
            return {"job_id": job_id, "state": "sealed", "version": 1,
                    "revision": expected_revision + 1, "frozen_sha256": frozen_sha}

        return self._idempotent("job.seal", idempotency_key, raw, write)

    def revise_job(
        self,
        actor_id: str,
        job_id: str,
        raw: Mapping[str, Any],
        include_telemetry_ids: Sequence[int] = (),
    ) -> dict[str, Any]:
        """暂停或尚未开工时冻结新版本；已建立屏障与已完成恢复动作必须保留。"""

        self._require(actor_id, "job.revise")
        idem_key = str(raw.get("idempotency_key") or "").strip()
        package = WorkPackage.from_dict(raw)
        self._validate_certificates(package)
        self._validate_recovery_owners(package)

        def write() -> dict[str, Any]:
            current = self._job_row(job_id)
            if current["state"] not in REVISABLE_STATES:
                raise InvalidState("只有已密封、已隔离或已暂停的作业可以冻结新版本")
            if package.well_id != current["well_id"]:
                raise ValidationFailed("新版本不能改变作业井口")
            telemetry_rows = []
            for telemetry_id in include_telemetry_ids:
                row = self.connection.execute(
                    "SELECT * FROM late_telemetry WHERE job_id=? AND telemetry_id=?", (job_id, telemetry_id)
                ).fetchone()
                if row is None:
                    raise NotFound(f"迟到遥测 {telemetry_id} 不存在")
                if row["status"] != "quarantined":
                    raise InvalidState(f"迟到遥测 {telemetry_id} 状态为 {row['status']}，不能并入")
                if row["equipment_id"] not in {item.equipment_id for item in package.equipment}:
                    raise ValidationFailed(f"迟到遥测 {telemetry_id} 的设备不在新版本设备清单中")
                telemetry_rows.append(row)
            incorporated = {
                "incorporated_telemetry": sorted(
                    (
                        {
                            "telemetry_id": row["telemetry_id"],
                            "evidence_id": f"tel-{row['telemetry_id']}",
                            "equipment_id": row["equipment_id"],
                            "observed_at": row["observed_at"],
                            "received_at": row["received_at"],
                            "metric": row["metric"],
                            "value": row["value"],
                            "unit": row["unit"],
                            "source_ref": row["source_ref"],
                        }
                        for row in telemetry_rows
                    ),
                    key=lambda item: item["telemetry_id"],
                )
            }
            frozen = {**freeze_content(package), **incorporated}
            frozen_sha = digest(frozen)
            if frozen_sha == current["frozen_sha256"]:
                raise InvalidState("作业包内容与当前冻结版本一致，无需新版本")
            new_version = current["version"] + 1
            now = self._now()
            try:
                self.connection.execute(
                    "INSERT INTO job_version_snapshots(job_id,version,frozen_json,frozen_sha256,"
                    "supersedes_version,sealed_by,sealed_at) VALUES(?,?,?,?,?,?,?)",
                    (job_id, new_version, canonical_json(frozen), frozen_sha,
                     current["version"], actor_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("作业版本内容重复或版本冲突") from exc
            self._persist_version_details(job_id, new_version, package)
            plan_barriers = {item.barrier_id: item for item in package.barriers}
            existing = self.connection.execute(
                "SELECT barrier_id,state FROM barrier_states WHERE job_id=?", (job_id,)
            ).fetchall()
            for row in existing:
                if row["barrier_id"] not in plan_barriers and row["state"] != "pending":
                    raise Conflict(f"已建立的屏障 {row['barrier_id']} 不能从新版本删除")
            for barrier in package.barriers:
                match = next((row for row in existing if row["barrier_id"] == barrier.barrier_id), None)
                if match is None:
                    self.connection.execute(
                        "INSERT INTO barrier_states(job_id,barrier_id,version,state) VALUES(?,?,?,'pending')",
                        (job_id, barrier.barrier_id, new_version),
                    )
                elif match["state"] == "pending":
                    self.connection.execute(
                        "UPDATE barrier_states SET version=? WHERE job_id=? AND barrier_id=?",
                        (new_version, job_id, barrier.barrier_id),
                    )
            for row in existing:
                if row["barrier_id"] not in plan_barriers and row["state"] == "pending":
                    self.connection.execute(
                        "DELETE FROM barrier_states WHERE job_id=? AND barrier_id=?",
                        (job_id, row["barrier_id"]),
                    )
            done_rows = self.connection.execute(
                "SELECT action_id FROM recovery_actions WHERE job_id=? AND state='done'", (job_id,)
            ).fetchall()
            for row in done_rows:
                if row["action_id"] not in {item.action_id for item in package.recovery}:
                    raise Conflict(f"已完成的恢复动作 {row['action_id']} 不能从新版本删除")
            for action in package.recovery:
                self.connection.execute(
                    "INSERT INTO recovery_actions(job_id,action_id,version,kind,description,resource_ref,"
                    "owner_role,state) VALUES(?,?,?,?,?,?,?,'pending') "
                    "ON CONFLICT(job_id,action_id) DO UPDATE SET "
                    "version=excluded.version,kind=excluded.kind,description=excluded.description,"
                    "resource_ref=excluded.resource_ref,owner_role=excluded.owner_role",
                    (job_id, action.action_id, new_version, action.kind, action.description,
                     action.resource_ref, action.owner_role),
                )
            held = frozenset(
                row["resource_ref"]
                for row in self.connection.execute(
                    "SELECT resource_ref FROM active_resource_commitments WHERE job_id=?", (job_id,)
                ).fetchall()
            )
            self._commit_components(job_id, package.components, now, held)
            for row in telemetry_rows:
                evidence_id = f"tel-{row['telemetry_id']}"
                self.connection.execute(
                    "INSERT INTO job_evidence(job_id,version,evidence_id,kind,equipment_id,observed_at,"
                    "received_at,metric,value,unit,source_ref) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, new_version, evidence_id, "pressure-telemetry", row["equipment_id"],
                     row["observed_at"], row["received_at"], row["metric"], row["value"], row["unit"],
                     row["source_ref"]),
                )
                updated = self.connection.execute(
                    "UPDATE late_telemetry SET status='incorporated',incorporated_version=? "
                    "WHERE telemetry_id=? AND status='quarantined'",
                    (new_version, row["telemetry_id"]),
                )
                if updated.rowcount != 1:
                    raise InvalidState(f"迟到遥测 {row['telemetry_id']} 已被其他事务处理")
            self.connection.execute(
                "UPDATE job_drafts SET package_json=?,updated_by=?,updated_at=? WHERE job_id=?",
                (canonical_json(frozen), actor_id, now, job_id),
            )
            cursor = self.connection.execute(
                "UPDATE intervention_jobs SET version=?,frozen_sha256=?,revision=revision+1,"
                "current_gate=? WHERE job_id=? AND revision=?",
                (new_version, frozen_sha, f"revised-v{new_version}", job_id, current["revision"]),
            )
            if cursor.rowcount != 1:
                raise InvalidState("作业修订版本已变化")
            self._audit("job", job_id, "job.revised", actor_id,
                        {"version": new_version, "frozen_sha256": frozen_sha,
                         "telemetry_ids": list(include_telemetry_ids)})
            return {"job_id": job_id, "state": current["state"], "version": new_version,
                    "revision": current["revision"] + 1, "frozen_sha256": frozen_sha}

        request_obj = dict(raw)
        request_obj["include_telemetry_ids"] = list(include_telemetry_ids)
        return self._idempotent("job.revise", idem_key, request_obj, write)

    # ------------------------------------------------------------------ 闸门签署

    def _phase_exists(self, job_id: str, gate: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM gate_signoffs WHERE job_id=? AND gate=?", (job_id, gate)
        ).fetchone() is not None

    def _cycle_count(self, job_id: str, gate: str) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) AS n FROM gate_cycle_signoffs WHERE job_id=? AND gate=?", (job_id, gate)
        ).fetchone()["n"])

    def _previous_signer(self, job_id: str, revision: int) -> str | None:
        """最近一次闸门签署人（中间可能穿插不产生签署的版本修订）。"""

        row = self.connection.execute(
            "SELECT signer_id FROM ("
            "SELECT signer_id,new_revision FROM gate_signoffs WHERE job_id=? "
            "UNION ALL "
            "SELECT signer_id,new_revision FROM gate_cycle_signoffs WHERE job_id=?"
            ") WHERE new_revision<=? ORDER BY new_revision DESC LIMIT 1",
            (job_id, job_id, revision),
        ).fetchone()
        return None if row is None else row["signer_id"]

    def _sign_gate(
        self,
        actor_id: str,
        job_id: str,
        gate: str,
        expected_revision: int,
        idempotency_key: str,
        note: str,
        extra_payload: Mapping[str, Any],
        precheck: Callable[[sqlite3.Row], None] | None = None,
        apply: Callable[[sqlite3.Row], None] | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, GATE_PERMISSION[gate])
        if not note.strip():
            raise ValidationFailed("签署备注不能为空")
        request_obj = {"job_id": job_id, "gate": gate, "expected_revision": expected_revision,
                       "idempotency_key": idempotency_key, "note": note.strip(), **dict(extra_payload)}

        def write() -> dict[str, Any]:
            job = self._job_row(job_id)
            if job["version"] < 1:
                raise InvalidState("作业尚未密封，不能签署")
            if job["state"] not in GATE_SOURCE_STATES[gate]:
                raise InvalidState(
                    f"作业当前状态 {job['state']} 不允许{gate}签署；闸门必须按 "
                    f"{'→'.join(GATE_ORDER)} 顺序推进（暂停/恢复可成对重复）"
                )
            if job["revision"] != expected_revision:
                raise InvalidState("作业修订版本已变化，拒绝签署")
            previous_signer = self._previous_signer(job_id, expected_revision)
            if previous_signer is not None and previous_signer == actor_id:
                raise Forbidden("相邻闸门必须由不同职责的人员确认，不能与上一签署人相同")
            if gate == "isolation" and self._phase_exists(job_id, "isolation"):
                raise InvalidState("隔离闸门已经签署")
            if gate == "start" and not self._phase_exists(job_id, "isolation"):
                raise InvalidState("必须先完成隔离闸门")
            if gate == "start" and self._phase_exists(job_id, "start"):
                raise InvalidState("开工闸门已经签署")
            if gate == "complete" and self._phase_exists(job_id, "complete"):
                raise InvalidState("完工闸门已经签署")
            pause_count = self._cycle_count(job_id, "pause")
            resume_count = self._cycle_count(job_id, "resume")
            if gate == "pause" and pause_count != resume_count:
                raise InvalidState("作业已暂停，必须先恢复才能再次暂停")
            if gate == "resume" and pause_count != resume_count + 1:
                raise InvalidState("作业不在暂停状态，不能恢复")
            if precheck is not None:
                precheck(job)
            now = self._now()
            new_revision = expected_revision + 1
            if gate in PHASE_GATES:
                seq = GATE_SEQ[gate]
                self.connection.execute(
                    "INSERT INTO gate_signoffs(job_id,version,gate,seq,signer_id,signer_role,signed_at,note,"
                    "expected_revision,new_revision) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (job_id, job["version"], gate, seq, actor_id, self._user(actor_id)["role"], now,
                     note.strip(), expected_revision, new_revision),
                )
                sign_ref: dict[str, Any] = {"seq": seq}
            else:
                cycle_no = pause_count + 1 if gate == "pause" else pause_count
                self.connection.execute(
                    "INSERT INTO gate_cycle_signoffs(job_id,version,gate,cycle_no,signer_id,signer_role,"
                    "signed_at,note,expected_revision,new_revision) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (job_id, job["version"], gate, cycle_no, actor_id, self._user(actor_id)["role"], now,
                     note.strip(), expected_revision, new_revision),
                )
                sign_ref = {"cycle_no": cycle_no}
            if apply is not None:
                apply(job)
            self.connection.execute(
                "UPDATE intervention_jobs SET state=?,revision=?,current_gate=?,responsible_user_id=? "
                "WHERE job_id=? AND revision=?",
                (GATE_TARGET_STATE[gate], new_revision, gate, actor_id, job_id, expected_revision),
            )
            self._audit("job", job_id, f"gate.{gate}.signed", actor_id,
                        {**sign_ref, "version": job["version"], **dict(extra_payload)})
            return {"job_id": job_id, "gate": gate, **sign_ref, "state": GATE_TARGET_STATE[gate],
                    "signed_by": actor_id, "revision": new_revision}

        return self._idempotent(f"gate.{gate}", idempotency_key, request_obj, write)

    def confirm_isolation(
        self,
        actor_id: str,
        job_id: str,
        verifications: Sequence[Mapping[str, Any]],
        note: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """隔离闸门：当前版本的每道屏障都必须建立并附带验证证据。"""

        normalized = []
        for item in verifications:
            normalized.append((str(item.get("barrier_id") or "").strip(),
                               str(item.get("evidence_ref") or "").strip()))

        def precheck(current: sqlite3.Row) -> None:
            required = {
                row["barrier_id"]
                for row in self.connection.execute(
                    "SELECT barrier_id FROM barrier_states WHERE job_id=? AND version=? AND state='pending'",
                    (job_id, current["version"]),
                ).fetchall()
            }
            if not normalized:
                raise ValidationFailed("必须为每道屏障提交建立证据")
            seen: set[str] = set()
            for barrier_id, evidence_ref in normalized:
                if barrier_id not in required:
                    raise ValidationFailed(f"屏障 {barrier_id} 不属于当前冻结版本或已建立")
                if not evidence_ref:
                    raise ValidationFailed(f"屏障 {barrier_id} 缺少建立证据")
                if barrier_id in seen:
                    raise ValidationFailed(f"屏障 {barrier_id} 重复验证")
                seen.add(barrier_id)
            missing = required - seen
            if missing:
                raise InvalidState(f"屏障尚未全部建立：{sorted(missing)}")

        def apply(current: sqlite3.Row) -> None:
            now = self._now()
            for barrier_id, evidence_ref in normalized:
                cursor = self.connection.execute(
                    "UPDATE barrier_states SET state='established',established_at=?,established_by=?,"
                    "gate='isolation',evidence_ref=? WHERE job_id=? AND version=? AND barrier_id=? "
                    "AND state='pending'",
                    (now, actor_id, evidence_ref, job_id, current["version"], barrier_id),
                )
                if cursor.rowcount != 1:
                    raise InvalidState(f"屏障 {barrier_id} 建立失败")

        payload_verifications = sorted(normalized)
        return self._sign_gate(
            actor_id, job_id, "isolation", expected_revision, idempotency_key, note,
            {"verifications": payload_verifications}, precheck, apply,
        )

    def establish_barrier(
        self, actor_id: str, job_id: str, barrier_id: str, evidence_ref: str, idempotency_key: str
    ) -> dict[str, Any]:
        """隔离闸门之后的修订新增屏障，由隔离工程师在隔离/暂停状态下补建并留证。"""

        self._require(actor_id, "gate.isolation")
        barrier_id = barrier_id.strip()
        evidence_ref = evidence_ref.strip()
        if not evidence_ref:
            raise ValidationFailed("建立证据不能为空")
        request_obj = {"job_id": job_id, "barrier_id": barrier_id, "evidence_ref": evidence_ref,
                       "idempotency_key": idempotency_key}

        def write() -> dict[str, Any]:
            job = self._job_row(job_id)
            if job["state"] not in {"isolated", "paused"}:
                raise InvalidState("只有已隔离或已暂停作业可以补建屏障")
            in_version = self.connection.execute(
                "SELECT 1 FROM job_barriers WHERE job_id=? AND version=? AND barrier_id=?",
                (job_id, job["version"], barrier_id),
            ).fetchone()
            if in_version is None:
                raise ValidationFailed("屏障不属于当前冻结版本")
            row = self.connection.execute(
                "SELECT state,evidence_ref FROM barrier_states WHERE job_id=? AND barrier_id=?",
                (job_id, barrier_id),
            ).fetchone()
            if row is None:
                raise NotFound("屏障不存在")
            if row["state"] != "pending":
                if row["evidence_ref"] == evidence_ref:
                    return {"job_id": job_id, "barrier_id": barrier_id, "state": row["state"]}
                raise InvalidState(f"屏障已处于 {row['state']} 状态，不能用其他证据覆盖")
            now = self._now()
            cursor = self.connection.execute(
                "UPDATE barrier_states SET state='established',established_at=?,established_by=?,"
                "gate='supplement',evidence_ref=? WHERE job_id=? AND barrier_id=? AND state='pending'",
                (now, actor_id, evidence_ref, job_id, barrier_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("屏障建立失败")
            self._audit("job", job_id, "barrier.established", actor_id,
                        {"barrier_id": barrier_id, "evidence_ref": evidence_ref})
            return {"job_id": job_id, "barrier_id": barrier_id, "state": "established"}

        return self._idempotent("barrier.establish", idempotency_key, request_obj, write)

    def verify_barrier(
        self, actor_id: str, job_id: str, barrier_id: str, evidence_ref: str, idempotency_key: str
    ) -> dict[str, Any]:
        """作业期间对已建立屏障的独立复核；重复回调幂等。"""

        self._require(actor_id, "barrier.verify")
        barrier_id = barrier_id.strip()
        evidence_ref = evidence_ref.strip()
        if not evidence_ref:
            raise ValidationFailed("复核证据不能为空")
        request_obj = {"job_id": job_id, "barrier_id": barrier_id, "evidence_ref": evidence_ref,
                       "idempotency_key": idempotency_key}

        def write() -> dict[str, Any]:
            job = self._job_row(job_id)
            if job["state"] not in {"isolated", "active", "paused"}:
                raise InvalidState("当前作业状态不能复核屏障")
            row = self.connection.execute(
                "SELECT state,evidence_ref FROM barrier_states WHERE job_id=? AND barrier_id=?",
                (job_id, barrier_id),
            ).fetchone()
            if row is None:
                raise NotFound("屏障不存在")
            if row["state"] == "verified":
                if row["evidence_ref"] != evidence_ref:
                    raise InvalidState("屏障已用其他证据复核，不能覆盖")
                return {"job_id": job_id, "barrier_id": barrier_id, "state": "verified"}
            if row["state"] != "established":
                raise InvalidState("屏障尚未建立，不能复核")
            self.connection.execute(
                "UPDATE barrier_states SET state='verified' WHERE job_id=? AND barrier_id=? AND state='established'",
                (job_id, barrier_id),
            )
            self._audit("job", job_id, "barrier.verified", actor_id,
                        {"barrier_id": barrier_id, "evidence_ref": evidence_ref})
            return {"job_id": job_id, "barrier_id": barrier_id, "state": "verified"}

        return self._idempotent("barrier.verify", idempotency_key, request_obj, write)

    def _weather_precheck(self, job_id: str, window_id: str) -> Callable[[sqlite3.Row], None]:
        def precheck(current: sqlite3.Row) -> None:
            window = self.connection.execute(
                "SELECT * FROM job_windows WHERE job_id=? AND version=? AND window_id=?",
                (job_id, current["version"], window_id),
            ).fetchone()
            if window is None:
                raise ValidationFailed("海况窗口不属于当前冻结版本")
            now = self.clock.now()
            opens = parse_utc(window["opens_at"])
            closes = parse_utc(window["closes_at"])
            if not opens <= now <= closes:
                raise InvalidState("当前时间不在所选海况窗口内")
            reading = self.connection.execute(
                "SELECT * FROM sea_state_readings WHERE job_id=? ORDER BY reading_id DESC LIMIT 1", (job_id,)
            ).fetchone()
            if reading is None:
                raise InvalidState("没有最新海况读数，不能开工或恢复")
            observed = parse_utc(reading["observed_at"])
            if not opens <= observed <= closes:
                raise InvalidState("最新海况读数不在所选窗口时间范围内")
            for field, limit in (
                ("wave_height_m", "max_wave_height_m"),
                ("current_ms", "max_current_ms"),
                ("wind_ms", "max_wind_ms"),
            ):
                if Decimal(reading[field]) > Decimal(window[limit]):
                    raise InvalidState(
                        f"最新海况读数 {field}={reading[field]} 超出窗口限值 {window[limit]}"
                    )

        return precheck

    def start_job(
        self, actor_id: str, job_id: str, window_id: str, note: str,
        expected_revision: int, idempotency_key: str,
    ) -> dict[str, Any]:
        return self._sign_gate(
            actor_id, job_id, "start", expected_revision, idempotency_key, note,
            {"window_id": window_id}, self._weather_precheck(job_id, window_id),
        )

    def pause_job(
        self, actor_id: str, job_id: str, reason: str, note: str,
        expected_revision: int, idempotency_key: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationFailed("暂停原因不能为空")
        return self._sign_gate(
            actor_id, job_id, "pause", expected_revision, idempotency_key, note, {"reason": reason.strip()},
        )

    def resume_job(
        self, actor_id: str, job_id: str, window_id: str, note: str,
        expected_revision: int, idempotency_key: str,
    ) -> dict[str, Any]:
        return self._sign_gate(
            actor_id, job_id, "resume", expected_revision, idempotency_key, note,
            {"window_id": window_id}, self._weather_precheck(job_id, window_id),
        )

    def complete_job(
        self, actor_id: str, job_id: str, note: str,
        expected_revision: int, idempotency_key: str,
    ) -> dict[str, Any]:
        def precheck(current: sqlite3.Row) -> None:
            pending = self.connection.execute(
                "SELECT barrier_id FROM barrier_states WHERE job_id=? AND state='pending'", (job_id,)
            ).fetchall()
            if pending:
                raise InvalidState(f"仍有屏障未建立：{[row['barrier_id'] for row in pending]}")

        return self._sign_gate(
            actor_id, job_id, "complete", expected_revision, idempotency_key, note, {}, precheck,
        )

    def cancel_job(
        self, actor_id: str, job_id: str, reason: str,
        expected_revision: int, idempotency_key: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "job.cancel")
        if not reason.strip():
            raise ValidationFailed("取消原因不能为空")
        request_obj = {"job_id": job_id, "expected_revision": expected_revision,
                       "idempotency_key": idempotency_key, "reason": reason.strip()}

        def write() -> dict[str, Any]:
            job = self._job_row(job_id)
            if job["state"] not in OPEN_STATES:
                raise InvalidState(f"作业状态 {job['state']} 不能取消")
            if job["revision"] != expected_revision:
                raise InvalidState("作业修订版本已变化，拒绝取消")
            now = self._now()
            cursor = self.connection.execute(
                "UPDATE intervention_jobs SET state='cancelled',revision=revision+1,"
                "cancelled_by=?,cancelled_at=?,cancel_reason=?,current_gate=?,responsible_user_id=? "
                "WHERE job_id=? AND revision=?",
                (actor_id, now, reason.strip(), "cancelled", actor_id, job_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("作业取消失败：修订版本已变化")
            pending = self.connection.execute(
                "SELECT COUNT(*) AS n FROM recovery_actions WHERE job_id=? AND state='pending'", (job_id,)
            ).fetchone()["n"]
            self._audit("job", job_id, "job.cancelled", actor_id,
                        {"reason": reason.strip(), "pending_recovery_actions": pending})
            return {"job_id": job_id, "state": "cancelled", "revision": expected_revision + 1,
                    "pending_recovery_actions": int(pending)}

        return self._idempotent("job.cancel", idempotency_key, request_obj, write)

    # ------------------------------------------------------------------ 遥测/海况

    def record_telemetry(
        self,
        actor_id: str,
        job_id: str,
        equipment_id: str,
        observed_at: str,
        metric: str,
        value: object,
        unit: str,
        source_ref: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """迟到遥测只追加到隔离表，永不覆盖已签署版本中的事实。"""

        self._require(actor_id, "telemetry.write")
        observed_text = parse_utc(observed_at, "observed_at")
        try:
            number = Decimal(str(value))
        except Exception as exc:  # Decimal 极少抛出，但统一为契约错误
            raise ValidationFailed("value 必须是数值") from exc
        if not number.is_finite():
            raise ValidationFailed("value 必须是有限数值")
        metric = metric.strip()
        unit = unit.strip()
        source_ref = source_ref.strip()
        if not metric or not unit or not source_ref:
            raise ValidationFailed("metric、unit、source_ref 不能为空")
        request_obj = {"job_id": job_id, "equipment_id": equipment_id, "observed_at": utc_text(observed_text),
                       "metric": metric, "value": str(number), "unit": unit, "source_ref": source_ref,
                       "idempotency_key": idempotency_key}

        def write() -> dict[str, Any]:
            job = self._job_row(job_id)
            if job["state"] not in OPEN_STATES:
                raise InvalidState("作业已结束，不再接收遥测")
            belongs = self.connection.execute(
                "SELECT 1 FROM job_equipment WHERE job_id=? AND version=? AND equipment_id=?",
                (job_id, job["version"], equipment_id),
            ).fetchone()
            if belongs is None:
                raise ValidationFailed("设备不属于当前冻结版本")
            now = self._now()
            cursor = self.connection.execute(
                "INSERT INTO late_telemetry(job_id,equipment_id,observed_at,received_at,metric,value,unit,"
                "source_ref,status,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,'quarantined',?,?)",
                (job_id, equipment_id, utc_text(observed_text), now, metric, str(number), unit,
                 source_ref, actor_id, now),
            )
            telemetry_id = int(cursor.lastrowid)
            self._audit("job", job_id, "telemetry.quarantined", actor_id,
                        {"telemetry_id": telemetry_id, "observed_at": utc_text(observed_text)})
            return {"telemetry_id": telemetry_id, "job_id": job_id, "status": "quarantined"}

        return self._idempotent("telemetry.record", idempotency_key, request_obj, write)

    def record_sea_state(
        self,
        actor_id: str,
        job_id: str,
        observed_at: str,
        wave_height_m: object,
        current_ms: object,
        wind_ms: object,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """海况读数只追加；开工/恢复闸门永远依据最新读数。"""

        self._require(actor_id, "weather.write")
        observed_text = parse_utc(observed_at, "observed_at")
        values: dict[str, Decimal] = {}
        for key, raw_value in (("wave_height_m", wave_height_m), ("current_ms", current_ms), ("wind_ms", wind_ms)):
            try:
                number = Decimal(str(raw_value))
            except Exception as exc:
                raise ValidationFailed(f"{key} 必须是数值") from exc
            if not number.is_finite() or number < 0:
                raise ValidationFailed(f"{key} 必须是非负有限数值")
            values[key] = number
        request_obj = {"job_id": job_id, "observed_at": utc_text(observed_text),
                       **{key: str(value) for key, value in values.items()},
                       "idempotency_key": idempotency_key}

        def write() -> dict[str, Any]:
            job = self._job_row(job_id)
            if job["state"] not in OPEN_STATES:
                raise InvalidState("作业已结束，不再接收海况读数")
            now = self._now()
            cursor = self.connection.execute(
                "INSERT INTO sea_state_readings(job_id,observed_at,received_at,wave_height_m,current_ms,"
                "wind_ms,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, utc_text(observed_text), now, str(values["wave_height_m"]),
                 str(values["current_ms"]), str(values["wind_ms"]), actor_id, now),
            )
            reading_id = int(cursor.lastrowid)
            self._audit("job", job_id, "weather.recorded", actor_id, {"reading_id": reading_id})
            return {"reading_id": reading_id, "job_id": job_id}

        return self._idempotent("weather.record", idempotency_key, request_obj, write)

    # ------------------------------------------------------------------ 恢复闭环

    def complete_recovery_action(
        self,
        actor_id: str,
        job_id: str,
        action_id: str,
        note: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """完工/取消后由动作指定责任角色逐项闭环；释放动作连带释放资源，解除隔离动作释放屏障。"""

        user = self._user(actor_id)
        if not note.strip():
            raise ValidationFailed("闭环备注不能为空")
        request_obj = {"job_id": job_id, "action_id": action_id, "note": note.strip(),
                       "idempotency_key": idempotency_key}

        def write() -> dict[str, Any]:
            job = self._job_row(job_id)
            if job["state"] not in {"completed", "cancelled"}:
                raise InvalidState("作业完工或取消后才能闭环恢复动作")
            action = self.connection.execute(
                "SELECT * FROM recovery_actions WHERE job_id=? AND action_id=?", (job_id, action_id)
            ).fetchone()
            if action is None:
                raise NotFound("恢复动作不存在")
            if action["state"] == "done":
                raise InvalidState("恢复动作已经闭环")
            if action["owner_role"] != user["role"]:
                raise Forbidden(f"恢复动作必须由责任角色 {action['owner_role']} 闭环")
            now = self._now()
            cursor = self.connection.execute(
                "UPDATE recovery_actions SET state='done',completed_by=?,completed_at=?,completion_note=? "
                "WHERE job_id=? AND action_id=? AND state='pending'",
                (actor_id, now, note.strip(), job_id, action_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("恢复动作闭环失败")
            released_resource = None
            commitment = self.connection.execute(
                "SELECT * FROM active_resource_commitments WHERE job_id=? AND resource_ref=?",
                (job_id, action["resource_ref"]),
            ).fetchone()
            if commitment is not None:
                # 无论是释放、消耗还是撤站，引用在持资源的动作闭环即结束作业对资源的占用。
                self.connection.execute(
                    "UPDATE resource_commitments SET state='released',released_at=?,released_by=? "
                    "WHERE job_id=? AND resource_ref=? AND state='committed'",
                    (now, actor_id, job_id, action["resource_ref"]),
                )
                self.connection.execute(
                    "DELETE FROM active_resource_commitments WHERE job_id=? AND resource_ref=?",
                    (job_id, action["resource_ref"]),
                )
                released_resource = action["resource_ref"]
            released_barriers: list[str] = []
            if action["kind"] == "remove-isolation":
                rows = self.connection.execute(
                    "UPDATE barrier_states SET state='released',released_at=?,released_by=? "
                    "WHERE job_id=? AND state IN ('established','verified') RETURNING barrier_id",
                    (now, actor_id, job_id),
                ).fetchall()
                released_barriers = [row["barrier_id"] for row in rows]
            self._audit("job", job_id, "recovery.completed", actor_id,
                        {"action_id": action_id, "kind": action["kind"],
                         "released_resource": released_resource, "released_barriers": released_barriers})
            return {"job_id": job_id, "action_id": action_id, "state": "done",
                    "released_resource": released_resource, "released_barriers": released_barriers}

        return self._idempotent("recovery.complete", idempotency_key, request_obj, write)

    # ------------------------------------------------------------------ 查询/还原

    def job(self, job_id: str) -> dict[str, Any]:
        job = self._job_row(job_id)
        result = dict(job)
        result["signoffs"] = [
            dict(row) for row in self.connection.execute(
                "SELECT gate,seq AS cycle_or_seq,'phase' AS signoff_kind,signer_id,signer_role,signed_at,"
                "new_revision AS order_revision FROM gate_signoffs WHERE job_id=? "
                "UNION ALL "
                "SELECT gate,cycle_no,'cycle',signer_id,signer_role,signed_at,new_revision "
                "FROM gate_cycle_signoffs WHERE job_id=? ORDER BY order_revision",
                (job_id, job_id),
            ).fetchall()
        ]
        return result

    def job_version(self, job_id: str, version: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM job_version_snapshots WHERE job_id=? AND version=?", (job_id, version)
        ).fetchone()
        if row is None:
            raise NotFound("作业版本不存在")
        frozen = json.loads(row["frozen_json"])
        calculated = digest(frozen)
        return {"job_id": job_id, "version": version, "frozen_sha256": row["frozen_sha256"],
                "sha256_intact": calculated == row["frozen_sha256"],
                "supersedes_version": row["supersedes_version"], "sealed_by": row["sealed_by"],
                "sealed_at": row["sealed_at"], "frozen": frozen}

    def barrier_evidence(self, job_id: str) -> list[dict[str, Any]]:
        self._job_row(job_id)
        rows = self.connection.execute(
            "SELECT cur.barrier_id,cur.kind,cur.description,cur.verification_method,cur.equipment_id,"
            "s.state,s.version AS established_version,s.established_at,s.established_by,s.evidence_ref,"
            "s.released_at,s.released_by "
            "FROM barrier_states s "
            "JOIN job_barriers cur ON cur.job_id=s.job_id AND cur.barrier_id=s.barrier_id "
            "AND cur.version=(SELECT version FROM intervention_jobs WHERE job_id=?) "
            "WHERE s.job_id=? ORDER BY cur.barrier_id",
            (job_id, job_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def resources_status(self, job_id: str) -> list[dict[str, Any]]:
        self._job_row(job_id)
        rows = self.connection.execute(
            "SELECT resource_ref,component_id,state,committed_at,released_at,released_by "
            "FROM resource_commitments WHERE job_id=? ORDER BY commitment_id",
            (job_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def recovery_status(self, job_id: str) -> dict[str, Any]:
        self._job_row(job_id)
        rows = self.connection.execute(
            "SELECT action_id,version,kind,description,resource_ref,owner_role,state,completed_by,completed_at,"
            "completion_note FROM recovery_actions WHERE job_id=? ORDER BY action_id",
            (job_id,),
        ).fetchall()
        actions = [dict(row) for row in rows]
        return {
            "job_id": job_id,
            "actions": actions,
            "pending": [item["action_id"] for item in actions if item["state"] == "pending"],
            "completed": [item["action_id"] for item in actions if item["state"] == "done"],
        }

    def late_telemetry(self, job_id: str) -> list[dict[str, Any]]:
        self._job_row(job_id)
        rows = self.connection.execute(
            "SELECT telemetry_id,equipment_id,observed_at,received_at,metric,value,unit,source_ref,status,"
            "incorporated_version FROM late_telemetry WHERE job_id=? ORDER BY telemetry_id",
            (job_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def reconstruction(self, actor_id: str, job_id: str) -> dict[str, Any]:
        """管理人员还原：每个屏障的证据、当前责任方、未完成恢复动作与资源占用。"""

        self._require(actor_id, "report.read")
        job = self._job_row(job_id)
        pending_recovery = self.connection.execute(
            "SELECT action_id,version,kind,description,resource_ref,owner_role FROM recovery_actions "
            "WHERE job_id=? AND state='pending' ORDER BY action_id",
            (job_id,),
        ).fetchall()
        held = self.connection.execute(
            "SELECT resource_ref,component_id,committed_at FROM active_resource_commitments WHERE job_id=?",
            (job_id,),
        ).fetchall()
        return {
            "job": dict(job),
            "frozen_version": self.job_version(job_id, job["version"]),
            "barriers": self.barrier_evidence(job_id),
            "current_responsible": {
                "user_id": job["responsible_user_id"],
                "gate": job["current_gate"],
            },
            "signoffs": [
                dict(row) for row in self.connection.execute(
                    "SELECT gate,seq AS cycle_or_seq,'phase' AS signoff_kind,signer_id,signer_role,signed_at,"
                    "note,version,new_revision AS order_revision FROM gate_signoffs WHERE job_id=? "
                    "UNION ALL "
                    "SELECT gate,cycle_no,'cycle',signer_id,signer_role,signed_at,note,version,new_revision "
                    "FROM gate_cycle_signoffs WHERE job_id=? ORDER BY order_revision",
                    (job_id, job_id),
                ).fetchall()
            ],
            "resources": {
                "all": self.resources_status(job_id),
                "still_held": [dict(row) for row in held],
            },
            "recovery": self.recovery_status(job_id),
            "outstanding_recovery": [dict(row) for row in pending_recovery],
            "late_telemetry": self.late_telemetry(job_id),
            "audit": self._audit_chain(),
        }

    def _audit_chain(self) -> dict[str, Any]:
        rows = self.connection.execute(
            "SELECT * FROM intervention_audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        return self._audit_chain()
