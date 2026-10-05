"""领域模型：只追加的许可证版本链、变更申请、项目授权、未来预约。"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


def _now() -> datetime:
    return datetime.now().astimezone()


class LicenseStatus(str, Enum):
    ACTIVE = "active"          # 正常执业
    SUSPENDED = "suspended"    # 停业（暂停）
    REVOKED = "revoked"        # 吊销/注销（终态，不可恢复）


class ChangeKind(str, Enum):
    ADDRESS = "address"                    # 地址变更
    SUBJECT_EXPAND = "subject_expand"      # 科目增设
    SUBJECT_REDUCE = "subject_reduce"      # 部分科目缩减
    SUSPEND = "suspend"                    # 暂停（停业）
    RESUME = "resume"                      # 恢复
    REVOKE = "revoke"                      # 吊销
    STATUS_CHANGE = "status_change"        # 其他许可证状态变更


class ChangeState(str, Enum):
    REGISTERED = "登记"
    PENDING_VERIFY = "待核验"
    DISPOSING = "处置中"
    DECIDED = "已决定"
    ARCHIVED = "已归档"


# 合法状态迁移图（契约：登记→待核验→处置中→已决定→已归档）
STATE_TRANSITIONS: dict[ChangeState, set[ChangeState]] = {
    ChangeState.REGISTERED: {ChangeState.PENDING_VERIFY, ChangeState.ARCHIVED},
    ChangeState.PENDING_VERIFY: {ChangeState.DISPOSING, ChangeState.ARCHIVED},
    ChangeState.DISPOSING: {ChangeState.PENDING_VERIFY, ChangeState.DECIDED},
    ChangeState.DECIDED: {ChangeState.ARCHIVED},
    ChangeState.ARCHIVED: set(),
}


class ProjectStatus(str, Enum):
    AUTHORIZED = "authorized"        # 授权有效
    RESTRICTED = "restricted"        # 受限（部分科目不可用）
    SUSPENDED = "suspended"          # 暂停提供服务
    TERMINATED = "terminated"        # 授权终止


class BookingStatus(str, Enum):
    FUTURE = "future"                # 未来预约
    CANCELLED = "cancelled"          # 已取消（处置）
    RESCHEDULED = "rescheduled"      # 已改期（处置）
    FULFILLED = "fulfilled"          # 已履约（历史服务）
    FORBIDDEN = "forbidden"          # 生效时原子禁止
    HISTORICAL = "historical"        # 已发生的历史服务（不可变）


class ItemSeverity(str, Enum):
    BLOCKER = "blocker"      # 阻塞项：批准前必须处置
    WARNING = "warning"      # 提示项：记录即可


class ItemStatus(str, Enum):
    OPEN = "open"            # 待处置
    RESOLVED = "resolved"    # 已处置
    WAIVED = "waived"        # 非阻塞项豁免


class DispositionAction(str, Enum):
    SUSPEND_PROJECT = "suspend_project"
    RESTRICT_PROJECT = "restrict_project"
    TERMINATE_PROJECT = "terminate_project"
    KEEP_PROJECT = "keep_project"
    CANCEL_BOOKING = "cancel_booking"
    RESCHEDULE_BOOKING = "reschedule_booking"
    WAIVE = "waive"


@dataclass
class Institution:
    id: str
    name: str
    address: str
    subjects: tuple[str, ...]
    status: LicenseStatus = LicenseStatus.ACTIVE


@dataclass
class LicenseVersion:
    """许可证版本：只追加，prev_id 构成版本链。"""
    version_no: int
    institution_id: str
    address: str
    subjects: frozenset[str]
    status: LicenseStatus
    effective_from: date
    created_at: datetime = field(default_factory=_now)
    change_request_id: str | None = None
    prev_id: int | None = None
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_no": self.version_no,
            "institution_id": self.institution_id,
            "address": self.address,
            "subjects": sorted(self.subjects),
            "status": self.status.value,
            "effective_from": self.effective_from.isoformat(),
            "created_at": self.created_at.isoformat(),
            "change_request_id": self.change_request_id,
            "prev_id": self.prev_id,
            "rationale": self.rationale,
        }


@dataclass
class Project:
    """已立项授权：依赖机构地址与科目范围。"""
    id: str
    institution_id: str
    name: str
    address_required: str
    subjects_required: frozenset[str]
    status: ProjectStatus = ProjectStatus.AUTHORIZED
    active_subjects: frozenset[str] | None = None  # 受限后剩余可用科目
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.subjects_required, (set, list, tuple)):
            self.subjects_required = frozenset(self.subjects_required)
        if self.active_subjects is not None:
            self.active_subjects = frozenset(self.active_subjects)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "institution_id": self.institution_id,
            "name": self.name,
            "address_required": self.address_required,
            "subjects_required": sorted(self.subjects_required),
            "status": self.status.value,
            "active_subjects": sorted(self.active_subjects) if self.active_subjects is not None else None,
        }


@dataclass
class Booking:
    """业务引用：预约（未来）或服务记录（历史）。

    service_date < 生效日 => 历史服务，任何限制都不可改写；
    service_date >= 生效日 => 未来引用，可被禁止/取消/改期。
    """
    id: str
    institution_id: str
    project_id: str
    subject: str
    service_date: date
    status: BookingStatus = BookingStatus.FUTURE
    created_at: datetime = field(default_factory=_now)
    resolved_by: str | None = None
    resolution_note: str = ""

    @property
    def is_historical(self) -> bool:
        return self.status in (BookingStatus.HISTORICAL, BookingStatus.FULFILLED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "institution_id": self.institution_id,
            "project_id": self.project_id,
            "subject": self.subject,
            "service_date": self.service_date.isoformat(),
            "status": self.status.value,
            "resolved_by": self.resolved_by,
            "resolution_note": self.resolution_note,
        }


@dataclass
class ImpactItem:
    """影响清单条目。blocker 未处置则不得批准变更。"""
    id: str
    ref_type: str            # "project" | "booking"
    ref_id: str
    severity: ItemSeverity
    status: ItemStatus
    reason: str
    basis: str               # 判定依据（许可证版本条款 / 规则）
    detail: dict[str, Any] = field(default_factory=dict)
    disposition: str | None = None
    disposition_by: str | None = None
    disposition_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ref_type": self.ref_type,
            "ref_id": self.ref_id,
            "severity": self.severity.value,
            "status": self.status.value,
            "reason": self.reason,
            "basis": self.basis,
            "detail": self.detail,
            "disposition": self.disposition,
            "disposition_by": self.disposition_by,
            "disposition_at": self.disposition_at.isoformat() if self.disposition_at else None,
        }


@dataclass
class Disposition:
    item_id: str
    action: DispositionAction
    note: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChainLink:
    """替代链：暂停→恢复、缩减、撤回之间的因果关联。"""
    seq: int
    link_type: str           # "suspend" | "resume" | "reduce" | "withdraw"
    change_request_id: str
    related_request_id: str | None
    at: datetime = field(default_factory=_now)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "link_type": self.link_type,
            "change_request_id": self.change_request_id,
            "related_request_id": self.related_request_id,
            "at": self.at.isoformat(),
            "note": self.note,
        }


@dataclass
class ChangeRequest:
    id: str
    institution_id: str
    kind: ChangeKind
    state: ChangeState
    proposed: dict[str, Any]
    effective_date: date
    created_by: str
    created_at: datetime = field(default_factory=_now)
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision: str | None = None           # "approved" | "rejected"
    new_version_no: int | None = None
    impact_items: dict[str, ImpactItem] = field(default_factory=dict)
    impact_generated_at: datetime | None = None
    supersedes_id: str | None = None      # 替代链：恢复替代暂停
    withdrawn_at: datetime | None = None
    withdraw_reason: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def open_blockers(self) -> list[ImpactItem]:
        return [
            i for i in self.impact_items.values()
            if i.severity is ItemSeverity.BLOCKER and i.status is ItemStatus.OPEN
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "institution_id": self.institution_id,
            "kind": self.kind.value,
            "state": self.state.value,
            "proposed": self.proposed,
            "effective_date": self.effective_date.isoformat(),
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decided_by": self.decided_by,
            "decision": self.decision,
            "new_version_no": self.new_version_no,
            "supersedes_id": self.supersedes_id,
            "withdrawn_at": self.withdrawn_at.isoformat() if self.withdrawn_at else None,
            "withdraw_reason": self.withdraw_reason,
            "impact_items": [i.to_dict() for i in self.impact_items.values()],
            "history": self.history,
        }


id_counter = itertools.count(1)


def new_id(prefix: str) -> str:
    return f"{prefix}-{next(id_counter):06d}"
