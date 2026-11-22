"""转诊资料接收与跟踪能力的业务规则测试。全部使用合成数据。"""

import json
import unittest
from pathlib import Path

from referral_tracker import (
    Actor,
    ConflictError,
    InvalidTransitionError,
    KeyMismatchError,
    ReferralKey,
    ReferralTracker,
    UnauthorizedError,
    UnknownReferralError,
)

CLERK = Actor(staff_id="clerk-1", role="records_clerk")
COORD = Actor(staff_id="coord-1", role="coordinator")
CASHIER = Actor(staff_id="cashier-1", role="cashier")

FIXTURES = Path(__file__).parents[1] / "fixtures"
T0 = "2026-10-01T08:00:00+08:00"


def env(source_id, kind, payload, at=T0):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": at,
        "payload": payload,
    }


def visit(pid="P-1", vid="V-1", dept="首诊科", state="seen", sid="v1", at=T0):
    return env(sid, "visit", {"patient_ref": pid, "visit_id": vid, "department": dept, "state": state}, at)


def exam(vid, rid, rev, state="final", finding="F-1", sid=None, at=T0, pid=None):
    payload = {"visit_id": vid, "report_id": rid, "revision": rev, "state": state, "finding_code": finding}
    if pid:
        payload["patient_ref"] = pid
    return env(sid or f"e-{rid}-{rev}", "exam_report", payload, at)


def referral(rid, vid, to, state="sent", rev=None, sid=None, at=T0, requires=(), frm="首诊科"):
    payload = {"referral_id": rid, "visit_id": vid, "from_department": frm, "to_department": to, "state": state}
    if rev is not None:
        payload["revision"] = rev
    if requires:
        payload["requires"] = list(requires)
    return env(sid or f"r-{rid}-{state}-{rev}", "referral", payload, at)


def fee(vid, rid, kind, referral_id=None, sid=None, at=T0):
    payload = {"visit_id": vid, "receipt_id": rid, "fee_kind": kind, "state": "settled"}
    if referral_id:
        payload["referral_id"] = referral_id
    return env(sid or f"f-{rid}", "fee_receipt", payload, at)


def slot(vid, ref_id, dept, slot_id, sid=None, at=T0):
    payload = {"visit_id": vid, "referral_id": ref_id, "department": dept, "action": "reserve", "slot_id": slot_id}
    return env(sid or f"s-{slot_id}", "slot_update", payload, at)


def make_tracker(*records):
    tracker = ReferralTracker()
    tracker.ingest_many(records, actor=CLERK)
    return tracker


class BaselineFixtureTests(unittest.TestCase):
    def setUp(self):
        document = json.loads((FIXTURES / "sources.json").read_text(encoding="utf-8"))
        self.sources = document["sources"]
        self.tracker = make_tracker(*self.sources)

    def test_baseline_flow_accepted(self):
        status = self.tracker.referral_status(
            ReferralKey("P-SYN-14", "V-4401", "RF-290"), actor=COORD
        )
        self.assertEqual(status.state, "accepted")
        self.assertEqual(status.stage, "in_consultation")
        self.assertEqual(status.owner, "心血管内科")
        self.assertIsNone(status.outcome)
        self.assertEqual(status.missing_materials, ())

    def test_baseline_visit_services_and_fee(self):
        view = self.tracker.coordinator_view("P-SYN-14", "V-4401", actor=COORD)
        self.assertIn("seen", view.visit_state)
        self.assertTrue(any("首诊科室接诊" in s for s in view.completed_services))
        self.assertTrue(any("RC-24" in s for s in view.completed_services))
        fee_view = {f.receipt_id: f for f in view.fees}["RC-24"]
        self.assertEqual(fee_view.status, "applied")

    def test_reimport_is_idempotent(self):
        results = self.tracker.ingest_many(self.sources, actor=CLERK)
        self.assertTrue(all(not r.applied for r in results))
        status = self.tracker.referral_status(
            ReferralKey("P-SYN-14", "V-4401", "RF-290"), actor=COORD
        )
        self.assertEqual(len(status.timeline), 2)  # 发出 + 接诊确认，没有重复事件


