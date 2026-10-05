"""水下干预测试共享作业包。"""

from __future__ import annotations


def sea_ok() -> dict[str, str]:
    return {"wave_height_m": "1.8", "current_ms": "0.6", "wind_ms": "9"}


def base_package() -> dict[str, object]:
    return {
        "job_id": "job-a7",
        "idempotency_key": "create-job-a7",
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
             "closes_at": "2026-10-05T18:00:00Z", "max_wave_height_m": "2.5", "max_current_ms": "0.8",
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


def contending_package() -> dict[str, object]:
    return {
        "job_id": "job-b9",
        "idempotency_key": "create-job-b9",
        "well_id": "well-b9",
        "title": "B9 井口干预",
        "trigger_evidence_id": "ev-pressure-1",
        "equipment": [
            {"equipment_id": "xt-b9", "well_id": "well-b9", "name": "B9 采油树",
             "kind": "christmas-tree", "design_pressure_bar": "345"},
        ],
        "evidence": [
            {"evidence_id": "ev-pressure-1", "kind": "pressure-telemetry", "equipment_id": "xt-b9",
             "observed_at": "2026-10-05T04:00:00Z", "received_at": "2026-10-05T04:05:00Z",
             "metric": "annulus_b_pressure_bar", "value": "279.0", "unit": "bar",
             "source_ref": "scada/xt-b9/0400"},
        ],
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


def resolved_package() -> dict[str, object]:
    package = contending_package()
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
