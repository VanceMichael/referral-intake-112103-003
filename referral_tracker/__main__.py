"""演示：接入合成演练数据，输出协调员视图与科室视图。

用法：
    python3 -m referral_tracker [fixture.json ...]

不带参数时读取 fixtures/sources.json 与 fixtures/drill_multi_specialty.json。
所有数据均为合成数据，不含真实医疗信息。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .actors import Actor
from .tracker import ReferralTracker

CLERK = Actor(staff_id="clerk-01", role="records_clerk")
COORDINATOR = Actor(staff_id="coord-01", role="coordinator")


def _print_coordinator_view(tracker: ReferralTracker) -> None:
    for patient_ref, visit_id in tracker.known_visits(actor=COORDINATOR):
        view = tracker.coordinator_view(patient_ref, visit_id, actor=COORDINATOR)
        print(f"\n== 协调员视图：患者 {view.patient_ref} / 就诊 {view.visit_id}"
              f"（{view.department}，{view.visit_state}）==")
        print("首诊已完成服务：")
        for service in view.completed_services:
            print(f"  - {service}")
        for line in view.explanation:
            print(f"  {line}")
        for status in view.referrals:
            print(f"  转诊单 {status.key.referral_id} 时间线：")
            for when, note in status.timeline:
                print(f"    {when}  {note}")


def _print_department_views(tracker: ReferralTracker) -> None:
    for department in tracker.known_departments(actor=COORDINATOR):
        staff = Actor(staff_id="dept-view", role="records_clerk")
        view = tracker.department_view(department, actor=staff)
        print(f"\n== 科室视图：{department} ==")
        if not view.referrals:
            print("  （无相关转诊）")
        for item in view.referrals:
            arrow = "转入" if item.direction == "incoming" else "转出"
            outcome = item.outcome or "进行中"
            print(f"  转诊单 {item.referral_id}（{arrow}，{item.state}，结局：{outcome}）"
                  f" 患者 {item.patient_ref} / 就诊 {item.visit_id}")
            for evidence in item.evidence:
                basis = "当前有效版" if evidence.basis == "current_valid" else "接诊时留存版"
                print(f"    依据 {evidence.report_id} 第 {evidence.revision} 版"
                      f"（{basis}，{evidence.finding_code}）")
            for flag in item.rereview_flags:
                print(f"    复核提示：{flag.report_id} {flag.reason}")
            if item.missing_materials:
                print(f"    依据未到齐：{', '.join(item.missing_materials)}")


def main(argv: list[str]) -> int:
    base = Path(__file__).resolve().parent.parent
    paths = [Path(arg) for arg in argv] or [
        base / "fixtures" / "sources.json",
        base / "fixtures" / "drill_multi_specialty.json",
    ]
    tracker = ReferralTracker()
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        results = tracker.ingest_many(document["sources"], actor=CLERK)
        applied = sum(1 for result in results if result.applied)
        print(f"接入 {path.name}：{applied}/{len(results)} 条已应用")
    _print_coordinator_view(tracker)
    _print_department_views(tracker)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
