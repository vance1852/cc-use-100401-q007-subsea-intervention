"""水下干预作业版本冻结内容的输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Tuple

from .clock import parse_utc, utc_text
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
EQUIPMENT_KINDS = {"xt", "tree-valve", "connector", "bop", "manifold", "wellhead", "sensor"}
BARRIER_KINDS = {"mechanical", "hydraulic", "electrical", "procedural"}
RESOURCE_KINDS = {"rov", "spare-part", "dive-support"}
MILESTONE_KINDS = ("isolation", "start", "pause", "resume", "complete")
KNOWN_ROLES = {"planner", "isolation_officer", "supervisor", "resource_controller", "telemetry", "auditor"}


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


def sha256_text(value: object, field: str) -> str:
    result = required_text(value, field, 64).lower()
    if not SHA256.fullmatch(result):
        raise ValidationFailed(f"{field} 必须是 64 位十六进制 SHA-256")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
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
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def utc_text_field(value: object, field: str) -> str:
    result = required_text(value, field, 40)
    try:
        parsed = parse_utc(result, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return utc_text(parsed)


def _require_list(value: object, field: str) -> list[Any]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValidationFailed(f"{field} 必须是非空数组")
    return list(value)


def _require_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationFailed(f"{field} 必须是对象")
    return value


@dataclass(frozen=True, slots=True)
class BarrierSpec:
    barrier_id: str
    name: str
    kind: str
    verification: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "BarrierSpec":
        kind = required_text(raw.get("kind"), "risk_barriers.kind", 24)
        if kind not in BARRIER_KINDS:
            raise ValidationFailed("risk_barriers.kind 不是受支持的屏障类型")
        return cls(
            barrier_id=identifier(raw.get("barrier_id"), "risk_barriers.barrier_id"),
            name=required_text(raw.get("name"), "risk_barriers.name"),
            kind=kind,
            verification=required_text(raw.get("verification"), "risk_barriers.verification"),
        )


@dataclass(frozen=True, slots=True)
class StepSpec:
    sequence: int
    title: str
    required_role: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StepSpec":
        role = required_text(raw.get("required_role"), "operation_steps.required_role", 32)
        if role not in KNOWN_ROLES:
            raise ValidationFailed("operation_steps.required_role 不是已知职责")
        return cls(
            sequence=positive_integer(raw.get("sequence"), "operation_steps.sequence"),
            title=required_text(raw.get("title"), "operation_steps.title"),
            required_role=role,
        )


@dataclass(frozen=True, slots=True)
class PersonnelSpec:
    user_id: str
    qualification: str
    valid_until: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PersonnelSpec":
        return cls(
            user_id=identifier(raw.get("user_id"), "personnel.user_id"),
            qualification=required_text(raw.get("qualification"), "personnel.qualification", 64),
            valid_until=utc_text_field(raw.get("valid_until"), "personnel.valid_until"),
        )


@dataclass(frozen=True, slots=True)
class ToolComponentSpec:
    component_id: str
    description: str
    quantity: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ToolComponentSpec":
        return cls(
            component_id=identifier(raw.get("component_id"), "tool_components.component_id"),
            description=required_text(raw.get("description"), "tool_components.description"),
            quantity=positive_integer(raw.get("quantity"), "tool_components.quantity"),
        )


@dataclass(frozen=True, slots=True)
class SeaStateWindow:
    starts_at: str
    ends_at: str
    max_wave_height_m: Decimal
    max_current_knots: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SeaStateWindow":
        starts_at = utc_text_field(raw.get("starts_at"), "sea_state_windows.starts_at")
        ends_at = utc_text_field(raw.get("ends_at"), "sea_state_windows.ends_at")
        if parse_utc(ends_at) <= parse_utc(starts_at):
            raise ValidationFailed("sea_state_windows.ends_at 必须晚于 starts_at")
        return cls(
            starts_at=starts_at,
            ends_at=ends_at,
            max_wave_height_m=decimal_value(
                raw.get("max_wave_height_m"), "sea_state_windows.max_wave_height_m",
                minimum=Decimal("0"), maximum=Decimal("30"),
            ),
            max_current_knots=decimal_value(
                raw.get("max_current_knots"), "sea_state_windows.max_current_knots",
                minimum=Decimal("0"), maximum=Decimal("10"),
            ),
        )


@dataclass(frozen=True, slots=True)
class RecoveryActionSpec:
    action_id: str
    title: str
    required_role: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RecoveryActionSpec":
        role = required_text(raw.get("required_role"), "recovery_plan.required_role", 32)
        if role not in KNOWN_ROLES:
            raise ValidationFailed("recovery_plan.required_role 不是已知职责")
        return cls(
            action_id=identifier(raw.get("action_id"), "recovery_plan.action_id"),
            title=required_text(raw.get("title"), "recovery_plan.title"),
            required_role=role,
        )


@dataclass(frozen=True, slots=True)
class InterventionPlan:
    """冻结到作业版本的完整计划：设备、屏障、步骤、人员、工具、海况和应急恢复。"""

    equipment_ids: Tuple[str, ...]
    risk_barriers: Tuple[BarrierSpec, ...]
    operation_steps: Tuple[StepSpec, ...]
    personnel: Tuple[PersonnelSpec, ...]
    tool_components: Tuple[ToolComponentSpec, ...]
    sea_state_windows: Tuple[SeaStateWindow, ...]
    recovery_plan: Tuple[RecoveryActionSpec, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "InterventionPlan":
        raw = _require_mapping(raw, "plan")
        equipment_ids = tuple(
            identifier(item, "wellhead_equipment 元素")
            for item in _require_list(raw.get("wellhead_equipment"), "wellhead_equipment")
        )
        if len(set(equipment_ids)) != len(equipment_ids):
            raise ValidationFailed("wellhead_equipment 存在重复设备")
        barriers = tuple(
            BarrierSpec.from_dict(_require_mapping(item, "risk_barriers 元素"))
            for item in _require_list(raw.get("risk_barriers"), "risk_barriers")
        )
        barrier_ids = [item.barrier_id for item in barriers]
        if len(set(barrier_ids)) != len(barrier_ids):
            raise ValidationFailed("risk_barriers.barrier_id 存在重复")
        steps = tuple(
            StepSpec.from_dict(_require_mapping(item, "operation_steps 元素"))
            for item in _require_list(raw.get("operation_steps"), "operation_steps")
        )
        sequences = [item.sequence for item in steps]
        if sorted(sequences) != list(range(1, len(steps) + 1)):
            raise ValidationFailed("operation_steps.sequence 必须从 1 开始连续编号")
        personnel = tuple(
            PersonnelSpec.from_dict(_require_mapping(item, "personnel 元素"))
            for item in _require_list(raw.get("personnel"), "personnel")
        )
        tools = tuple(
            ToolComponentSpec.from_dict(_require_mapping(item, "tool_components 元素"))
            for item in _require_list(raw.get("tool_components"), "tool_components")
        )
        windows = tuple(
            SeaStateWindow.from_dict(_require_mapping(item, "sea_state_windows 元素"))
            for item in _require_list(raw.get("sea_state_windows"), "sea_state_windows")
        )
        recovery = tuple(
            RecoveryActionSpec.from_dict(_require_mapping(item, "recovery_plan 元素"))
            for item in _require_list(raw.get("recovery_plan"), "recovery_plan")
        )
        action_ids = [item.action_id for item in recovery]
        if len(set(action_ids)) != len(action_ids):
            raise ValidationFailed("recovery_plan.action_id 存在重复")
        return cls(
            equipment_ids=equipment_ids,
            risk_barriers=barriers,
            operation_steps=steps,
            personnel=personnel,
            tool_components=tools,
            sea_state_windows=windows,
            recovery_plan=recovery,
        )
