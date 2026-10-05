"""核心领域服务。

职责：
1. 变更申请生命周期（登记/待核验/处置中/已决定/已归档）；
2. 批准前生成影响清单，阻塞项（blocker）全部处置才可批准（R-BLOCKER）；
3. 暂停/恢复/缩减/撤回替代链（chain_links，恢复显式 supersedes 暂停）；
4. 生效时在单个原子操作内追加许可证版本并传播限制，失败整体回滚；
5. 历史服务与未来禁止项分别给出判定依据（版本号 + 规则码 + 申请号）。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .errors import ConflictError, DomainError, NotFoundError
from .models import (
    Booking,
    BookingStatus,
    ChainLink,
    ChangeKind,
    ChangeRequest,
    ChangeState,
    DispositionAction,
    ImpactItem,
    Institution,
    ItemSeverity,
    ItemStatus,
    LicenseStatus,
    LicenseVersion,
    Project,
    ProjectStatus,
    STATE_TRANSITIONS,
    new_id,
)
from .repository import InMemoryStore

# 规则码：影响判定依据
R_LIC_CHAIN = "R-LIC-CHAIN"       # 许可证版本只追加、前后链接
R_BLOCKER = "R-BLOCKER"           # 阻塞项未处置不得批准
R_PROJ_ADDR = "R-PROJ-ADDR"       # 项目授权地址须与许可证地址一致
R_PROJ_SUBJ = "R-PROJ-SUBJ"       # 项目科目须在许可证科目范围内
R_PROJ_STATUS = "R-PROJ-STATUS"   # 停业/吊销期间项目授权不得提供服务
R_BOOK_DATE = "R-BOOK-DATE"       # 历史/未来边界：生效日之前已发生的服务不可改写
R_BOOK_STATUS = "R-BOOK-STATUS"   # 停业/吊销期间未来预约禁止履约
R_BOOK_SUBJ = "R-BOOK-SUBJ"      # 缩减科目的未来预约禁止履约
R_ATOMIC = "R-ATOMIC"             # 生效传播原子性

TODAY = date  # 便于测试 monkeypatch


class LicenseChangeService:
    def __init__(self, store: InMemoryStore | None = None) -> None:
        self.store = store or InMemoryStore()

    # ------------------------------------------------------------------ 基础数据

    def register_institution(
        self, institution_id: str, name: str, address: str,
        subjects: list[str] | set[str], effective_from: date | None = None,
    ) -> LicenseVersion:
        with self.store.lock:
            if institution_id in self.store.table("institutions"):
                raise ConflictError(f"机构已存在：{institution_id}")
            effective_from = effective_from or date.today()
            inst = Institution(institution_id, name, address, tuple(sorted(subjects)))
            self.store.table("institutions")[institution_id] = inst
            version = LicenseVersion(
                version_no=1, institution_id=institution_id, address=address,
                subjects=frozenset(subjects), status=LicenseStatus.ACTIVE,
                effective_from=effective_from, rationale="初始登记",
            )
            self.store.table("versions")[institution_id] = [version]
            self.store.table("current_version")[institution_id] = 1
            self.store.table("chains")[institution_id] = []
            return version

    def add_project(
        self, project_id: str, institution_id: str, name: str,
        address_required: str, subjects_required: set[str] | list[str],
    ) -> Project:
        with self.store.lock:
            self._institution(institution_id)
            if project_id in self.store.table("projects"):
                raise ConflictError(f"项目已存在：{project_id}")
            project = Project(
                id=project_id, institution_id=institution_id, name=name,
                address_required=address_required,
                subjects_required=frozenset(subjects_required),
            )
            self.store.table("projects")[project_id] = project
            return project

    def add_booking(
        self, booking_id: str, institution_id: str, project_id: str,
        subject: str, service_date: date,
        status: BookingStatus = BookingStatus.FUTURE,
    ) -> Booking:
        with self.store.lock:
            self._institution(institution_id)
            project = self.store.table("projects").get(project_id)
            if project is None or project.institution_id != institution_id:
                raise NotFoundError(f"项目不存在：{project_id}")
            if booking_id in self.store.table("bookings"):
                raise ConflictError(f"预约已存在：{booking_id}")
            booking = Booking(
                id=booking_id, institution_id=institution_id, project_id=project_id,
                subject=subject, service_date=service_date, status=status,
            )
            self.store.table("bookings")[booking_id] = booking
            return booking

    # ------------------------------------------------------------------ 查询

    def _institution(self, institution_id: str) -> Institution:
        inst = self.store.table("institutions").get(institution_id)
        if inst is None:
            raise NotFoundError(f"机构不存在：{institution_id}")
        return inst

    def _request(self, request_id: str) -> ChangeRequest:
        req = self.store.table("requests").get(request_id)
        if req is None:
            raise NotFoundError(f"变更申请不存在：{request_id}")
        return req

    def _version_at(self, institution_id: str, version_no: int) -> LicenseVersion:
        for version in self.store.table("versions")[institution_id]:
            if version.version_no == version_no:
                return version
        raise NotFoundError(f"许可证版本不存在：{institution_id} v{version_no}")

    def current_version(self, institution_id: str) -> LicenseVersion:
        self._institution(institution_id)
        no = self.store.table("current_version")[institution_id]
        return self._version_at(institution_id, no)

    def list_versions(self, institution_id: str) -> list[LicenseVersion]:
        self._institution(institution_id)
        return list(self.store.table("versions")[institution_id])

    def list_requests(self, institution_id: str) -> list[ChangeRequest]:
        return [
            r for r in self.store.table("requests").values()
            if r.institution_id == institution_id
        ]

    def get_chain(self, institution_id: str) -> list[dict[str, Any]]:
        self._institution(institution_id)
        return [link.to_dict() for link in self.store.table("chains")[institution_id]]

    def effective_version_at(self, institution_id: str, on_date: date) -> LicenseVersion | None:
        """on_date 当日生效的许可证版本（生效日 <= on_date 的最新版本）。"""
        versions = self.store.table("versions").get(institution_id, [])
        effective = [v for v in versions if v.effective_from <= on_date]
        if not effective:
            return None
        return max(effective, key=lambda v: (v.effective_from, v.version_no))

    # ------------------------------------------------------------------ 申请登记

    def create_change_request(
        self, institution_id: str, kind: ChangeKind, proposed: dict[str, Any],
        effective_date: date, created_by: str,
    ) -> ChangeRequest:
        with self.store.lock:
            inst = self._institution(institution_id)
            current = self.current_version(institution_id)

            # 同一机构同时只允许一个未决定申请，避免替代链分叉；已决定申请仅待归档
            for open_req in self.list_requests(institution_id):
                if open_req.state in (ChangeState.REGISTERED,
                                      ChangeState.PENDING_VERIFY,
                                      ChangeState.DISPOSING):
                    raise ConflictError(f"机构存在未决定申请：{open_req.id}")

            normalized = self._normalize_proposed(kind, proposed, current)
            self._validate_kind(kind, current, normalized)

            req = ChangeRequest(
                id=new_id("REQ"), institution_id=institution_id, kind=kind,
                state=ChangeState.REGISTERED, proposed=normalized,
                effective_date=effective_date, created_by=created_by,
            )
            req.history.append({"at": datetime.now().astimezone().isoformat(),
                                "event": "registered", "by": created_by})
            self.store.table("requests")[req.id] = req
            return req

    @staticmethod
    def _normalize_proposed(
        kind: ChangeKind, proposed: dict[str, Any], current: LicenseVersion,
    ) -> dict[str, Any]:
        if kind is ChangeKind.ADDRESS:
            return {"address": str(proposed["address"])}
        if kind is ChangeKind.SUBJECT_EXPAND:
            return {"add_subjects": sorted(set(proposed["add_subjects"]))}
        if kind is ChangeKind.SUBJECT_REDUCE:
            return {"remove_subjects": sorted(set(proposed["remove_subjects"]))}
        if kind is ChangeKind.SUSPEND:
            return {"status": LicenseStatus.SUSPENDED.value}
        if kind is ChangeKind.RESUME:
            return {"status": LicenseStatus.ACTIVE.value}
        if kind is ChangeKind.REVOKE:
            return {"status": LicenseStatus.REVOKED.value}
        if kind is ChangeKind.STATUS_CHANGE:
            return {"status": LicenseStatus(proposed["status"]).value}
        raise DomainError("unsupported_kind", f"不支持的变更类型：{kind}")

    @staticmethod
    def _validate_kind(
        kind: ChangeKind, current: LicenseVersion, normalized: dict[str, Any],
    ) -> None:
        if kind is ChangeKind.SUSPEND and current.status is not LicenseStatus.ACTIVE:
            raise DomainError("invalid_state", "仅正常状态许可证可申请暂停")
        if kind is ChangeKind.RESUME and current.status is not LicenseStatus.SUSPENDED:
            raise DomainError("invalid_state", "仅停业许可证可申请恢复")
        if kind is ChangeKind.REVOKE and current.status is LicenseStatus.REVOKED:
            raise DomainError("invalid_state", "许可证已吊销")
        if kind is ChangeKind.ADDRESS and normalized["address"] == current.address:
            raise DomainError("no_change", "新地址与现地址相同")
        if kind is ChangeKind.SUBJECT_REDUCE:
            remove = set(normalized["remove_subjects"])
            if not remove:
                raise DomainError("no_change", "缩减科目不能为空")
            missing = remove - current.subjects
            if missing:
                raise DomainError("subject_not_held", f"科目不在许可范围内：{sorted(missing)}")
        if kind is ChangeKind.SUBJECT_EXPAND:
            add = set(normalized["add_subjects"])
            if not add or add <= current.subjects:
                raise DomainError("no_change", "增设科目均已在许可范围内")

    def submit_for_verification(self, request_id: str, by: str) -> ChangeRequest:
        """登记 -> 待核验（监管受理）。"""
        with self.store.lock:
            req = self._request(request_id)
            self._transition(req, ChangeState.PENDING_VERIFY)
            req.history.append({"at": datetime.now().astimezone().isoformat(),
                                "event": "submitted_for_verification", "by": by})
            return req

    # ------------------------------------------------------------------ 影响清单

    def _target_state(
        self, kind: ChangeKind, normalized: dict[str, Any], current: LicenseVersion,
    ) -> dict[str, Any]:
        address, subjects, status = current.address, set(current.subjects), current.status
        if kind is ChangeKind.ADDRESS:
            address = normalized["address"]
        elif kind is ChangeKind.SUBJECT_EXPAND:
            subjects |= set(normalized["add_subjects"])
        elif kind is ChangeKind.SUBJECT_REDUCE:
            subjects -= set(normalized["remove_subjects"])
        elif kind in (ChangeKind.SUSPEND, ChangeKind.RESUME,
                      ChangeKind.REVOKE, ChangeKind.STATUS_CHANGE):
            status = LicenseStatus(normalized["status"])
        return {"address": address, "subjects": frozenset(subjects), "status": status}

    def generate_impact_list(self, request_id: str, by: str) -> ChangeRequest:
        """待核验 -> 处置中：对照目标状态扫描项目授权与未来预约，生成影响清单。"""
        with self.store.lock:
            req = self._request(request_id)
            if req.impact_generated_at is not None:
                raise ConflictError("影响清单已生成；新增引用将在生效时自动复核")
            if req.state is ChangeState.REGISTERED:
                self._transition(req, ChangeState.PENDING_VERIFY)
            self._transition(req, ChangeState.DISPOSING)
            current = self.current_version(req.institution_id)
            target = self._target_state(req.kind, req.proposed, current)

            items: list[ImpactItem] = []
            seq = 0

            def add_item(**kw: Any) -> None:
                nonlocal seq
                seq += 1
                items.append(ImpactItem(id=f"{req.id}-I{seq:02d}", **kw))

            # 项目授权（仅活跃/受限授权需要重新核验）
            for project in self.store.table("projects").values():
                if project.institution_id != req.institution_id:
                    continue
                if project.status in (ProjectStatus.TERMINATED, ProjectStatus.SUSPENDED):
                    continue
                basis_chain = f"{R_LIC_CHAIN}:v{current.version_no}->待生效v{current.version_no + 1}"
                if target["address"] != project.address_required:
                    add_item(
                        ref_type="project", ref_id=project.id,
                        severity=ItemSeverity.BLOCKER, status=ItemStatus.OPEN,
                        reason=f"项目「{project.name}」授权地址与许可证新地址不一致",
                        basis=f"{basis_chain}；{R_PROJ_ADDR}",
                        detail={"conflict": "address",
                                "from": project.address_required,
                                "to": target["address"]},
                    )
                removed = project.subjects_required - target["subjects"]
                if removed:
                    retained = project.subjects_required & target["subjects"]
                    add_item(
                        ref_type="project", ref_id=project.id,
                        severity=ItemSeverity.BLOCKER, status=ItemStatus.OPEN,
                        reason=f"项目「{project.name}」依赖科目被移出许可范围：{sorted(removed)}",
                        basis=f"{basis_chain}；{R_PROJ_SUBJ}",
                        detail={"conflict": "subjects", "removed_subjects": sorted(removed),
                                "retained_subjects": sorted(retained)},
                    )
                if target["status"] in (LicenseStatus.SUSPENDED, LicenseStatus.REVOKED):
                    add_item(
                        ref_type="project", ref_id=project.id,
                        severity=ItemSeverity.BLOCKER, status=ItemStatus.OPEN,
                        reason=f"许可证{target['status'].value == 'suspended' and '停业' or '吊销'}期间项目不得继续提供服务",
                        basis=f"{basis_chain}；{R_PROJ_STATUS}",
                        detail={"conflict": "status", "target_status": target["status"].value},
                    )

            # 未来业务引用：仅扫描生效日（含）之后的未决预约；历史服务不进入清单（R-BOOK-DATE）
            for booking in self.store.table("bookings").values():
                if booking.institution_id != req.institution_id:
                    continue
                if booking.status is not BookingStatus.FUTURE:
                    continue  # 历史/已处置引用不可改写
                if booking.service_date < req.effective_date:
                    continue  # 早于生效日，本变更对其无约束
                conflict = self._booking_conflict(booking, target)
                if conflict:
                    rule, reason = conflict
                    add_item(
                        ref_type="booking", ref_id=booking.id,
                        severity=ItemSeverity.BLOCKER, status=ItemStatus.OPEN,
                        reason=reason,
                        basis=(f"{R_LIC_CHAIN}:v{current.version_no}->待生效v{current.version_no + 1}；"
                               f"{R_BOOK_DATE}；{rule}"),
                        detail={"service_date": booking.service_date.isoformat(),
                                "subject": booking.subject,
                                "project_id": booking.project_id},
                    )

            req.impact_items = {i.id: i for i in items}
            req.impact_generated_at = datetime.now().astimezone()
            req.history.append({
                "at": req.impact_generated_at.isoformat(),
                "event": "impact_list_generated", "by": by,
                "blocker_count": sum(1 for i in items if i.severity is ItemSeverity.BLOCKER),
            })
            return req

    @staticmethod
    def _booking_conflict(
        booking: Booking, target: dict[str, Any],
    ) -> tuple[str, str] | None:
        if target["status"] is LicenseStatus.SUSPENDED:
            return R_BOOK_STATUS, "停业期间该未来预约不得履约"
        if target["status"] is LicenseStatus.REVOKED:
            return R_BOOK_STATUS, "许可证吊销后该未来预约禁止履约"
        if booking.subject not in target["subjects"]:
            return R_BOOK_SUBJ, f"科目「{booking.subject}」被缩减，该未来预约禁止履约"
        return None

    def impact_list(self, request_id: str) -> dict[str, Any]:
        with self.store.lock:
            req = self._request(request_id)
            items = list(req.impact_items.values())
            return {
                "request_id": req.id,
                "state": req.state.value,
                "generated": req.impact_generated_at is not None,
                "blockers_total": sum(1 for i in items if i.severity is ItemSeverity.BLOCKER),
                "blockers_open": len(req.open_blockers()),
                "approvable": (
                    req.impact_generated_at is not None
                    and not req.open_blockers()
                    and req.state is ChangeState.DISPOSING
                ),
                "items": [i.to_dict() for i in items],
            }

    # ------------------------------------------------------------------ 阻塞项处置

    _PROJECT_ACTIONS = {
        DispositionAction.SUSPEND_PROJECT, DispositionAction.RESTRICT_PROJECT,
        DispositionAction.TERMINATE_PROJECT,
    }
    _BOOKING_ACTIONS = {
        DispositionAction.CANCEL_BOOKING, DispositionAction.RESCHEDULE_BOOKING,
    }

    def dispose_blocker(
        self, request_id: str, item_id: str, action: DispositionAction,
        by: str, note: str = "", payload: dict[str, Any] | None = None,
    ) -> ImpactItem:
        with self.store.lock:
            req = self._request(request_id)
            if req.state is not ChangeState.DISPOSING:
                raise DomainError("invalid_state", "仅处置中申请可处置影响项")
            item = req.impact_items.get(item_id)
            if item is None:
                raise NotFoundError(f"影响项不存在：{item_id}")
            if item.status is ItemStatus.RESOLVED:
                raise ConflictError("影响项已处置")
            payload = payload or {}

            if item.ref_type == "project":
                self._validate_project_disposition(req, item, action, payload)
            else:
                self._validate_booking_disposition(req, item, action, payload)

            item.status = ItemStatus.RESOLVED
            item.disposition = action.value
            item.disposition_by = by
            item.disposition_at = datetime.now().astimezone()
            item.detail["disposition_note"] = note
            item.detail["disposition_payload"] = payload
            req.history.append({
                "at": item.disposition_at.isoformat(),
                "event": "item_resolved", "by": by,
                "item_id": item.id, "action": action.value,
            })
            return item

    def _validate_project_disposition(
        self, req: ChangeRequest, item: ImpactItem,
        action: DispositionAction, payload: dict[str, Any],
    ) -> None:
        if action not in self._PROJECT_ACTIONS:
            raise DomainError("invalid_action", f"项目影响项不支持处置动作：{action.value}")
        if action is DispositionAction.RESTRICT_PROJECT:
            if item.detail.get("conflict") != "subjects":
                raise DomainError("invalid_action", "地址或状态冲突不能以科目受限方式处置")
            current = self.current_version(req.institution_id)
            target = self._target_state(req.kind, req.proposed, current)
            project = self.store.table("projects")[item.ref_id]
            active = set(payload.get("active_subjects", []))
            allowed = project.subjects_required & target["subjects"]
            if not active or not active <= allowed:
                raise DomainError(
                    "invalid_subjects",
                    f"保留科目必须是许可范围与项目依赖的交集非空子集：{sorted(allowed)}",
                )

    def _validate_booking_disposition(
        self, req: ChangeRequest, item: ImpactItem,
        action: DispositionAction, payload: dict[str, Any],
    ) -> None:
        if action not in self._BOOKING_ACTIONS:
            raise DomainError("invalid_action", f"预约影响项不支持处置动作：{action.value}")
        if action is DispositionAction.RESCHEDULE_BOOKING:
            new_date = payload.get("new_date")
            if not isinstance(new_date, date):
                raise DomainError("invalid_date", "改期需提供 new_date")
            # 改期目标时间点必须允许该科目履约，否则只是把禁止项挪窝
            target_version = self.effective_version_at_map(req, new_date)
            if target_version is None:
                raise DomainError("no_effective_license", "改期日期无有效许可证版本")
            booking = self.store.table("bookings")[item.ref_id]
            if target_version.status in (LicenseStatus.SUSPENDED, LicenseStatus.REVOKED):
                raise DomainError("still_prohibited", "改期日期许可证处于停业/吊销状态")
            if booking.subject not in target_version.subjects:
                raise DomainError("still_prohibited", "改期日期该科目不在许可范围")

    def effective_version_at_map(
        self, req: ChangeRequest, on_date: date,
    ) -> LicenseVersion | None:
        """改期校验：若日期落在本次变更生效之后，按目标状态构造临时视图。"""
        current = self.current_version(req.institution_id)
        if on_date < req.effective_date:
            return self.effective_version_at(req.institution_id, on_date)
        target = self._target_state(req.kind, req.proposed, current)
        return LicenseVersion(
            version_no=current.version_no + 1, institution_id=req.institution_id,
            address=target["address"], subjects=target["subjects"],
            status=target["status"], effective_from=req.effective_date,
            change_request_id=req.id, prev_id=current.version_no,
        )

    # ------------------------------------------------------------------ 撤回

    def withdraw_request(self, request_id: str, by: str, reason: str) -> ChangeRequest:
        """登记/待核验/处置中 -> 已归档（申请撤回，替代链记录 withdraw 环）。"""
        with self.store.lock:
            req = self._request(request_id)
            if req.state not in (ChangeState.REGISTERED, ChangeState.PENDING_VERIFY,
                                 ChangeState.DISPOSING):
                raise DomainError("invalid_state", "已决定申请不可撤回")
            req.state = ChangeState.ARCHIVED
            req.withdrawn_at = datetime.now().astimezone()
            req.withdraw_reason = reason
            req.decision = "withdrawn"
            req.history.append({"at": req.withdrawn_at.isoformat(),
                                "event": "withdrawn", "by": by, "reason": reason})
            self._append_chain(req.institution_id, "withdraw", req.id, note=reason)
            return req

    # ------------------------------------------------------------------ 审批与生效

    def approve_change(self, request_id: str, by: str) -> ChangeRequest:
        """批准并立即生效：快照 -> 版本追加 + 限制传播，任一步失败整体回滚。"""
        with self.store.lock:
            req = self._request(request_id)
            if req.state is not ChangeState.DISPOSING:
                raise DomainError("invalid_state", "仅处置中申请可提交批准")
            if req.impact_generated_at is None:
                raise DomainError("impact_missing", "尚未生成影响清单")
            if req.open_blockers():
                ids = [i.id for i in req.open_blockers()]
                raise DomainError(
                    "blockers_open",
                    f"存在 {len(ids)} 个未处置阻塞项，依据 {R_BLOCKER} 不得批准：{ids}",
                )

            snap = self.store.snapshot()
            try:
                req = self._effective_propagate(req, by)
            except Exception:
                self.store.restore(snap)
                raise
            return req

    def reject_change(self, request_id: str, by: str, reason: str) -> ChangeRequest:
        with self.store.lock:
            req = self._request(request_id)
            if req.state is not ChangeState.DISPOSING:
                raise DomainError("invalid_state", "仅处置中申请可驳回")
            self._transition(req, ChangeState.DECIDED)
            req.decision = "rejected"
            req.decided_at = datetime.now().astimezone()
            req.decided_by = by
            req.history.append({"at": req.decided_at.isoformat(),
                                "event": "rejected", "by": by, "reason": reason})
            return req

    def archive_request(self, request_id: str) -> ChangeRequest:
        with self.store.lock:
            req = self._request(request_id)
            self._transition(req, ChangeState.ARCHIVED)
            req.history.append({"at": datetime.now().astimezone().isoformat(),
                                "event": "archived"})
            return req

    def _effective_propagate(self, req: ChangeRequest, by: str) -> ChangeRequest:
        institution_id = req.institution_id
        current = self.current_version(institution_id)
        target = self._target_state(req.kind, req.proposed, current)

        # 1) 生效前再次核验：清单生成后新增的项目授权若构成冲突，必须重新走清单
        handled_projects = {
            i.ref_id for i in req.impact_items.values()
            if i.ref_type == "project" and i.status is ItemStatus.RESOLVED
        }
        for project in self.store.table("projects").values():
            if project.institution_id != institution_id:
                continue
            if project.status in (ProjectStatus.TERMINATED, ProjectStatus.SUSPENDED):
                continue
            if project.id in handled_projects:
                continue
            if self._project_conflicts(project, target):
                raise DomainError(
                    "new_blocker",
                    f"清单生成后新增项目「{project.id}」与变更冲突，"
                    f"依据 {R_BLOCKER} 需重新生成影响清单并处置",
                )

        # 2) 追加许可证版本（版本链只追加、前后相接）
        new_no = current.version_no + 1
        version = LicenseVersion(
            version_no=new_no, institution_id=institution_id,
            address=target["address"], subjects=target["subjects"],
            status=target["status"], effective_from=req.effective_date,
            change_request_id=req.id, prev_id=current.version_no,
            rationale=f"变更申请 {req.id}（{req.kind.value}）批准生效",
        )
        self.store.table("versions")[institution_id].append(version)
        self.store.table("current_version")[institution_id] = new_no
        inst = self.store.table("institutions")[institution_id]
        inst.address = target["address"]
        inst.subjects = tuple(sorted(target["subjects"]))
        inst.status = target["status"]

        # 3) 按阻塞项处置决定原子传播
        for item in req.impact_items.values():
            if item.status is not ItemStatus.RESOLVED:
                continue
            action = DispositionAction(item.disposition)
            payload = item.detail.get("disposition_payload", {})
            if item.ref_type == "project":
                self._apply_project_action(item.ref_id, action, payload, req)
            else:
                self._apply_booking_action(item.ref_id, action, payload, req)

        # 4) 恢复链反向传播：暂停期挂起的项目/预约在恢复日后解冻
        if req.kind is ChangeKind.RESUME:
            suspend_id = self._find_active_suspend(institution_id)
            self._propagate_resume(req, target, suspend_id)

        # 5) 安全网：清单后新增/漏网的未来冲突预约，生效时刻原子禁止，杜绝停业期间服务
        for booking in self.store.table("bookings").values():
            if booking.institution_id != institution_id:
                continue
            if booking.status is not BookingStatus.FUTURE:
                continue
            if booking.service_date < req.effective_date:
                continue
            if self._booking_conflict(booking, target):
                booking.status = BookingStatus.FORBIDDEN
                booking.resolved_by = req.id
                booking.resolution_note = f"生效时原子传播禁止（{R_ATOMIC}）"
                req.history.append({
                    "at": datetime.now().astimezone().isoformat(),
                    "event": "booking_auto_forbidden",
                    "booking_id": booking.id, "basis": f"{R_ATOMIC}；{R_BLOCKER}",
                })

        # 5) 替代链
        self._record_chain(req)

        # 6) 申请定稿
        self._transition(req, ChangeState.DECIDED)
        req.decision = "approved"
        req.new_version_no = new_no
        req.decided_at = datetime.now().astimezone()
        req.decided_by = by
        req.history.append({
            "at": req.decided_at.isoformat(), "event": "approved_effective",
            "by": by, "new_version_no": new_no, "basis": R_ATOMIC,
        })
        return req

    @staticmethod
    def _project_conflicts(project: Project, target: dict[str, Any]) -> bool:
        if target["address"] != project.address_required:
            return True
        if project.subjects_required - target["subjects"]:
            return True
        if target["status"] in (LicenseStatus.SUSPENDED, LicenseStatus.REVOKED):
            return True
        return False

    def _apply_project_action(
        self, project_id: str, action: DispositionAction,
        payload: dict[str, Any], req: ChangeRequest,
    ) -> None:
        project: Project = self.store.table("projects")[project_id]
        at = req.effective_date.isoformat()
        if action is DispositionAction.SUSPEND_PROJECT:
            project.status = ProjectStatus.SUSPENDED
        elif action is DispositionAction.TERMINATE_PROJECT:
            project.status = ProjectStatus.TERMINATED
        elif action is DispositionAction.RESTRICT_PROJECT:
            project.status = ProjectStatus.RESTRICTED
            project.active_subjects = frozenset(payload["active_subjects"])
        else:
            return
        project.history.append({
            "date": at, "action": action.value,
            "by_request": req.id, "basis": f"{R_ATOMIC}；{R_PROJ_STATUS}/{R_PROJ_SUBJ}",
            "payload": {k: (sorted(v) if isinstance(v, (set, frozenset)) else v)
                        for k, v in payload.items()},
        })

    def _apply_booking_action(
        self, booking_id: str, action: DispositionAction,
        payload: dict[str, Any], req: ChangeRequest,
    ) -> None:
        booking: Booking = self.store.table("bookings")[booking_id]
        if action is DispositionAction.CANCEL_BOOKING:
            booking.status = BookingStatus.CANCELLED
            booking.resolved_by = req.id
            booking.resolution_note = f"变更批准前处置：取消（{R_BOOK_DATE}）"
        elif action is DispositionAction.RESCHEDULE_BOOKING:
            replacement = Booking(
                id=new_id("BKG"), institution_id=booking.institution_id,
                project_id=booking.project_id, subject=booking.subject,
                service_date=payload["new_date"], status=BookingStatus.FUTURE,
                resolution_note="",
            )
            self.store.table("bookings")[replacement.id] = replacement
            booking.status = BookingStatus.RESCHEDULED
            booking.resolved_by = req.id
            booking.resolution_note = (
                f"变更批准前处置：改期为 {payload['new_date'].isoformat()}，"
                f"新预约 {replacement.id}（{R_BOOK_DATE}）"
            )

    def _propagate_resume(
        self, req: ChangeRequest, target: dict[str, Any], suspend_id: str | None,
    ) -> None:
        """恢复替代暂停：解冻暂停链挂起的项目与原子禁止的未来预约。

        显式处置（取消/改期/终止）不自动复活；仅解冻暂停生效时安全网原子禁止的引用。
        """
        at = req.effective_date.isoformat()
        for project in self.store.table("projects").values():
            if project.institution_id != req.institution_id:
                continue
            if project.status is not ProjectStatus.SUSPENDED:
                continue
            suspended_by_suspend = any(
                h.get("action") == DispositionAction.SUSPEND_PROJECT.value
                and h.get("by_request") == suspend_id
                for h in project.history
            )
            if suspend_id is not None and not suspended_by_suspend:
                continue
            if (project.address_required == target["address"]
                    and not (project.subjects_required - target["subjects"])):
                project.status = ProjectStatus.AUTHORIZED
                project.history.append({
                    "date": at, "action": "resume_project", "by_request": req.id,
                    "supersedes": suspend_id,
                    "basis": f"{R_ATOMIC}；替代链恢复",
                })

        for booking in self.store.table("bookings").values():
            if booking.institution_id != req.institution_id:
                continue
            if booking.status is not BookingStatus.FORBIDDEN:
                continue
            if suspend_id is None or booking.resolved_by != suspend_id:
                continue
            if booking.service_date < req.effective_date:
                continue  # 恢复日之前仍在停业期，保持禁止
            if booking.subject in target["subjects"] and target["status"] is LicenseStatus.ACTIVE:
                booking.status = BookingStatus.FUTURE
                note = booking.resolution_note
                booking.resolved_by = None
                booking.resolution_note = (
                    f"恢复申请 {req.id}（替代暂停 {suspend_id}）解冻；原依据：{note}"
                )
                req.history.append({
                    "at": datetime.now().astimezone().isoformat(),
                    "event": "booking_unfrozen", "booking_id": booking.id,
                    "supersedes": suspend_id, "basis": f"{R_ATOMIC}；替代链恢复",
                })

    # ------------------------------------------------------------------ 替代链

    def _record_chain(self, req: ChangeRequest) -> None:
        if req.kind is ChangeKind.SUSPEND:
            self._append_chain(req.institution_id, "suspend", req.id)
        elif req.kind is ChangeKind.RESUME:
            suspend_id = self._find_active_suspend(req.institution_id)
            req.supersedes_id = suspend_id
            self._append_chain(
                req.institution_id, "resume", req.id,
                related=suspend_id,
                note="恢复申请替代暂停申请，许可证版本链接续" if suspend_id else "未找到对应暂停申请",
            )
        elif req.kind is ChangeKind.SUBJECT_REDUCE:
            self._append_chain(req.institution_id, "reduce", req.id)
        elif req.kind is ChangeKind.REVOKE:
            self._append_chain(req.institution_id, "revoke", req.id)

    def _find_active_suspend(self, institution_id: str) -> str | None:
        requests = self.store.table("requests")
        resumed = {
            r.supersedes_id for r in requests.values()
            if r.institution_id == institution_id
            and r.kind is ChangeKind.RESUME and r.supersedes_id
        }
        candidates = [
            r for r in requests.values()
            if r.institution_id == institution_id
            and r.kind is ChangeKind.SUSPEND
            and r.decision == "approved" and r.id not in resumed
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.decided_at or r.created_at).id

    def _append_chain(
        self, institution_id: str, link_type: str, request_id: str,
        related: str | None = None, note: str = "",
    ) -> None:
        chain = self.store.table("chains")[institution_id]
        chain.append(ChainLink(
            seq=len(chain) + 1, link_type=link_type,
            change_request_id=request_id, related_request_id=related, note=note,
        ))

    # ------------------------------------------------------------------ 状态迁移

    @staticmethod
    def _transition(req: ChangeRequest, target: ChangeState) -> None:
        if target not in STATE_TRANSITIONS[req.state] and target is not req.state:
            raise DomainError(
                "illegal_transition",
                f"申请 {req.id} 不能从 {req.state.value} 迁移到 {target.value}",
            )
        req.state = target

    # ------------------------------------------------------------------ 历史/未来判定

    def classify_booking(self, booking_id: str, today: date | None = None) -> dict[str, Any]:
        """区分历史服务与未来禁止项，并给出依据（版本号/规则码/申请号）。"""
        with self.store.lock:
            today = today or date.today()
            booking: Booking = self.store.table("bookings").get(booking_id)
            if booking is None:
                raise NotFoundError(f"预约不存在：{booking_id}")
            version = self.effective_version_at(booking.institution_id, booking.service_date)
            version_ref = (
                f"v{version.version_no}"
                + (f"（申请 {version.change_request_id} 生效）" if version.change_request_id else "")
                if version else "无生效版本"
            )

            if booking.is_historical:
                return {
                    "booking_id": booking.id,
                    "service_date": booking.service_date.isoformat(),
                    "subject": booking.subject,
                    "classification": "historical_service",
                    "enforceable": False,
                    "allowed": True,
                    "reason": "生效日之前已实际发生的服务为历史服务，限制不溯及既往",
                    "basis": [f"{R_BOOK_DATE}:历史/未来边界", f"{R_LIC_CHAIN}:{version_ref}"],
                }

            if booking.status is BookingStatus.CANCELLED:
                return {
                    "booking_id": booking.id,
                    "service_date": booking.service_date.isoformat(),
                    "subject": booking.subject,
                    "classification": "future_disposed_cancelled",
                    "enforceable": False, "allowed": False,
                    "reason": "未来预约已在变更批准前取消处置",
                    "basis": [f"{R_BOOK_DATE}", f"处置申请 {booking.resolved_by}"],
                }
            if booking.status is BookingStatus.RESCHEDULED:
                return {
                    "booking_id": booking.id,
                    "service_date": booking.service_date.isoformat(),
                    "subject": booking.subject,
                    "classification": "future_disposed_rescheduled",
                    "enforceable": False, "allowed": False,
                    "reason": booking.resolution_note,
                    "basis": [f"{R_BOOK_DATE}", f"处置申请 {booking.resolved_by}"],
                }
            if booking.status is BookingStatus.FORBIDDEN:
                return {
                    "booking_id": booking.id,
                    "service_date": booking.service_date.isoformat(),
                    "subject": booking.subject,
                    "classification": "future_prohibited",
                    "enforceable": True, "allowed": False,
                    "reason": booking.resolution_note or "生效时原子传播禁止",
                    "basis": [f"{R_LIC_CHAIN}:{version_ref}", f"{R_BOOK_STATUS}/{R_BOOK_SUBJ}",
                              f"{R_ATOMIC}", f"禁止来源申请 {booking.resolved_by}"],
                }

            # FUTURE：按服务日生效版本判定
            if version is None:
                return {
                    "booking_id": booking.id,
                    "service_date": booking.service_date.isoformat(),
                    "subject": booking.subject,
                    "classification": "future_prohibited",
                    "enforceable": True, "allowed": False,
                    "reason": "该日期无有效许可证版本",
                    "basis": [f"{R_LIC_CHAIN}:无生效版本"],
                }
            if version.status in (LicenseStatus.SUSPENDED, LicenseStatus.REVOKED):
                return {
                    "booking_id": booking.id,
                    "service_date": booking.service_date.isoformat(),
                    "subject": booking.subject,
                    "classification": "future_prohibited",
                    "enforceable": True, "allowed": False,
                    "reason": f"许可证{version.status.value}期间不得履约",
                    "basis": [f"{R_LIC_CHAIN}:{version_ref}", R_BOOK_STATUS],
                }
            if booking.subject not in version.subjects:
                return {
                    "booking_id": booking.id,
                    "service_date": booking.service_date.isoformat(),
                    "subject": booking.subject,
                    "classification": "future_prohibited",
                    "enforceable": True, "allowed": False,
                    "reason": f"科目「{booking.subject}」不在当日许可范围",
                    "basis": [f"{R_LIC_CHAIN}:{version_ref}", R_BOOK_SUBJ],
                }
            return {
                "booking_id": booking.id,
                "service_date": booking.service_date.isoformat(),
                "subject": booking.subject,
                "classification": "future_allowed",
                "enforceable": True, "allowed": True,
                "reason": "服务日许可证有效且科目在范围内",
                "basis": [f"{R_LIC_CHAIN}:{version_ref}"],
            }

    def list_bookings_classified(
        self, institution_id: str, today: date | None = None,
    ) -> list[dict[str, Any]]:
        with self.store.lock:
            self._institution(institution_id)
            ids = sorted(
                b.id for b in self.store.table("bookings").values()
                if b.institution_id == institution_id
            )
        return [self.classify_booking(bid, today) for bid in ids]
