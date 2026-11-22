"""对外门面：接入记录 + 查询转诊跟踪视图。"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .actors import Actor, Authorizer
from .errors import KeyMismatchError
from .identity import ReferralKey
from .records import Envelope, parse_envelope
from .store import IngestResult, ReferralAggregate, ReferralStore, VisitAggregate
from .views import (
    CoordinatorVisitView,
    DepartmentReferralView,
    DepartmentView,
    EvidenceItem,
    FeeView,
    RereviewFlag,
    ReferralStatus,
    SlotView,
)

_STAGE_TEXT = {
    "sent": ("awaiting_acceptance", "已发出，等待接收科室确认接诊"),
    "accepted": ("in_consultation", "接收科室已确认，接诊中"),
    "completed": ("completed", "会诊已完成"),
    "rejected": ("returned", "被接收科室退回，需首诊科室跟进"),
    "no_show": ("no_show", "患者未到诊，需重新安排"),
}


def _owner(ref: ReferralAggregate) -> tuple[str, str]:
    """当前责任方：谁应当接手下一步。"""
    origin = ref.from_department or "首诊科室"
    if ref.state == "sent":
        return ref.to_department, f"{ref.to_department}（待确认接诊）"
    if ref.state == "accepted":
        return ref.to_department, f"{ref.to_department}（接诊中）"
    if ref.state == "completed":
        return ref.to_department, f"{ref.to_department}（已完成）"
    if ref.state == "rejected":
        return origin, f"{origin}（转诊被退回，需跟进）"
    return origin, f"{origin}（患者未到诊，需重新安排）"


class ReferralTracker:
    """转诊资料接收与跟踪能力的入口。"""

    def __init__(
        self,
        authorizer: Authorizer | None = None,
        store: ReferralStore | None = None,
    ) -> None:
        self._store = store or ReferralStore()
        self._auth = authorizer or Authorizer()

    # ------------------------------------------------------------------ 接入

    def ingest(self, raw: Mapping[str, Any] | Envelope, *, actor: Actor) -> IngestResult:
        envelope = raw if isinstance(raw, Envelope) else parse_envelope(raw)
        self._auth.require_ingest(actor, envelope.record_kind)
        return self._store.ingest(envelope)

    def ingest_many(
        self, raws: Iterable[Mapping[str, Any] | Envelope], *, actor: Actor
    ) -> list[IngestResult]:
        return [self.ingest(raw, actor=actor) for raw in raws]

    # ------------------------------------------------------------------ 查询

    def referral_status(self, key: ReferralKey, *, actor: Actor) -> ReferralStatus:
        """单张转诊单：当前由谁接手、哪些依据仍未到齐。"""
        self._auth.require_view(actor, "referral")
        visit = self._store.visit(key.visit_id)
        if visit.patient_ref != key.patient_ref:
            raise KeyMismatchError(
                f"就诊 {key.visit_id} 属于 {visit.patient_ref}，与 {key.patient_ref} 不匹配"
            )
        ref = self._store.referral(key.visit_id, key.referral_id)
        return self._status(visit, ref)

    def coordinator_view(
        self, patient_ref: str, visit_id: str, *, actor: Actor
    ) -> CoordinatorVisitView:
        """门诊协调员视图：按就诊过程解释每张转诊卡在哪里。"""
        self._auth.require_view(actor, "coordinator")
        visit = self._store.visit(visit_id)
        if visit.patient_ref != patient_ref:
            raise KeyMismatchError(
                f"就诊 {visit_id} 属于 {visit.patient_ref}，与 {patient_ref} 不匹配"
            )
        referrals = tuple(
            self._status(visit, ref) for ref in self._store.referrals_for_visit(visit_id)
        )
        fees = tuple(
            FeeView(f.receipt_id, f.fee_kind, f.referral_id, f.status, f.reason)
            for f in self._store.fees_for_visit(visit_id)
        )
        slots = tuple(
            SlotView(s.slot_id, s.referral_id, s.department, s.status, s.reason)
            for s in self._store.slots_for_visit(visit_id)
        )
        return CoordinatorVisitView(
            patient_ref=visit.patient_ref,
            visit_id=visit.visit_id,
            department=visit.department,
            visit_state=visit.state,
            completed_services=tuple(visit.services),
            referrals=referrals,
            fees=fees,
            slots=slots,
            explanation=self._explain(referrals, fees, slots),
        )

    def department_view(self, department: str, *, actor: Actor) -> DepartmentView:
        """科室视图：科室人员只看到适用于本科室的资料版本。"""
        self._auth.require_view(actor, "department", department=department)
        items: list[DepartmentReferralView] = []
        for ref in self._store.all_referrals():
            if department not in (ref.from_department, ref.to_department):
                continue
            visit = self._store.find_visit(ref.visit_id)
            patient_ref = (
                visit.patient_ref if visit else (ref.patient_hint or "（就诊未登记）")
            )
            direction = "incoming" if ref.to_department == department else "outgoing"
            items.append(
                DepartmentReferralView(
                    referral_id=ref.referral_id,
                    visit_id=ref.visit_id,
                    patient_ref=patient_ref,
                    direction=direction,
                    state=ref.state,
                    outcome=ref.outcome,
                    evidence=tuple(
                        EvidenceItem(**item) for item in self._store.evidence_for(ref)
                    ),
                    rereview_flags=tuple(
                        RereviewFlag(**flag) for flag in self._store.rereview_flags(ref)
                    ),
                    missing_materials=self._store.missing_materials(ref),
                )
            )
        items.sort(key=lambda item: (item.visit_id, item.referral_id))
        return DepartmentView(department=department, referrals=tuple(items))

    def known_visits(self, *, actor: Actor) -> tuple[tuple[str, str], ...]:
        self._auth.require_view(actor, "visit")
        return self._store.known_visits()

    def known_departments(self, *, actor: Actor) -> tuple[str, ...]:
        self._auth.require_view(actor, "visit")
        return self._store.known_departments()

    # ------------------------------------------------------------------ 内部

    def _status(self, visit: VisitAggregate, ref: ReferralAggregate) -> ReferralStatus:
        stage, _ = _STAGE_TEXT[ref.state]
        owner, owner_note = _owner(ref)
        timeline = tuple(
            (event.collected_at.isoformat(), event.note)
            for event in sorted(ref.events, key=lambda e: e.collected_at)
        )
        return ReferralStatus(
            key=ReferralKey(visit.patient_ref, ref.visit_id, ref.referral_id),
            state=ref.state,
            stage=stage,
            owner=owner,
            owner_note=owner_note,
            outcome=ref.outcome,
            missing_materials=self._store.missing_materials(ref),
            rereview_flags=tuple(
                RereviewFlag(**flag) for flag in self._store.rereview_flags(ref)
            ),
            timeline=timeline,
        )

    @staticmethod
    def _explain(
        referrals: tuple[ReferralStatus, ...],
        fees: tuple[FeeView, ...],
        slots: tuple[SlotView, ...],
    ) -> tuple[str, ...]:
        lines: list[str] = []
        for status in referrals:
            _, stage_text = _STAGE_TEXT[status.state]
            line = (
                f"转诊单 {status.key.referral_id}：{stage_text}；"
                f"当前由{status.owner_note}负责。"
            )
            if status.missing_materials:
                line += f" 依据仍未到齐：{', '.join(status.missing_materials)}。"
            if status.rereview_flags:
                line += f" {len(status.rereview_flags)} 项依据在接诊后更正，需复核。"
            if status.outcome == "rejected":
                line += " 该转诊未成交，首诊已完成的服务不受影响。"
            elif status.outcome == "no_show":
                line += " 患者未到诊，记为未成交，首诊已完成的服务不受影响。"
            lines.append(line)
        for fee in fees:
            if fee.status == "held":
                lines.append(f"费用 {fee.receipt_id}（{fee.fee_kind}）挂起：{fee.reason}。")
            elif fee.status == "void":
                lines.append(f"费用 {fee.receipt_id}（{fee.fee_kind}）不予入账：{fee.reason}。")
        for slot in slots:
            if slot.status == "held":
                lines.append(f"号源 {slot.slot_id}（{slot.department}）挂起：{slot.reason}。")
            elif slot.status == "released":
                lines.append(f"号源 {slot.slot_id}（{slot.department}）已释放：{slot.reason}。")
        return tuple(lines)
