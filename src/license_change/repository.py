"""内存存储（带锁），可选 JSON 快照持久化。

生产环境可替换为数据库实现：接口即按集合划分的读写方法，
服务层只依赖本类，保证事务边界清晰。
"""
from __future__ import annotations

import dataclasses
import json
import threading
from pathlib import Path
from typing import Iterator

from .errors import DomainError, ErrorCode
from .models import (
    Appointment,
    AuditEvent,
    ChangeRequest,
    ImpactReport,
    Institution,
    LicenseVersion,
    ProjectAuthorization,
    Restriction,
)


class Store:
    """线程安全的实体仓库；服务层在单把锁内完成复合操作（原子传播）。"""

    def __init__(self, snapshot_path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._institutions: dict[str, Institution] = {}
        self._license_versions: dict[str, LicenseVersion] = {}
        self._change_requests: dict[str, ChangeRequest] = {}
        self._reports: dict[str, ImpactReport] = {}
        self._authorizations: dict[str, ProjectAuthorization] = {}
        self._appointments: dict[str, Appointment] = {}
        self._restrictions: dict[str, Restriction] = {}
        self._audit: list[AuditEvent] = []
        self._seq = 0
        self.snapshot_path = Path(snapshot_path) if snapshot_path else None

    # -- 锁 ---------------------------------------------------------------
    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def next_id(self, prefix: str) -> str:
        with self._lock:
            self._seq += 1
            return f"{prefix}-{self._seq:06d}"

    # -- 机构 -------------------------------------------------------------
    def add_institution(self, inst: Institution) -> None:
        self._institutions[inst.id] = inst

    def has_institution(self, inst_id: str) -> bool:
        return inst_id in self._institutions

    def get_institution(self, inst_id: str) -> Institution:
        inst = self._institutions.get(inst_id)
        if inst is None:
            raise DomainError(ErrorCode.NOT_FOUND, f"机构不存在：{inst_id}")
        return inst

    def list_institutions(self) -> list[Institution]:
        return sorted(self._institutions.values(), key=lambda x: x.id)

    # -- 许可证版本 --------------------------------------------------------
    def add_license_version(self, ver: LicenseVersion) -> None:
        if not ver.id:
            ver.id = self.next_id("LV")
        self._license_versions[ver.id] = ver

    def get_license_version(self, version_id: str) -> LicenseVersion:
        ver = self._license_versions.get(version_id)
        if ver is None:
            raise DomainError(ErrorCode.NOT_FOUND, f"许可证版本不存在：{version_id}")
        return ver

    def license_versions(self, institution_id: str) -> list[LicenseVersion]:
        rows = [v for v in self._license_versions.values() if v.institution_id == institution_id]
        return sorted(rows, key=lambda v: v.version_no)

    def current_license(self, institution_id: str) -> LicenseVersion:
        rows = self.license_versions(institution_id)
        if not rows:
            raise DomainError(ErrorCode.NOT_FOUND, f"机构尚无许可证登记：{institution_id}")
        return rows[-1]

    # -- 变更申请 ----------------------------------------------------------
    def add_change_request(self, req: ChangeRequest) -> None:
        self._change_requests[req.id] = req

    def get_change_request(self, req_id: str) -> ChangeRequest:
        req = self._change_requests.get(req_id)
        if req is None:
            raise DomainError(ErrorCode.NOT_FOUND, f"变更申请不存在：{req_id}")
        return req

    def list_change_requests(self, institution_id: str | None = None) -> list[ChangeRequest]:
        rows = list(self._change_requests.values())
        if institution_id:
            rows = [r for r in rows if r.institution_id == institution_id]
        return sorted(rows, key=lambda r: r.created_at)

    # -- 影响清单 ----------------------------------------------------------
    def save_report(self, report: ImpactReport) -> None:
        self._reports[report.request_id] = report

    def get_report(self, req_id: str) -> ImpactReport:
        report = self._reports.get(req_id)
        if report is None:
            raise DomainError(ErrorCode.NOT_FOUND, f"影响清单尚未生成：{req_id}")
        return report

    # -- 项目授权 / 预约 ----------------------------------------------------
    def add_authorization(self, auth: ProjectAuthorization) -> None:
        self._authorizations[auth.id] = auth

    def get_authorization(self, auth_id: str) -> ProjectAuthorization:
        auth = self._authorizations.get(auth_id)
        if auth is None:
            raise DomainError(ErrorCode.NOT_FOUND, f"项目授权不存在：{auth_id}")
        return auth

    def authorizations_for(self, institution_id: str) -> list[ProjectAuthorization]:
        return [a for a in self._authorizations.values() if a.institution_id == institution_id]

    def add_appointment(self, appt: Appointment) -> None:
        self._appointments[appt.id] = appt

    def get_appointment(self, appt_id: str) -> Appointment:
        appt = self._appointments.get(appt_id)
        if appt is None:
            raise DomainError(ErrorCode.NOT_FOUND, f"预约不存在：{appt_id}")
        return appt

    def appointments_for(self, institution_id: str) -> list[Appointment]:
        return [a for a in self._appointments.values() if a.institution_id == institution_id]

    # -- 限制 --------------------------------------------------------------
    def add_restriction(self, r: Restriction) -> None:
        self._restrictions[r.id] = r

    def get_restriction(self, restriction_id: str) -> Restriction:
        return self._restrictions[restriction_id]

    def restrictions_for(self, institution_id: str) -> list[Restriction]:
        return sorted(
            (r for r in self._restrictions.values() if r.institution_id == institution_id),
            key=lambda r: r.effective_at,
        )

    # -- 审计 --------------------------------------------------------------
    def add_audit(self, event: AuditEvent) -> None:
        if not event.id:
            event.id = self.next_id("AUD")
        self._audit.append(event)

    def audit_trail(self, target_type: str | None = None, target_id: str | None = None) -> list[AuditEvent]:
        rows = self._audit
        if target_type:
            rows = [e for e in rows if e.target_type == target_type]
        if target_id:
            rows = [e for e in rows if e.target_id == target_id]
        return list(rows)

    # -- 快照（演示/测试用） -------------------------------------------------
    def snapshot(self) -> dict:
        def enc(obj: object) -> object:
            if dataclasses.is_dataclass(obj):
                d = dataclasses.asdict(obj)
                return d
            if isinstance(obj, list):
                return [enc(x) for x in obj]
            if isinstance(obj, dict):
                return {k: enc(v) for k, v in obj.items()}
            if hasattr(obj, "value"):
                return obj.value
            return obj

        with self._lock:
            return {
                "institutions": [enc(x) for x in self._institutions.values()],
                "license_versions": [enc(x) for x in self._license_versions.values()],
                "change_requests": [enc(x) for x in self._change_requests.values()],
                "reports": [enc(x) for x in self._reports.values()],
                "authorizations": [enc(x) for x in self._authorizations.values()],
                "appointments": [enc(x) for x in self._appointments.values()],
                "restrictions": [enc(x) for x in self._restrictions.values()],
                "audit": [enc(x) for x in self._audit],
            }

    def save_snapshot(self) -> None:
        if not self.snapshot_path:
            raise ValueError("未配置 snapshot_path")
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.snapshot_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.snapshot_path)

    def iter_audit(self) -> Iterator[AuditEvent]:
        return iter(self._audit)
