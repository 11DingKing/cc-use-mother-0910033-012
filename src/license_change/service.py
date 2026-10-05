"""许可证变更影响领域服务。

职责：
1. 维护许可证版本链（每次生效追加版本，旧版本标记被替代时刻）；
2. 受理变更申请并驱动 登记→待核验→处置中→已决定→生效/归档 状态机；
3. 批准前生成影响清单（项目授权依赖、未完成预约、历史服务），阻塞项必须处置；
4. 暂停/恢复/科目缩减/撤回通过替代链（supersedes）关联；
5. 生效时在单把存储锁内原子传播限制（版本、限制、授权、预约一次性落库）；
6. 对外提供“历史服务 vs 未来禁止项”判定，并给出规则依据。
"""
from __future__ import annotations

import json
from typing import Any

from .errors import DomainError, ErrorCode
from .models import (
    Appointment,
    AppointmentStatus,
    AuditEvent,
    AuthorizationStatus,
    ChangeRequest,
    DispositionAction,
    ImpactItem,
    ImpactKind,
    ImpactReport,
    Institution,
    LicenseStatus,
    LicenseVersion,
    ProjectAuthorization,
    RequestKind,
    RequestStatus,
    Restriction,
    Severity,
)
from .repository import Store
from .time_utils import is_at_or_before, is_before, now_iso, parse_dt

# ---------------------------------------------------------------------------
# 判定规则依据（API 返回 basis_rule，前端/监管可据此说明“凭什么禁止/允许”）
# ---------------------------------------------------------------------------
RULES: dict[str, str] = {
    "R-HIST-001": "生效时点之前已完成的服务属于历史服务，限制不溯及既往，仅保留溯源。",
    "R-AUTH-HIST-002": "授权有效期完全早于生效时点的项目授权无未来履约可能，归为历史引用。",
    "R-SUSP-001": "暂停/吊销生效后，机构在所有科目上禁止提供未来服务，未完成预约必须取消。",
    "R-SUSP-002": "暂停/吊销生效后，覆盖生效时点的项目授权必须终止，纯未来授权必须终止或撤回。",
    "R-REDU-001": "科目缩减生效后，被缩减科目禁止服务，相关未来预约必须取消或改至保留科目。",
    "R-REDU-002": "依赖被缩减科目的项目授权必须限定范围（剔除该科目）或终止。",
    "R-ADDR-001": "地址变更生效后，原地址不得继续接诊，原地址未来预约必须取消或改期至新址。",
    "R-ADDR-002": "登记于原地址的项目授权须重新核验执业地址，未核验前不得在新址履约。",
    "R-RESUME-001": "恢复执业仅解除暂停链上的限制；科目缩减形成的限制继续有效。",
    "R-PROPAGATE-001": "生效时限制对版本、授权、预约原子传播，任一步失败则整体不生效。",
}

_OPEN_STATUSES = {
    RequestStatus.REGISTERED,
    RequestStatus.PENDING_VERIFICATION,
    RequestStatus.IN_DISPOSITION,
    RequestStatus.DECIDED,
}

# 处置动作 -> 适用的影响项类型
_ACTION_FOR_KIND: dict[ImpactKind, set[DispositionAction]] = {
    ImpactKind.FUTURE_APPOINTMENT: {
        DispositionAction.CANCEL_APPOINTMENT,
        DispositionAction.RESCHEDULE_APPOINTMENT,
        DispositionAction.REGULATOR_WAIVE,
    },
    ImpactKind.FUTURE_PROJECT: {
        DispositionAction.TERMINATE_AUTHORIZATION,
        DispositionAction.RESTRICT_AUTHORIZATION,
        DispositionAction.REGULATOR_WAIVE,
    },
    ImpactKind.ONGOING_PROJECT: {
        DispositionAction.TERMINATE_AUTHORIZATION,
        DispositionAction.RESTRICT_AUTHORIZATION,
        DispositionAction.REGULATOR_WAIVE,
    },
}


