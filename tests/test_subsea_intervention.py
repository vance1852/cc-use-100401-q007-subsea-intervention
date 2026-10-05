from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from subsea_intervention.api import JsonApplication
from subsea_intervention.clock import FrozenClock
from subsea_intervention.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from subsea_intervention.service import InterventionService

from subsea_fixtures import base_package, contending_package, resolved_package, sea_ok


class InterventionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc))
        self.service = InterventionService(self.connection, self.clock)
        for user_id, name, role in (
            ("iso", "隔离工程师", "isolation-engineer"),
            ("super", "干预监督", "intervention-supervisor"),
            ("marine", "海况监督", "marine-supervisor"),
            ("dive", "潜水监督", "diving-supervisor"),
            ("commander", "现场总指挥", "onscene-commander"),
            ("audit", "审计员", "auditor"),
        ):
            self.service.create_user(user_id, name, role)
        for ref, kind in (("rov-01", "rov"), ("dive-01", "diving-spread"),
                          ("spare-pt-09", "spare-part"), ("permit-a7", "isolation-permit"),
                          ("rov-02", "rov"), ("permit-b9", "isolation-permit")):
            self.service.register_resource("super", ref, kind, ref)

    def tearDown(self) -> None:
        self.connection.close()

    def create_seal_a(self, package=None) -> dict:
        package = package or base_package()
        self.service.create_job("super", package)
        return self.service.seal_job("super", "job-a7", 1, "seal-a")

    def open_barriers(self, expected_revision: int = 2) -> dict:
        return self.service.confirm_isolation(
            "iso", "job-a7",
            [{"barrier_id": "bar-primary", "evidence_ref": "ev-p"},
             {"barrier_id": "bar-secondary", "evidence_ref": "ev-s"}],
            "屏障建立", expected_revision, "iso-a",
        )

    def start_a(self, expected_revision: int = 3) -> dict:
        self.service.record_sea_state("marine", "job-a7", "2026-10-05T06:30:00Z",
                                      **sea_ok(), idempotency_key="w1")
        return self.service.start_job("super", "job-a7", "win-morning", "开工",
                                      expected_revision, "start-a")


class FrozenVersionTests(InterventionTestBase):
    def test_eight_evidence_categories_freeze_with_sha256(self) -> None:
        sealed = self.create_seal_a()
        self.assertEqual((sealed["state"], sealed["version"]), ("sealed", 1))
        version = self.service.job_version("job-a7", 1)
        self.assertTrue(version["sha256_intact"])
        frozen = version["frozen"]
        for category in ("equipment", "evidence", "barriers", "steps", "qualifications",
                         "components", "windows", "recovery"):
            self.assertTrue(frozen[category], category)

    def test_primary_and_secondary_barriers_required(self) -> None:
        package = base_package()
        package["barriers"] = [b for b in package["barriers"] if b["kind"] != "secondary"]
        package["job_id"] = "job-a7"
        package["idempotency_key"] = "k"
        with self.assertRaises(ValidationFailed):
            self.service.create_job("super", package)

    def test_expired_certificate_rejected(self) -> None:
        package = base_package()
        package["qualifications"][0]["valid_until"] = "2026-09-01"
        package["job_id"] = "job-a7"
        package["idempotency_key"] = "k"
        with self.assertRaises(ValidationFailed):
            self.service.create_job("super", package)

    def test_equipment_mismatch_rejected(self) -> None:
        package = base_package()
        package["evidence"][0]["equipment_id"] = "xt-other"
        package["job_id"] = "job-a7"
        package["idempotency_key"] = "k"
        with self.assertRaises(ValidationFailed):
            self.service.create_job("super", package)

    def test_every_committed_resource_needs_release_recovery(self) -> None:
        package = base_package()
        package["recovery"] = [a for a in package["recovery"] if a["resource_ref"] != "dive-01"]
        package["idempotency_key"] = "k"
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.create_job("super", package)
        self.assertIn("dive-01", str(ctx.exception))

    def test_seal_is_optimistically_locked_by_revision(self) -> None:
        self.service.create_job("super", base_package())
        with self.assertRaises(InvalidState):
            self.service.seal_job("super", "job-a7", 99, "seal-stale")


