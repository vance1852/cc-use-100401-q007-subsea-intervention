"""水下井干预闭环：作业版本冻结、顺序签署、资源承诺与遥测归集的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, parse_utc, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .models import (
    EQUIPMENT_KINDS,
    MILESTONE_KINDS,
    RESOURCE_KINDS,
    InterventionPlan,
    decimal_value,
    identifier,
    required_text,
    sha256_text,
    utc_text_field,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"well.register", "equipment.register", "job.create", "job.freeze", "evidence.attach", "job.cancel"},
    "isolation_officer": {"isolation.confirm", "recovery.complete"},
    "supervisor": {"operation.start", "operation.pause", "operation.resume", "operation.complete"},
    "resource_controller": {"resource.register", "resource.commit", "resource.release"},
    "telemetry": {"telemetry.write"},
    "auditor": {"report.read", "audit.read"},
}

MILESTONE_PERMISSION = {
    "isolation": "isolation.confirm",
    "start": "operation.start",
    "pause": "operation.pause",
    "resume": "operation.resume",
    "complete": "operation.complete",
}

MILESTONE_FROM_STATES = {
    "isolation": ("frozen",),
    "start": ("isolated",),
    "pause": ("in_progress",),
    "resume": ("paused",),
    "complete": ("in_progress",),
}

MILESTONE_TO_STATE = {
    "isolation": "isolated",
    "start": "in_progress",
    "pause": "paused",
    "resume": "in_progress",
    "complete": "completed",
}

NEXT_DUTY = {
    "frozen": {"permission": "isolation.confirm", "role": "isolation_officer", "milestone": "isolation"},
    "isolated": {"permission": "operation.start", "role": "supervisor", "milestone": "start"},
    "in_progress": {"permission": "operation.pause", "role": "supervisor", "milestone": "pause"},
    "paused": {"permission": "operation.resume", "role": "supervisor", "milestone": "resume"},
}

RESOURCE_ACTIVE_STATES = ("frozen", "isolated", "in_progress", "paused")


class InterventionService:
    """在单个 SQLite 连接上提供水下干预闭环的全部业务操作。"""

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

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM intervention_idempotency WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def _store_idempotent(self, scope: str, key: str, request_digest: str, response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO intervention_idempotency(scope,key,request_sha256,response_json,created_at) VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), self._now()),
        )

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

    # ------------------------------------------------------------------
    # 基础资料：井、井口设备、作业与故障证据
    # ------------------------------------------------------------------

    def register_well(
        self, actor_id: str, well_id: str, name: str, field_name: str, water_depth_m: object
    ) -> dict[str, Any]:
        self._require(actor_id, "well.register")
        well_id = identifier(well_id, "well_id")
        depth = decimal_value(water_depth_m, "water_depth_m", minimum=Decimal("0"))
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO wells(well_id,name,field_name,water_depth_m,created_at) VALUES(?,?,?,?,?)",
                    (well_id, required_text(name, "name"), required_text(field_name, "field_name"), str(depth), self._now()),
                )
                self._audit("well", well_id, "well.registered", actor_id, {"field_name": field_name})
        except sqlite3.IntegrityError as exc:
            raise Conflict("井编号已经存在") from exc
        return {"well_id": well_id, "state": "anomaly"}

    def register_equipment(
        self, actor_id: str, well_id: str, equipment_id: str, kind: str, serial_no: str
    ) -> dict[str, Any]:
        self._require(actor_id, "equipment.register")
        equipment_id = identifier(equipment_id, "equipment_id")
        if kind not in EQUIPMENT_KINDS:
            raise ValidationFailed("kind 不是受支持的井口设备类型")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO wellhead_equipment(equipment_id,well_id,kind,serial_no,created_at) VALUES(?,?,?,?,?)",
                    (equipment_id, identifier(well_id, "well_id"), kind, required_text(serial_no, "serial_no", 64), self._now()),
                )
                self._audit("equipment", equipment_id, "equipment.registered", actor_id, {"well_id": well_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("井口设备编号冲突或井不存在") from exc
        return {"equipment_id": equipment_id, "well_id": well_id, "revision": 1}

    def create_job(
        self,
        actor_id: str,
        job_id: str,
        well_id: str,
        title: str,
        anomaly_summary: str,
        priority: object = 100,
    ) -> dict[str, Any]:
        self._require(actor_id, "job.create")
        job_id = identifier(job_id, "job_id")
        well_id = identifier(well_id, "well_id")
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        well = self.connection.execute("SELECT state FROM wells WHERE well_id=?", (well_id,)).fetchone()
        if well is None:
            raise NotFound("井不存在")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO intervention_jobs(job_id,well_id,title,anomaly_summary,priority,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        job_id,
                        well_id,
                        required_text(title, "title"),
                        required_text(anomaly_summary, "anomaly_summary", 1024),
                        priority,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.execute(
                    "UPDATE wells SET state='under_intervention' WHERE well_id=? AND state='anomaly'",
                    (well_id,),
                )
                self._audit("job", job_id, "job.created", actor_id, {"well_id": well_id, "priority": priority})
        except sqlite3.IntegrityError as exc:
            raise Conflict("作业编号已经存在") from exc
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM intervention_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound("干预作业不存在")
        return dict(row)

    def attach_evidence(
        self,
        actor_id: str,
        job_id: str,
        evidence_id: str,
        kind: str,
        summary: str,
        content_sha256: str,
        observed_at: str,
        source: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "evidence.attach")
        job = self.get_job(job_id)
        if job["state"] != "draft":
            raise InvalidState("只有草稿状态的作业可以补充故障证据")
        evidence_id = identifier(evidence_id, "evidence_id")
        observed_at = utc_text_field(observed_at, "observed_at")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO fault_evidence(evidence_id,job_id,kind,summary,content_sha256,observed_at,source,"
                    "attached_by,attached_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        evidence_id,
                        job_id,
                        required_text(kind, "kind", 32),
                        required_text(summary, "summary", 512),
                        sha256_text(content_sha256, "content_sha256"),
                        observed_at,
                        required_text(source, "source", 64),
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("job", job_id, "evidence.attached", actor_id, {"evidence_id": evidence_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("故障证据编号已经存在") from exc
        return {"evidence_id": evidence_id, "job_id": job_id}

    # ------------------------------------------------------------------
    # 作业版本冻结
    # ------------------------------------------------------------------

    def freeze_job(self, actor_id: str, job_id: str, plan_raw: Mapping[str, Any], expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "job.freeze")
        job = self.get_job(job_id)
        if job["state"] != "draft":
            raise InvalidState("作业已经冻结，不能重复冻结")
        plan = InterventionPlan.from_dict(plan_raw)
        now = self.clock.now()
        for person in plan.personnel:
            user = self.connection.execute(
                "SELECT user_id FROM intervention_users WHERE user_id=? AND active=1", (person.user_id,)
            ).fetchone()
            if user is None:
                raise ValidationFailed(f"人员 {person.user_id} 不存在或已停用")
            if parse_utc(person.valid_until) < now:
                raise ValidationFailed(f"人员 {person.user_id} 的资格 {person.qualification} 已过期")
        equipment_rows = []
        for equipment_id in plan.equipment_ids:
            row = self.connection.execute(
                "SELECT * FROM wellhead_equipment WHERE equipment_id=?", (equipment_id,)
            ).fetchone()
            if row is None:
                raise ValidationFailed(f"井口设备不存在: {equipment_id}")
            if row["well_id"] != job["well_id"]:
                raise ValidationFailed(f"井口设备 {equipment_id} 不属于作业井 {job['well_id']}")
            equipment_rows.append(row)
        evidence_rows = self.connection.execute(
            "SELECT evidence_id,content_sha256 FROM fault_evidence WHERE job_id=? ORDER BY evidence_id",
            (job_id,),
        ).fetchall()
        if not evidence_rows:
            raise ValidationFailed("冻结前至少需要一条故障证据")
        evidence_sha256 = content_digest([dict(row) for row in evidence_rows])
        plan_sha256 = hashlib.sha256(canonical_json(plan_raw).encode("utf-8")).hexdigest()
        version_no = 1
        frozen_at = self._now()
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "UPDATE intervention_jobs SET state='frozen',revision=revision+1,current_version_no=? "
                    "WHERE job_id=? AND state='draft' AND revision=?",
                    (version_no, job_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("作业不是当前草稿版本")
                self.connection.execute(
                    "INSERT INTO job_versions(job_id,version_no,plan_json,plan_sha256,evidence_sha256,frozen_by,frozen_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (job_id, version_no, canonical_json(plan_raw), plan_sha256, evidence_sha256, actor_id, frozen_at),
                )
                for row in equipment_rows:
                    self.connection.execute(
                        "INSERT INTO job_version_equipment(job_id,version_no,equipment_id,kind,serial_no,equipment_revision) "
                        "VALUES(?,?,?,?,?,?)",
                        (job_id, version_no, row["equipment_id"], row["kind"], row["serial_no"], row["revision"]),
                    )
                self._audit(
                    "job", job_id, "job.frozen", actor_id,
                    {"version_no": version_no, "plan_sha256": plan_sha256, "evidence_sha256": evidence_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("作业版本并发冲突") from exc
        return {
            "job_id": job_id,
            "state": "frozen",
            "version_no": version_no,
            "plan_sha256": plan_sha256,
            "evidence_sha256": evidence_sha256,
            "revision": expected_revision + 1,
        }

    def _version(self, job_id: str, version_no: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM job_versions WHERE job_id=? AND version_no=?", (job_id, version_no)
        ).fetchone()
        if row is None:
            raise NotFound("作业版本不存在")
        return row

    # ------------------------------------------------------------------
    # 顺序签署的里程碑
    # ------------------------------------------------------------------

    def _last_milestone(self, job_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM milestones WHERE job_id=? ORDER BY sequence DESC LIMIT 1", (job_id,)
        ).fetchone()

    def _sign_milestone(
        self,
        actor_id: str,
        job_id: str,
        kind: str,
        expected_revision: int,
        idempotency_key: str,
        note: str,
        extra: Mapping[str, Any],
        apply: Any = None,
    ) -> dict[str, Any]:
        user = self._require(actor_id, MILESTONE_PERMISSION[kind])
        idempotency_key = identifier(idempotency_key, "idempotency_key")
        note = required_text(note, "note", 512)
        request_digest = content_digest([{
            "job_id": job_id, "kind": kind, "expected_revision": expected_revision,
            "note": note, "extra": dict(extra),
        }])
        scope = f"milestone:{job_id}:{kind}"
        replay = self._idempotent_response(scope, idempotency_key, request_digest)
        if replay is not None:
            return replay
        job = self.get_job(job_id)
        from_states = MILESTONE_FROM_STATES[kind]
        if job["state"] not in from_states:
            raise InvalidState(f"作业状态 {job['state']} 不能确认 {kind}")
        version = self._version(job_id, job["current_version_no"])
        plan = InterventionPlan.from_dict(json.loads(version["plan_json"]))
        if kind in ("start", "resume"):
            now = self.clock.now()
            if not any(
                parse_utc(window.starts_at) <= now <= parse_utc(window.ends_at)
                for window in plan.sea_state_windows
            ):
                raise InvalidState("当前时间不在冻结的海况窗口内")
        last = self._last_milestone(job_id)
        if last is not None and last["actor_id"] == actor_id:
            raise Forbidden("相邻里程碑必须由不同职责的人员确认")
        sequence = 1 if last is None else last["sequence"] + 1
        previous_fact_hash = version["plan_sha256"] if last is None else last["fact_hash"]
        signed_at = self._now()
        fact_body = {
            "job_id": job_id,
            "version_no": version["version_no"],
            "sequence": sequence,
            "kind": kind,
            "actor_id": actor_id,
            "note": note,
            "extra": dict(extra),
            "signed_at": signed_at,
            "previous_fact_hash": previous_fact_hash,
        }
        fact_hash = hashlib.sha256(canonical_json(fact_body).encode("utf-8")).hexdigest()
        response = {
            "job_id": job_id,
            "kind": kind,
            "sequence": sequence,
            "state": MILESTONE_TO_STATE[kind],
            "revision": expected_revision + 1,
            "fact_hash": fact_hash,
            "signed_at": signed_at,
        }
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    f"UPDATE intervention_jobs SET state=?,revision=revision+1 "
                    f"WHERE job_id=? AND state IN ({','.join('?' * len(from_states))}) AND revision=?",
                    (MILESTONE_TO_STATE[kind], job_id, *from_states, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("作业已被他人变更，请刷新后重试")
                self.connection.execute(
                    "INSERT INTO milestones(job_id,sequence,kind,actor_id,actor_role,note,idempotency_key,"
                    "previous_fact_hash,fact_hash,signed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        job_id, sequence, kind, actor_id, user["role"], note, idempotency_key,
                        previous_fact_hash, fact_hash, signed_at,
                    ),
                )
                if apply is not None:
                    extra_response = apply(job, version, plan)
                    if extra_response:
                        response.update(extra_response)
                self._store_idempotent(scope, idempotency_key, request_digest, response)
                self._audit(
                    "job", job_id, f"milestone.{kind}", actor_id,
                    {"sequence": sequence, "fact_hash": fact_hash, **dict(extra)},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("里程碑序号或幂等键并发冲突") from exc
        return response

    def confirm_isolation(
        self,
        actor_id: str,
        job_id: str,
        expected_revision: int,
        idempotency_key: str,
        barrier_confirmations: Sequence[Mapping[str, Any]],
        note: str,
    ) -> dict[str, Any]:
        confirmations = []
        for raw in barrier_confirmations:
            if not isinstance(raw, Mapping):
                raise ValidationFailed("barrier_confirmations 元素必须是对象")
            confirmations.append({
                "barrier_id": identifier(raw.get("barrier_id"), "barrier_confirmations.barrier_id"),
                "evidence_ref": required_text(raw.get("evidence_ref"), "barrier_confirmations.evidence_ref"),
                "evidence_sha256": sha256_text(raw.get("evidence_sha256"), "barrier_confirmations.evidence_sha256"),
            })
        if not confirmations:
            raise ValidationFailed("barrier_confirmations 不能为空")

        def apply(job: sqlite3.Row, version: sqlite3.Row, plan: InterventionPlan) -> None:
            by_barrier = {item["barrier_id"]: item for item in confirmations}
            missing = [item.barrier_id for item in plan.risk_barriers if item.barrier_id not in by_barrier]
            if missing:
                raise ValidationFailed(f"缺少屏障确认: {', '.join(missing)}")
            unknown = sorted(set(by_barrier) - {item.barrier_id for item in plan.risk_barriers})
            if unknown:
                raise ValidationFailed(f"确认包含未知屏障: {', '.join(unknown)}")
            names = {item.barrier_id: item.name for item in plan.risk_barriers}
            for barrier_id, item in by_barrier.items():
                self.connection.execute(
                    "INSERT INTO barrier_states(job_id,version_no,barrier_id,barrier_name,evidence_ref,"
                    "evidence_sha256,confirmed_by,confirmed_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        job_id, version["version_no"], barrier_id, names[barrier_id],
                        item["evidence_ref"], item["evidence_sha256"], actor_id, self._now(),
                    ),
                )

        return self._sign_milestone(
            actor_id, job_id, "isolation", expected_revision, idempotency_key, note,
            {"barrier_confirmations": confirmations}, apply,
        )

    def start_operation(
        self, actor_id: str, job_id: str, expected_revision: int, idempotency_key: str, note: str
    ) -> dict[str, Any]:
        return self._sign_milestone(actor_id, job_id, "start", expected_revision, idempotency_key, note, {})

    def pause_operation(
        self, actor_id: str, job_id: str, expected_revision: int, idempotency_key: str, reason: str
    ) -> dict[str, Any]:
        return self._sign_milestone(
            actor_id, job_id, "pause", expected_revision, idempotency_key, reason, {"reason": reason}
        )

    def resume_operation(
        self, actor_id: str, job_id: str, expected_revision: int, idempotency_key: str, note: str
    ) -> dict[str, Any]:
        return self._sign_milestone(actor_id, job_id, "resume", expected_revision, idempotency_key, note, {})

    def complete_operation(
        self, actor_id: str, job_id: str, expected_revision: int, idempotency_key: str, summary: str
    ) -> dict[str, Any]:
        def apply(job: sqlite3.Row, version: sqlite3.Row, plan: InterventionPlan) -> dict[str, Any]:
            released = self._release_all_resources(job_id, actor_id, "job-completed")
            self.connection.execute(
                "UPDATE wells SET state='normal' WHERE well_id=?", (job["well_id"],)
            )
            return {"released_resources": released}

        return self._sign_milestone(
            actor_id, job_id, "complete", expected_revision, idempotency_key, summary,
            {"summary": summary}, apply,
        )

    def cancel_job(self, actor_id: str, job_id: str, expected_revision: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "job.cancel")
        reason = required_text(reason, "reason", 512)
        job = self.get_job(job_id)
        if job["state"] == "in_progress":
            raise InvalidState("作业进行中，必须先暂停再取消")
        if job["state"] not in ("frozen", "isolated", "paused"):
            raise InvalidState(f"作业状态 {job['state']} 不能取消")
        version = self._version(job_id, job["current_version_no"])
        plan = InterventionPlan.from_dict(json.loads(version["plan_json"]))
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE intervention_jobs SET state='cancelled',revision=revision+1,cancelled_at=?,cancel_reason=? "
                "WHERE job_id=? AND state IN ('frozen','isolated','paused') AND revision=?",
                (now, reason, job_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("作业已被他人变更，请刷新后重试")
            released = self._release_all_resources(job_id, actor_id, "job-cancelled")
            for action in plan.recovery_plan:
                self.connection.execute(
                    "INSERT INTO recovery_actions(job_id,action_id,title,required_role) VALUES(?,?,?,?)",
                    (job_id, action.action_id, action.title, action.required_role),
                )
            self.connection.execute(
                "UPDATE wells SET state='anomaly' WHERE well_id=?", (job["well_id"],)
            )
            self._audit(
                "job", job_id, "job.cancelled", actor_id,
                {"reason": reason, "released_resources": released,
                 "recovery_actions": [action.action_id for action in plan.recovery_plan]},
            )
        return {
            "job_id": job_id,
            "state": "cancelled",
            "revision": expected_revision + 1,
            "released_resources": released,
            "recovery_actions": [action.action_id for action in plan.recovery_plan],
        }

    def complete_recovery_action(
        self, actor_id: str, job_id: str, action_id: str, note: str, evidence_ref: str
    ) -> dict[str, Any]:
        user = self._user(actor_id)
        row = self.connection.execute(
            "SELECT * FROM recovery_actions WHERE job_id=? AND action_id=?", (job_id, action_id)
        ).fetchone()
        if row is None:
            raise NotFound("恢复动作不存在")
        if user["role"] != row["required_role"]:
            raise Forbidden(f"恢复动作要求职责 {row['required_role']}")
        if row["state"] != "pending":
            raise InvalidState("恢复动作已经完成")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE recovery_actions SET state='done',note=?,evidence_ref=?,completed_by=?,completed_at=? "
                "WHERE job_id=? AND action_id=? AND state='pending'",
                (
                    required_text(note, "note", 512), required_text(evidence_ref, "evidence_ref"),
                    actor_id, self._now(), job_id, action_id,
                ),
            )
            self._audit("job", job_id, "recovery.completed", actor_id, {"action_id": action_id})
        return {"job_id": job_id, "action_id": action_id, "state": "done"}

    # ------------------------------------------------------------------
    # 资源承诺：同一资源同一时刻只能承诺给一个作业
    # ------------------------------------------------------------------

    def register_resource(self, actor_id: str, resource_id: str, kind: str, name: str) -> dict[str, Any]:
        self._require(actor_id, "resource.register")
        resource_id = identifier(resource_id, "resource_id")
        if kind not in RESOURCE_KINDS:
            raise ValidationFailed("kind 不是受支持的资源类型")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO resources(resource_id,kind,name,created_at) VALUES(?,?,?,?)",
                    (resource_id, kind, required_text(name, "name"), self._now()),
                )
                self._audit("resource", resource_id, "resource.registered", actor_id, {"kind": kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict("资源编号已经存在") from exc
        return {"resource_id": resource_id, "kind": kind}

    def commit_resource(self, actor_id: str, job_id: str, resource_id: str) -> dict[str, Any]:
        self._require(actor_id, "resource.commit")
        job = self.get_job(job_id)
        if job["state"] not in RESOURCE_ACTIVE_STATES:
            raise InvalidState(f"作业状态 {job['state']} 不能承诺资源")
        resource_id = identifier(resource_id, "resource_id")
        resource = self.connection.execute(
            "SELECT * FROM resources WHERE resource_id=?", (resource_id,)
        ).fetchone()
        if resource is None:
            raise NotFound("资源不存在")
        existing = self.connection.execute(
            "SELECT * FROM resource_commitments WHERE resource_id=? AND state='committed'", (resource_id,)
        ).fetchone()
        if existing is not None:
            if existing["job_id"] == job_id:
                return dict(existing)
            raise Conflict(f"资源 {resource_id} 已承诺给作业 {existing['job_id']}")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO resource_commitments(job_id,resource_id,job_version_no,committed_by,committed_at) "
                    "VALUES(?,?,?,?,?)",
                    (job_id, resource_id, job["current_version_no"], actor_id, self._now()),
                )
                commitment_id = int(cursor.lastrowid)
                self._audit(
                    "job", job_id, "resource.committed", actor_id,
                    {"resource_id": resource_id, "commitment_id": commitment_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"资源 {resource_id} 已被其他作业承诺") from exc
        row = self.connection.execute(
            "SELECT * FROM resource_commitments WHERE commitment_id=?", (commitment_id,)
        ).fetchone()
        return dict(row)

    def release_resource(self, actor_id: str, job_id: str, resource_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "resource.release")
        row = self.connection.execute(
            "SELECT * FROM resource_commitments WHERE job_id=? AND resource_id=? AND state='committed'",
            (job_id, identifier(resource_id, "resource_id")),
        ).fetchone()
        if row is None:
            raise InvalidState("资源当前未承诺给该作业")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE resource_commitments SET state='released',released_by=?,released_at=?,release_reason=? "
                "WHERE commitment_id=? AND state='committed'",
                (actor_id, self._now(), required_text(reason, "reason", 256), row["commitment_id"]),
            )
            self._audit("job", job_id, "resource.released", actor_id, {"resource_id": resource_id, "reason": reason})
        return {"job_id": job_id, "resource_id": resource_id, "state": "released"}

    def _release_all_resources(self, job_id: str, actor_id: str, reason: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT commitment_id,resource_id FROM resource_commitments WHERE job_id=? AND state='committed'",
            (job_id,),
        ).fetchall()
        now = self._now()
        released = []
        for row in rows:
            self.connection.execute(
                "UPDATE resource_commitments SET state='released',released_by=?,released_at=?,release_reason=? "
                "WHERE commitment_id=?",
                (actor_id, now, reason, row["commitment_id"]),
            )
            released.append(row["resource_id"])
        return released

    # ------------------------------------------------------------------
    # 遥测回调：幂等、迟到不可覆盖已签署事实
    # ------------------------------------------------------------------

    def record_telemetry(
        self,
        actor_id: str,
        well_id: str,
        metric: str,
        value: object,
        unit: str,
        observed_at: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "telemetry.write")
        well_id = identifier(well_id, "well_id")
        if self.connection.execute("SELECT 1 FROM wells WHERE well_id=?", (well_id,)).fetchone() is None:
            raise NotFound("井不存在")
        metric = required_text(metric, "metric", 64)
        unit = required_text(unit, "unit", 16)
        value_text = str(decimal_value(value, "value"))
        observed_at = utc_text_field(observed_at, "observed_at")
        idempotency_key = identifier(idempotency_key, "idempotency_key")
        request_digest = content_digest([{
            "well_id": well_id, "metric": metric, "value": value_text,
            "unit": unit, "observed_at": observed_at,
        }])
        scope = f"telemetry:{well_id}"
        replay = self._idempotent_response(scope, idempotency_key, request_digest)
        if replay is not None:
            return replay
        observed = parse_utc(observed_at)
        late_reason = None
        current = self.connection.execute(
            "SELECT observed_at FROM well_current_state WHERE well_id=? AND metric=?", (well_id, metric)
        ).fetchone()
        if current is not None and observed <= parse_utc(current["observed_at"]):
            late_reason = "观测时间不晚于已应用的最新遥测"
        if late_reason is None:
            horizon = self.connection.execute(
                "SELECT max(m.signed_at) horizon FROM milestones m "
                "JOIN intervention_jobs j ON j.job_id=m.job_id WHERE j.well_id=?",
                (well_id,),
            ).fetchone()["horizon"]
            if horizon is not None and observed < parse_utc(horizon):
                late_reason = "观测时间早于已签署的里程碑事实"
        late = late_reason is not None
        recorded_at = self._now()
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO telemetry_records(well_id,metric,value,unit,observed_at,recorded_at,recorded_by,"
                    "late,late_reason,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        well_id, metric, value_text, unit, observed_at, recorded_at, actor_id,
                        1 if late else 0, late_reason, idempotency_key,
                    ),
                )
                telemetry_id = int(cursor.lastrowid)
                if not late:
                    self.connection.execute(
                        "INSERT INTO well_current_state(well_id,metric,value,unit,observed_at,updated_at) "
                        "VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(well_id,metric) DO UPDATE SET value=excluded.value,unit=excluded.unit,"
                        "observed_at=excluded.observed_at,updated_at=excluded.updated_at",
                        (well_id, metric, value_text, unit, observed_at, recorded_at),
                    )
                response = {
                    "telemetry_id": telemetry_id,
                    "well_id": well_id,
                    "metric": metric,
                    "observed_at": observed_at,
                    "late": late,
                    "late_reason": late_reason,
                    "applied": not late,
                }
                self._store_idempotent(scope, idempotency_key, request_digest, response)
                self._audit(
                    "well", well_id, "telemetry.recorded", actor_id,
                    {"telemetry_id": telemetry_id, "metric": metric, "late": late},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("遥测幂等键并发冲突") from exc
        return response

    # ------------------------------------------------------------------
    # 管理还原视图与审计链
    # ------------------------------------------------------------------

    def job_report(self, actor_id: str, job_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        job = self.get_job(job_id)
        version = None
        equipment_snapshot: list[dict[str, Any]] = []
        plan: dict[str, Any] | None = None
        if job["current_version_no"] is not None:
            version_row = self._version(job_id, job["current_version_no"])
            plan = json.loads(version_row["plan_json"])
            version = {
                "version_no": version_row["version_no"],
                "plan_sha256": version_row["plan_sha256"],
                "evidence_sha256": version_row["evidence_sha256"],
                "frozen_by": version_row["frozen_by"],
                "frozen_at": version_row["frozen_at"],
                "plan": plan,
            }
            equipment_snapshot = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM job_version_equipment WHERE job_id=? AND version_no=? ORDER BY equipment_id",
                    (job_id, version_row["version_no"]),
                ).fetchall()
            ]
        evidence = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM fault_evidence WHERE job_id=? ORDER BY evidence_id", (job_id,)
            ).fetchall()
        ]
        barriers = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM barrier_states WHERE job_id=? ORDER BY barrier_id", (job_id,)
            ).fetchall()
        ]
        milestones = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM milestones WHERE job_id=? ORDER BY sequence", (job_id,)
            ).fetchall()
        ]
        commitments = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM resource_commitments WHERE job_id=? ORDER BY commitment_id", (job_id,)
            ).fetchall()
        ]
        recovery = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM recovery_actions WHERE job_id=? ORDER BY action_id", (job_id,)
            ).fetchall()
        ]
        telemetry = self.connection.execute(
            "SELECT count(*) total,coalesce(sum(late),0) late FROM telemetry_records t "
            "JOIN intervention_jobs j ON j.well_id=t.well_id WHERE j.job_id=?",
            (job_id,),
        ).fetchone()
        last = milestones[-1] if milestones else None
        holder = None
        if last is not None:
            holder = {"actor_id": last["actor_id"], "role": last["actor_role"], "since": last["signed_at"]}
        elif version is not None:
            holder = {"actor_id": version["frozen_by"], "role": "planner", "since": version["frozen_at"]}
        responsibility = {
            "current_holder": holder,
            "next_required_duty": NEXT_DUTY.get(job["state"]),
        }
        return {
            "job": job,
            "version": version,
            "equipment_snapshot": equipment_snapshot,
            "evidence": evidence,
            "barriers": barriers,
            "milestones": milestones,
            "current_responsibility": responsibility,
            "resources": commitments,
            "recovery_actions": recovery,
            "pending_recovery_actions": [item["action_id"] for item in recovery if item["state"] == "pending"],
            "telemetry": {"total": telemetry["total"], "late": telemetry["late"]},
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
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
