"""领域模型：许可证版本链、变更申请、项目依赖、业务引用与限制。

时间字段统一使用 ISO-8601 字符串（可带时区），服务层通过 ``time_utils``
做可比较的归一化处理。
"""
from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# 枚举（内部使用稳定英文标识，对外通过 label 提供中文名）
# ---------------------------------------------------------------------------

class LicenseStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"          # 正常执业
    SUSPENDED = "SUSPENDED"    # 停业整顿/暂停
    REVOKED = "REVOKED"        # 吊销
    EXPIRED = "EXPIRED"        # 过期

    @property
    def label(self) -> str:
        return {
            "ACTIVE": "正常",
            "SUSPENDED": "暂停",
            "REVOKED": "吊销",
            "EXPIRED": "过期",
        }[self.value]


class RequestKind(str, enum.Enum):
    ADDRESS_CHANGE = "ADDRESS_CHANGE"          # 地址变更
    SUBJECT_REDUCTION = "SUBJECT_REDUCTION"    # 部分诊疗科目缩减
    LICENSE_SUSPEND = "LICENSE_SUSPEND"        # 暂停执业
    LICENSE_RESUME = "LICENSE_RESUME"          # 恢复执业
    LICENSE_REVOKE = "LICENSE_REVOKE"          # 吊销（监管主动）

    @property
    def label(self) -> str:
        return {
            "ADDRESS_CHANGE": "地址变更",
            "SUBJECT_REDUCTION": "科目缩减",
            "LICENSE_SUSPEND": "暂停执业",
            "LICENSE_RESUME": "恢复执业",
            "LICENSE_REVOKE": "吊销",
        }[self.value]


class RequestStatus(str, enum.Enum):
    REGISTERED = "REGISTERED"                 # 登记
    PENDING_VERIFICATION = "PENDING_VERIFICATION"  # 待核验
    IN_DISPOSITION = "IN_DISPOSITION"         # 处置中
    DECIDED = "DECIDED"                       # 已决定（批准，未生效）
    REJECTED = "REJECTED"                     # 已决定（驳回）
    EFFECTIVE = "EFFECTIVE"                   # 已生效（限制已传播）
    ARCHIVED = "ARCHIVED"                     # 已归档
    WITHDRAWN = "WITHDRAWN"                   # 已撤回（替代链节点）

    @property
    def label(self) -> str:
        return {
            "REGISTERED": "登记",
            "PENDING_VERIFICATION": "待核验",
            "IN_DISPOSITION": "处置中",
            "DECIDED": "已决定",
            "REJECTED": "已驳回",
            "EFFECTIVE": "已生效",
            "ARCHIVED": "已归档",
            "WITHDRAWN": "已撤回",
        }[self.value]


# 契约规定的五个主状态映射（撤回/驳回/生效属于决定后的细分）
CONTRACT_PHASE = {
    RequestStatus.REGISTERED: "登记",
    RequestStatus.PENDING_VERIFICATION: "待核验",
    RequestStatus.IN_DISPOSITION: "处置中",
    RequestStatus.DECIDED: "已决定",
    RequestStatus.REJECTED: "已决定",
    RequestStatus.EFFECTIVE: "已归档",
    RequestStatus.ARCHIVED: "已归档",
    RequestStatus.WITHDRAWN: "已归档",
}


class ImpactKind(str, enum.Enum):
    FUTURE_PROJECT = "FUTURE_PROJECT"        # 未来项目授权依赖
    FUTURE_APPOINTMENT = "FUTURE_APPOINTMENT"  # 未完成的未来预约
    HISTORICAL_SERVICE = "HISTORICAL_SERVICE"  # 历史服务（仅供溯源）
    ONGOING_PROJECT = "ONGOING_PROJECT"      # 横跨生效时点的项目授权


class Severity(str, enum.Enum):
    BLOCKING = "BLOCKING"    # 阻塞项：批准前必须处置
    ADVISORY = "ADVISORY"    # 提示项：不阻塞
    HISTORICAL = "HISTORICAL"  # 历史项：永不阻塞


class DispositionAction(str, enum.Enum):
    TERMINATE_AUTHORIZATION = "TERMINATE_AUTHORIZATION"  # 终止项目授权
    RESTRICT_AUTHORIZATION = "RESTRICT_AUTHORIZATION"    # 限定授权范围
    CANCEL_APPOINTMENT = "CANCEL_APPOINTMENT"            # 取消预约
    RESCHEDULE_APPOINTMENT = "RESCHEDULE_APPOINTMENT"    # 改期到生效边界之后/其他科目
    REGULATOR_WAIVE = "REGULATOR_WAIVE"                  # 监管豁免（需说明）


class AuthorizationStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    RESTRICTED = "RESTRICTED"
    TERMINATED = "TERMINATED"


