"""对外视图的只读数据结构：协调员视图与科室视图。"""

from __future__ import annotations

from dataclasses import dataclass

from .identity import ReferralKey


@dataclass(frozen=True)
class EvidenceItem:
    """一条适用于某科室的依据版本。"""

    report_id: str
    revision: int
    finding_code: str | None
    basis: str  # "current_valid"（未接诊，用有效版）| "pinned_at_acceptance"（接诊时留存）


@dataclass(frozen=True)
class RereviewFlag:
    """接诊后依据被更正/失效的复核提示。"""

    report_id: str
    accepted_revision: int
    current_revision: int | None
    reason: str


@dataclass(frozen=True)
class FeeView:
    receipt_id: str
    fee_kind: str
    referral_id: str | None
    status: str  # applied / held / void
    reason: str


@dataclass(frozen=True)
class SlotView:
    slot_id: str
    referral_id: str
    department: str
    status: str  # held / reserved / consumed / released / dropped
    reason: str


@dataclass(frozen=True)
class ReferralStatus:
    """一张转诊单的当前状态：谁在接手、缺哪些依据、有哪些复核提示。"""

    key: ReferralKey
    state: str
    stage: str
    owner: str
    owner_note: str
    outcome: str | None  # success / rejected / no_show / None（进行中）
    missing_materials: tuple[str, ...]
    rereview_flags: tuple[RereviewFlag, ...]
    timeline: tuple[tuple[str, str], ...]  # (collected_at ISO, 说明)


@dataclass(frozen=True)
class CoordinatorVisitView:
    """门诊协调员视图：按就诊过程解释每张转诊卡在哪里。"""

    patient_ref: str
    visit_id: str
    department: str
    visit_state: str
    completed_services: tuple[str, ...]
    referrals: tuple[ReferralStatus, ...]
    fees: tuple[FeeView, ...]
    slots: tuple[SlotView, ...]
    explanation: tuple[str, ...]


@dataclass(frozen=True)
class DepartmentReferralView:
    """科室视图中的一张转诊单：只含适用于该科室的依据版本。"""

    referral_id: str
    visit_id: str
    patient_ref: str
    direction: str  # incoming（转入本科室）/ outgoing（从本科室转出）
    state: str
    outcome: str | None
    evidence: tuple[EvidenceItem, ...]
    rereview_flags: tuple[RereviewFlag, ...]
    missing_materials: tuple[str, ...]


@dataclass(frozen=True)
class DepartmentView:
    department: str
    referrals: tuple[DepartmentReferralView, ...]
