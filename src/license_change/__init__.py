"""许可证变更影响后端。

维护：
- 许可证版本链（LicenseVersion，只追加）
- 变更申请（ChangeRequest，登记→待核验→处置中→已决定→已归档）
- 项目依赖（Project 授权）与未来业务引用（Booking 预约）
- 暂停/恢复/缩减/撤回的替代链（chain_links）
"""
from __future__ import annotations

from .errors import DomainError
from .models import (
    Booking,
    ChangeKind,
    ChangeRequest,
    ChangeState,
    Disposition,
    DispositionAction,
    ImpactItem,
    ItemSeverity,
    ItemStatus,
    Institution,
    LicenseStatus,
    LicenseVersion,
    Project,
    ProjectStatus,
    BookingStatus,
)
from .service import LicenseChangeService

__all__ = [
    "LicenseChangeService",
    "DomainError",
    "Booking",
    "ChangeKind",
    "ChangeRequest",
    "ChangeState",
    "Disposition",
    "DispositionAction",
    "ImpactItem",
    "ItemSeverity",
    "ItemStatus",
    "Institution",
    "LicenseStatus",
    "LicenseVersion",
    "Project",
    "ProjectStatus",
    "BookingStatus",
]