class GateSequenceTests(InterventionTestBase):
    def test_gates_follow_fixed_order_with_distinct_responsibilities(self) -> None:
        self.create_seal_a()
        # 未隔离不能开工。
        with self.assertRaises(InvalidState):
            self.service.start_job("super", "job-a7", "win-morning", "x", 2, "k")
        # 干预监督无权确认隔离。
        with self.assertRaises(Forbidden):
            self.service.confirm_isolation(
                "super", "job-a7",
                [{"barrier_id": "bar-primary", "evidence_ref": "p"},
                 {"barrier_id": "bar-secondary", "evidence_ref": "s"}],
                "x", 2, "k")
        self.open_barriers()
        # 海况监督无权开工；且没有海况读数不能开工。
        with self.assertRaises(Forbidden):
            self.service.start_job("marine", "job-a7", "win-morning", "x", 3, "k")
        with self.assertRaises(InvalidState):
            self.service.start_job("super", "job-a7", "win-morning", "x", 3, "k")
        self.start_a()
        # 现场总指挥不能自己暂停（必须海况监督）。
        with self.assertRaises(Forbidden):
            self.service.pause_job("commander", "job-a7", "r", "x", 4, "k")
        self.service.pause_job("marine", "job-a7", "涌浪", "暂停", 4, "pause-a")
        # 未恢复不能再暂停。
        with self.assertRaises(InvalidState):
            self.service.pause_job("marine", "job-a7", "r", "x", 5, "k")
        # 海况不达标不能恢复。
        self.clock.advance(minutes=30)
        self.service.record_sea_state("marine", "job-a7", "2026-10-05T06:55:00Z",
                                      "3.4", "0.6", "9", "w-bad")
        with self.assertRaises(InvalidState):
            self.service.resume_job("super", "job-a7", "win-morning", "x", 5, "k")
        # 隔离工程师无权恢复。
        with self.assertRaises(Forbidden):
            self.service.resume_job("iso", "job-a7", "win-morning", "x", 5, "k2")
        self.service.record_sea_state("marine", "job-a7", "2026-10-05T07:00:00Z",
                                      "2.0", "0.5", "10", "w-ok")
        self.service.resume_job("super", "job-a7", "win-morning", "恢复", 5, "resume-a")
        # 干预监督不能自己完工。
        with self.assertRaises(Forbidden):
            self.service.complete_job("super", "job-a7", "x", 6, "k")
        completed = self.service.complete_job("commander", "job-a7", "完工", 6, "complete-a")
        self.assertEqual(completed["state"], "completed")
        jobs = self.service.job("job-a7")
        self.assertEqual([s["gate"] for s in jobs["signoffs"]],
                         ["isolation", "start", "pause", "resume", "complete"])

    def test_isolation_requires_every_barrier_evidence(self) -> None:
        self.create_seal_a()
        with self.assertRaises(InvalidState):
            self.service.confirm_isolation(
                "iso", "job-a7",
                [{"barrier_id": "bar-primary", "evidence_ref": "p"}],
                "只建一道", 2, "iso-partial")

    def test_stale_revision_rejected_at_gate(self) -> None:
        self.create_seal_a()
        with self.assertRaises(InvalidState):
            self.service.confirm_isolation(
                "iso", "job-a7",
                [{"barrier_id": "bar-primary", "evidence_ref": "p"},
                 {"barrier_id": "bar-secondary", "evidence_ref": "s"}],
                "旧版本", 42, "iso-stale")


