from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from subsea_intervention.acceptance import run as acceptance_run
from subsea_intervention.api import JsonApplication
from subsea_intervention.clock import FrozenClock
from subsea_intervention.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from subsea_intervention.models import InterventionPlan
from subsea_intervention.service import InterventionService


PLAN = {
    "wellhead_equipment": ["xt-1", "valve-1"],
    "risk_barriers": [
        {"barrier_id": "b1", "name": "主生产阀关闭", "kind": "mechanical", "verification": "阀位与压力双确认"},
        {"barrier_id": "b2", "name": "井下安全阀关闭", "kind": "hydraulic", "verification": "控制管线泄压"},
    ],
    "operation_steps": [
        {"sequence": 1, "title": "建立隔离", "required_role": "isolation_officer"},
        {"sequence": 2, "title": "ROV 检查", "required_role": "supervisor"},
    ],
    "personnel": [
        {"user_id": "iso", "qualification": "IWCF-L4", "valid_until": "2027-01-01T00:00:00Z"},
    ],
    "tool_components": [
        {"component_id": "tool-1", "description": "扭矩工具", "quantity": 1},
    ],
    "sea_state_windows": [
        {"starts_at": "2026-10-05T00:00:00Z", "ends_at": "2026-10-06T00:00:00Z",
         "max_wave_height_m": "2.5", "max_current_knots": "1.2"},
    ],
    "recovery_plan": [
        {"action_id": "ra-1", "title": "恢复主屏障", "required_role": "isolation_officer"},
    ],
}

BARRIERS = [
    {"barrier_id": "b1", "evidence_ref": "photo://b1", "evidence_sha256": "a" * 64},
    {"barrier_id": "b2", "evidence_ref": "photo://b2", "evidence_sha256": "b" * 64},
]


class InterventionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))
        self.service = InterventionService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"), ("iso", "isolation_officer"), ("sup1", "supervisor"),
            ("sup2", "supervisor"), ("res", "resource_controller"), ("tel", "telemetry"),
            ("aud", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_well("plan", "well-1", "A-7 井", "流花油田", "1450")
        self.service.register_equipment("plan", "well-1", "xt-1", "xt", "XT-001")
        self.service.register_equipment("plan", "well-1", "valve-1", "tree-valve", "TV-114")
        self.service.register_resource("res", "rov-1", "rov", "工作级 ROV")
        self.service.register_resource("res", "dsv-1", "dive-support", "潜水支持船")
        self.service.create_job("plan", "job-1", "well-1", "压力异常干预", "井口压力异常上升", 10)
        self.service.attach_evidence("plan", "job-1", "ev-1", "pressure-log", "压力趋势", "c" * 64, "2026-10-05T05:00:00Z", "scada")

    def tearDown(self) -> None:
        self.connection.close()

    def freeze(self) -> dict:
        return self.service.freeze_job("plan", "job-1", PLAN, 1)

    def isolate(self) -> dict:
        return self.service.confirm_isolation("iso", "job-1", 2, "iso-key", BARRIERS, "双屏障建立")

    def test_full_closed_loop_releases_resources_on_completion(self) -> None:
        self.freeze()
        self.service.commit_resource("res", "job-1", "rov-1")
        self.service.commit_resource("res", "job-1", "dsv-1")
        self.isolate()
        self.service.start_operation("sup1", "job-1", 3, "start-key", "开工")
        self.service.pause_operation("sup2", "job-1", 4, "pause-key", "海流超限")
        self.service.resume_operation("sup1", "job-1", 5, "resume-key", "恢复")
        completed = self.service.complete_operation("sup2", "job-1", 6, "complete-key", "完工")
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(sorted(completed["released_resources"]), ["dsv-1", "rov-1"])
        job = self.service.get_job("job-1")
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["revision"], 7)
        rows = self.connection.execute(
            "SELECT state FROM resource_commitments WHERE job_id='job-1'"
        ).fetchall()
        self.assertEqual({row[0] for row in rows}, {"released"})
        well = self.connection.execute("SELECT state FROM wells WHERE well_id='well-1'").fetchone()
        self.assertEqual(well[0], "normal")

    def test_milestones_must_follow_order(self) -> None:
        self.freeze()
        with self.assertRaises(InvalidState):
            self.service.start_operation("sup1", "job-1", 2, "start-key", "未隔离先开工")
        self.isolate()
        with self.assertRaises(InvalidState):
            self.service.pause_operation("sup1", "job-1", 3, "pause-key", "未开工先暂停")
        with self.assertRaises(InvalidState):
            self.service.complete_operation("sup1", "job-1", 3, "complete-key", "未开工先完工")

    def test_consecutive_milestones_require_different_duties(self) -> None:
        self.freeze()
        self.isolate()
        self.service.start_operation("sup1", "job-1", 3, "start-key", "开工")
        with self.assertRaises(Forbidden):
            self.service.pause_operation("sup1", "job-1", 4, "pause-key", "同一人连续确认")
        self.service.pause_operation("sup2", "job-1", 4, "pause-key", "换人暂停")
        with self.assertRaises(Forbidden):
            self.service.resume_operation("sup2", "job-1", 5, "resume-key", "同一人连续确认")

    def test_role_separation_for_milestones(self) -> None:
        self.freeze()
        with self.assertRaises(Forbidden):
            self.service.confirm_isolation("plan", "job-1", 2, "iso-key", BARRIERS, "计划员越权")
        with self.assertRaises(Forbidden):
            self.service.start_operation("iso", "job-1", 2, "start-key", "隔离员越权")

    def test_isolation_requires_every_barrier_with_evidence(self) -> None:
        self.freeze()
        with self.assertRaises(ValidationFailed):
            self.service.confirm_isolation("iso", "job-1", 2, "iso-key", BARRIERS[:1], "缺少屏障")
        with self.assertRaises(ValidationFailed):
            self.service.confirm_isolation(
                "iso", "job-1", 2, "iso-key",
                BARRIERS + [{"barrier_id": "b9", "evidence_ref": "x", "evidence_sha256": "d" * 64}],
                "未知屏障",
            )

    def test_milestone_replay_is_idempotent_and_conflict_on_different_payload(self) -> None:
        self.freeze()
        first = self.isolate()
        second = self.service.confirm_isolation("iso", "job-1", 2, "iso-key", BARRIERS, "双屏障建立")
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.confirm_isolation("iso", "job-1", 2, "iso-key", BARRIERS, "不同备注")
        count = self.connection.execute("SELECT count(*) FROM milestones").fetchone()[0]
        self.assertEqual(count, 1)

    def test_optimistic_revision_guards_concurrent_confirmation(self) -> None:
        self.freeze()
        self.isolate()
        with self.assertRaises(InvalidState):
            self.service.start_operation("sup1", "job-1", 2, "start-key", "过期版本")

    def test_start_outside_sea_state_window_rejected(self) -> None:
        self.freeze()
        self.isolate()
        self.clock.advance(days=2)
        with self.assertRaises(InvalidState):
            self.service.start_operation("sup1", "job-1", 3, "start-key", "超出海况窗口")

    def test_late_telemetry_cannot_overwrite_signed_facts(self) -> None:
        self.freeze()
        applied = self.service.record_telemetry("tel", "well-1", "pressure", "38.4", "MPa", "2026-10-05T07:50:00Z", "tk-1")
        self.assertTrue(applied["applied"])
        self.isolate()
        signed_at = self.connection.execute(
            "SELECT signed_at,fact_hash FROM milestones WHERE job_id='job-1'"
        ).fetchone()
        late = self.service.record_telemetry("tel", "well-1", "pressure", "10.0", "MPa", "2026-10-05T07:00:00Z", "tk-2")
        self.assertTrue(late["late"])
        self.assertFalse(late["applied"])
        current = self.connection.execute(
            "SELECT value FROM well_current_state WHERE well_id='well-1' AND metric='pressure'"
        ).fetchone()
        self.assertEqual(current[0], "38.4")
        after = self.connection.execute(
            "SELECT signed_at,fact_hash FROM milestones WHERE job_id='job-1'"
        ).fetchone()
        self.assertEqual(tuple(signed_at), tuple(after))
        older_than_applied = self.service.record_telemetry("tel", "well-1", "pressure", "39.0", "MPa", "2026-10-05T07:40:00Z", "tk-3")
        self.assertTrue(older_than_applied["late"])

    def test_telemetry_callback_replay_is_idempotent(self) -> None:
        first = self.service.record_telemetry("tel", "well-1", "pressure", "38.4", "MPa", "2026-10-05T07:50:00Z", "tk-1")
        second = self.service.record_telemetry("tel", "well-1", "pressure", "38.4", "MPa", "2026-10-05T07:50:00Z", "tk-1")
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.record_telemetry("tel", "well-1", "pressure", "39.9", "MPa", "2026-10-05T07:50:00Z", "tk-1")
        count = self.connection.execute("SELECT count(*) FROM telemetry_records").fetchone()[0]
        self.assertEqual(count, 1)

    def test_resource_contention_has_single_winner(self) -> None:
        self.freeze()
        self.service.create_job("plan", "job-2", "well-1", "第二作业", "另一异常", 20)
        self.service.attach_evidence("plan", "job-2", "ev-2", "alarm", "报警记录", "e" * 64, "2026-10-05T06:00:00Z", "alarm")
        self.service.freeze_job("plan", "job-2", PLAN, 1)
        first = self.service.commit_resource("res", "job-1", "rov-1")
        replay = self.service.commit_resource("res", "job-1", "rov-1")
        self.assertEqual(first["commitment_id"], replay["commitment_id"])
        with self.assertRaises(Conflict):
            self.service.commit_resource("res", "job-2", "rov-1")
        self.service.release_resource("res", "job-1", "rov-1", "提前释放")
        won = self.service.commit_resource("res", "job-2", "rov-1")
        self.assertEqual(won["job_id"], "job-2")

    def test_cannot_commit_resource_before_freeze(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.commit_resource("res", "job-1", "rov-1")

    def test_cancellation_releases_resources_and_tracks_recovery(self) -> None:
        self.freeze()
        self.service.commit_resource("res", "job-1", "rov-1")
        self.isolate()
        with self.assertRaises(InvalidState):
            self.service.cancel_job("plan", "job-1", 2, "过期版本")
        cancelled = self.service.cancel_job("plan", "job-1", 3, "气象恶化取消")
        self.assertEqual(cancelled["released_resources"], ["rov-1"])
        self.assertEqual(cancelled["recovery_actions"], ["ra-1"])
        report = self.service.job_report("aud", "job-1")
        self.assertEqual(report["pending_recovery_actions"], ["ra-1"])
        self.assertEqual(report["resources"][0]["state"], "released")
        self.assertEqual(report["resources"][0]["release_reason"], "job-cancelled")
        with self.assertRaises(Forbidden):
            self.service.complete_recovery_action("sup1", "job-1", "ra-1", "越权恢复", "ref")
        done = self.service.complete_recovery_action("iso", "job-1", "ra-1", "已恢复并挂牌", "photo://recovered")
        self.assertEqual(done["state"], "done")
        with self.assertRaises(InvalidState):
            self.service.complete_recovery_action("iso", "job-1", "ra-1", "重复恢复", "ref")
        self.assertEqual(self.service.job_report("aud", "job-1")["pending_recovery_actions"], [])

    def test_cancel_requires_pause_when_in_progress(self) -> None:
        self.freeze()
        self.isolate()
        self.service.start_operation("sup1", "job-1", 3, "start-key", "开工")
        with self.assertRaises(InvalidState):
            self.service.cancel_job("plan", "job-1", 4, "进行中直接取消")
        self.service.pause_operation("sup2", "job-1", 4, "pause-key", "先暂停")
        cancelled = self.service.cancel_job("plan", "job-1", 5, "再取消")
        self.assertEqual(cancelled["state"], "cancelled")

    def test_frozen_version_is_immutable_and_snapshots_equipment(self) -> None:
        frozen = self.freeze()
        self.assertEqual(len(frozen["plan_sha256"]), 64)
        with self.assertRaises(InvalidState):
            self.service.freeze_job("plan", "job-1", PLAN, 2)
        with self.assertRaises(InvalidState):
            self.service.attach_evidence("plan", "job-1", "ev-2", "late", "冻结后补证据", "f" * 64, "2026-10-05T07:00:00Z", "rov")
        self.connection.execute("UPDATE wellhead_equipment SET revision=99 WHERE equipment_id='xt-1'")
        snapshot = self.connection.execute(
            "SELECT equipment_revision FROM job_version_equipment WHERE job_id='job-1' AND equipment_id='xt-1'"
        ).fetchone()
        self.assertEqual(snapshot[0], 1)

    def test_freeze_validates_personnel_equipment_and_evidence(self) -> None:
        expired = json.loads(json.dumps(PLAN))
        expired["personnel"][0]["valid_until"] = "2026-01-01T00:00:00Z"
        with self.assertRaises(ValidationFailed):
            self.service.freeze_job("plan", "job-1", expired, 1)
        foreign = json.loads(json.dumps(PLAN))
        foreign["wellhead_equipment"] = ["xt-1", "xt-elsewhere"]
        self.service.register_well("plan", "well-2", "B-2 井", "流花油田", "1380")
        self.service.register_equipment("plan", "well-2", "xt-elsewhere", "xt", "XT-002")
        with self.assertRaises(ValidationFailed):
            self.service.freeze_job("plan", "job-1", foreign, 1)
        self.service.create_job("plan", "job-3", "well-1", "无证据作业", "无证据", 30)
        with self.assertRaises(ValidationFailed):
            self.service.freeze_job("plan", "job-3", PLAN, 1)

    def test_report_restores_barriers_responsibility_and_recovery(self) -> None:
        self.freeze()
        self.isolate()
        self.service.start_operation("sup1", "job-1", 3, "start-key", "开工")
        report = self.service.job_report("aud", "job-1")
        self.assertEqual(len(report["barriers"]), 2)
        self.assertEqual(report["barriers"][0]["evidence_ref"], "photo://b1")
        self.assertEqual(report["barriers"][0]["confirmed_by"], "iso")
        self.assertEqual(report["current_responsibility"]["current_holder"]["actor_id"], "sup1")
        self.assertEqual(report["current_responsibility"]["next_required_duty"]["role"], "supervisor")
        self.assertEqual(report["version"]["plan"]["risk_barriers"][0]["barrier_id"], "b1")
        self.assertEqual(len(report["equipment_snapshot"]), 2)
        with self.assertRaises(Forbidden):
            self.service.job_report("plan", "job-1")

    def test_audit_chain_detects_tampering(self) -> None:
        self.freeze()
        self.assertTrue(self.service.audit_chain("aud")["valid"])
        self.connection.execute("UPDATE intervention_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("aud")["valid"])

    def test_fact_hash_chain_links_milestones_to_frozen_version(self) -> None:
        frozen = self.freeze()
        self.isolate()
        self.service.start_operation("sup1", "job-1", 3, "start-key", "开工")
        rows = self.connection.execute(
            "SELECT previous_fact_hash,fact_hash FROM milestones WHERE job_id='job-1' ORDER BY sequence"
        ).fetchall()
        self.assertEqual(rows[0][0], frozen["plan_sha256"])
        self.assertEqual(rows[1][0], rows[0][1])


class ContractTests(unittest.TestCase):
    def test_plan_requires_continuous_step_sequences(self) -> None:
        broken = json.loads(json.dumps(PLAN))
        broken["operation_steps"][1]["sequence"] = 5
        with self.assertRaises(ValidationFailed):
            InterventionPlan.from_dict(broken)

    def test_plan_rejects_duplicate_barriers_and_bad_window(self) -> None:
        duplicated = json.loads(json.dumps(PLAN))
        duplicated["risk_barriers"].append(duplicated["risk_barriers"][0])
        with self.assertRaises(ValidationFailed):
            InterventionPlan.from_dict(duplicated)
        window = json.loads(json.dumps(PLAN))
        window["sea_state_windows"][0]["ends_at"] = "2026-10-04T00:00:00Z"
        with self.assertRaises(ValidationFailed):
            InterventionPlan.from_dict(window)


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))
        self.app = JsonApplication(InterventionService(self.connection, self.clock))

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict, actor: str = "plan") -> tuple[int, dict]:
        response = self.app.handle(
            "POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode("utf-8")
        )
        return response.status, response.body

    def test_health_and_error_shape(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")
        response = self.app.handle("GET", "/nope", {"X-Actor-Id": "x"})
        self.assertEqual(response.status, 404)

    def test_http_closed_loop(self) -> None:
        self.post("/users", {"user_id": "plan", "display_name": "计划", "role": "planner"})
        self.post("/users", {"user_id": "iso", "display_name": "隔离", "role": "isolation_officer"})
        self.post("/users", {"user_id": "sup", "display_name": "监督", "role": "supervisor"})
        self.post("/users", {"user_id": "aud", "display_name": "审计", "role": "auditor"})
        status, _ = self.post("/wells", {"well_id": "well-1", "name": "A-7", "field_name": "流花", "water_depth_m": "1450"})
        self.assertEqual(status, 201)
        self.post("/equipment", {"well_id": "well-1", "equipment_id": "xt-1", "kind": "xt", "serial_no": "XT-001"})
        self.post("/equipment", {"well_id": "well-1", "equipment_id": "valve-1", "kind": "tree-valve", "serial_no": "TV-1"})
        status, _ = self.post("/jobs", {"job_id": "job-1", "well_id": "well-1", "title": "干预", "anomaly_summary": "压力异常"})
        self.assertEqual(status, 201)
        self.post("/jobs/job-1/evidence", {
            "evidence_id": "ev-1", "kind": "pressure-log", "summary": "趋势",
            "content_sha256": "a" * 64, "observed_at": "2026-10-05T05:00:00Z", "source": "scada",
        })
        status, body = self.post("/jobs/job-1/freeze", {"plan": PLAN, "expected_revision": 1})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "frozen")
        status, body = self.post("/jobs/job-1/isolation", {
            "expected_revision": 2, "idempotency_key": "iso-key", "barrier_confirmations": BARRIERS, "note": "隔离完成",
        }, actor="iso")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "isolated")
        status, body = self.post("/jobs/job-1/start", {
            "expected_revision": 3, "idempotency_key": "start-key", "note": "开工",
        }, actor="sup")
        self.assertEqual(status, 200)
        response = self.app.handle("GET", "/jobs/job-1/report", {"X-Actor-Id": "aud"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["job"]["state"], "in_progress")
        response = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "aud"})
        self.assertTrue(response.body["valid"])


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        from pathlib import Path

        result = acceptance_run(Path(__file__).resolve().parents[1])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["completed_job_state"], "completed")
        self.assertTrue(result["late_telemetry_flagged"])
        self.assertEqual(result["pending_recovery_actions"], ["recover-rov"])
        self.assertEqual(result["barrier_count"], 2)
        self.assertEqual(sorted(result["completed_released"]), ["dsv-1", "rov-1", "spare-seal-9"])
        self.assertEqual(sorted(result["cancelled_released"]), ["rov-1"])


if __name__ == "__main__":
    unittest.main()