class IdentityTests(unittest.TestCase):
    def test_same_referral_id_in_different_visits_stays_distinct(self):
        tracker = make_tracker(
            visit(pid="P-1", vid="V-1", sid="v1"),
            visit(pid="P-2", vid="V-2", sid="v2"),
            referral("RF-9", "V-1", "甲科", sid="r1"),
            referral("RF-9", "V-2", "乙科", sid="r2"),
        )
        s1 = tracker.referral_status(ReferralKey("P-1", "V-1", "RF-9"), actor=COORD)
        s2 = tracker.referral_status(ReferralKey("P-2", "V-2", "RF-9"), actor=COORD)
        self.assertEqual(s1.owner, "甲科")
        self.assertEqual(s2.owner, "乙科")

    def test_multiple_specialties_in_one_visit(self):
        tracker = make_tracker(
            visit(),
            referral("RF-1", "V-1", "甲科", sid="r1"),
            referral("RF-2", "V-1", "乙科", sid="r2"),
        )
        view = tracker.coordinator_view("P-1", "V-1", actor=COORD)
        self.assertEqual({s.key.referral_id for s in view.referrals}, {"RF-1", "RF-2"})

    def test_wrong_patient_in_key_is_rejected(self):
        tracker = make_tracker(visit(), referral("RF-1", "V-1", "甲科", sid="r1"))
        with self.assertRaises(KeyMismatchError):
            tracker.referral_status(ReferralKey("P-2", "V-1", "RF-1"), actor=COORD)
        with self.assertRaises(UnknownReferralError):
            tracker.referral_status(ReferralKey("P-1", "V-1", "RF-X"), actor=COORD)

    def test_referral_patient_hint_must_match_visit(self):
        tracker = make_tracker(visit(pid="P-1", vid="V-1"))
        bad = referral("RF-1", "V-1", "甲科", sid="r1")
        bad["payload"]["patient_ref"] = "P-2"
        with self.assertRaises(ConflictError):
            tracker.ingest(bad, actor=CLERK)


class ExamVersionTests(unittest.TestCase):
    def test_conflicting_revision_is_rejected(self):
        tracker = make_tracker(visit(), exam("V-1", "R-1", 1, finding="A"))
        with self.assertRaises(ConflictError):
            tracker.ingest(exam("V-1", "R-1", 1, finding="B", sid="e-dup"), actor=CLERK)
        # 有效版本保持原内容
        tracker.ingest(referral("RF-1", "V-1", "甲科", sid="r1", requires=["R-1"]), actor=CLERK)
        dept = tracker.department_view("甲科", actor=CLERK)
        self.assertEqual(dept.referrals[0].evidence[0].finding_code, "A")

    def test_redundant_same_content_is_ignored(self):
        tracker = make_tracker(visit(), exam("V-1", "R-1", 1))
        result = tracker.ingest(exam("V-1", "R-1", 1, sid="e-dup"), actor=CLERK)
        self.assertFalse(result.applied)

    def test_pending_referral_uses_valid_version_after_correction(self):
        tracker = make_tracker(
            visit(),
            exam("V-1", "R-1", 1, finding="OLD"),
            referral("RF-1", "V-1", "甲科", sid="r1", requires=["R-1"]),
            exam("V-1", "R-1", 2, finding="NEW", sid="e2", at="2026-10-01T09:00:00+08:00"),
        )
        dept = tracker.department_view("甲科", actor=CLERK)
        evidence = dept.referrals[0].evidence[0]
        self.assertEqual((evidence.revision, evidence.finding_code, evidence.basis), (2, "NEW", "current_valid"))
        self.assertEqual(dept.referrals[0].rereview_flags, ())

    def test_accepted_referral_keeps_pinned_basis_and_flags_correction(self):
        tracker = make_tracker(
            visit(),
            exam("V-1", "R-1", 1, finding="OLD"),
            referral("RF-1", "V-1", "甲科", sid="r1", requires=["R-1"]),
            referral("RF-1", "V-1", "甲科", state="accepted", rev=2, sid="r1b",
                     at="2026-10-01T09:00:00+08:00"),
            exam("V-1", "R-1", 2, finding="NEW", sid="e2", at="2026-10-01T10:00:00+08:00"),
        )
        status = tracker.referral_status(ReferralKey("P-1", "V-1", "RF-1"), actor=COORD)
        self.assertEqual(len(status.rereview_flags), 1)
        flag = status.rereview_flags[0]
        self.assertEqual((flag.report_id, flag.accepted_revision, flag.current_revision), ("R-1", 1, 2))
        dept = tracker.department_view("甲科", actor=CLERK)
        evidence = dept.referrals[0].evidence[0]
        self.assertEqual((evidence.revision, evidence.finding_code, evidence.basis), (1, "OLD", "pinned_at_acceptance"))
        self.assertTrue(any("复核" in note for _, note in status.timeline))

    def test_exam_arrival_never_changes_referral_state(self):
        tracker = make_tracker(
            visit(),
            referral("RF-1", "V-1", "甲科", sid="r1", requires=["R-1"]),
        )
        before = tracker.referral_status(ReferralKey("P-1", "V-1", "RF-1"), actor=COORD)
        self.assertEqual(before.missing_materials, ("R-1",))
        tracker.ingest(exam("V-1", "R-1", 1), actor=CLERK)
        after = tracker.referral_status(ReferralKey("P-1", "V-1", "RF-1"), actor=COORD)
        self.assertEqual(after.state, "sent")  # 资料到达不等于任何环节完成
        self.assertEqual(after.missing_materials, ())