class LateTelemetryTests(InterventionTestBase):
    def test_late_telemetry_never_overwrites_signed_version(self) -> None:
        self.create_seal_a()
        before = self.service.job_version("job-a7", 1)["frozen"]
        result = self.service.record_telemetry(
            "super", "job-a7", "xt-a7", "2026-10-05T03:40:00Z",
            "annulus_b_pressure_bar", "291.8", "bar", "scada/late", "tel-1")
        self.assertEqual(result["status"], "quarantined")
        after = self.service.job_version("job-a7", 1)["frozen"]
        self.assertEqual(before, after)
        rows = self.service.late_telemetry("job-a7")
        self.assertEqual(rows[0]["status"], "quarantined")
        # 设备不属于当前版本时拒收。
        with self.assertRaises(ValidationFailed):
            self.service.record_telemetry(
                "super", "job-a7", "xt-nope", "2026-10-05T03:41:00Z",
                "p", "1", "bar", "s", "tel-2")

    def test_quarantined_telemetry_incorporates_only_in_new_version(self) -> None:
        self.create_seal_a()
        self.open_barriers()
        self.start_a()
        self.service.pause_job("marine", "job-a7", "涌浪", "暂停", 4, "p")
        tel = self.service.record_telemetry(
            "super", "job-a7", "xt-a7", "2026-10-05T04:00:00Z",
            "p", "292", "bar", "scada/x", "tel-x")
        package = base_package()
        package["evidence"].append({
            "evidence_id": "ev-trend", "kind": "trend", "equipment_id": "xt-a7",
            "observed_at": "2026-10-05T04:30:00Z", "received_at": "2026-10-05T05:00:00Z",
            "metric": "rate", "value": "4.2", "unit": "bar/h", "source_ref": "trend/x",
        })
        package["idempotency_key"] = "rev-1"
        revised = self.service.revise_job("super", "job-a7", package, [tel["telemetry_id"]])
        self.assertEqual(revised["version"], 2)
        self.assertEqual(self.service.late_telemetry("job-a7")[0]["status"], "incorporated")
        v1 = self.service.job_version("job-a7", 1)["frozen"]
        self.assertNotIn("tel-1", [e["evidence_id"] for e in v1["evidence"]])
        v2 = self.service.job_version("job-a7", 2)["frozen"]
        self.assertIn("ev-trend", [e["evidence_id"] for e in v2["evidence"]])

    def test_revision_rejects_removing_established_barriers_and_done_recovery(self) -> None:
        self.create_seal_a()
        self.open_barriers()
        self.start_a()
        self.service.pause_job("marine", "job-a7", "涌浪", "暂停", 4, "p")
        package = base_package()
        package["barriers"] = [
            package["barriers"][0],
            {"barrier_id": "bar-secondary-new", "kind": "secondary",
             "description": "修订中替换的副屏障", "equipment_id": "xt-a7",
             "verification_method": "ROV 目视"},
        ]
        package["idempotency_key"] = "rev-bad"
        with self.assertRaises(Conflict):
            self.service.revise_job("super", "job-a7", package)

    def test_cannot_revise_active_job(self) -> None:
        self.create_seal_a()
        self.open_barriers()
        self.start_a()
        package = dict(base_package(), idempotency_key="rev-active")
        with self.assertRaises(InvalidState):
            self.service.revise_job("super", "job-a7", package)


class IdempotencyTests(InterventionTestBase):
    def test_duplicate_callbacks_replay_same_fact(self) -> None:
        package = base_package()
        first = self.service.create_job("super", package)
        second = self.service.create_job("super", package)
        self.assertEqual(first, second)
        sealed = self.service.seal_job("super", "job-a7", 1, "seal-x")
        replay = self.service.seal_job("super", "job-a7", 1, "seal-x")
        self.assertEqual(replay, sealed)
        tel1 = self.service.record_telemetry(
            "super", "job-a7", "xt-a7", "2026-10-05T03:40:00Z",
            "p", "1", "bar", "s", "tel-k")
        tel2 = self.service.record_telemetry(
            "super", "job-a7", "xt-a7", "2026-10-05T03:40:00Z",
            "p", "1", "bar", "s", "tel-k")
        self.assertEqual(tel1["telemetry_id"], tel2["telemetry_id"])

    def test_same_key_different_payload_conflicts(self) -> None:
        self.service.create_job("super", base_package())
        other = base_package()
        other["title"] = "被篡改的标题"
        with self.assertRaises(Conflict):
            self.service.create_job("super", other)


class ResourceContentionTests(InterventionTestBase):
    def test_only_one_job_commits_contended_resource(self) -> None:
        sealed_a = self.create_seal_a()
        self.assertEqual(sealed_a["state"], "sealed")
        self.service.create_job("super", contending_package())
        with self.assertRaises(Conflict) as ctx:
            self.service.seal_job("super", "job-b9", 1, "seal-b")
        self.assertIn("job-a7", str(ctx.exception))
        # 竞争失败不留半承诺。
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) AS n FROM resource_commitments WHERE job_id='job-b9'").fetchone()["n"],
            0)
        # A 释放资源后 B 的资源可用。
        self.open_barriers()
        self.start_a()
        self.service.complete_job("commander", "job-a7", "完工", 4, "done-a")
        for action_id, actor in (("rec-release-rov", "super"), ("rec-release-dive", "dive"),
                                 ("rec-consume-spare", "super"), ("rec-remove-isolation", "iso")):
            self.service.complete_recovery_action(actor, "job-a7", action_id, "闭环", f"ra-{action_id}")
        self.service.update_draft("super", "job-b9", resolved_package())
        self.service.seal_job("super", "job-b9", 1, "seal-b2")
        held = {r["resource_ref"] for r in self.service.resources_status("job-b9") if r["state"] == "committed"}
        self.assertEqual(held, {"rov-02", "permit-b9"})


