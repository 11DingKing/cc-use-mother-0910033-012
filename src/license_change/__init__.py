"""许可证变更影响后端。

对外导出领域服务、存储与模型，供 HTTP API、命令行与测试复用。
"""
from __future__ import annotations

from .errors import DomainError, ErrorCode
from .models import (
    Appointment,
    AuditEvent,
    ChangeRequest,
    DispositionAction,
    ImpactItem,
    ImpactReport,
    Institution,
    LicenseVersion,
    ProjectAuthorization,
    RequestKind,
    RequestStatus,
    Restriction,
)
from .repository import Store
from .service import RULES, LicenseChangeService

__all__ = [
    "DomainError",
    "ErrorCode",
    "Store",
    "LicenseChangeService",
    "RULES",
    "Institution",
    "LicenseVersion",
    "ChangeRequest",
    "RequestKind",
    "RequestStatus",
    "DispositionAction",
    "ImpactItem",
    "ImpactReport",
    "ProjectAuthorization",
    "Appointment",
    "Restriction",
    "AuditEvent",
]