class OutcomeTests(unittest.TestCase):
    def _services(self, tracker):
        return tracker.coordinator_view("P-1", "V-1", actor=COORD).completed_services

    def test_rejection_is_not_success_and_keeps_first_visit_services(self):
        tracker = make_tracker(
            visit(),
            fee("V-1", "RC-1", "initial_registration"),
            referral("RF-1", "V-1", "甲科", sid="r1"),
            referral("RF-1", "V-1", "甲科", state="rejected", rev=2, sid="r1b",
                     at="2026-10-01T09:00:00+08:00"),
        )
        status = tracker.referral_status(ReferralKey("P-1", "V-1", "RF-1"), actor=COORD)
        self.assertEqual(status.outcome, "rejected")
        self.assertNotEqual(status.outcome, "success")
        self.assertEqual(status.stage, "returned")
        self.assertEqual(status.owner, "首诊科")
        services = self._services(tracker)
        self.assertTrue(any("首诊科室接诊" in s for s in services))
        self.assertTrue(any("RC-1" in s for s in services))

    def test_no_show_is_not_success_and_keeps_first_visit_services(self):
        tracker = make_tracker(
            visit(),
            referral("RF-1", "V-1", "甲科", sid="r1"),
            referral("RF-1", "V-1", "甲科", state="accepted", rev=2, sid="r1b",
                     at="2026-10-01T09:00:00+08:00"),
            referral("RF-1", "V-1", "甲科", state="no_show", rev=3, sid="r1c",
                     at="2026-10-01T10:00:00+08:00"),
        )
        status = tracker.referral_status(ReferralKey("P-1", "V-1", "RF-1"), actor=COORD)
        self.assertEqual(status.outcome, "no_show")
        self.assertTrue(any("首诊科室接诊" in s for s in self._services(tracker)))

    def test_completion_is_the_only_success(self):
        tracker = make_tracker(
            visit(),
            referral("RF-1", "V-1", "甲科", sid="r1"),
            referral("RF-1", "V-1", "甲科", state="accepted", rev=2, sid="r1b",
                     at="2026-10-01T09:00:00+08:00"),
            referral("RF-1", "V-1", "甲科", state="completed", rev=3, sid="r1c",
                     at="2026-10-01T10:00:00+08:00"),
        )
        status = tracker.referral_status(ReferralKey("P-1", "V-1", "RF-1"), actor=COORD)
        self.assertEqual(status.outcome, "success")

    def test_terminal_states_reject_further_transitions(self):
        tracker = make_tracker(
            visit(),
            referral("RF-1", "V-1", "甲科", sid="r1"),
            referral("RF-1", "V-1", "甲科", state="rejected", rev=2, sid="r1b",
                     at="2026-10-01T09:00:00+08:00"),
        )
        with self.assertRaises(InvalidTransitionError):
            tracker.ingest(
                referral("RF-1", "V-1", "甲科", state="accepted", rev=3, sid="r1c",
                         at="2026-10-01T10:00:00+08:00"),
                actor=CLERK,
            )

    def test_referral_revision_conflicts(self):
        tracker = make_tracker(visit(), referral("RF-1", "V-1", "甲科", sid="r1"))
        with self.assertRaises(ConflictError):
            tracker.ingest(referral("RF-1", "V-1", "甲科", state="accepted", rev=1, sid="r-x"), actor=CLERK)
        tracker.ingest(referral("RF-1", "V-1", "甲科", state="accepted", rev=2, sid="r1b",
                                at="2026-10-01T09:00:00+08:00"), actor=CLERK)
        with self.assertRaises(ConflictError):
            tracker.ingest(referral("RF-1", "V-1", "甲科", state="completed", rev=1, sid="r-y"), actor=CLERK)


