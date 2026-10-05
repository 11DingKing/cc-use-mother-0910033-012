"""许可证变更影响后端的领域回归测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from license_change import (  # noqa: E402
    DispositionAction,
    DomainError,
    ErrorCode,
    LicenseChangeService,
    RequestKind,
    RequestStatus,
    Store,
)
from license_change.models import AppointmentStatus, AuthorizationStatus  # noqa: E402


def base_world(svc: LicenseChangeService, t0: str = "2026-10-01T08:00:00+08:00") -> None:
    svc.register_institution(
        "INST-1", "测试门诊部", "旧址 1 号", ["内科", "口腔科", "中医科"], at=t0,
    )
    svc.add_project_authorization(
        "AUTH-HIST", "PRJ-OLD", "INST-1", ["内科"],
        "2026-08-01T00:00:00+08:00", "2026-09-30T00:00:00+08:00",
    )
    svc.add_project_authorization(
        "AUTH-1", "PRJ-1", "INST-1", ["内科", "口腔科"],
        "2026-09-15T00:00:00+08:00", "2026-12-31T00:00:00+08:00",
    )
    appt = svc.add_appointment("APT-HIST", "INST-1", "内科", "2026-09-20T09:00:00+08:00")
    appt.status = AppointmentStatus.COMPLETED
    svc.add_appointment("APT-KQ", "INST-1", "口腔科", "2026-10-20T09:00:00+08:00")
    svc.add_appointment("APT-NK", "INST-1", "内科", "2026-10-21T09:00:00+08:00")


def drive_to_effective(
    svc: LicenseChangeService, req_id: str, resolver,
    submit_t: str, report_t: str, approve_t: str, eff_t: str,
    approver: str = "监管员",
):
    svc.submit_for_verification(req_id, at=submit_t)
    report = svc.generate_impact_report(req_id, at=report_t)
    for item in report.items:
        if not item.severity.name == "BLOCKING":
            continue
        resolver(item)
    svc.approve_request(req_id, approver, at=approve_t)
    return svc.apply_effective(req_id, at=eff_t)


class VersionChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = LicenseChangeService(Store())
        base_world(self.svc)

    def test_version_chain_is_monotonic_and_superseded(self) -> None:
        svc = self.svc

        def resolver(item):
            if item.reference_type == "appointment":
                svc.resolve_blocking_item("CR-1", item.id, DispositionAction.CANCEL_APPOINTMENT,
                                          "合规员", "取消", at="2026-10-03T13:00:00+08:00")
            else:
                svc.resolve_blocking_item("CR-1", item.id, DispositionAction.TERMINATE_AUTHORIZATION,
                                          "合规员", "终止", at="2026-10-03T13:30:00+08:00")

        svc.create_change_request("CR-1", "INST-1", RequestKind.LICENSE_SUSPEND, "合规员",
                                  {"reason": "整改"}, at="2026-10-02T09:00:00+08:00")
        drive_to_effective(svc, "CR-1", resolver,
                           "2026-10-02T10:00:00+08:00", "2026-10-02T11:00:00+08:00",
                           "2026-10-02T12:00:00+08:00", "2026-10-05T00:00:00+08:00")
        versions = svc.store.license_versions("INST-1")
        self.assertEqual([v.version_no for v in versions], [1, 2])
        self.assertIsNotNone(versions[0].superseded_at)
        self.assertEqual(versions[1].status.name, "SUSPENDED")
        self.assertEqual(versions[1].change_request_id, "CR-1")


class ImpactAndBlockingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = LicenseChangeService(Store())
        base_world(self.svc)
        svc = self.svc
        svc.create_change_request("CR-1", "INST-1", RequestKind.SUBJECT_REDUCTION, "合规员",
                                  {"removed_subjects": ["口腔科"]},
                                  at="2026-10-02T09:00:00+08:00")
        svc.submit_for_verification("CR-1", at="2026-10-02T10:00:00+08:00")
        self.report = svc.generate_impact_report("CR-1", at="2026-10-02T11:00:00+08:00")

    def test_report_classifies_historical_and_blocking(self) -> None:
        kinds = {(i.reference_id, i.severity.name) for i in self.report.items}
        self.assertIn(("APT-HIST", "HISTORICAL"), kinds)
        self.assertIn(("AUTH-HIST", "HISTORICAL"), kinds)
        self.assertIn(("APT-KQ", "BLOCKING"), kinds)
        self.assertIn(("AUTH-1", "BLOCKING"), kinds)
        # 内科预约不受影响，不进清单
        self.assertNotIn("APT-NK", {i.reference_id for i in self.report.items})
        rules = {i.basis_rule for i in self.report.items}
        self.assertIn("R-HIST-001", rules)
        self.assertIn("R-REDU-002", rules)

    def test_approval_blocked_until_disposed(self) -> None:
        svc = self.svc
        with self.assertRaises(DomainError) as ctx:
            svc.approve_request("CR-1", "监管员", at="2026-10-02T12:00:00+08:00")
        self.assertIs(ctx.exception.code, ErrorCode.BLOCKING_ITEMS)
        self.assertEqual(len(ctx.exception.reasons), 2)

    def test_historical_item_needs_no_disposition(self) -> None:
        svc = self.svc
        hist = next(i for i in self.report.items if i.severity.name == "HISTORICAL")
        with self.assertRaises(DomainError):
            svc.resolve_blocking_item("CR-1", hist.id, DispositionAction.CANCEL_APPOINTMENT,
                                      "合规员", "x")

    def test_wrong_disposition_action_rejected(self) -> None:
        svc = self.svc
        appt_item = next(i for i in self.report.items if i.reference_id == "APT-KQ")
        with self.assertRaises(DomainError):
            svc.resolve_blocking_item("CR-1", appt_item.id,
                                      DispositionAction.TERMINATE_AUTHORIZATION,
                                      "合规员", "动作类型不符")

    def test_restrict_disposition_only_for_reduction(self) -> None:
        svc = self.svc
        svc.create_change_request("CR-S", "INST-1", RequestKind.LICENSE_SUSPEND, "合规员",
                                  {"reason": "x"}, at="2026-10-02T15:00:00+08:00")
        svc.submit_for_verification("CR-S", at="2026-10-02T15:30:00+08:00")
        report_s = svc.generate_impact_report("CR-S", at="2026-10-02T16:00:00+08:00")
        auth_item = next(i for i in report_s.items
                         if i.reference_type == "project_authorization" and i.severity.name == "BLOCKING")
        with self.assertRaises(DomainError):
            svc.resolve_blocking_item("CR-S", auth_item.id,
                                      DispositionAction.RESTRICT_AUTHORIZATION,
                                      "合规员", "停业不能仅限定范围")


class AtomicPropagationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = LicenseChangeService(Store())
        base_world(self.svc)

    def test_suspension_propagates_atomically(self) -> None:
        svc = self.svc

        def resolver(item):
            if item.reference_type == "appointment":
                svc.resolve_blocking_item("CR-S", item.id, DispositionAction.CANCEL_APPOINTMENT,
                                          "合规员", "停业取消", at="2026-10-03T13:00:00+08:00")
            else:
                svc.resolve_blocking_item("CR-S", item.id, DispositionAction.TERMINATE_AUTHORIZATION,
                                          "合规员", "停业终止", at="2026-10-03T13:30:00+08:00")

        svc.create_change_request("CR-S", "INST-1", RequestKind.LICENSE_SUSPEND, "合规员",
                                  {"reason": "消防"}, at="2026-10-02T09:00:00+08:00")
        drive_to_effective(svc, "CR-S", resolver,
                           "2026-10-02T10:00:00+08:00", "2026-10-02T11:00:00+08:00",
                           "2026-10-02T12:00:00+08:00", "2026-10-05T00:00:00+08:00")

        self.assertEqual(svc.store.get_appointment("APT-KQ").status, AppointmentStatus.CANCELLED)
        self.assertEqual(svc.store.get_appointment("APT-NK").status, AppointmentStatus.CANCELLED)
        self.assertEqual(svc.store.get_authorization("AUTH-1").status, AuthorizationStatus.TERMINATED)
        # 限制落库
        restrictions = svc.store.restrictions_for("INST-1")
        self.assertEqual(len(restrictions), 1)
        self.assertEqual(restrictions[0].scope, "ALL")
        # 判定
        self.assertFalse(svc.check_service("INST-1", "内科", "2026-10-10T09:00:00+08:00")["allowed"])

    def test_new_reference_after_approval_is_enforced_on_effective(self) -> None:
        """批准后、生效前补录的未来预约，生效时兜底取消（防止停业期间继续服务）。"""
        svc = self.svc

        def resolver(item):
            svc.resolve_blocking_item("CR-S", item.id, DispositionAction.CANCEL_APPOINTMENT,
                                      "合规员", "取消", at="2026-10-03T13:00:00+08:00") \
                if item.reference_type == "appointment" else \
                svc.resolve_blocking_item("CR-S", item.id, DispositionAction.TERMINATE_AUTHORIZATION,
                                          "合规员", "终止", at="2026-10-03T13:30:00+08:00")

        svc.create_change_request("CR-S", "INST-1", RequestKind.LICENSE_SUSPEND, "合规员",
                                  {"reason": "消防"}, at="2026-10-02T09:00:00+08:00")
        svc.submit_for_verification("CR-S", at="2026-10-02T10:00:00+08:00")
        svc.generate_impact_report("CR-S", at="2026-10-02T11:00:00+08:00")
        for item in svc.store.get_report("CR-S").items:
            if item.severity.name == "BLOCKING":
                resolver(item)
        svc.approve_request("CR-S", "监管员", at="2026-10-02T12:00:00+08:00")
        # 批准后补录
        svc.add_appointment("APT-LATE", "INST-1", "中医科", "2026-10-08T09:00:00+08:00")
        svc.apply_effective("CR-S", at="2026-10-05T00:00:00+08:00")
        self.assertEqual(svc.store.get_appointment("APT-LATE").status, AppointmentStatus.CANCELLED)

    def test_effective_before_expected_time_rejected(self) -> None:
        svc = self.svc
        svc.create_change_request("CR-S", "INST-1", RequestKind.LICENSE_SUSPEND, "合规员",
                                  {"reason": "x"}, at="2026-10-02T09:00:00+08:00",
                                  expected_effective_at="2026-10-10T00:00:00+08:00")
        svc.submit_for_verification("CR-S", at="2026-10-02T10:00:00+08:00")
        svc.generate_impact_report("CR-S", at="2026-10-02T11:00:00+08:00")
        for item in svc.store.get_report("CR-S").items:
            if item.severity.name == "BLOCKING":
                action = (DispositionAction.CANCEL_APPOINTMENT if item.reference_type == "appointment"
                          else DispositionAction.TERMINATE_AUTHORIZATION)
                svc.resolve_blocking_item("CR-S", item.id, action, "合规员", "处置",
                                          at="2026-10-02T12:00:00+08:00")
        svc.approve_request("CR-S", "监管员", at="2026-10-02T13:00:00+08:00")
        with self.assertRaises(DomainError):
            svc.apply_effective("CR-S", at="2026-10-09T00:00:00+08:00")


class SupersedeChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = LicenseChangeService(Store())
        base_world(self.svc)

    def test_open_request_superseded_is_withdrawn(self) -> None:
        svc = self.svc
        svc.create_change_request("CR-A", "INST-1", RequestKind.LICENSE_SUSPEND, "合规员",
                                  {"reason": "整改"}, at="2026-10-02T09:00:00+08:00")
        svc.create_change_request("CR-B", "INST-1", RequestKind.SUBJECT_REDUCTION, "合规员",
                                  {"removed_subjects": ["口腔科"]},
                                  at="2026-10-03T09:00:00+08:00", supersedes_id="CR-A")
        old = svc.store.get_change_request("CR-A")
        self.assertEqual(old.status, RequestStatus.WITHDRAWN)
        self.assertEqual(old.replaced_by_id, "CR-B")
        chain = svc.get_request_chain("CR-A")
        self.assertEqual([r["id"] for r in chain["chain"]], ["CR-A", "CR-B"])
        self.assertEqual(chain["leaf_id"], "CR-B")
        self.assertEqual(chain["relations"], [{"from": "CR-A", "to": "CR-B", "type": "SUPERSEDED_BY"}])

    def test_direct_withdraw_requires_reason(self) -> None:
        svc = self.svc
        svc.create_change_request("CR-A", "INST-1", RequestKind.ADDRESS_CHANGE, "合规员",
                                  {"new_address": "新址 9 号"}, at="2026-10-02T09:00:00+08:00")
        with self.assertRaises(DomainError):
            svc.withdraw_request("CR-A", "合规员", "  ")
        svc.withdraw_request("CR-A", "合规员", "材料不全", at="2026-10-02T10:00:00+08:00")
        self.assertEqual(svc.store.get_change_request("CR-A").status, RequestStatus.WITHDRAWN)
        with self.assertRaises(DomainError):
            svc.submit_for_verification("CR-A")

    def test_resume_lifts_suspension_but_keeps_subject_restriction(self) -> None:
        svc = self.svc

        # 科目缩减先生效
        def redu_resolver(item):
            if item.reference_type == "appointment":
                svc.resolve_blocking_item("CR-REDU", item.id, DispositionAction.CANCEL_APPOINTMENT,
                                          "合规员", "取消", at="2026-10-03T13:00:00+08:00")
            else:
                svc.resolve_blocking_item("CR-REDU", item.id, DispositionAction.RESTRICT_AUTHORIZATION,
                                          "合规员", "剔除口腔科", at="2026-10-03T13:30:00+08:00")

        svc.create_change_request("CR-REDU", "INST-1", RequestKind.SUBJECT_REDUCTION, "合规员",
                                  {"removed_subjects": ["口腔科"]}, at="2026-10-02T09:00:00+08:00")
        drive_to_effective(svc, "CR-REDU", redu_resolver,
                           "2026-10-02T10:00:00+08:00", "2026-10-02T11:00:00+08:00",
                           "2026-10-02T12:00:00+08:00", "2026-10-05T00:00:00+08:00")

        # 之后暂停
        svc.add_appointment("APT-LATE", "INST-1", "内科", "2026-11-09T09:00:00+08:00")

        def susp_resolver(item):
            if item.reference_type == "appointment":
                svc.resolve_blocking_item("CR-SUSP", item.id, DispositionAction.CANCEL_APPOINTMENT,
                                          "合规员", "取消", at="2026-11-02T13:00:00+08:00")
            else:
                svc.resolve_blocking_item("CR-SUSP", item.id, DispositionAction.TERMINATE_AUTHORIZATION,
                                          "合规员", "终止", at="2026-11-02T13:30:00+08:00")

        svc.create_change_request("CR-SUSP", "INST-1", RequestKind.LICENSE_SUSPEND, "监管员",
                                  {"reason": "抽查"}, at="2026-11-01T09:00:00+08:00")
        drive_to_effective(svc, "CR-SUSP", susp_resolver,
                           "2026-11-01T10:00:00+08:00", "2026-11-01T11:00:00+08:00",
                           "2026-11-01T12:00:00+08:00", "2026-11-03T00:00:00+08:00")
        self.assertFalse(svc.check_service("INST-1", "内科", "2026-11-04T09:00:00+08:00")["allowed"])

        # 恢复（链接已生效的暂停，历史保留）
        svc.create_change_request("CR-RES", "INST-1", RequestKind.LICENSE_RESUME, "监管员", {},
                                  at="2026-11-08T09:00:00+08:00", supersedes_id="CR-SUSP")
        svc.submit_for_verification("CR-RES", at="2026-11-08T10:00:00+08:00")
        svc.generate_impact_report("CR-RES", at="2026-11-08T11:00:00+08:00")
        svc.approve_request("CR-RES", "监管员", at="2026-11-08T12:00:00+08:00")
        svc.apply_effective("CR-RES", at="2026-11-09T00:00:00+08:00")

        # 已生效的暂停申请状态保持 EFFECTIVE（历史不被抹成撤回）
        self.assertEqual(svc.store.get_change_request("CR-SUSP").status, RequestStatus.EFFECTIVE)
        chain_ids = [r["id"] for r in svc.get_request_chain("CR-SUSP")["chain"]]
        self.assertEqual(chain_ids, ["CR-SUSP", "CR-RES"])

        verdict_nk = svc.check_service("INST-1", "内科", "2026-11-12T09:00:00+08:00")
        verdict_kq = svc.check_service("INST-1", "口腔科", "2026-11-12T09:00:00+08:00")
        self.assertTrue(verdict_nk["allowed"])
        self.assertFalse(verdict_kq["allowed"])
        self.assertEqual(verdict_kq["basis"][0]["rule"], "R-REDU-001")

    def test_resume_rejected_when_not_suspended(self) -> None:
        svc = self.svc
        with self.assertRaises(DomainError):
            svc.create_change_request("CR-RES", "INST-1", RequestKind.LICENSE_RESUME,
                                      "监管员", {}, at="2026-10-02T09:00:00+08:00")


class AddressChangeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = LicenseChangeService(Store())
        base_world(self.svc)

    def test_address_change_reschedule_and_enforcement(self) -> None:
        svc = self.svc
        eff = "2026-10-10T00:00:00+08:00"
        svc.create_change_request("CR-A", "INST-1", RequestKind.ADDRESS_CHANGE, "合规员",
                                  {"new_address": "新址 9 号"}, at="2026-10-02T09:00:00+08:00",
                                  expected_effective_at=eff)
        svc.submit_for_verification("CR-A", at="2026-10-02T10:00:00+08:00")
        report = svc.generate_impact_report("CR-A", at="2026-10-02T11:00:00+08:00")
        # 地址变更的未来预约为阻塞项、授权为提示项
        appt_items = [i for i in report.items
                      if i.reference_type == "appointment" and i.severity.name != "HISTORICAL"]
        self.assertTrue(appt_items)
        self.assertTrue(all(i.severity.name == "BLOCKING" for i in appt_items))
        kq = next(i for i in appt_items if i.reference_id == "APT-KQ")
        # 改期早于生效时点被拒
        with self.assertRaises(DomainError):
            svc.resolve_blocking_item("CR-A", kq.id, DispositionAction.RESCHEDULE_APPOINTMENT,
                                      "合规员", "改期",
                                      new_scheduled_at="2026-10-09T09:00:00+08:00")
        svc.resolve_blocking_item("CR-A", kq.id, DispositionAction.RESCHEDULE_APPOINTMENT,
                                  "合规员", "改至新址",
                                  new_scheduled_at="2026-10-15T09:00:00+08:00",
                                  at="2026-10-02T13:00:00+08:00")
        nk = next(i for i in appt_items if i.reference_id == "APT-NK")
        svc.resolve_blocking_item("CR-A", nk.id, DispositionAction.CANCEL_APPOINTMENT,
                                  "合规员", "取消", at="2026-10-02T13:30:00+08:00")
        svc.approve_request("CR-A", "监管员", at="2026-10-02T14:00:00+08:00")
        svc.apply_effective("CR-A", at=eff)
        self.assertEqual(svc.store.get_appointment("APT-KQ").scheduled_at,
                         "2026-10-15T09:00:00+08:00")
        self.assertEqual(svc.store.get_appointment("APT-NK").status, AppointmentStatus.CANCELLED)
        self.assertEqual(svc.store.current_license("INST-1").address, "新址 9 号")

    def test_address_change_requires_new_address(self) -> None:
        svc = self.svc
        with self.assertRaises(DomainError):
            svc.create_change_request("CR-A", "INST-1", RequestKind.ADDRESS_CHANGE,
                                      "合规员", {}, at="2026-10-02T09:00:00+08:00")


class FutureBookingGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = LicenseChangeService(Store())
        base_world(self.svc)
        svc = self.svc

        def resolver(item):
            if item.reference_type == "appointment":
                svc.resolve_blocking_item("CR-S", item.id, DispositionAction.CANCEL_APPOINTMENT,
                                          "合规员", "取消", at="2026-10-03T13:00:00+08:00")
            else:
                svc.resolve_blocking_item("CR-S", item.id, DispositionAction.TERMINATE_AUTHORIZATION,
                                          "合规员", "终止", at="2026-10-03T13:30:00+08:00")

        svc.create_change_request("CR-S", "INST-1", RequestKind.LICENSE_SUSPEND, "合规员",
                                  {"reason": "消防"}, at="2026-10-02T09:00:00+08:00")
        drive_to_effective(svc, "CR-S", resolver,
                           "2026-10-02T10:00:00+08:00", "2026-10-02T11:00:00+08:00",
                           "2026-10-02T12:00:00+08:00", "2026-10-05T00:00:00+08:00")

    def test_future_appointment_rejected_during_suspension(self) -> None:
        svc = self.svc
        with self.assertRaises(DomainError) as ctx:
            svc.add_appointment("APT-NEW", "INST-1", "内科", "2026-10-20T09:00:00+08:00")
        self.assertIs(ctx.exception.code, ErrorCode.ILLEGAL_TRANSITION)
        self.assertIn("R-SUSP-001", str(ctx.exception))
        self.assertTrue(ctx.exception.reasons)

    def test_new_authorization_rejected_during_suspension(self) -> None:
        svc = self.svc
        with self.assertRaises(DomainError):
            svc.add_project_authorization(
                "AUTH-NEW", "PRJ-NEW", "INST-1", ["内科"],
                "2026-10-20T00:00:00+08:00", "2026-12-01T00:00:00+08:00",
            )

    def test_historical_appointment_before_restriction_still_registerable(self) -> None:
        # 限制生效时点之前的补录不拦截（历史事实）
        appt = self.svc.add_appointment("APT-OLD2", "INST-1", "内科",
                                        "2026-09-01T09:00:00+08:00")
        self.assertEqual(appt.status, AppointmentStatus.BOOKED)


class ClassificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = LicenseChangeService(Store())
        base_world(self.svc)
        svc = self.svc

        def resolver(item):
            if item.reference_type == "appointment":
                svc.resolve_blocking_item("CR-1", item.id, DispositionAction.CANCEL_APPOINTMENT,
                                          "合规员", "取消", at="2026-10-03T13:00:00+08:00")
            else:
                svc.resolve_blocking_item("CR-1", item.id, DispositionAction.RESTRICT_AUTHORIZATION,
                                          "合规员", "剔除口腔科", at="2026-10-03T13:30:00+08:00")

        svc.create_change_request("CR-1", "INST-1", RequestKind.SUBJECT_REDUCTION, "合规员",
                                  {"removed_subjects": ["口腔科"]}, at="2026-10-02T09:00:00+08:00")
        drive_to_effective(svc, "CR-1", resolver,
                           "2026-10-02T10:00:00+08:00", "2026-10-02T11:00:00+08:00",
                           "2026-10-02T12:00:00+08:00", "2026-10-10T00:00:00+08:00")

    def test_historical_vs_future_split_with_basis(self) -> None:
        svc = self.svc
        result = svc.classify_references("INST-1", "2026-10-10T00:00:00+08:00")
        hist_ids = {x["reference_id"] for x in result["historical_services"]}
        self.assertEqual(hist_ids, {"APT-HIST", "AUTH-HIST"})
        prohibited = {(x["reference_type"], x["reference_id"]) for x in result["future_prohibited"]}
        # 口腔科预约已取消 -> 不在禁止清单；AUTH-1 已受限 -> 不在禁止清单
        self.assertEqual(prohibited, set())
        allowed_ids = {x["reference_id"] for x in result["future_allowed"]}
        self.assertIn("APT-KQ", allowed_ids)   # 已取消，列允许侧（不再可约）
        self.assertIn("AUTH-1", allowed_ids)   # 已限制
        self.assertIn("R-HIST-001", result["rules_cited"])

    def test_fresh_booked_appointment_is_prohibited_with_basis(self) -> None:
        svc = self.svc
        # 生效后新录入的口腔科预约（绕过 add_appointment 的科目校验模拟外部预约系统）
        appt = svc.add_appointment  # noqa: F841
        from license_change.models import Appointment
        svc.store.add_appointment(Appointment(
            id="APT-X", institution_id="INST-1", subject="口腔科",
            scheduled_at="2026-10-25T09:00:00+08:00",
        ))
        result = svc.classify_references("INST-1", "2026-10-10T00:00:00+08:00")
        hit = next(x for x in result["future_prohibited"] if x["reference_id"] == "APT-X")
        self.assertEqual(hit["basis"][0]["rule"], "R-REDU-001")
        self.assertTrue(hit["basis"][0]["change_request_id"])


if __name__ == "__main__":
    unittest.main()
