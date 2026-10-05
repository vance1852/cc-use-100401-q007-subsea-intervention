"""贯通水下干预全闭环的离线验收。

覆盖：作业版本冻结、迟到遥测隔离、屏障按闸门建立、隔离/开工/暂停/恢复/完工
顺序签署且职责分离、暂停期间并入迟到遥测冻结新版本、资源竞争唯一承诺、
取消后恢复动作闭环与资源释放、管理人员证据还原与哈希审计链。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import InterventionService


def base_package(job_window_end: str = "2026-10-05T18:00:00Z") -> dict[str, object]:
    return {
        "well_id": "well-a7",
        "title": "A7 井口采油树压力异常干预",
        "trigger_evidence_id": "ev-pressure-1",
        "equipment": [
            {"equipment_id": "xt-a7", "well_id": "well-a7", "name": "A7 卧式采油树",
             "kind": "christmas-tree", "design_pressure_bar": "345"},
            {"equipment_id": "man-a7", "well_id": "well-a7", "name": "A7 管汇",
             "kind": "manifold", "design_pressure_bar": "300"},
        ],
        "evidence": [
            {"evidence_id": "ev-pressure-1", "kind": "pressure-telemetry", "equipment_id": "xt-a7",
             "observed_at": "2026-10-05T03:10:00Z", "received_at": "2026-10-05T03:12:00Z",
             "metric": "annulus_b_pressure_bar", "value": "286.4", "unit": "bar",
             "source_ref": "scada/xt-a7/2026-10-05T03:10Z"},
        ],
        "barriers": [
            {"barrier_id": "bar-primary", "kind": "primary", "description": "井下安全阀液压隔离",
             "equipment_id": "xt-a7", "verification_method": "ROV 读取阀位与下行压力归零"},
            {"barrier_id": "bar-secondary", "kind": "secondary", "description": "采油树翼阀机械隔离",
             "equipment_id": "xt-a7", "verification_method": "ROV 目视翼阀关闭并铅封"},
        ],
        "steps": [
            {"step_no": 1, "title": "ROV 巡检采油树", "instruction": "沿环空接口检查可见泄漏",
             "estimated_minutes": 45, "requires_rov": True, "requires_diving": False},
            {"step_no": 2, "title": "更换压力传感器", "instruction": "拆卸旧传感器并回装备件",
             "estimated_minutes": 90, "requires_rov": True, "requires_diving": True},
        ],
        "qualifications": [
            {"person_id": "rov-pilot-1", "display_name": "遥控作业驾驶员甲", "qualification": "rov-pilot",
             "certificate_ref": "CERT-ROV-001", "valid_until": "2027-12-31"},
            {"person_id": "iso-eng-1", "display_name": "隔离工程师乙", "qualification": "isolation-engineer",
             "certificate_ref": "CERT-ISO-002", "valid_until": "2027-06-30"},
            {"person_id": "dive-sup-1", "display_name": "潜水监督丙", "qualification": "diving-supervisor",
             "certificate_ref": "CERT-DIV-003", "valid_until": "2027-03-31"},
        ],
        "components": [
            {"component_id": "tool-rov", "resource_kind": "rov", "name": "重型工作级 ROV 一组",
             "resource_ref": "rov-01", "quantity": 1},
            {"component_id": "tool-dive", "resource_kind": "diving-spread", "name": "饱和潜水支持包",
             "resource_ref": "dive-01", "quantity": 1},
            {"component_id": "tool-sensor", "resource_kind": "spare-part", "name": "环空压力传感器备件",
             "resource_ref": "spare-pt-09", "quantity": 2},
            {"component_id": "tool-permit", "resource_kind": "isolation-permit", "name": "A7 隔离作业许可",
             "resource_ref": "permit-a7", "quantity": 1},
        ],
        "windows": [
            {"window_id": "win-morning", "opens_at": "2026-10-05T05:00:00Z",
             "closes_at": job_window_end, "max_wave_height_m": "2.5", "max_current_ms": "0.8",
             "max_wind_ms": "12", "forecast_ref": "metocean/wave-2026-10-05/am"},
        ],
        "recovery": [
            {"action_id": "rec-release-rov", "kind": "release-resource",
             "description": "作业结束后释放 ROV 一组给其他井口", "resource_ref": "rov-01",
             "owner_role": "intervention-supervisor"},
            {"action_id": "rec-release-dive", "kind": "stand-down",
             "description": "潜水员减压结束后撤站", "resource_ref": "dive-01",
             "owner_role": "diving-supervisor"},
            {"action_id": "rec-consume-spare", "kind": "consume-resource",
             "description": "核销更换下来的压力传感器备件并退库剩余备件", "resource_ref": "spare-pt-09",
             "owner_role": "intervention-supervisor"},
            {"action_id": "rec-remove-isolation", "kind": "remove-isolation",
             "description": "确认现场安全后解除隔离并归还许可", "resource_ref": "permit-a7",
             "owner_role": "isolation-engineer"},
        ],
    }


def _contending_package() -> dict[str, object]:
    """作业 B 的首版草稿：试图抢占 A 已承诺的 rov-01。"""

    return {
        "job_id": "job-b9",
        "idempotency_key": "create-job-b9-01",
        "well_id": "well-b9",
        "title": "B9 井口干预",
        "trigger_evidence_id": "ev-pressure-1",
        "equipment": [
            {"equipment_id": "xt-b9", "well_id": "well-b9", "name": "B9 采油树",
             "kind": "christmas-tree", "design_pressure_bar": "345"},
        ],
        "evidence": [{
            "evidence_id": "ev-pressure-1", "kind": "pressure-telemetry", "equipment_id": "xt-b9",
            "observed_at": "2026-10-05T04:00:00Z", "received_at": "2026-10-05T04:05:00Z",
            "metric": "annulus_b_pressure_bar", "value": "279.0", "unit": "bar",
            "source_ref": "scada/xt-b9/0400",
        }],
        "barriers": [
            {"barrier_id": "bar-primary", "kind": "primary", "description": "井下安全阀隔离",
             "equipment_id": "xt-b9", "verification_method": "压力归零"},
            {"barrier_id": "bar-secondary", "kind": "secondary", "description": "翼阀隔离",
             "equipment_id": "xt-b9", "verification_method": "ROV 目视"},
        ],
        "steps": [
            {"step_no": 1, "title": "ROV 巡检", "instruction": "检查 B9 采油树",
             "estimated_minutes": 40, "requires_rov": True, "requires_diving": False},
        ],
        "qualifications": [
            {"person_id": "rov-pilot-1", "display_name": "遥控作业驾驶员甲", "qualification": "rov-pilot",
             "certificate_ref": "CERT-ROV-001", "valid_until": "2027-12-31"},
            {"person_id": "iso-eng-1", "display_name": "隔离工程师乙", "qualification": "isolation-engineer",
             "certificate_ref": "CERT-ISO-002", "valid_until": "2027-06-30"},
        ],
        "components": [
            {"component_id": "tool-rov", "resource_kind": "rov", "name": "抢占 A7 的 ROV",
             "resource_ref": "rov-01", "quantity": 1},
        ],
        "windows": [
            {"window_id": "win-morning", "opens_at": "2026-10-05T05:00:00Z",
             "closes_at": "2026-10-05T18:00:00Z", "max_wave_height_m": "2.5", "max_current_ms": "0.8",
             "max_wind_ms": "12", "forecast_ref": "metocean/b9/am"},
        ],
        "recovery": [
            {"action_id": "rec-release-rov", "kind": "release-resource",
             "description": "取消后释放 ROV", "resource_ref": "rov-01",
             "owner_role": "intervention-supervisor"},
        ],
    }


def _resolved_package() -> dict[str, object]:
    """竞争失败后改用自有资源重新提交的草稿。"""

    package = _contending_package()
    package["components"] = [
        {"component_id": "tool-rov", "resource_kind": "rov", "name": "备用 ROV 二组",
         "resource_ref": "rov-02", "quantity": 1},
        {"component_id": "tool-permit", "resource_kind": "isolation-permit", "name": "B9 许可",
         "resource_ref": "permit-b9", "quantity": 1},
    ]
    package["recovery"] = [
        {"action_id": "rec-release-rov", "kind": "release-resource",
         "description": "取消后释放 ROV 二组", "resource_ref": "rov-02",
         "owner_role": "intervention-supervisor"},
        {"action_id": "rec-return-permit", "kind": "release-resource",
         "description": "归还 B9 许可", "resource_ref": "permit-b9",
         "owner_role": "isolation-engineer"},
    ]
    return package


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc))
    service = InterventionService(connection, clock)

    users = (
        ("iso", "隔离工程师", "isolation-engineer"),
        ("super", "干预监督", "intervention-supervisor"),
        ("marine", "海况监督", "marine-supervisor"),
        ("dive", "潜水监督", "diving-supervisor"),
        ("commander", "现场总指挥", "onscene-commander"),
        ("audit", "审计员", "auditor"),
    )
    for user_id, name, role in users:
        service.create_user(user_id, name, role)

    for ref, kind, name in (
        ("rov-01", "rov", "重型工作级 ROV 一组"),
        ("dive-01", "diving-spread", "饱和潜水支持包"),
        ("spare-pt-09", "spare-part", "环空压力传感器备件"),
        ("permit-a7", "isolation-permit", "A7 隔离作业许可"),
        ("rov-02", "rov", "备用 ROV 二组"),
        ("dive-02", "diving-spread", "应急潜水支持包"),
        ("permit-b9", "isolation-permit", "B9 隔离作业许可"),
    ):
        service.register_resource("super", ref, kind, name)

    # ---- 作业 A：完整闭环 + 暂停期间并入迟到遥测 ----
    package_a = dict(base_package())
    package_a["job_id"] = "job-a7"
    package_a["idempotency_key"] = "create-job-a7-01"
    created = service.create_job("super", package_a)
    assert created["state"] == "draft" and created["version"] == 0

    sealed = service.seal_job("super", "job-a7", 1, "seal-job-a7-01")
    # 重复回调：同一幂等键返回同一签署事实。
    sealed_replay = service.seal_job("super", "job-a7", 1, "seal-job-a7-01")
    assert sealed_replay == sealed
    assert sealed_replay["revision"] == 2
    version1 = service.job_version("job-a7", 1)
    assert version1["sha256_intact"] is True

    clock.advance(hours=1)
    # 密封后到达的迟到遥测不能覆盖版本 1，只能隔离待审。
    late = service.record_telemetry(
        "super", "job-a7", "xt-a7", "2026-10-05T03:40:00Z",
        "annulus_b_pressure_bar", "291.8", "bar",
        "scada/xt-a7/2026-10-05T03:40Z", "tel-a7-late-01",
    )
    assert late["status"] == "quarantined"
    frozen_evidence = [e["evidence_id"] for e in version1["frozen"]["evidence"]]
    assert "tel-1" not in frozen_evidence

    # ---- 作业 B 在 A 仍持有 rov-01 时创建并尝试密封：资源竞争必须失败 ----
    package_b = _contending_package()
    service.create_job("super", package_b)
    contention = None
    try:
        service.seal_job("super", "job-b9", 1, "seal-job-b9-attempt")
    except Exception as exc:  # noqa: BLE001 - 验收需要捕获竞争失败
        contention = str(exc)
    assert contention is not None and "job-a7" in contention
    # 竞争失败不能留下任何半承诺资源。
    assert connection.execute(
        "SELECT COUNT(*) AS n FROM resource_commitments WHERE job_id='job-b9'").fetchone()["n"] == 0
    # 重复回调同样失败且消息一致（没有产生新版本）。
    try:
        service.seal_job("super", "job-b9", 1, "seal-job-b9-attempt")
    except Exception as exc:  # noqa: BLE001
        assert str(exc) == contention

    # 隔离闸门：隔离工程师逐道建立屏障。
    isolation = service.confirm_isolation(
        "iso", "job-a7",
        [{"barrier_id": "bar-primary", "evidence_ref": "rov/downhole-valve/zero-pressure"},
         {"barrier_id": "bar-secondary", "evidence_ref": "rov/wing-valve/sealed-0912"}],
        "两道屏障均已现场验证", 2, "iso-job-a7-01",
    )
    assert isolation["state"] == "isolated"

    # 开工闸门需要最新海况读数且处于窗口限值内，由不同于隔离人的干预监督确认。
    service.record_sea_state("marine", "job-a7", "2026-10-05T07:00:00Z",
                             "1.8", "0.6", "9", "weather-a7-01")
    started = service.start_job("super", "job-a7", "win-morning", "海况满足，开工",
                                3, "start-job-a7-01")
    assert started["state"] == "active"

    clock.advance(hours=1)
    service.pause_job("marine", "job-a7", "海面涌浪增大，暂停人员暴露作业",
                      "暂停等待海况复核", 4, "pause-job-a7-01")

    # 暂停期间冻结新版本：并入已隔离的迟到遥测，并追加一条趋势证据。
    package_v2 = dict(base_package())
    package_v2["evidence"] = [
        *package_v2["evidence"],
        {"evidence_id": "ev-trend-2", "kind": "trend", "equipment_id": "xt-a7",
         "observed_at": "2026-10-05T04:30:00Z", "received_at": "2026-10-05T05:00:00Z",
         "metric": "pressure_rising_rate_bar_h", "value": "4.2", "unit": "bar/h",
         "source_ref": "trend/xt-a7/0430"},
    ]
    revised = service.revise_job(
        "super", "job-a7",
        dict(package_v2, idempotency_key="revise-job-a7-01"),
        [late["telemetry_id"]],
    )
    assert revised["version"] == 2
    assert service.late_telemetry("job-a7")[0]["status"] == "incorporated"
    v2_evidence = {e["evidence_id"] for e in service.job_version("job-a7", 2)["frozen"]["evidence"]}
    assert "tel-1" in {row["evidence_id"] for row in connection.execute(
        "SELECT evidence_id FROM job_evidence WHERE job_id='job-a7' AND version=2").fetchall()}
    assert "ev-trend-2" in v2_evidence
    # 已建立的屏障随版本保留。
    assert all(row["state"] == "established" for row in connection.execute(
        "SELECT state FROM barrier_states WHERE job_id='job-a7'").fetchall())

    service.record_sea_state("marine", "job-a7", "2026-10-05T08:00:00Z",
                             "2.0", "0.5", "10", "weather-a7-02")
    resumed = service.resume_job("super", "job-a7", "win-morning", "海况回落，恢复作业",
                                 6, "resume-job-a7-01")
    assert resumed["state"] == "active"

    clock.advance(hours=2)
    completed = service.complete_job("commander", "job-a7", "传感器更换完成，压力恢复正常",
                                     7, "complete-job-a7-01")
    assert completed["state"] == "completed"

    # 作业 A 的恢复闭环（备件核销动作同样释放占用）。
    rec_rov = service.complete_recovery_action("super", "job-a7", "rec-release-rov",
                                               "ROV 已返航释放", "rec-a7-01")
    rec_dive = service.complete_recovery_action("dive", "job-a7", "rec-release-dive",
                                                "潜水员已撤站", "rec-a7-02")
    rec_spare = service.complete_recovery_action("super", "job-a7", "rec-consume-spare",
                                                 "一只备件已装机，一只退库", "rec-a7-04")
    rec_iso = service.complete_recovery_action("iso", "job-a7", "rec-remove-isolation",
                                               "现场安全，解除两道屏障并归还许可", "rec-a7-03")
    assert rec_rov["released_resource"] == "rov-01"
    assert rec_spare["released_resource"] == "spare-pt-09"
    assert set(rec_iso["released_barriers"]) == {"bar-primary", "bar-secondary"}
    recovery_a = service.recovery_status("job-a7")
    assert recovery_a["pending"] == []

    # ---- 作业 B：竞争失败后改用自有资源重新提交草稿 → 密封 → 取消 → 恢复闭环 ----
    package_b2 = _resolved_package()
    service.update_draft("super", "job-b9", package_b2)
    sealed_b = service.seal_job("super", "job-b9", 1, "seal-job-b9-02")
    assert sealed_b["state"] == "sealed"
    cancelled_b = service.cancel_job("commander", "job-b9", "海况窗口提前关闭，取消作业",
                                     2, "cancel-job-b9-01")
    assert cancelled_b["pending_recovery_actions"] == 2
    service.complete_recovery_action("super", "job-b9", "rec-release-rov", "ROV 二组释放", "rec-b9-01")
    service.complete_recovery_action("iso", "job-b9", "rec-return-permit", "许可归还", "rec-b9-02")
    assert service.recovery_status("job-b9")["pending"] == []

    # ---- 管理人员最终还原 ----
    report_a = service.reconstruction("audit", "job-a7")
    audit = report_a["audit"]
    result = {
        "status": "ok",
        "workspace": workspace.name,
        "job_a": {
            "final_state": report_a["job"]["state"],
            "frozen_version": report_a["frozen_version"]["version"],
            "signoffs": [
                {"gate": row["gate"], "signer": row["signer_id"], "kind": row["signoff_kind"]}
                for row in report_a["signoffs"]
            ],
            "barriers": [
                {"barrier_id": row["barrier_id"], "state": row["state"],
                 "evidence_ref": row["evidence_ref"], "established_by": row["established_by"]}
                for row in report_a["barriers"]
            ],
            "resources_still_held": report_a["resources"]["still_held"],
            "outstanding_recovery": report_a["outstanding_recovery"],
            "late_telemetry": [
                {"telemetry_id": row["telemetry_id"], "status": row["status"],
                 "incorporated_version": row["incorporated_version"]}
                for row in report_a["late_telemetry"]
            ],
        },
        "job_b": {
            "final_state": service.job("job-b9")["state"],
            "contention_message": contention,
            "pending_recovery": service.recovery_status("job-b9")["pending"],
        },
        "audit": audit,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行水下干预闭环离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