class FeeAndSlotGatingTests(unittest.TestCase):
    def test_consult_fee_held_until_acceptance(self):
        tracker = make_tracker(
            visit(),
            referral("RF-1", "V-1", "甲科", sid="r1"),
            fee("V-1", "RC-9", "referral_consult", referral_id="RF-1"),
        )
        fee_view = tracker.coordinator_view("P-1", "V-1", actor=COORD).fees[0]
        self.assertEqual(fee_view.status, "held")
        tracker.ingest(referral("RF-1", "V-1", "甲科", state="accepted", rev=2, sid="r1b",
                                at="2026-10-01T09:00:00+08:00"), actor=CLERK)
        fee_view = tracker.coordinator_view("P-1", "V-1", actor=COORD).fees[0]
        self.assertEqual(fee_view.status, "applied")

    def test_consult_fee_void_after_rejection(self):
        tracker = make_tracker(
            visit(),
            referral("RF-1", "V-1", "甲科", sid="r1"),
            fee("V-1", "RC-9", "referral_consult", referral_id="RF-1"),
            referral("RF-1", "V-1", "甲科", state="rejected", rev=2, sid="r1b",
                     at="2026-10-01T09:00:00+08:00"),
        )
        fee_view = tracker.coordinator_view("P-1", "V-1", actor=COORD).fees[0]
        self.assertEqual(fee_view.status, "void")

    def test_registration_fee_waits_for_visit_confirmation(self):
        tracker = make_tracker(fee("V-1", "RC-1", "initial_registration"))
        tracker.ingest(visit(sid="v1", at="2026-10-01T09:00:00+08:00"), actor=CLERK)
        view = tracker.coordinator_view("P-1", "V-1", actor=COORD)
        self.assertEqual(view.fees[0].status, "applied")
        self.assertTrue(any("RC-1" in s for s in view.completed_services))

    def test_slot_reserved_on_acceptance_and_released_on_no_show(self):
        tracker = make_tracker(
            visit(),
            referral("RF-1", "V-1", "甲科", sid="r1"),
            slot("V-1", "RF-1", "甲科", "S-1"),
        )
        slot_view = tracker.coordinator_view("P-1", "V-1", actor=COORD).slots[0]
        self.assertEqual(slot_view.status, "held")
        tracker.ingest(referral("RF-1", "V-1", "甲科", state="accepted", rev=2, sid="r1b",
                                at="2026-10-01T09:00:00+08:00"), actor=CLERK)
        slot_view = tracker.coordinator_view("P-1", "V-1", actor=COORD).slots[0]
        self.assertEqual(slot_view.status, "reserved")
        tracker.ingest(referral("RF-1", "V-1", "甲科", state="no_show", rev=3, sid="r1c",
                                at="2026-10-01T10:00:00+08:00"), actor=CLERK)
        slot_view = tracker.coordinator_view("P-1", "V-1", actor=COORD).slots[0]
        self.assertEqual(slot_view.status, "released")


