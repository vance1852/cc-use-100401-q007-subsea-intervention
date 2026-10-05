"""水下干预作业版本的严格数据契约。

作业包（work package）是一次干预的冻结证据基线，包含八类不可变事实：
井口设备、故障证据、风险屏障、作业步骤、人员资格、工具组件、
海况窗口和应急恢复方案。作业版本一经密封，任何迟到遥测都不能改写。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

EQUIPMENT_KINDS = {"christmas-tree", "manifold", "jumper", "flowline", "riser", "umbilical", "bop", "sensor"}
EVIDENCE_KINDS = {"pressure-telemetry", "trend", "inspection", "photograph", "report", "manual-reading"}
BARRIER_KINDS = {"primary", "secondary", "hydrocarbon", "electrical", "mechanical", "marine"}
REQUIRED_BARRIER_KINDS = {"primary", "secondary"}
QUALIFICATION_KINDS = {
    "rov-pilot",
    "rov-supervisor",
    "isolation-engineer",
    "diving-supervisor",
    "intervention-supervisor",
    "marine-supervisor",
}
RESOURCE_KINDS = {"rov", "diving-spread", "vessel", "spare-part", "isolation-permit"}
RECOVERY_ACTIONS = {"release-resource", "consume-resource", "remove-isolation", "stand-down", "postpone", "notify"}
# 闭环时会结束作业对资源占用的恢复动作类型。
RESOURCE_RELEASING_KINDS = frozenset({"release-resource", "consume-resource", "remove-isolation", "stand-down"})

# 作业生命周期的签署闸门：必须按此顺序由不同职责确认。
GATE_ORDER = ("isolation", "start", "pause", "resume", "complete")
GATE_LABELS = {
    "isolation": "隔离确认",
    "start": "开工确认",
    "pause": "暂停确认",
    "resume": "恢复确认",
    "complete": "完工确认",
}
# 每个闸门要求的权限（职责）。
GATE_PERMISSION = {
    "isolation": "gate.isolation",
    "start": "gate.start",
    "pause": "gate.pause",
    "resume": "gate.resume",
    "complete": "gate.complete",
}
CANCEL_PERMISSION = "job.cancel"


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(value: object, field: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    return result


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationFailed(f"{field} 必须是对象")
    return value


def _sequence(value: object, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationFailed(f"{field} 必须是数组")
    return value


def _timestamp(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        parse_utc(text, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return text


@dataclass(frozen=True, slots=True)
class WellEquipment:
    equipment_id: str
    well_id: str
    name: str
    kind: str
    design_pressure_bar: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], path: str) -> "WellEquipment":
        kind = required_text(raw.get("kind"), f"{path}.kind", 32)
        if kind not in EQUIPMENT_KINDS:
            raise ValidationFailed(f"{path}.kind 不是受支持的井口设备类型")
        return cls(
            equipment_id=identifier(raw.get("equipment_id"), f"{path}.equipment_id"),
            well_id=identifier(raw.get("well_id"), f"{path}.well_id"),
            name=required_text(raw.get("name"), f"{path}.name"),
            kind=kind,
            design_pressure_bar=decimal_value(
                raw.get("design_pressure_bar"), f"{path}.design_pressure_bar", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class FailureEvidence:
    """故障证据：observed_at 是事实发生时间，迟到数据只能追加、不能覆盖签署。"""

    evidence_id: str
    kind: str
    equipment_id: str
    observed_at: str
    received_at: str
    metric: str
    value: Decimal
    unit: str
    source_ref: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], path: str, equipment_ids: frozenset[str]) -> "FailureEvidence":
        kind = required_text(raw.get("kind"), f"{path}.kind", 32)
        if kind not in EVIDENCE_KINDS:
            raise ValidationFailed(f"{path}.kind 不是受支持的故障证据类型")
        equipment_id = identifier(raw.get("equipment_id"), f"{path}.equipment_id")
        if equipment_id not in equipment_ids:
            raise ValidationFailed(f"{path}.equipment_id 未在井口设备清单中声明")
        observed_at = _timestamp(raw.get("observed_at"), f"{path}.observed_at")
        received_at = _timestamp(raw.get("received_at"), f"{path}.received_at")
        return cls(
            evidence_id=identifier(raw.get("evidence_id"), f"{path}.evidence_id"),
            kind=kind,
            equipment_id=equipment_id,
            observed_at=observed_at,
            received_at=received_at,
            metric=required_text(raw.get("metric"), f"{path}.metric", 64),
            value=decimal_value(raw.get("value"), f"{path}.value"),
            unit=required_text(raw.get("unit"), f"{path}.unit", 24),
            source_ref=required_text(raw.get("source_ref"), f"{path}.source_ref", 128),
        )


@dataclass(frozen=True, slots=True)
class RiskBarrier:
    """风险屏障：作业版本密封时记录为 pending，随后按隔离闸门逐个建立。"""

    barrier_id: str
    kind: str
    description: str
    equipment_id: str
    verification_method: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], path: str, equipment_ids: frozenset[str]) -> "RiskBarrier":
        kind = required_text(raw.get("kind"), f"{path}.kind", 32)
        if kind not in BARRIER_KINDS:
            raise ValidationFailed(f"{path}.kind 不是受支持的风险屏障类型")
        equipment_id = identifier(raw.get("equipment_id"), f"{path}.equipment_id")
        if equipment_id not in equipment_ids:
            raise ValidationFailed(f"{path}.equipment_id 未在井口设备清单中声明")
        return cls(
            barrier_id=identifier(raw.get("barrier_id"), f"{path}.barrier_id"),
            kind=kind,
            description=required_text(raw.get("description"), f"{path}.description", 512),
            equipment_id=equipment_id,
            verification_method=required_text(raw.get("verification_method"), f"{path}.verification_method", 256),
        )


@dataclass(frozen=True, slots=True)
class JobStep:
    step_no: int
    title: str
    instruction: str
    estimated_minutes: int
    requires_rov: bool
    requires_diving: bool

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], path: str) -> "JobStep":
        estimated = raw.get("estimated_minutes")
        if isinstance(estimated, bool) or not isinstance(estimated, int) or estimated <= 0:
            raise ValidationFailed(f"{path}.estimated_minutes 必须是正整数")
        for flag_field in ("requires_rov", "requires_diving"):
            if not isinstance(raw.get(flag_field, False), bool):
                raise ValidationFailed(f"{path}.{flag_field} 必须是布尔值")
        step_no = raw.get("step_no")
        if isinstance(step_no, bool) or not isinstance(step_no, int) or step_no <= 0:
            raise ValidationFailed(f"{path}.step_no 必须是正整数")
        return cls(
            step_no=step_no,
            title=required_text(raw.get("title"), f"{path}.title"),
            instruction=required_text(raw.get("instruction"), f"{path}.instruction", 1024),
            estimated_minutes=estimated,
            requires_rov=bool(raw.get("requires_rov", False)),
            requires_diving=bool(raw.get("requires_diving", False)),
        )


@dataclass(frozen=True, slots=True)
class PersonnelQualification:
    person_id: str
    display_name: str
    qualification: str
    certificate_ref: str
    valid_until: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], path: str) -> "PersonnelQualification":
        qualification = required_text(raw.get("qualification"), f"{path}.qualification", 48)
        if qualification not in QUALIFICATION_KINDS:
            raise ValidationFailed(f"{path}.qualification 不是受支持的资格类型")
        valid_until = required_text(raw.get("valid_until"), f"{path}.valid_until", 10)
        try:
            valid_until = date.fromisoformat(valid_until).isoformat()
        except ValueError as exc:
            raise ValidationFailed(f"{path}.valid_until 必须是 YYYY-MM-DD 日期") from exc
        return cls(
            person_id=identifier(raw.get("person_id"), f"{path}.person_id"),
            display_name=required_text(raw.get("display_name"), f"{path}.display_name"),
            qualification=qualification,
            certificate_ref=required_text(raw.get("certificate_ref"), f"{path}.certificate_ref", 128),
            valid_until=valid_until,
        )


@dataclass(frozen=True, slots=True)
class ToolComponent:
    """工具组件与可承诺资源：committed_by 在资源竞争中只允许一个作业占位。"""

    component_id: str
    resource_kind: str
    name: str
    resource_ref: str
    quantity: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], path: str) -> "ToolComponent":
        resource_kind = required_text(raw.get("resource_kind"), f"{path}.resource_kind", 32)
        if resource_kind not in RESOURCE_KINDS:
            raise ValidationFailed(f"{path}.resource_kind 不是受支持的资源类型")
        quantity = raw.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValidationFailed(f"{path}.quantity 必须是正整数")
        return cls(
            component_id=identifier(raw.get("component_id"), f"{path}.component_id"),
            resource_kind=resource_kind,
            name=required_text(raw.get("name"), f"{path}.name"),
            resource_ref=required_text(raw.get("resource_ref"), f"{path}.resource_ref", 128),
            quantity=quantity,
        )


@dataclass(frozen=True, slots=True)
class SeaStateWindow:
    window_id: str
    opens_at: str
    closes_at: str
    max_wave_height_m: Decimal
    max_current_ms: Decimal
    max_wind_ms: Decimal
    forecast_ref: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], path: str) -> "SeaStateWindow":
        opens_at = _timestamp(raw.get("opens_at"), f"{path}.opens_at")
        closes_at = _timestamp(raw.get("closes_at"), f"{path}.closes_at")
        if parse_utc(closes_at) <= parse_utc(opens_at):
            raise ValidationFailed(f"{path}.closes_at 必须晚于 opens_at")
        return cls(
            window_id=identifier(raw.get("window_id"), f"{path}.window_id"),
            opens_at=opens_at,
            closes_at=closes_at,
            max_wave_height_m=decimal_value(
                raw.get("max_wave_height_m"), f"{path}.max_wave_height_m", minimum=Decimal("0")
            ),
            max_current_ms=decimal_value(raw.get("max_current_ms"), f"{path}.max_current_ms", minimum=Decimal("0")),
            max_wind_ms=decimal_value(raw.get("max_wind_ms"), f"{path}.max_wind_ms", minimum=Decimal("0")),
            forecast_ref=required_text(raw.get("forecast_ref"), f"{path}.forecast_ref", 128),
        )


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    """应急恢复方案：每个动作在取消/完工后必须闭环，未完成项始终可被还原。"""

    action_id: str
    kind: str
    description: str
    resource_ref: str
    owner_role: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], path: str) -> "RecoveryAction":
        kind = required_text(raw.get("kind"), f"{path}.kind", 32)
        if kind not in RECOVERY_ACTIONS:
            raise ValidationFailed(f"{path}.kind 不是受支持的恢复动作类型")
        return cls(
            action_id=identifier(raw.get("action_id"), f"{path}.action_id"),
            kind=kind,
            description=required_text(raw.get("description"), f"{path}.description", 512),
            resource_ref=required_text(raw.get("resource_ref"), f"{path}.resource_ref", 128),
            owner_role=required_text(raw.get("owner_role"), f"{path}.owner_role", 48),
        )


@dataclass(frozen=True, slots=True)
class WorkPackage:
    """一次水下干预的冻结作业版本输入。"""

    well_id: str
    title: str
    trigger_evidence_id: str
    equipment: tuple[WellEquipment, ...]
    evidence: tuple[FailureEvidence, ...]
    barriers: tuple[RiskBarrier, ...]
    steps: tuple[JobStep, ...]
    qualifications: tuple[PersonnelQualification, ...]
    components: tuple[ToolComponent, ...]
    windows: tuple[SeaStateWindow, ...]
    recovery: tuple[RecoveryAction, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WorkPackage":
        well_id = identifier(raw.get("well_id"), "well_id")
        equipment = tuple(
            WellEquipment.from_dict(item, f"equipment[{index}]")
            for index, item in enumerate(_sequence(raw.get("equipment"), "equipment"))
        )
        if not equipment:
            raise ValidationFailed("equipment 至少包含一项井口设备")
        equipment_ids = frozenset(item.equipment_id for item in equipment)
        well_ids = frozenset(item.well_id for item in equipment)
        if well_id not in well_ids or len(well_ids) != 1:
            raise ValidationFailed("equipment 必须全部属于 well_id 指定的井口")
        if len(equipment_ids) != len(equipment):
            raise ValidationFailed("equipment.equipment_id 不能重复")
        evidence = tuple(
            FailureEvidence.from_dict(item, f"evidence[{index}]", equipment_ids)
            for index, item in enumerate(_sequence(raw.get("evidence"), "evidence"))
        )
        evidence_ids = frozenset(item.evidence_id for item in evidence)
        if len(evidence_ids) != len(evidence):
            raise ValidationFailed("evidence.evidence_id 不能重复")
        trigger = identifier(raw.get("trigger_evidence_id"), "trigger_evidence_id")
        if trigger not in evidence_ids:
            raise ValidationFailed("trigger_evidence_id 必须引用故障证据中的一条")
        barriers = tuple(
            RiskBarrier.from_dict(item, f"barriers[{index}]", equipment_ids)
            for index, item in enumerate(_sequence(raw.get("barriers"), "barriers"))
        )
        if not barriers:
            raise ValidationFailed("barriers 至少包含一道风险屏障")
        barrier_ids = frozenset(item.barrier_id for item in barriers)
        if len(barrier_ids) != len(barriers):
            raise ValidationFailed("barriers.barrier_id 不能重复")
        barrier_kinds = frozenset(item.kind for item in barriers)
        missing = sorted(REQUIRED_BARRIER_KINDS - barrier_kinds)
        if missing:
            raise ValidationFailed(f"风险屏障必须至少覆盖主屏障与副屏障，缺少 {missing}")
        steps = tuple(
            JobStep.from_dict(item, f"steps[{index}]")
            for index, item in enumerate(_sequence(raw.get("steps"), "steps"))
        )
        if not steps:
            raise ValidationFailed("steps 至少包含一个作业步骤")
        step_numbers = [item.step_no for item in steps]
        if sorted(step_numbers) != list(range(1, len(steps) + 1)):
            raise ValidationFailed("steps.step_no 必须从 1 开始连续编号且不重复")
        qualifications = tuple(
            PersonnelQualification.from_dict(item, f"qualifications[{index}]")
            for index, item in enumerate(_sequence(raw.get("qualifications"), "qualifications"))
        )
        if not qualifications:
            raise ValidationFailed("qualifications 至少包含一名合格人员")
        person_ids = [item.person_id for item in qualifications]
        if len(frozenset(person_ids)) != len(person_ids):
            raise ValidationFailed("qualifications.person_id 不能重复")
        components = tuple(
            ToolComponent.from_dict(item, f"components[{index}]")
            for index, item in enumerate(_sequence(raw.get("components"), "components"))
        )
        if not components:
            raise ValidationFailed("components 至少包含一个工具组件")
        component_ids = frozenset(item.component_id for item in components)
        if len(component_ids) != len(components):
            raise ValidationFailed("components.component_id 不能重复")
        windows = tuple(
            SeaStateWindow.from_dict(item, f"windows[{index}]")
            for index, item in enumerate(_sequence(raw.get("windows"), "windows"))
        )
        if not windows:
            raise ValidationFailed("windows 至少包含一个海况窗口")
        if len(frozenset(item.window_id for item in windows)) != len(windows):
            raise ValidationFailed("windows.window_id 不能重复")
        recovery = tuple(
            RecoveryAction.from_dict(item, f"recovery[{index}]")
            for index, item in enumerate(_sequence(raw.get("recovery"), "recovery"))
        )
        if not recovery:
            raise ValidationFailed("recovery 至少包含一个应急恢复动作")
        if len(frozenset(item.action_id for item in recovery)) != len(recovery):
            raise ValidationFailed("recovery.action_id 不能重复")
        # 每个承诺资源都必须能在取消/完工后通过某个释放类恢复动作闭环。
        releasable = {
            item.resource_ref
            for item in recovery
            if item.kind in RESOURCE_RELEASING_KINDS
        }
        missing_release = sorted({
            item.resource_ref for item in components if item.resource_ref not in releasable
        })
        if missing_release:
            raise ValidationFailed(f"以下承诺资源缺少释放/核销/撤站类恢复动作：{missing_release}")
        return cls(
            well_id=well_id,
            title=required_text(raw.get("title"), "title"),
            trigger_evidence_id=trigger,
            equipment=equipment,
            evidence=evidence,
            barriers=barriers,
            steps=steps,
            qualifications=qualifications,
            components=components,
            windows=windows,
            recovery=recovery,
        )
