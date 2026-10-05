"""端到端情景演示：暂停 → 科目缩减替代 → 生效 → 恢复 的替代链与影响传播。

运行：python3 tools/demo_flow.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from license_change import (  # noqa: E402
    DispositionAction,
    LicenseChangeService,
    RequestKind,
    Store,
)


def main() -> None:
    svc = LicenseChangeService(Store())
    T = "2026-10-01T08:00:00+08:00"

    def step(title: str, payload: object) -> None:
        print(f"\n=== {title} ===")
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    # 1. 机构与许可证 v1
    svc.register_institution(
        "INST-1", "仁康门诊部", "旧街 1 号",
        ["内科", "口腔科", "中医科"], at=T,
    )

    # 2. 项目授权与预约（一个历史、一个未来）
    svc.add_project_authorization(
        "AUTH-OLD", "PRJ-OLD", "INST-1", ["内科"],
        "2026-08-01T00:00:00+08:00", "2026-09-30T00:00:00+08:00",
    )
    svc.add_project_authorization(
        "AUTH-1", "PRJ-1", "INST-1", ["内科", "口腔科"],
        "2026-09-15T00:00:00+08:00", "2026-12-31T00:00:00+08:00",
    )
    svc.add_appointment("APT-HIST", "INST-1", "内科", "2026-09-20T09:00:00+08:00")
    # 历史预约标记完成（实际系统由业务侧回写，演示直接改状态）
    svc.store.get_appointment("APT-HIST").status.name  # 确认存在
    from license_change.models import AppointmentStatus
    svc.store.get_appointment("APT-HIST").status = AppointmentStatus.COMPLETED
    svc.add_appointment("APT-1", "INST-1", "口腔科", "2026-10-20T09:00:00+08:00")
    svc.add_appointment("APT-2", "INST-1", "内科", "2026-10-21T09:00:00+08:00")

    # 3. 机构先申请“暂停执业”
    svc.create_change_request(
        "CR-SUSP", "INST-1", RequestKind.LICENSE_SUSPEND, "合规员甲",
        {"reason": "消防整改"}, at="2026-10-02T09:00:00+08:00",
    )

    # 4. 监管认为无需全停，机构撤回全停，改申请“缩减口腔科”（替代链）
    svc.create_change_request(
        "CR-REDU", "INST-1", RequestKind.SUBJECT_REDUCTION, "合规员甲",
        {"removed_subjects": ["口腔科"]}, at="2026-10-03T09:00:00+08:00",
        supersedes_id="CR-SUSP",
    )
    step("替代链", svc.get_request_chain("CR-SUSP"))

    # 5. 核验 + 影响清单
    svc.submit_for_verification("CR-REDU", at="2026-10-03T10:00:00+08:00")
    report = svc.generate_impact_report("CR-REDU", at="2026-10-03T11:00:00+08:00")
    step("影响清单（含历史/阻塞/提示）", report.to_dict())

    # 6. 未处置阻塞项时批准被拒
    try:
        svc.approve_request("CR-REDU", "监管员乙", at="2026-10-03T12:00:00+08:00")
    except Exception as exc:  # noqa: BLE001
        step("批准被拒绝（阻塞项未清零）", getattr(exc, "to_dict", lambda: str(exc))())

    # 7. 处置：口腔科预约取消；项目授权限定范围
    for item in report.items:
        if item.severity.value != "BLOCKING":
            continue
        if item.reference_type == "appointment":
            svc.resolve_blocking_item(
                "CR-REDU", item.id, DispositionAction.CANCEL_APPOINTMENT,
                "合规员甲", "联系群众改至其他机构", at="2026-10-03T13:00:00+08:00",
            )
        else:
            svc.resolve_blocking_item(
                "CR-REDU", item.id, DispositionAction.RESTRICT_AUTHORIZATION,
                "合规员甲", "项目剔除口腔科，仅保留内科",
                at="2026-10-03T13:30:00+08:00",
            )

    # 8. 批准并生效（原子传播）
    svc.approve_request("CR-REDU", "监管员乙", at="2026-10-03T14:00:00+08:00")
    req = svc.apply_effective("CR-REDU", at="2026-10-10T00:00:00+08:00")
    step("生效结果", req.to_dict())
    step("许可证版本链", [v.to_dict() for v in svc.store.license_versions("INST-1")])

    # 9. 历史服务 vs 未来禁止项
    verdict = svc.classify_references("INST-1", "2026-10-10T00:00:00+08:00")
    step("历史/未来分类（含依据）", verdict)

    # 10. 时点判定：口腔科禁止、内科允许
    step("口腔科 2026-10-20 判定",
         svc.check_service("INST-1", "口腔科", "2026-10-20T09:00:00+08:00"))
    step("内科 2026-10-20 判定",
         svc.check_service("INST-1", "内科", "2026-10-20T09:00:00+08:00"))

    # 11. 后续整改合格，申请暂停→恢复演示（另一机构维度的恢复链）
    svc.create_change_request(
        "CR-SUSP2", "INST-1", RequestKind.LICENSE_SUSPEND, "监管员乙",
        {"reason": "抽查整改"}, at="2026-11-01T09:00:00+08:00",
    )
    svc.submit_for_verification("CR-SUSP2", at="2026-11-01T10:00:00+08:00")
    svc.generate_impact_report("CR-SUSP2", at="2026-11-01T11:00:00+08:00")
    svc.approve_request("CR-SUSP2", "监管员乙", at="2026-11-01T12:00:00+08:00")
    svc.apply_effective("CR-SUSP2", at="2026-11-02T00:00:00+08:00")

    svc.create_change_request(
        "CR-RESUME", "INST-1", RequestKind.LICENSE_RESUME, "监管员乙", {},
        at="2026-11-10T09:00:00+08:00", supersedes_id="CR-SUSP2",
    )
    svc.submit_for_verification("CR-RESUME", at="2026-11-10T10:00:00+08:00")
    svc.generate_impact_report("CR-RESUME", at="2026-11-10T11:00:00+08:00")
    svc.approve_request("CR-RESUME", "监管员乙", at="2026-11-10T12:00:00+08:00")
    svc.apply_effective("CR-RESUME", at="2026-11-11T00:00:00+08:00")
    step("恢复后：内科允许（暂停已解除）",
         svc.check_service("INST-1", "内科", "2026-11-12T09:00:00+08:00"))
    step("恢复后：口腔科仍禁止（缩减限制保留，R-RESUME-001）",
         svc.check_service("INST-1", "口腔科", "2026-11-12T09:00:00+08:00"))


if __name__ == "__main__":
    main()
