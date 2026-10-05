"""许可证变更影响：领域服务端到端测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from license_change import (  # noqa: E402
    BookingStatus,
    ChangeKind,
    ChangeState,
    DispositionAction,
    DomainError,
    ItemSeverity,
    LicenseChangeService,
    LicenseStatus,
    ProjectStatus,
)


def setup_service() -> LicenseChangeService:
    svc = LicenseChangeService()
    svc.register_institution(
        "INST-1", "康泰门诊部", "旧街1号",
        ["内科", "外科", "口腔科"], date(2026, 1, 1),
    )
    svc.add_project("PRJ-1", "INST-1", "社区体检项目", "旧街1号", {"内科", "外科"})
    svc.add_project("PRJ-2", "INST-1", "口腔诊疗项目", "旧街1号", {"口腔科"})
    # 历史服务（暂停生效日之前，已履约）
    svc.add_booking("BKG-HIST", "INST-1", "PRJ-1", "内科",
                    date(2026, 6, 15), BookingStatus.HISTORICAL)
    # 未来预约
    svc.add_booking("BKG-F1", "INST-1", "PRJ-1", "内科", date(2026, 8, 20))
    svc.add_booking("BKG-F2", "INST-1", "PRJ-2", "口腔科", date(2026, 8, 25))
    svc.add_booking("BKG-F3", "INST-1", "PRJ-1", "外科", date(2026, 9, 1))
    return svc


class VersionChainTest(unittest.TestCase):
    def test_versions_append_only_and_linked(self) -> None:
        svc = setup_service()
        req = svc.create_change_request(
            "INST-1", ChangeKind.ADDRESS, {"address": "新巷8号"},
            date(2026, 8, 1), "合规员甲")
        svc.submit_for_verification(req.id, "监管员乙")
        svc.generate_impact_list(req.id, "监管员乙")
        for item in svc.impact_list(req.id)["items"]:
            svc.dispose_blocker(req.id, item["id"], DispositionAction.SUSPEND_PROJECT
                                if item["ref_type"] == "project"
                                else DispositionAction.CANCEL_BOOKING,
                                "合规员甲")
        approved = svc.approve_change(req.id, "监管员乙")
        self.assertEqual(approved.new_version_no, 2)
        versions = svc.list_versions("INST-1")
        self.assertEqual([v.version_no for v in versions], [1, 2])
        self.assertEqual(versions[1].prev_id, 1)
        self.assertEqual(versions[1].address, "新巷8号")
        self.assertEqual(versions[0].address, "旧街1号")  # 历史版本不可变

    def test_current_version_reflects_effective_date(self) -> None:
        svc = setup_service()
        v = svc.effective_version_at("INST-1", date(2026, 6, 1))
        self.assertEqual(v.version_no, 1)
        self.assertIsNone(svc.effective_version_at("INST-1", date(2025, 12, 31)))


class ImpactListAndBlockerTest(unittest.TestCase):
    def test_blocker_must_be_resolved_before_approval(self) -> None:
        svc = setup_service()
        req = svc.create_change_request(
            "INST-1", ChangeKind.SUSPEND, {}, date(2026, 8, 1), "合规员甲")
        svc.submit_for_verification(req.id, "监管员乙")
        impact = svc.generate_impact_list(req.id, "监管员乙")
        listing = svc.impact_list(req.id)
        self.assertFalse(listing["approvable"])
        self.assertEqual(listing["blockers_open"], listing["blockers_total"])
        self.assertGreaterEqual(listing["blockers_total"], 2)  # 2 个项目
        with self.assertRaises(DomainError) as ctx:
            svc.approve_change(req.id, "监管员乙")
        self.assertEqual(ctx.exception.code, "blockers_open")

        # 每一条都给出依据
        for item in listing["items"]:
            self.assertTrue(item["basis"])
            self.assertIn("R-", item["basis"])

        for item in svc.impact_list(req.id)["items"]:
            action = (DispositionAction.SUSPEND_PROJECT if item["ref_type"] == "project"
                      else DispositionAction.CANCEL_BOOKING)
            svc.dispose_blocker(req.id, item["id"], action, "合规员甲")
        self.assertTrue(svc.impact_list(req.id)["approvable"])
        approved = svc.approve_change(req.id, "监管员乙")
        self.assertEqual(approved.state, ChangeState.DECIDED)
        self.assertEqual(svc.current_version("INST-1").status, LicenseStatus.SUSPENDED)

    def test_disposition_validation(self) -> None:
        svc = setup_service()
        req = svc.create_change_request(
            "INST-1", ChangeKind.SUSPEND, {}, date(2026, 8, 1), "合规员甲")
        svc.submit_for_verification(req.id, "监管员乙")
        svc.generate_impact_list(req.id, "监管员乙")
        project_item = next(i for i in svc.impact_list(req.id)["items"]
                            if i["ref_type"] == "project")
        with self.assertRaises(DomainError) as ctx:
            svc.dispose_blocker(req.id, project_item["id"],
                                DispositionAction.CANCEL_BOOKING, "合规员甲")
        self.assertEqual(ctx.exception.code, "invalid_action")

    def test_subject_reduce_restriction_must_stay_within_scope(self) -> None:
        svc = setup_service()
        req = svc.create_change_request(
            "INST-1", ChangeKind.SUBJECT_REDUCE, {"remove_subjects": ["外科"]},
            date(2026, 8, 1), "合规员甲")
        svc.submit_for_verification(req.id, "监管员乙")
        svc.generate_impact_list(req.id, "监管员乙")
        items = {i["ref_id"]: i for i in svc.impact_list(req.id)["items"]}
        # PRJ-1 受影响（含外科），PRJ-2 不受影响
        self.assertIn("PRJ-1", items)
        self.assertNotIn("PRJ-2", {i["ref_id"] for i in svc.impact_list(req.id)["items"]
                                   if i["ref_type"] == "project"})
        prj_item = items["PRJ-1"]
        with self.assertRaises(DomainError):  # 保留外科（已被缩减）非法
            svc.dispose_blocker(req.id, prj_item["id"],
                                DispositionAction.RESTRICT_PROJECT, "合规员甲",
                                payload={"active_subjects": ["外科"]})
        svc.dispose_blocker(req.id, prj_item["id"],
                            DispositionAction.RESTRICT_PROJECT, "合规员甲",
                            payload={"active_subjects": ["内科"]})
        # BKG-F3（外科未来预约）取消
        bkg_item = items["BKG-F3"]
        svc.dispose_blocker(req.id, bkg_item["id"],
                            DispositionAction.CANCEL_BOOKING, "合规员甲")
        svc.approve_change(req.id, "监管员乙")
        prj = svc.store.table("projects")["PRJ-1"]
        self.assertEqual(prj.status, ProjectStatus.RESTRICTED)
        self.assertEqual(set(prj.active_subjects), {"内科"})

    def test_reschedule_must_target_a_permitted_date(self) -> None:
        svc = setup_service()
        req = svc.create_change_request(
            "INST-1", ChangeKind.SUSPEND, {}, date(2026, 8, 1), "合规员甲")
        svc.submit_for_verification(req.id, "监管员乙")
        svc.generate_impact_list(req.id, "监管员乙")
        bkg_item = next(i for i in svc.impact_list(req.id)["items"]
                        if i["ref_id"] == "BKG-F1")
        with self.assertRaises(DomainError) as ctx:
            svc.dispose_blocker(req.id, bkg_item["id"],
                                DispositionAction.RESCHEDULE_BOOKING, "合规员甲",
                                payload={"new_date": date(2026, 8, 22)})  # 仍停业
        self.assertEqual(ctx.exception.code, "still_prohibited")

    def test_new_project_after_impact_list_blocks_effect(self) -> None:
        svc = setup_service()
        req = svc.create_change_request(
            "INST-1", ChangeKind.SUSPEND, {}, date(2026, 8, 1), "合规员甲")
        svc.submit_for_verification(req.id, "监管员乙")
        svc.generate_impact_list(req.id, "监管员乙")
        for item in svc.impact_list(req.id)["items"]:
            action = (DispositionAction.SUSPEND_PROJECT if item["ref_type"] == "project"
                      else DispositionAction.CANCEL_BOOKING)
            svc.dispose_blocker(req.id, item["id"], action, "合规员甲")
        # 清单之后新增授权：生效时必须被发现
        svc.add_project("PRJ-NEW", "INST-1", "夜间急诊项目", "旧街1号", {"内科"})
        with self.assertRaises(DomainError) as ctx:
            svc.approve_change(req.id, "监管员乙")
        self.assertEqual(ctx.exception.code, "new_blocker")
        # 原子性：未产生新版本
        self.assertEqual(svc.current_version("INST-1").version_no, 1)


class AtomicPropagationTest(unittest.TestCase):
    def test_propagation_failure_rolls_back_everything(self) -> None:
        svc = setup_service()
        req = svc.create_change_request(
            "INST-1", ChangeKind.ADDRESS, {"address": "新巷8号"},
            date(2026, 8, 1), "合规员甲")
        svc.submit_for_verification(req.id, "监管员乙")
        svc.generate_impact_list(req.id, "监管员乙")
        for item in svc.impact_list(req.id)["items"]:
            action = (DispositionAction.SUSPEND_PROJECT if item["ref_type"] == "project"
                      else DispositionAction.CANCEL_BOOKING)
            svc.dispose_blocker(req.id, item["id"], action, "合规员甲")

        versions_before = [v.version_no for v in svc.list_versions("INST-1")]
        # 在版本追加之后、项目传播阶段注入故障
        original = svc._apply_project_action

        def boom(project_id, action, payload, r):  # noqa: ANN001
            raise RuntimeError("模拟传播故障")

        svc._apply_project_action = boom  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            svc.approve_change(req.id, "监管员乙")
        svc._apply_project_action = original  # type: ignore[method-assign]

        # 快照还原：版本未追加、机构地址未变、项目未暂停、申请回到处置中
        self.assertEqual([v.version_no for v in svc.list_versions("INST-1")],
                         versions_before)
        self.assertEqual(svc.current_version("INST-1").address, "旧街1号")
        self.assertEqual(svc.store.table("projects")["PRJ-1"].status,
                         ProjectStatus.AUTHORIZED)
        self.assertEqual(svc.store.table("requests")[req.id].state,
                         ChangeState.DISPOSING)
        # 故障恢复后可重新批准成功
        svc.approve_change(req.id, "监管员乙")
        self.assertEqual(svc.current_version("INST-1").address, "新巷8号")
        self.assertEqual(svc.current_version("INST-1").version_no, 2)

    def test_future_bookings_auto_forbidden_on_suspend(self) -> None:
        svc = setup_service()
        req = svc.create_change_request(
            "INST-1", ChangeKind.SUSPEND, {}, date(2026, 8, 1), "合规员甲")
        svc.submit_for_verification(req.id, "监管员乙")
        svc.generate_impact_list(req.id, "监管员乙")
        # 只处置项目，故意留一个预约不处置 -> 不能批准
        project_items = [i for i in svc.impact_list(req.id)["items"]
                         if i["ref_type"] == "project"]
        for item in project_items:
            svc.dispose_blocker(req.id, item["id"],
                                DispositionAction.SUSPEND_PROJECT, "合规员甲")
        with self.assertRaises(DomainError):
            svc.approve_change(req.id, "监管员乙")
        # 全部处置后，清单生成后新增的未来预约在生效时被原子禁止
        for item in svc.impact_list(req.id)["items"]:
            if item["ref_type"] == "booking":
                svc.dispose_blocker(req.id, item["id"],
                                    DispositionAction.CANCEL_BOOKING, "合规员甲")
        svc.add_booking("BKG-LATE", "INST-1", "PRJ-1", "内科", date(2026, 8, 28))
        svc.approve_change(req.id, "监管员乙")
        late = svc.store.table("bookings")["BKG-LATE"]
        self.assertEqual(late.status, BookingStatus.FORBIDDEN)
        self.assertEqual(late.resolved_by, req.id)


class ChainTest(unittest.TestCase):
    def _approve_simple(self, svc: LicenseChangeService, kind: ChangeKind,
                        proposed: dict, eff: date) -> str:
        req = svc.create_change_request("INST-1", kind, proposed, eff, "合规员甲")
        svc.submit_for_verification(req.id, "监管员乙")
        svc.generate_impact_list(req.id, "监管员乙")
        for item in svc.impact_list(req.id)["items"]:
            action = (DispositionAction.SUSPEND_PROJECT if item["ref_type"] == "project"
                      else DispositionAction.CANCEL_BOOKING)
            svc.dispose_blocker(req.id, item["id"], action, "合规员甲")
        svc.approve_change(req.id, "监管员乙")
        return req.id

    def test_suspend_resume_reduce_withdraw_chain(self) -> None:
        svc = setup_service()
        suspend_id = self._approve_simple(svc, ChangeKind.SUSPEND, {}, date(2026, 8, 1))
        self.assertEqual(svc.current_version("INST-1").status, LicenseStatus.SUSPENDED)

        # 撤回链
        wd = svc.create_change_request(
            "INST-1", ChangeKind.RESUME, {}, date(2026, 9, 1), "合规员甲")
        svc.withdraw_request(wd.id, "合规员甲", "材料不齐")
        self.assertEqual(wd.state, ChangeState.ARCHIVED)
        self.assertEqual(wd.decision, "withdrawn")
        # 撤回不改变许可证
        self.assertEqual(svc.current_version("INST-1").status, LicenseStatus.SUSPENDED)
        with self.assertRaises(DomainError):  # 撤回后不可审批
            svc.submit_for_verification(wd.id, "监管员乙")

        # 恢复替代暂停
        resume_id = self._approve_simple(svc, ChangeKind.RESUME, {}, date(2026, 9, 10))
        chain = svc.get_chain("INST-1")
        types = [(c["link_type"], c["related_request_id"]) for c in chain]
        self.assertIn(("suspend", None), types)
        self.assertIn(("withdraw", None), types)
        resume_link = next(c for c in chain if c["link_type"] == "resume")
        self.assertEqual(resume_link["related_request_id"], suspend_id)
        resume_req = svc.store.table("requests")[resume_id]
        self.assertEqual(resume_req.supersedes_id, suspend_id)
        self.assertEqual(svc.current_version("INST-1").status, LicenseStatus.ACTIVE)
        self.assertEqual(svc.current_version("INST-1").prev_id, 2)

        # 部分科目缩减链
        self._approve_simple(
            svc, ChangeKind.SUBJECT_REDUCE, {"remove_subjects": ["口腔科"]},
            date(2026, 10, 1))
        chain_types = [c["link_type"] for c in svc.get_chain("INST-1")]
        self.assertEqual(chain_types, ["suspend", "withdraw", "resume", "reduce"])

    def test_resume_unfreezes_future_bookings_after_resume_date(self) -> None:
        svc = setup_service()
        # 清单生成后、暂停生效前新增预约 -> 生效安全网原子禁止（日期在恢复日之后）
        suspend_req = svc.create_change_request(
            "INST-1", ChangeKind.SUSPEND, {}, date(2026, 8, 1), "合规员甲")
        svc.submit_for_verification(suspend_req.id, "监管员乙")
        svc.generate_impact_list(suspend_req.id, "监管员乙")
        for item in svc.impact_list(suspend_req.id)["items"]:
            action = (DispositionAction.SUSPEND_PROJECT if item["ref_type"] == "project"
                      else DispositionAction.CANCEL_BOOKING)
            svc.dispose_blocker(suspend_req.id, item["id"], action, "合规员甲")
        svc.add_booking("BKG-FROZEN", "INST-1", "PRJ-1", "内科", date(2026, 9, 15))
        suspend_id = suspend_req.id
        svc.approve_change(suspend_id, "监管员乙")
        self.assertEqual(svc.store.table("bookings")["BKG-FROZEN"].status,
                         BookingStatus.FORBIDDEN)
        # 停业期间新增的、服务日在恢复后的预约
        svc.add_booking("BKG-LATER", "INST-1", "PRJ-1", "内科", date(2026, 9, 20))
        self.assertEqual(
            svc.classify_booking("BKG-LATER", date(2026, 8, 2))["classification"],
            "future_prohibited")
        # 停业期间日期的预约不会被恢复溯及
        svc.add_booking("BKG-DURING", "INST-1", "PRJ-1", "内科", date(2026, 8, 20))

        # 恢复后
        self._approve_simple(svc, ChangeKind.RESUME, {}, date(2026, 9, 1))
        # 原子禁止的预约在恢复日后解冻
        self.assertEqual(svc.store.table("bookings")["BKG-FROZEN"].status,
                         BookingStatus.FUTURE)
        self.assertEqual(
            svc.classify_booking("BKG-FROZEN", date(2026, 9, 2))["classification"],
            "future_allowed")
        self.assertEqual(
            svc.classify_booking("BKG-LATER", date(2026, 9, 2))["classification"],
            "future_allowed")
        # 服务日落在停业期内的预约仍判禁止（恢复不溯及既往）
        self.assertEqual(
            svc.classify_booking("BKG-DURING", date(2026, 9, 2))["classification"],
            "future_prohibited")


class HistoricalFutureBoundaryTest(unittest.TestCase):
    def test_historical_service_is_never_rewritten(self) -> None:
        svc = setup_service()
        # 暂停生效日晚于历史服务日
        req = svc.create_change_request(
            "INST-1", ChangeKind.SUSPEND, {}, date(2026, 8, 1), "合规员甲")
        svc.submit_for_verification(req.id, "监管员乙")
        svc.generate_impact_list(req.id, "监管员乙")
        # 历史预约不出现在影响清单
        self.assertNotIn("BKG-HIST",
                         {i["ref_id"] for i in svc.impact_list(req.id)["items"]})
        for item in svc.impact_list(req.id)["items"]:
            action = (DispositionAction.SUSPEND_PROJECT if item["ref_type"] == "project"
                      else DispositionAction.CANCEL_BOOKING)
            svc.dispose_blocker(req.id, item["id"], action, "合规员甲")
        svc.approve_change(req.id, "监管员乙")
        # 历史服务保持可查、标记 historical、不被禁止，并给出依据
        result = svc.classify_booking("BKG-HIST")
        self.assertEqual(result["classification"], "historical_service")
        self.assertFalse(result["enforceable"])
        self.assertTrue(result["allowed"])
        self.assertTrue(any("R-BOOK-DATE" in b for b in result["basis"]))
        self.assertEqual(svc.store.table("bookings")["BKG-HIST"].status,
                         BookingStatus.HISTORICAL)

    def test_future_prohibited_gives_basis(self) -> None:
        svc = setup_service()
        req = svc.create_change_request(
            "INST-1", ChangeKind.SUSPEND, {}, date(2026, 8, 1), "合规员甲")
        svc.submit_for_verification(req.id, "监管员乙")
        svc.generate_impact_list(req.id, "监管员乙")
        for item in svc.impact_list(req.id)["items"]:
            action = (DispositionAction.SUSPEND_PROJECT if item["ref_type"] == "project"
                      else DispositionAction.CANCEL_BOOKING)
            svc.dispose_blocker(req.id, item["id"], action, "合规员甲")
        svc.add_booking("BKG-NEW", "INST-1", "PRJ-1", "内科", date(2026, 8, 15))
        svc.approve_change(req.id, "监管员乙")
        result = svc.classify_booking("BKG-NEW")
        self.assertEqual(result["classification"], "future_prohibited")
        self.assertFalse(result["allowed"])
        joined = " ".join(result["basis"])
        self.assertIn("R-LIC-CHAIN", joined)
        self.assertIn("R-BOOK-STATUS", joined)
        self.assertIn(req.id, joined)

    def test_future_allowed_after_subject_reduction_uses_version_at_service_date(self) -> None:
        svc = setup_service()
        req = svc.create_change_request(
            "INST-1", ChangeKind.SUBJECT_REDUCE, {"remove_subjects": ["口腔科"]},
            date(2026, 8, 1), "合规员甲")
        svc.submit_for_verification(req.id, "监管员乙")
        svc.generate_impact_list(req.id, "监管员乙")
        for item in svc.impact_list(req.id)["items"]:
            if item["ref_type"] == "project":
                svc.dispose_blocker(req.id, item["id"],
                                    DispositionAction.TERMINATE_PROJECT, "合规员甲")
            else:
                svc.dispose_blocker(req.id, item["id"],
                                    DispositionAction.CANCEL_BOOKING, "合规员甲")
        svc.approve_change(req.id, "监管员乙")
        # 口腔科未来预约禁止（清单后新建，生效版本已不含口腔科）
        svc.add_booking("BKG-ORAL", "INST-1", "PRJ-2", "口腔科", date(2026, 8, 26))
        self.assertEqual(svc.classify_booking("BKG-ORAL")["classification"],
                         "future_prohibited")
        # 清单内被取消处置的 BKG-F2 标记为已处置取消
        self.assertEqual(svc.classify_booking("BKG-F2")["classification"],
                         "future_disposed_cancelled")
        # 内科未来预约仍允许
        self.assertEqual(svc.classify_booking("BKG-F1")["classification"],
                         "future_allowed")
        # 生效日之前的口腔科服务（若为 FUTURE 但日期早于生效日）不受约束
        svc.add_booking("BKG-BEFORE", "INST-1", "PRJ-2", "口腔科", date(2026, 7, 30))
        self.assertEqual(svc.classify_booking("BKG-BEFORE")["classification"],
                         "future_allowed")


class LifecycleTest(unittest.TestCase):
    def test_illegal_transitions_rejected(self) -> None:
        svc = setup_service()
        req = svc.create_change_request(
            "INST-1", ChangeKind.SUSPEND, {}, date(2026, 8, 1), "合规员甲")
        with self.assertRaises(DomainError):  # 登记态不能直接批准
            svc.approve_change(req.id, "监管员乙")
        svc.submit_for_verification(req.id, "监管员乙")
        with self.assertRaises(DomainError):  # 待核验态无清单不能批准
            svc.approve_change(req.id, "监管员乙")

    def test_only_one_open_request_per_institution(self) -> None:
        svc = setup_service()
        svc.create_change_request(
            "INST-1", ChangeKind.SUSPEND, {}, date(2026, 8, 1), "合规员甲")
        with self.assertRaises(DomainError) as ctx:
            svc.create_change_request(
                "INST-1", ChangeKind.ADDRESS, {"address": "别处"},
                date(2026, 8, 2), "合规员甲")
        self.assertEqual(ctx.exception.code, "conflict")

    def test_kind_preconditions(self) -> None:
        svc = setup_service()
        req = svc.create_change_request(
            "INST-1", ChangeKind.SUSPEND, {}, date(2026, 8, 1), "合规员甲")
        svc.submit_for_verification(req.id, "监管员乙")
        svc.generate_impact_list(req.id, "监管员乙")
        for item in svc.impact_list(req.id)["items"]:
            action = (DispositionAction.SUSPEND_PROJECT if item["ref_type"] == "project"
                      else DispositionAction.CANCEL_BOOKING)
            svc.dispose_blocker(req.id, item["id"], action, "合规员甲")
        svc.approve_change(req.id, "监管员乙")
        # 已停业不能再次暂停
        with self.assertRaises(DomainError):
            svc.create_change_request(
                "INST-1", ChangeKind.SUSPEND, {}, date(2026, 9, 1), "合规员甲")
        # 缩减不存在的科目
        with self.assertRaises(DomainError):
            svc.create_change_request(
                "INST-1", ChangeKind.SUBJECT_REDUCE, {"remove_subjects": ["儿科"]},
                date(2026, 9, 1), "合规员甲")


if __name__ == "__main__":
    unittest.main()