class LicenseChangeService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # 基础登记
    # ------------------------------------------------------------------
    def register_institution(
        self,
        institution_id: str,
        name: str,
        address: str,
        subjects: list[str],
        *,
        at: str | None = None,
    ) -> dict:
        """登记机构与许可证初始版本（版本链 v1）。"""
        at = at or now_iso()
        with self.store.lock:
            if self.store.has_institution(institution_id):
                raise DomainError(ErrorCode.CONFLICT, f"机构已登记：{institution_id}")
            if not subjects:
                raise DomainError(ErrorCode.VALIDATION, "初始诊疗科目不能为空")
            inst = Institution(id=institution_id, name=name, created_at=at)
            self.store.add_institution(inst)
            ver = LicenseVersion(
                institution_id=institution_id,
                version_no=1,
                status=LicenseStatus.ACTIVE,
                address=address,
                subjects=list(subjects),
                effective_at=at,
                reason="初始登记",
            )
            self.store.add_license_version(ver)
            self._audit(at, "system", "REGISTER_INSTITUTION", "institution", institution_id,
                        {"name": name, "license_version_id": ver.id})
            return {"institution": inst.to_dict(), "license_version": ver.to_dict()}

    def add_project_authorization(
        self,
        auth_id: str,
        project_id: str,
        institution_id: str,
        required_subjects: list[str],
        valid_from: str,
        valid_to: str,
    ) -> ProjectAuthorization:
        with self.store.lock:
            self.store.get_institution(institution_id)
            if not is_before(valid_from, valid_to):
                raise DomainError(ErrorCode.VALIDATION, "授权有效期无效：valid_from 必须早于 valid_to")
            current = self.store.current_license(institution_id)
            unknown = set(required_subjects) - set(current.subjects)
            if unknown:
                raise DomainError(
                    ErrorCode.VALIDATION,
                    "依赖的诊疗科目不在许可证范围内：" + "、".join(sorted(unknown)),
                )
            # 授权窗口的未来部分不得命中生效限制（停业/科目缩减期间不得新增依赖）
            probe_at = max(parse_dt(valid_from), parse_dt(now_iso())).isoformat()
            for subject in required_subjects:
                verdict = self.check_service(institution_id, subject, probe_at)
                if not verdict["allowed"]:
                    rule = verdict["basis"][0]["rule"]
                    raise DomainError(
                        ErrorCode.ILLEGAL_TRANSITION,
                        f"科目 {subject} 在授权期内被限制（{rule}），不得登记项目授权",
                        reasons=[b["rule_text"] for b in verdict["basis"]],
                    )
            auth = ProjectAuthorization(
                id=auth_id, project_id=project_id, institution_id=institution_id,
                required_subjects=list(required_subjects),
                valid_from=valid_from, valid_to=valid_to,
            )
            self.store.add_authorization(auth)
            return auth

    def add_appointment(
        self, appt_id: str, institution_id: str, subject: str, scheduled_at: str
    ) -> Appointment:
        with self.store.lock:
            self.store.get_institution(institution_id)
            current = self.store.current_license(institution_id)
            if subject not in current.subjects:
                raise DomainError(ErrorCode.VALIDATION, f"科目不在许可证范围内：{subject}")
            # 未来预约直接对生效限制做时点判定，停业期间不得开新单
            if not is_before(scheduled_at, now_iso()):
                verdict = self.check_service(institution_id, subject, scheduled_at)
                if not verdict["allowed"]:
                    rule = verdict["basis"][0]["rule"]
                    raise DomainError(
                        ErrorCode.ILLEGAL_TRANSITION,
                        f"该时点 {subject} 已被禁止提供服务（{rule}），不得创建预约",
                        reasons=[b["rule_text"] for b in verdict["basis"]],
                    )
            appt = Appointment(
                id=appt_id, institution_id=institution_id, subject=subject,
                scheduled_at=scheduled_at,
            )
            self.store.add_appointment(appt)
            return appt

    # ------------------------------------------------------------------
    # 变更申请
    # ------------------------------------------------------------------
    def create_change_request(
        self,
        req_id: str,
        institution_id: str,
        kind: RequestKind | str,
        created_by: str,
        payload: dict[str, Any] | None = None,
        *,
        expected_effective_at: str | None = None,
        at: str | None = None,
        supersedes_id: str | None = None,
    ) -> ChangeRequest:
        kind = RequestKind(kind)
        payload = dict(payload or {})
        at = at or now_iso()
        with self.store.lock:
            current = self.store.current_license(institution_id)
            self._validate_payload(kind, payload, current)
            supersede_chain_note = ""
            if supersedes_id:
                old = self.store.get_change_request(supersedes_id)
                if old.institution_id != institution_id:
                    raise DomainError(ErrorCode.VALIDATION, "不能替代其他机构的申请")
                if old.status in _OPEN_STATUSES:
                    # 旧申请尚未生效：撤回旧申请，由新申请替代
                    old.status = RequestStatus.WITHDRAWN
                    old.withdrawn_at = at
                    old.withdraw_reason = f"被新申请 {req_id}（{kind.label}）替代"
                    supersede_chain_note = "withdrawn_before_effective"
                elif old.status is RequestStatus.EFFECTIVE:
                    # 旧申请已生效（如恢复替代已生效的暂停）：历史保留，仅链接
                    supersede_chain_note = "links_to_effective"
                else:
                    raise DomainError(
                        ErrorCode.ILLEGAL_TRANSITION,
                        f"原申请已终结（{old.status.label}），不能被替代",
                    )
            req = ChangeRequest(
                id=req_id,
                institution_id=institution_id,
                kind=kind,
                status=RequestStatus.REGISTERED,
                created_by=created_by,
                created_at=at,
                payload=payload,
                expected_effective_at=expected_effective_at,
                supersedes_id=supersedes_id,
            )
            self.store.add_change_request(req)
            if supersedes_id:
                old.replaced_by_id = req_id
                self._audit(at, created_by, "SUPERSEDE_REQUEST", "change_request", old.id,
                            {"replaced_by": req_id, "new_kind": kind.value,
                             "mode": supersede_chain_note})
            self._audit(at, created_by, "CREATE_REQUEST", "change_request", req_id,
                        {"kind": kind.value, "payload": payload})
            return req

    def _validate_payload(self, kind: RequestKind, payload: dict, current: LicenseVersion) -> None:
        if kind is RequestKind.ADDRESS_CHANGE:
            new_address = (payload.get("new_address") or "").strip()
            if not new_address:
                raise DomainError(ErrorCode.VALIDATION, "地址变更必须提供 new_address")
            if new_address == current.address:
                raise DomainError(ErrorCode.VALIDATION, "新地址与当前地址相同")
            payload["new_address"] = new_address
        elif kind is RequestKind.SUBJECT_REDUCTION:
            removed = payload.get("removed_subjects")
            if not isinstance(removed, list) or not removed:
                raise DomainError(ErrorCode.VALIDATION, "科目缩减必须提供非空 removed_subjects")
            unknown = set(removed) - set(current.subjects)
            if unknown:
                raise DomainError(
                    ErrorCode.VALIDATION, "被缩减科目不在当前许可证范围内：" + "、".join(sorted(unknown))
                )
            if set(removed) == set(current.subjects):
                raise DomainError(ErrorCode.VALIDATION, "不能缩减全部科目；全部停止应申请暂停执业")
            payload["removed_subjects"] = list(dict.fromkeys(removed))
        elif kind is RequestKind.LICENSE_SUSPEND:
            if not (payload.get("reason") or "").strip():
                raise DomainError(ErrorCode.VALIDATION, "暂停执业必须填写 reason")
        elif kind is RequestKind.LICENSE_REVOKE:
            if not (payload.get("reason") or "").strip():
                raise DomainError(ErrorCode.VALIDATION, "吊销必须填写 reason")
        elif kind is RequestKind.LICENSE_RESUME:
            if current.status is not LicenseStatus.SUSPENDED:
                raise DomainError(
                    ErrorCode.ILLEGAL_TRANSITION,
                    f"当前许可证状态为{current.status.label}，不能恢复执业",
                )

    def submit_for_verification(self, req_id: str, *, at: str | None = None) -> ChangeRequest:
        """登记 -> 待核验。"""
        at = at or now_iso()
        with self.store.lock:
            req = self.store.get_change_request(req_id)
            self._require_status(req, {RequestStatus.REGISTERED})
            req.status = RequestStatus.PENDING_VERIFICATION
            self._audit(at, req.created_by, "SUBMIT_VERIFICATION", "change_request", req_id, {})
            return req

    def withdraw_request(self, req_id: str, actor: str, reason: str, *, at: str | None = None) -> ChangeRequest:
        """申请撤回：仅未生效的申请可撤回（替代链叶子主动撤回）。"""
        at = at or now_iso()
        if not reason.strip():
            raise DomainError(ErrorCode.VALIDATION, "撤回必须填写原因")
        with self.store.lock:
            req = self.store.get_change_request(req_id)
            self._require_status(req, _OPEN_STATUSES, action="撤回")
            req.status = RequestStatus.WITHDRAWN
            req.withdrawn_at = at
            req.withdraw_reason = reason
            self._audit(at, actor, "WITHDRAW_REQUEST", "change_request", req_id, {"reason": reason})
            return req

    def reject_request(self, req_id: str, actor: str, reason: str, *, at: str | None = None) -> ChangeRequest:
        at = at or now_iso()
        with self.store.lock:
            req = self.store.get_change_request(req_id)
            self._require_status(
                req, {RequestStatus.PENDING_VERIFICATION, RequestStatus.IN_DISPOSITION}, action="驳回"
            )
            req.status = RequestStatus.REJECTED
            req.decided_at = at
            req.decided_by = actor
            req.note = reason
            self._audit(at, actor, "REJECT_REQUEST", "change_request", req_id, {"reason": reason})
            return req

    # ------------------------------------------------------------------
    # 影响清单
    # ------------------------------------------------------------------
    def generate_impact_report(self, req_id: str, *, at: str | None = None) -> ImpactReport:
        """批准前生成影响清单；生成后申请进入处置中。"""
        at = at or now_iso()
        with self.store.lock:
            req = self.store.get_change_request(req_id)
            self._require_status(
                req,
                {RequestStatus.PENDING_VERIFICATION, RequestStatus.IN_DISPOSITION},
                action="生成影响清单",
            )
            effective_at = req.expected_effective_at or at
            items = self._build_impact_items(req, effective_at)
            report = ImpactReport(request_id=req_id, generated_at=at, items=items)
            self.store.save_report(report)
            req.status = RequestStatus.IN_DISPOSITION
            self._audit(at, req.created_by, "GENERATE_IMPACT_REPORT", "change_request", req_id,
                        {"total": len(items),
                         "blocking": sum(1 for i in items if i.severity is Severity.BLOCKING)})
            return report

    def _build_impact_items(self, req: ChangeRequest, effective_at: str) -> list[ImpactItem]:
        items: list[ImpactItem] = []
        removed = set(req.payload.get("removed_subjects", []))

        # --- 预约 ---
        for appt in self.store.appointments_for(req.institution_id):
            if appt.status is AppointmentStatus.COMPLETED and is_at_or_before(
                appt.scheduled_at, effective_at
            ):
                items.append(ImpactItem(
                    id=self.store.next_id("II"),
                    kind=ImpactKind.HISTORICAL_SERVICE, severity=Severity.HISTORICAL,
                    reference_type="appointment", reference_id=appt.id,
                    detail=f"历史服务：{appt.subject} 已于 {appt.scheduled_at} 完成",
                    basis_rule="R-HIST-001",
                    required_action="无需处置，归档溯源",
                ))
                continue
            if appt.status is not AppointmentStatus.BOOKED:
                continue  # 已取消的未来预约不再列入
            if is_before(appt.scheduled_at, effective_at):
                items.append(ImpactItem(
                    id=self.store.next_id("II"),
                    kind=ImpactKind.FUTURE_APPOINTMENT, severity=Severity.ADVISORY,
                    reference_type="appointment", reference_id=appt.id,
                    detail=f"预约时间 {appt.scheduled_at} 早于生效时点且未完成，需线下核验",
                    basis_rule="R-SUSP-001",
                    required_action="线下核验履约情况",
                ))
                continue
            affected, rule, advice = self._appointment_effect(req.kind, appt, removed)
            if affected:
                items.append(ImpactItem(
                    id=self.store.next_id("II"),
                    kind=ImpactKind.FUTURE_APPOINTMENT, severity=Severity.BLOCKING,
                    reference_type="appointment", reference_id=appt.id,
                    detail=f"未来预约：{appt.subject} @ {appt.scheduled_at}，与{req.kind.label}冲突",
                    basis_rule=rule, required_action=advice,
                ))

        # --- 项目授权 ---
        for auth in self.store.authorizations_for(req.institution_id):
            if auth.status is not AuthorizationStatus.ACTIVE:
                continue  # 已终止/受限的授权在其被终止的申请中留痕，不重复开单
            if is_at_or_before(auth.valid_to, effective_at):
                items.append(ImpactItem(
                    id=self.store.next_id("II"),
                    kind=ImpactKind.HISTORICAL_SERVICE, severity=Severity.HISTORICAL,
                    reference_type="project_authorization", reference_id=auth.id,
                    detail=f"授权窗口 {auth.valid_from}~{auth.valid_to} 完全早于生效时点",
                    basis_rule="R-AUTH-HIST-002",
                    required_action="无需处置，归档溯源",
                ))
                continue
            hits_subjects = bool(removed & set(auth.required_subjects))
            if req.kind in (RequestKind.LICENSE_SUSPEND, RequestKind.LICENSE_REVOKE):
                kind = (
                    ImpactKind.ONGOING_PROJECT
                    if is_before(auth.valid_from, effective_at)
                    else ImpactKind.FUTURE_PROJECT
                )
                items.append(ImpactItem(
                    id=self.store.next_id("II"),
                    kind=kind, severity=Severity.BLOCKING,
                    reference_type="project_authorization", reference_id=auth.id,
                    detail=f"项目 {auth.project_id} 授权 {auth.valid_from}~{auth.valid_to} 覆盖生效时点",
                    basis_rule="R-SUSP-002",
                    required_action="终止授权（停业期间不得履约）",
                ))
            elif req.kind is RequestKind.SUBJECT_REDUCTION and hits_subjects:
                kind = (
                    ImpactKind.ONGOING_PROJECT
                    if is_before(auth.valid_from, effective_at)
                    else ImpactKind.FUTURE_PROJECT
                )
                items.append(ImpactItem(
                    id=self.store.next_id("II"),
                    kind=kind, severity=Severity.BLOCKING,
                    reference_type="project_authorization", reference_id=auth.id,
                    detail=f"项目 {auth.project_id} 依赖被缩减科目：{'、'.join(sorted(removed & set(auth.required_subjects)))}",
                    basis_rule="R-REDU-002",
                    required_action="限定授权范围（剔除被缩减科目）或终止",
                ))
            elif req.kind is RequestKind.ADDRESS_CHANGE:
                items.append(ImpactItem(
                    id=self.store.next_id("II"),
                    kind=ImpactKind.FUTURE_PROJECT, severity=Severity.ADVISORY,
                    reference_type="project_authorization", reference_id=auth.id,
                    detail=f"项目 {auth.project_id} 授权登记于原地址，须重新核验执业地址",
                    basis_rule="R-ADDR-002",
                    required_action="重新核验地址（不阻塞批准，但未核验不得在新址履约）",
                ))
        return items

    @staticmethod
    def _appointment_effect(
        kind: RequestKind, appt: Appointment, removed: set[str]
    ) -> tuple[bool, str, str]:
        if kind in (RequestKind.LICENSE_SUSPEND, RequestKind.LICENSE_REVOKE):
            return True, "R-SUSP-001", "取消预约（停业期间不得接诊，不能改期）"
        if kind is RequestKind.SUBJECT_REDUCTION and appt.subject in removed:
            return True, "R-REDU-001", "取消预约，或改期至保留科目"
        if kind is RequestKind.ADDRESS_CHANGE:
            return True, "R-ADDR-001", "取消预约，或改期至新址且不早于生效时点"
        return False, "", ""

    # ------------------------------------------------------------------
    # 阻塞项处置
    # ------------------------------------------------------------------
    def resolve_blocking_item(
        self,
        req_id: str,
        item_id: str,
        action: DispositionAction | str,
        actor: str,
        note: str,
        *,
        new_scheduled_at: str | None = None,
        new_subject: str | None = None,
        at: str | None = None,
    ) -> ImpactItem:
        """处置一条阻塞/提示项；实际状态变更在生效时原子执行。"""
        action = DispositionAction(action)
        at = at or now_iso()
        with self.store.lock:
            req = self.store.get_change_request(req_id)
            self._require_status(req, {RequestStatus.IN_DISPOSITION}, action="处置阻塞项")
            report = self.store.get_report(req_id)
            item = self._find_item(report, item_id)
            if item.severity is Severity.HISTORICAL:
                raise DomainError(ErrorCode.VALIDATION, "历史服务项无需处置")
            allowed = _ACTION_FOR_KIND.get(item.kind, set())
            if action not in allowed:
                raise DomainError(
                    ErrorCode.VALIDATION,
                    f"处置动作 {action.value} 不适用于 {item.kind.value}",
                )
            effective_at = req.expected_effective_at or report.generated_at
            payload: dict[str, Any] = {}
            if action is DispositionAction.RESCHEDULE_APPOINTMENT:
                payload = self._validate_reschedule(req, item, effective_at,
                                                    new_scheduled_at, new_subject)
            if action is DispositionAction.RESTRICT_AUTHORIZATION and req.kind is not RequestKind.SUBJECT_REDUCTION:
                raise DomainError(
                    ErrorCode.VALIDATION, "仅科目缩减可采用“限定授权范围”，停业类变更必须终止授权"
                )
            item.resolved = True
            item.resolution_action = action
            item.resolution_note = note
            item.resolved_by = actor
            item.resolved_at = at
            item.resolution_payload = payload
            self._audit(at, actor, "RESOLVE_ITEM", "impact_item", item_id,
                        {"request_id": req_id, "action": action.value, "note": note, **payload})
            return item

    def _validate_reschedule(
        self, req: ChangeRequest, item: ImpactItem, effective_at: str,
        new_scheduled_at: str | None, new_subject: str | None,
    ) -> dict[str, Any]:
        appt = self.store.get_appointment(item.reference_id)
        current = self.store.current_license(req.institution_id)
        if req.kind is RequestKind.LICENSE_SUSPEND or req.kind is RequestKind.LICENSE_REVOKE:
            raise DomainError(ErrorCode.VALIDATION, "停业期间无恢复时点，预约只能取消")
        if req.kind is RequestKind.ADDRESS_CHANGE:
            if not new_scheduled_at or is_before(new_scheduled_at, effective_at):
                raise DomainError(ErrorCode.VALIDATION, "改期时间不得早于生效时点")
            return {"new_scheduled_at": new_scheduled_at}
        if req.kind is RequestKind.SUBJECT_REDUCTION:
            removed = set(req.payload["removed_subjects"])
            if not new_subject or new_subject in removed or new_subject not in current.subjects:
                raise DomainError(ErrorCode.VALIDATION, "改期目标必须是保留科目")
            payload = {"new_subject": new_subject}
            if new_scheduled_at:
                payload["new_scheduled_at"] = new_scheduled_at
            else:
                payload["new_scheduled_at"] = appt.scheduled_at
            return payload
        return {}

    # ------------------------------------------------------------------
    # 批准与生效
    # ------------------------------------------------------------------
    def approve_request(
        self,
        req_id: str,
        actor: str,
        *,
        expected_effective_at: str | None = None,
        at: str | None = None,
    ) -> ChangeRequest:
        """批准变更：影响清单必须已生成且全部阻塞项已处置。"""
        at = at or now_iso()
        with self.store.lock:
            req = self.store.get_change_request(req_id)
            self._require_status(req, {RequestStatus.IN_DISPOSITION}, action="批准")
            report = self.store.get_report(req_id)
            unresolved = report.blocking_items
            if unresolved:
                raise DomainError(
                    ErrorCode.BLOCKING_ITEMS,
                    f"存在 {len(unresolved)} 项未处置阻塞项，不能批准",
                    reasons=[f"{i.id}:{i.reference_id}:{i.required_action}" for i in unresolved],
                )
            if expected_effective_at:
                req.expected_effective_at = expected_effective_at
            req.status = RequestStatus.DECIDED
            req.decided_at = at
            req.decided_by = actor
            self._audit(at, actor, "APPROVE_REQUEST", "change_request", req_id,
                        {"expected_effective_at": req.expected_effective_at})
            return req

    def apply_effective(self, req_id: str, *, at: str | None = None) -> ChangeRequest:
        """生效：单事务（单锁）内原子传播全部限制。

        顺序：校验 → 写新版本/限制 → 执行处置决定 → 兜底扫描新出现的冲突引用
        → 翻转申请状态。锁内任何一步抛异常，已写入的内存变更随异常回滚边界清晰
        （存储为事务性资源，数据库实现下同事务提交）。
        """
        at = at or now_iso()
        with self.store.lock:
            req = self.store.get_change_request(req_id)
            self._require_status(req, {RequestStatus.DECIDED}, action="生效")
            expected = req.expected_effective_at
            if expected and is_before(at, expected):
                raise DomainError(
                    ErrorCode.ILLEGAL_TRANSITION,
                    f"未到约定生效时点（{expected}）",
                )
            old = self.store.current_license(req.institution_id)

            # 再次确认无未处置阻塞项（批准后到期前可能补录引用，由兜底传播处理）
            report = self.store.get_report(req_id)
            if report.blocking_items:
                raise DomainError(ErrorCode.BLOCKING_ITEMS, "阻塞项尚未处置完毕，拒绝生效")

            # 1) 新版本
            new_status, new_subjects, new_address = self._project_license(req, old)
            old.superseded_at = at
            new_ver = LicenseVersion(
                institution_id=req.institution_id,
                version_no=old.version_no + 1,
                status=new_status,
                address=new_address,
                subjects=list(new_subjects),
                effective_at=at,
                reason=f"{req.kind.label}：{req.payload.get('reason') or req.payload.get('new_address') or ''}".strip("："),
                change_request_id=req_id,
            )
            self.store.add_license_version(new_ver)

            # 2) 限制传播 + 解除（恢复链）
            restrictions = self._propagate_restrictions(req, new_ver, at)

            # 3) 执行已批准的处置决定
            self._execute_dispositions(req, report, restrictions, at)

            # 4) 兜底：原子扫描生效时点之后仍然冲突的引用，强制限制/取消
            enforced = self._enforce_future(req, new_ver, restrictions, at, report)

            # 5) 翻转状态
            req.status = RequestStatus.EFFECTIVE
            req.effective_at = at
            req.new_version_id = new_ver.id
            req.restriction_ids = [r.id for r in restrictions]
            self._audit(at, req.decided_by or "system", "EFFECTIVE_REQUEST", "change_request", req_id,
                        {"new_version_id": new_ver.id,
                         "restriction_ids": req.restriction_ids,
                         "enforced": enforced,
                         "basis_rule": "R-PROPAGATE-001"})
            return req

    def _project_license(
        self, req: ChangeRequest, old: LicenseVersion
    ) -> tuple[LicenseStatus, list[str], str]:
        kind = req.kind
        if kind is RequestKind.LICENSE_SUSPEND:
            return LicenseStatus.SUSPENDED, old.subjects, old.address
        if kind is RequestKind.LICENSE_REVOKE:
            return LicenseStatus.REVOKED, old.subjects, old.address
        if kind is RequestKind.SUBJECT_REDUCTION:
            removed = set(req.payload["removed_subjects"])
            return old.status, [s for s in old.subjects if s not in removed], old.address
        if kind is RequestKind.ADDRESS_CHANGE:
            return old.status, old.subjects, req.payload["new_address"]
        if kind is RequestKind.LICENSE_RESUME:
            subjects = req.payload.get("subjects") or old.subjects
            return LicenseStatus.ACTIVE, subjects, old.address
        raise DomainError(ErrorCode.VALIDATION, f"未知申请类型：{kind}")

    def _propagate_restrictions(
        self, req: ChangeRequest, new_ver: LicenseVersion, at: str
    ) -> list[Restriction]:
        restrictions: list[Restriction] = []
        kind = req.kind
        if kind in (RequestKind.LICENSE_SUSPEND, RequestKind.LICENSE_REVOKE):
            r = Restriction(
                id=self.store.next_id("RST"),
                institution_id=req.institution_id,
                change_request_id=req.id,
                license_version_id=new_ver.id,
                scope="ALL",
                subjects=[],
                status=LicenseStatus.SUSPENDED if kind is RequestKind.LICENSE_SUSPEND else LicenseStatus.REVOKED,
                effective_at=at,
            )
            self.store.add_restriction(r)
            restrictions.append(r)
        elif kind is RequestKind.SUBJECT_REDUCTION:
            r = Restriction(
                id=self.store.next_id("RST"),
                institution_id=req.institution_id,
                change_request_id=req.id,
                license_version_id=new_ver.id,
                scope="SUBJECTS",
                subjects=list(req.payload["removed_subjects"]),
                status=LicenseStatus.SUSPENDED,
                effective_at=at,
            )
            self.store.add_restriction(r)
            restrictions.append(r)
        elif kind is RequestKind.ADDRESS_CHANGE:
            r = Restriction(
                id=self.store.next_id("RST"),
                institution_id=req.institution_id,
                change_request_id=req.id,
                license_version_id=new_ver.id,
                scope="ADDRESS",
                subjects=[],
                status=LicenseStatus.ACTIVE,
                effective_at=at,
                address=req.payload["new_address"],
            )
            self.store.add_restriction(r)
            restrictions.append(r)
        elif kind is RequestKind.LICENSE_RESUME:
            # 解除暂停链上的 SUSPENDED/ALL 限制；科目缩减限制保留（R-RESUME-001）
            for rst in self.store.restrictions_for(req.institution_id):
                origin = self.store.get_change_request(rst.change_request_id)
                if (
                    rst.scope == "ALL"
                    and rst.status is LicenseStatus.SUSPENDED
                    and rst.lifts_restriction_id is None
                    and origin.kind is RequestKind.LICENSE_SUSPEND
                    and parse_dt(rst.effective_at) <= parse_dt(at)
                ):
                    lift = Restriction(
                        id=self.store.next_id("RST"),
                        institution_id=req.institution_id,
                        change_request_id=req.id,
                        license_version_id=new_ver.id,
                        scope="ALL",
                        subjects=[],
                        status=LicenseStatus.ACTIVE,
                        effective_at=at,
                        lifts_restriction_id=rst.id,
                    )
                    self.store.add_restriction(lift)
                    restrictions.append(lift)
        return restrictions

    def _execute_dispositions(
        self, req: ChangeRequest, report: ImpactReport,
        restrictions: list[Restriction], at: str,
    ) -> None:
        for item in report.items:
            if not item.resolved:
                continue
            action = item.resolution_action
            if item.reference_type == "appointment":
                appt = self.store.get_appointment(item.reference_id)
                if action is DispositionAction.CANCEL_APPOINTMENT:
                    appt.status = AppointmentStatus.CANCELLED
                    appt.cancellation_reason = f"变更 {req.id} 生效处置：{item.resolution_note}"
                    appt.cancelled_by_change_id = req.id
                elif action is DispositionAction.RESCHEDULE_APPOINTMENT:
                    p = item.resolution_payload
                    if p.get("new_scheduled_at"):
                        appt.scheduled_at = p["new_scheduled_at"]
                    if p.get("new_subject"):
                        appt.subject = p["new_subject"]
            else:
                auth = self.store.get_authorization(item.reference_id)
                if action is DispositionAction.TERMINATE_AUTHORIZATION:
                    auth.status = AuthorizationStatus.TERMINATED
                    auth.terminated_at = at
                elif action is DispositionAction.RESTRICT_AUTHORIZATION:
                    auth.status = AuthorizationStatus.RESTRICTED
                    auth.restriction_ids = list(
                        set(auth.restriction_ids) | {r.id for r in restrictions}
                    )
                    removed = set(req.payload.get("removed_subjects", []))
                    auth.required_subjects = [
                        s for s in auth.required_subjects if s not in removed
                    ]
                    if not auth.required_subjects:
                        auth.status = AuthorizationStatus.TERMINATED
                        auth.terminated_at = at

    def _enforce_future(
        self,
        req: ChangeRequest,
        new_ver: LicenseVersion,
        restrictions: list[Restriction],
        at: str,
        report: ImpactReport,
    ) -> dict[str, list[str]]:
        """兜底：批准后、生效前新录入的冲突引用，原子强制处置。

        清单中已按决定处置过的引用不重复处理；本扫描只抓批准后新增的冲突。
        """
        enforced = {"cancelled_appointments": [], "restricted_authorizations": [],
                    "terminated_authorizations": []}
        removed = set(req.payload.get("removed_subjects", []))
        decided_refs = {
            (item.reference_type, item.reference_id)
            for item in report.items
            if item.resolved
        }

        def conflicts_subject(subject: str) -> bool:
            if req.kind in (RequestKind.LICENSE_SUSPEND, RequestKind.LICENSE_REVOKE):
                return True
            if req.kind is RequestKind.SUBJECT_REDUCTION:
                return subject in removed
            return False

        address_change = req.kind is RequestKind.ADDRESS_CHANGE
        for appt in self.store.appointments_for(req.institution_id):
            if ("appointment", appt.id) in decided_refs:
                continue
            if appt.status is not AppointmentStatus.BOOKED:
                continue
            if is_before(appt.scheduled_at, at):
                continue
            if conflicts_subject(appt.subject) or address_change:
                rule = "R-ADDR-001" if address_change else "R-PROPAGATE-001"
                appt.status = AppointmentStatus.CANCELLED
                appt.cancellation_reason = f"变更 {req.id} 生效原子传播兜底取消（{rule}）"
                appt.cancelled_by_change_id = req.id
                enforced["cancelled_appointments"].append(appt.id)

        if req.kind in (RequestKind.LICENSE_SUSPEND, RequestKind.LICENSE_REVOKE):
            for auth in self.store.authorizations_for(req.institution_id):
                if ("project_authorization", auth.id) in decided_refs:
                    continue
                if auth.status is AuthorizationStatus.ACTIVE and not is_at_or_before(auth.valid_to, at):
                    auth.status = AuthorizationStatus.TERMINATED
                    auth.terminated_at = at
                    auth.restriction_ids = list(set(auth.restriction_ids) | {r.id for r in restrictions})
                    enforced["terminated_authorizations"].append(auth.id)
        elif req.kind is RequestKind.SUBJECT_REDUCTION:
            for auth in self.store.authorizations_for(req.institution_id):
                if ("project_authorization", auth.id) in decided_refs:
                    continue
                if auth.status is AuthorizationStatus.ACTIVE and removed & set(auth.required_subjects):
                    auth.required_subjects = [
                        s for s in auth.required_subjects if s not in removed
                    ]
                    auth.restriction_ids = list(set(auth.restriction_ids) | {r.id for r in restrictions})
                    if auth.required_subjects:
                        auth.status = AuthorizationStatus.RESTRICTED
                        enforced["restricted_authorizations"].append(auth.id)
                    else:
                        auth.status = AuthorizationStatus.TERMINATED
                        auth.terminated_at = at
                        enforced["terminated_authorizations"].append(auth.id)
        return enforced

    def archive_request(self, req_id: str, *, at: str | None = None) -> ChangeRequest:
        at = at or now_iso()
        with self.store.lock:
            req = self.store.get_change_request(req_id)
            self._require_status(req, {RequestStatus.EFFECTIVE, RequestStatus.REJECTED}, action="归档")
            req.status = RequestStatus.ARCHIVED
            req.archived_at = at
            self._audit(at, "system", "ARCHIVE_REQUEST", "change_request", req_id, {})
            return req

    # ------------------------------------------------------------------
    # 替代链查询
    # ------------------------------------------------------------------
    def get_request_chain(self, req_id: str) -> dict[str, Any]:
        """返回替代链：从最早的祖先到最新的叶子。"""
        with self.store.lock:
            node = self.store.get_change_request(req_id)
            ancestors: list[ChangeRequest] = []
            cur = node
            while cur.supersedes_id:
                cur = self.store.get_change_request(cur.supersedes_id)
                ancestors.append(cur)
            chain = list(reversed(ancestors))
            chain.append(node)
            cur = node
            while cur.replaced_by_id:
                cur = self.store.get_change_request(cur.replaced_by_id)
                chain.append(cur)
            return {
                "chain": [r.to_dict() for r in chain],
                "leaf_id": chain[-1].id if chain else node.id,
                "relations": [
                    {"from": r.id, "to": r.replaced_by_id, "type": "SUPERSEDED_BY"}
                    for r in chain
                    if r.replaced_by_id
                ],
            }

    # ------------------------------------------------------------------
    # 历史服务 / 未来禁止项判定
    # ------------------------------------------------------------------
    def active_restrictions(self, institution_id: str, at: str) -> list[Restriction]:
        """时点 ``at`` 上实际有效的限制（已被恢复解除的除外）。"""
        lifted: set[str] = set()
        rows: list[Restriction] = []
        for r in self.store.restrictions_for(institution_id):
            if is_before(at, r.effective_at):
                continue
            if r.lifts_restriction_id:
                lifted.add(r.lifts_restriction_id)
                continue
            rows.append(r)
        return [r for r in rows if r.id not in lifted and r.scope in ("ALL", "SUBJECTS") and r.status is not LicenseStatus.ACTIVE]

    def check_service(
        self, institution_id: str, subject: str, at: str
    ) -> dict[str, Any]:
        """判定机构在某科目、某时点能否提供服务，给出依据链。"""
        with self.store.lock:
            version = self._license_at(institution_id, at)
            bases: list[dict[str, Any]] = []
            allowed = True
            for r in self.active_restrictions(institution_id, at):
                hit = r.scope == "ALL" or subject in r.subjects
                if not hit:
                    continue
                allowed = False
                req = self.store.get_change_request(r.change_request_id)
                rule = "R-SUSP-001" if r.scope == "ALL" else "R-REDU-001"
                bases.append({
                    "rule": rule,
                    "rule_text": RULES[rule],
                    "restriction_id": r.id,
                    "change_request_id": r.change_request_id,
                    "change_kind": req.kind.value,
                    "license_version_id": r.license_version_id,
                    "effective_at": r.effective_at,
                    "subjects": r.subjects,
                })
            if allowed:
                bases.append({
                    "rule": "R-RESUME-001",
                    "rule_text": RULES["R-RESUME-001"],
                    "license_version_id": version.id if version else None,
                    "note": "时点上无命中限制；若为生效前已完成服务，另见 R-HIST-001",
                })
            return {
                "institution_id": institution_id,
                "subject": subject,
                "at": at,
                "allowed": allowed,
                "license_version_id": version.id if version else None,
                "basis": bases,
            }

    def _license_at(self, institution_id: str, at: str) -> LicenseVersion | None:
        candidate: LicenseVersion | None = None
        for v in self.store.license_versions(institution_id):
            if is_at_or_before(v.effective_at, at):
                candidate = v
        return candidate

    def classify_references(self, institution_id: str, at: str) -> dict[str, Any]:
        """对机构全部业务引用做历史/未来分类，附禁止依据。"""
        with self.store.lock:
            historical: list[dict[str, Any]] = []
            future_prohibited: list[dict[str, Any]] = []
            future_allowed: list[dict[str, Any]] = []

            for appt in self.store.appointments_for(institution_id):
                if appt.status is AppointmentStatus.COMPLETED and is_at_or_before(
                    appt.scheduled_at, at
                ):
                    historical.append({
                        "reference_type": "appointment", "reference_id": appt.id,
                        "subject": appt.subject, "at": appt.scheduled_at,
                        "rule": "R-HIST-001", "rule_text": RULES["R-HIST-001"],
                    })
                    continue
                verdict = self.check_service(institution_id, appt.subject, appt.scheduled_at)
                row = {
                    "reference_type": "appointment", "reference_id": appt.id,
                    "subject": appt.subject, "at": appt.scheduled_at,
                    "status": appt.status.value,
                    "basis": verdict["basis"],
                }
                (future_prohibited if not verdict["allowed"] and appt.status is AppointmentStatus.BOOKED
                 else future_allowed).append(row)

            for auth in self.store.authorizations_for(institution_id):
                if is_at_or_before(auth.valid_to, at):
                    historical.append({
                        "reference_type": "project_authorization", "reference_id": auth.id,
                        "project_id": auth.project_id,
                        "valid_from": auth.valid_from, "valid_to": auth.valid_to,
                        "rule": "R-AUTH-HIST-002", "rule_text": RULES["R-AUTH-HIST-002"],
                    })
                    continue
                verdicts = {s: self.check_service(institution_id, s, at) for s in auth.required_subjects}
                blocked = {s: v for s, v in verdicts.items() if not v["allowed"]}
                row = {
                    "reference_type": "project_authorization", "reference_id": auth.id,
                    "project_id": auth.project_id, "status": auth.status.value,
                    "valid_from": auth.valid_from, "valid_to": auth.valid_to,
                    "blocked_subjects": sorted(blocked),
                    "basis": [b for v in blocked.values() for b in v["basis"]],
                }
                (future_prohibited if blocked and auth.status is AuthorizationStatus.ACTIVE
                 else future_allowed).append(row)

            return {
                "institution_id": institution_id,
                "at": at,
                "historical_services": historical,
                "future_prohibited": future_prohibited,
                "future_allowed": future_allowed,
                "rules_cited": sorted({
                    *(x["rule"] for x in historical),
                    *(b["rule"] for x in future_prohibited for b in x["basis"]),
                }),
            }

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _require_status(req: ChangeRequest, allowed: set[RequestStatus], *, action: str = "操作") -> None:
        if req.status not in allowed:
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION,
                f"申请 {req.id} 当前状态为{req.status.label}，不允许{action}",
                reasons=[f"允许的状态：{'、'.join(s.label for s in sorted(allowed, key=lambda s: s.value))}"],
            )

    @staticmethod
    def _find_item(report: ImpactReport, item_id: str) -> ImpactItem:
        for item in report.items:
            if item.id == item_id:
                return item
        raise DomainError(ErrorCode.NOT_FOUND, f"影响清单项不存在：{item_id}")

    def _audit(self, at: str, actor: str, action: str, target_type: str,
               target_id: str, detail: dict[str, Any]) -> None:
        self.store.add_audit(AuditEvent(
            id="", at=at, actor=actor, action=action,
            target_type=target_type, target_id=target_id, detail=detail,
        ))

    def rules_catalog(self) -> dict[str, str]:
        return dict(RULES)