class CancellationRecoveryTests(InterventionTestBase):
    def _cancel_b(self) -> dict:
        self.service.create_job("super", resolved_package())
        self.service.seal_job("super", "job-b9", 1, "seal-b")
        return self.service.cancel_job("commander", "job-b9", "窗口关闭", 2, "cancel-b")

    def test_cancel_leaves_outstanding_recovery_for_responsibility_owners(self) -> None:
        cancelled = self._cancel_b()
        self.assertEqual(cancelled["pending_recovery_actions"], 2)
        status = self.service.recovery_status("job-b9")
        self.assertEqual(sorted(status["pending"]), ["rec-release-rov", "rec-return-permit"])
        # 作业进行中不能闭环恢复动作。
        # 错误责任角色不能闭环 ROV 释放。
        with self.assertRaises(Forbidden):
            self.service.complete_recovery_action("iso", "job-b9", "rec-release-rov", "x", "k")
        first = self.service.complete_recovery_action(
            "super", "job-b9", "rec-release-rov", "释放", "k1")
        self.assertEqual(first["released_resource"], "rov-02")
        # 重复闭环返回同一结果（幂等重放）；不同幂等键被拒绝。
        replay = self.service.complete_recovery_action(
            "super", "job-b9", "rec-release-rov", "释放", "k1")
        self.assertEqual(replay, first)
        with self.assertRaises(InvalidState):
            self.service.complete_recovery_action(
                "super", "job-b9", "rec-release-rov", "释放", "k-other")
        self.service.complete_recovery_action(
            "iso", "job-b9", "rec-return-permit", "归还", "k2")
        self.assertEqual(self.service.recovery_status("job-b9")["pending"], [])
        held = self.connection.execute(
            "SELECT COUNT(*) AS n FROM active_resource_commitments WHERE job_id='job-b9'").fetchone()["n"]
        self.assertEqual(held, 0)

    def test_cancel_requires_commander(self) -> None:
        self.service.create_job("super", resolved_package())
        self.service.seal_job("super", "job-b9", 1, "seal-b")
        with self.assertRaises(Forbidden):
            self.service.cancel_job("super", "job-b9", "x", 2, "k")

    def test_remove_isolation_action_releases_barriers(self) -> None:
        self.create_seal_a()
        self.open_barriers()
        self.start_a()
        self.service.complete_job("commander", "job-a7", "完工", 4, "done")
        self.service.complete_recovery_action("super", "job-a7", "rec-release-rov", "x", "r1")
        self.service.complete_recovery_action("dive", "job-a7", "rec-release-dive", "x", "r2")
        self.service.complete_recovery_action("super", "job-a7", "rec-consume-spare", "x", "r3")
        result = self.service.complete_recovery_action(
            "iso", "job-a7", "rec-remove-isolation", "解除隔离", "r4")
        self.assertEqual(set(result["released_barriers"]), {"bar-primary", "bar-secondary"})
        states = {row["barrier_id"]: row["state"] for row in self.service.barrier_evidence("job-a7")}
        self.assertEqual(set(states.values()), {"released"})


class ReconstructionAuditTests(InterventionTestBase):
    def test_reconstruction_reproduces_barrier_evidence_responsibility_and_open_actions(self) -> None:
        self.create_seal_a()
        self.open_barriers()
        self.start_a()
        self.service.cancel_job("commander", "job-a7", "取消", 4, "cancel")
        report = self.service.reconstruction("commander", "job-a7")
        self.assertEqual(report["current_responsible"]["user_id"], "commander")
        self.assertEqual(len(report["barriers"]), 2)
        self.assertTrue(all(row["established_by"] == "iso" for row in report["barriers"]))
        self.assertEqual(len(report["outstanding_recovery"]), 4)
        self.assertEqual(len(report["resources"]["still_held"]), 4)
        self.assertTrue(report["audit"]["valid"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.create_seal_a()
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute(
            "UPDATE intervention_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])


class ApiBoundaryTests(InterventionTestBase):
    def test_health_and_actor_header(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        response = app.handle("POST", "/jobs", {}, b"{}")
        self.assertEqual(response.status, 422)

    def test_full_flow_over_http_handler(self) -> None:
        app = JsonApplication(self.service)
        package = base_package()
        created = app.handle("POST", "/jobs", {"X-Actor-Id": "super"},
                             __import__("json").dumps(package).encode())
        self.assertEqual(created.status, 201)
        sealed = app.handle(
            "POST", "/jobs/job-a7/seal", {"X-Actor-Id": "super", "Content-Type": "application/json"},
            b'{"expected_revision":1,"idempotency_key":"s"}')
        self.assertEqual(sealed.status, 200)
        report = app.handle("GET", "/jobs/job-a7/reconstruction", {"X-Actor-Id": "audit"})
        self.assertEqual(report.status, 200)
        self.assertEqual(report.body["frozen_version"]["version"], 1)


if __name__ == "__main__":
    unittest.main()