class AuthorizationAndViewTests(unittest.TestCase):
    def test_ingest_requires_authorized_role(self):
        tracker = ReferralTracker()
        with self.assertRaises(UnauthorizedError):
            tracker.ingest(exam("V-1", "R-1", 1), actor=CASHIER)
        with self.assertRaises(UnauthorizedError):
            tracker.ingest(visit(), actor=COORD)
        result = tracker.ingest(fee("V-1", "RC-1", "initial_registration"), actor=CASHIER)
        self.assertTrue(result.applied)

    def test_department_staff_only_see_own_department(self):
        tracker = make_tracker(visit(), referral("RF-1", "V-1", "甲科", sid="r1"))
        staff = Actor(staff_id="d-1", role="department_staff", department="甲科")
        view = tracker.department_view("甲科", actor=staff)
        self.assertEqual(len(view.referrals), 1)
        self.assertEqual(view.referrals[0].direction, "incoming")
        outsider = Actor(staff_id="d-2", role="department_staff", department="乙科")
        with self.assertRaises(UnauthorizedError):
            tracker.department_view("甲科", actor=outsider)

    def test_coordinator_explanation_mentions_stuck_point(self):
        tracker = make_tracker(
            visit(),
            referral("RF-1", "V-1", "甲科", sid="r1", requires=["R-1"]),
        )
        view = tracker.coordinator_view("P-1", "V-1", actor=COORD)
        text = "\n".join(view.explanation)
        self.assertIn("等待接收科室确认接诊", text)
        self.assertIn("R-1", text)
        self.assertIn("甲科", text)


class OutOfOrderTests(unittest.TestCase):
    def test_referral_before_visit_resolves_after_visit_arrives(self):
        tracker = make_tracker(referral("RF-1", "V-1", "甲科", sid="r1"))
        tracker.ingest(visit(sid="v1", at="2026-10-01T09:00:00+08:00"), actor=CLERK)
        status = tracker.referral_status(ReferralKey("P-1", "V-1", "RF-1"), actor=COORD)
        self.assertEqual(status.state, "sent")

    def test_visit_rejects_mismatched_hints_from_early_records(self):
        tracker = make_tracker(exam("V-1", "R-1", 1, pid="P-9"))
        with self.assertRaises(ConflictError):
            tracker.ingest(visit(pid="P-1", vid="V-1", sid="v1"), actor=CLERK)


class DrillFixtureTests(unittest.TestCase):
    def test_multi_specialty_drill(self):
        document = json.loads((FIXTURES / "drill_multi_specialty.json").read_text(encoding="utf-8"))
        tracker = make_tracker(*document["sources"])
        key = lambda rid: ReferralKey("P-SYN-21", "V-5502", rid)

        rf01 = tracker.referral_status(key("RF-01"), actor=COORD)
        self.assertEqual(rf01.state, "sent")
        self.assertEqual(rf01.missing_materials, ("R-101",))
        self.assertEqual(rf01.owner, "心血管内科")

        rf02 = tracker.referral_status(key("RF-02"), actor=COORD)
        self.assertEqual(rf02.state, "accepted")
        self.assertEqual(len(rf02.rereview_flags), 1)  # R-100 接诊后更正

        rf03 = tracker.referral_status(key("RF-03"), actor=COORD)
        self.assertEqual(rf03.outcome, "rejected")
        self.assertEqual(rf03.owner, "消化内科")

        rf04 = tracker.referral_status(key("RF-04"), actor=COORD)
        self.assertEqual(rf04.outcome, "no_show")

        view = tracker.coordinator_view("P-SYN-21", "V-5502", actor=COORD)
        fees = {f.receipt_id: f.status for f in view.fees}
        self.assertEqual(fees["RC-88"], "applied")   # 首诊挂号费
        self.assertEqual(fees["RC-89"], "applied")   # RF-02 接诊确认后入账
        slots = {s.slot_id: s.status for s in view.slots}
        self.assertEqual(slots["S-301"], "held")     # RF-01 未接诊，号源挂起
        self.assertEqual(slots["S-302"], "released")  # RF-04 未到诊，号源释放
        self.assertTrue(any("首诊科室接诊" in s for s in view.completed_services))

        dept = tracker.department_view("内分泌科", actor=CLERK)
        evidence = dept.referrals[0].evidence[0]
        self.assertEqual((evidence.revision, evidence.basis), (1, "pinned_at_acceptance"))
        cardio = tracker.department_view("心血管内科", actor=CLERK)
        self.assertEqual(cardio.referrals[0].evidence[0].revision, 2)  # 未接诊用有效版


if __name__ == "__main__":
    unittest.main()