class AppointmentStatus(str, enum.Enum):
    BOOKED = "BOOKED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


# ---------------------------------------------------------------------------
# 实体
# ---------------------------------------------------------------------------

@dataclass
class Institution:
    id: str
    name: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class LicenseVersion:
    """许可证版本。版本号在机构内单调递增，形成版本链。"""

    institution_id: str
    version_no: int
    status: LicenseStatus
    address: str
    subjects: list[str]
    effective_at: str
    reason: str
    change_request_id: str | None = None  # 由哪条变更申请产生（首个登记版本为 None）
    superseded_at: str | None = None      # 被下一版本替代的时刻
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["status_label"] = self.status.label
        return data


@dataclass
class ImpactItem:
    """影响清单中的单条引用。"""

    id: str
    kind: ImpactKind
    severity: Severity
    reference_type: str          # "project_authorization" | "appointment"
    reference_id: str
    detail: str
    basis_rule: str              # 依据的判定规则编号（见 service.RULES）
    required_action: str         # 建议/要求的处置
    resolved: bool = False
    resolution_action: DispositionAction | None = None
    resolution_note: str = ""
    resolved_by: str = ""
    resolved_at: str = ""
    # 处置附带数据（改期时间、改期目标科目等），生效时执行
    resolution_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["kind"] = self.kind.value
        data["severity"] = self.severity.value
        data["resolution_action"] = (
            self.resolution_action.value if self.resolution_action else None
        )
        return data


@dataclass
class ImpactReport:
    """批准前生成的影响清单。"""

    request_id: str
    generated_at: str
    items: list[ImpactItem] = field(default_factory=list)

    @property
    def blocking_items(self) -> list[ImpactItem]:
        return [i for i in self.items if i.severity is Severity.BLOCKING and not i.resolved]

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "generated_at": self.generated_at,
            "items": [i.to_dict() for i in self.items],
            "blocking_total": sum(1 for i in self.items if i.severity is Severity.BLOCKING),
            "blocking_unresolved": len(self.blocking_items),
            "historical_total": sum(1 for i in self.items if i.severity is Severity.HISTORICAL),
            "advisory_total": sum(1 for i in self.items if i.severity is Severity.ADVISORY),
        }


@dataclass
class ChangeRequest:
    """许可证变更申请；通过 supersedes_id 串成替代链。"""

    id: str
    institution_id: str
    kind: RequestKind
    status: RequestStatus
    created_by: str
    created_at: str
    payload: dict[str, Any] = field(default_factory=dict)
    # 期望生效时刻；为空时批准后立即生效
    expected_effective_at: str | None = None
    decided_at: str | None = None
    decided_by: str = ""
    effective_at: str | None = None
    archived_at: str | None = None
    withdraw_reason: str = ""
    withdrawn_at: str | None = None
    # 替代链：本申请替代了哪一条旧申请（如以“科目缩减”替代“暂停”）
    supersedes_id: str | None = None
    replaced_by_id: str | None = None
    # 生效时产出的新版本与限制
    new_version_id: str | None = None
    restriction_ids: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["kind"] = self.kind.value
        data["kind_label"] = self.kind.label
        data["status"] = self.status.value
        data["status_label"] = self.status.label
        data["contract_phase"] = CONTRACT_PHASE[self.status]
        return data


@dataclass
class ProjectAuthorization:
    """项目对机构许可证的依赖（项目授权）。"""

    id: str
    project_id: str
    institution_id: str
    required_subjects: list[str]
    valid_from: str
    valid_to: str
    status: AuthorizationStatus = AuthorizationStatus.ACTIVE
    restriction_ids: list[str] = field(default_factory=list)
    # 历史窗口保留：被限制/终止不擦除已履约部分
    terminated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class Appointment:
    """群众预约/业务单据。"""

    id: str
    institution_id: str
    subject: str
    scheduled_at: str
    status: AppointmentStatus = AppointmentStatus.BOOKED
    cancellation_reason: str = ""
    cancelled_by_change_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class Restriction:
    """生效时原子传播的执业限制。"""

    id: str
    institution_id: str
    change_request_id: str
    license_version_id: str
    scope: str                 # "ALL" 暂停/吊销 | "SUBJECTS" 部分科目
    subjects: list[str]        # scope=SUBJECTS 时被限制的科目
    status: LicenseStatus      # SUSPENDED / REVOKED / ACTIVE(恢复时解除)
    effective_at: str
    # 恢复链：本限制解除了哪一条旧限制
    lifts_restriction_id: str | None = None
    address: str | None = None  # 地址变更时记录新址

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["status"] = self.status.value
        data["status_label"] = self.status.label
        return data


@dataclass
class AuditEvent:
    """审计留痕，每条状态流转与处置都记录依据。"""

    id: str
    at: str
    actor: str
    action: str
    target_type: str
    target_id: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
