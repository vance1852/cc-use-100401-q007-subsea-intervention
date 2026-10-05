"""水下井干预闭环的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import InterventionService
from .storage import connect, inspect_schema


def _plan(equipment_ids: list[str], window_start: str, window_end: str) -> dict[str, object]:
    return {
        "wellhead_equipment": equipment_ids,
        "risk_barriers": [
            {"barrier_id": "barrier-1", "name": "主生产阀关闭并锁定", "kind": "mechanical", "verification": "阀位指示与压力双确认"},
            {"barrier_id": "barrier-2", "name": "井下安全阀关闭", "kind": "hydraulic", "verification": "控制管线泄压读数"},
        ],
        "operation_steps": [
            {"sequence": 1, "title": "建立隔离并泄压", "required_role": "isolation_officer"},
            {"sequence": 2, "title": "ROV 检查井口连接器", "required_role": "supervisor"},
        ],
        "personnel": [
            {"user_id": "iso-1", "qualification": "IWCF-L4", "valid_until": "2027-06-30T00:00:00Z"},
            {"user_id": "sup-1", "qualification": "ROV-SUP", "valid_until": "2027-03-31T00:00:00Z"},
        ],
        "tool_components": [
            {"component_id": "torque-tool-1", "description": "ROV 扭矩工具", "quantity": 1},
            {"component_id": "seal-kit-9", "description": "井口密封组件备件", "quantity": 2},
        ],
        "sea_state_windows": [
            {"starts_at": window_start, "ends_at": window_end, "max_wave_height_m": "2.5", "max_current_knots": "1.2"},
        ],
        "recovery_plan": [
            {"action_id": "recover-barrier", "title": "恢复主生产阀至安全状态", "required_role": "isolation_officer"},
            {"action_id": "recover-rov", "title": "回收 ROV 并解除潜水支持警戒", "required_role": "supervisor"},
        ],
    }


def run(workspace: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="subsea-intervention-") as temporary:
        database = Path(temporary) / "intervention.sqlite3"
        connection = connect(database)
        try:
            clock = FrozenClock(datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))
            service = InterventionService(connection, clock)
            service.create_user("plan-1", "干预计划工程师", "planner")
            service.create_user("iso-1", "隔离许可负责人", "isolation_officer")
            service.create_user("sup-1", "作业监督甲", "supervisor")
            service.create_user("sup-2", "作业监督乙", "supervisor")
            service.create_user("res-1", "资源调度员", "resource_controller")
            service.create_user("tel-1", "遥测网关", "telemetry")
            service.create_user("aud-1", "管理人员", "auditor")
            service.register_well("plan-1", "well-a", "流花 A-7 井", "流花油田", "1450")
            service.register_well("plan-1", "well-b", "流花 B-2 井", "流花油田", "1380")
            service.register_equipment("plan-1", "well-a", "xt-a01", "xt", "XT-2024-001")
            service.register_equipment("plan-1", "well-a", "valve-a02", "tree-valve", "TV-2024-114")
            service.register_equipment("plan-1", "well-b", "xt-b01", "xt", "XT-2024-002")
            service.register_equipment("plan-1", "well-b", "valve-b02", "tree-valve", "TV-2024-208")
            service.register_resource("res-1", "rov-1", "rov", "工作级 ROV 一号")
            service.register_resource("res-1", "dsv-1", "dive-support", "潜水支持船 海洋石油 286")
            service.register_resource("res-1", "spare-seal-9", "spare-part", "井口密封组件备件批次 9")

            # 作业一：完整闭环，从冻结到完工
            service.create_job("plan-1", "job-a", "well-a", "A-7 井压力异常干预", "井口压力 6 小时内上升 12%", 10)
            service.attach_evidence(
                "plan-1", "job-a", "ev-a1", "pressure-log", "井口压力趋势记录",
                "a" * 64, "2026-10-05T05:30:00Z", "scada-export",
            )
            service.freeze_job("plan-1", "job-a", _plan(["xt-a01", "valve-a02"], "2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z"), 1)
            service.commit_resource("res-1", "job-a", "rov-1")
            service.commit_resource("res-1", "job-a", "dsv-1")
            service.commit_resource("res-1", "job-a", "spare-seal-9")
            applied = service.record_telemetry(
                "tel-1", "well-a", "wellhead_pressure_mpa", "38.4", "MPa",
                "2026-10-05T07:55:00Z", "tel-key-1",
            )
            service.confirm_isolation(
                "iso-1", "job-a", 2, "iso-key-1",
                [
                    {"barrier_id": "barrier-1", "evidence_ref": "photo://xt-a01/valve-closed", "evidence_sha256": "b" * 64},
                    {"barrier_id": "barrier-2", "evidence_ref": "scada://scssv/closed", "evidence_sha256": "c" * 64},
                ],
                "双屏障建立，压力读数回零",
            )
            late = service.record_telemetry(
                "tel-1", "well-a", "wellhead_pressure_mpa", "37.9", "MPa",
                "2026-10-05T07:40:00Z", "tel-key-2",
            )
            service.start_operation("sup-1", "job-a", 3, "start-key-1", "海况窗口内开工")
            service.pause_operation("sup-2", "job-a", 4, "pause-key-1", "瞬时海流超限，暂停待命")
            service.resume_operation("sup-1", "job-a", 5, "resume-key-1", "海流回落，恢复作业")
            completed = service.complete_operation("sup-2", "job-a", 6, "complete-key-1", "连接器更换完成，压力恢复正常")

            # 作业二：冻结并隔离后取消，跟踪资源释放与恢复动作
            service.create_job("plan-1", "job-b", "well-b", "B-2 井压力异常排查", "井口压力波动超阈值", 20)
            service.attach_evidence(
                "plan-1", "job-b", "ev-b1", "alarm-record", "平台报警记录",
                "d" * 64, "2026-10-05T06:10:00Z", "alarm-system",
            )
            service.freeze_job("plan-1", "job-b", _plan(["xt-b01", "valve-b02"], "2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z"), 1)
            service.commit_resource("res-1", "job-b", "rov-1")
            service.confirm_isolation(
                "iso-1", "job-b", 2, "iso-key-b1",
                [
                    {"barrier_id": "barrier-1", "evidence_ref": "photo://xt-b01/valve-closed", "evidence_sha256": "e" * 64},
                    {"barrier_id": "barrier-2", "evidence_ref": "scada://b2/scssv", "evidence_sha256": "f" * 64},
                ],
                "B-2 井双屏障建立",
            )
            cancelled = service.cancel_job("plan-1", "job-b", 3, "气象预报恶化，取消本次干预")
            service.complete_recovery_action("iso-1", "job-b", "recover-barrier", "主生产阀已恢复并挂牌", "photo://xt-b01/recovered")
            report = service.job_report("aud-1", "job-b")
            chain = service.audit_chain("aud-1")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    if not chain["valid"]:
        raise RuntimeError("审计链校验失败")
    if late["applied"] or not applied["applied"]:
        raise RuntimeError("迟到遥测处理不符合预期")
    return {
        "status": "ok",
        "completed_job_state": completed["state"],
        "completed_released": completed["released_resources"],
        "late_telemetry_flagged": late["late"],
        "cancelled_released": cancelled["released_resources"],
        "pending_recovery_actions": report["pending_recovery_actions"],
        "barrier_count": len(report["barriers"]),
        "current_holder": report["current_responsibility"]["current_holder"],
        "audit_events": chain["events"],
        "workspace": workspace.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行水下井干预闭环离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
