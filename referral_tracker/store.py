"""转诊资料接收与跟踪的核心存储。

不变量：
1. 同一 source_id 只应用一次，重复导入被识别并忽略；
   同一报告/转诊的同一版本号若出现不同内容，判为冲突并拒绝，
   避免医生看到相互矛盾的时间线。
2. 检查报告到达只影响“依据”，绝不改变转诊状态；
   资料到达不等于任何环节完成。
3. 费用与号源只有在对应环节被明确确认后才生效，否则保持挂起。
4. 转诊被拒绝或患者未到诊记为未成交，不记为成功完成，
   也不影响首诊已经完成的服务。
5. 患者身份、就诊号、转诊单号分别存放，绝不合并为一个键。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .errors import (
    ConflictError,
    EnvelopeValidationError,
    InvalidTransitionError,
    UnknownReferralError,
    UnknownVisitError,
)
from .records import Envelope

# 转诊状态机：被拒绝 / 患者未到诊 / 已完成 都是终态
TERMINAL_STATES = frozenset({"rejected", "no_show", "completed"})
SUCCESS_STATE = "completed"
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "sent": frozenset({"accepted", "rejected"}),
    "accepted": frozenset({"completed", "no_show"}),
    "rejected": frozenset(),
    "no_show": frozenset(),
    "completed": frozenset(),
}


def _require(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise EnvelopeValidationError(f"payload 缺少有效字段 {name}")
    return value.strip()


def _optional_revision(payload: Mapping[str, Any], default: int = 1) -> int:
    value = payload.get("revision", default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise EnvelopeValidationError("revision 必须是正整数")
    return value


def _optional_requires(payload: Mapping[str, Any]) -> tuple[str, ...]:
    value = payload.get("requires", ())
    if not isinstance(value, (list, tuple)):
        raise EnvelopeValidationError("requires 必须是字符串列表")
    items = tuple(v.strip() for v in value if isinstance(v, str) and v.strip())
    if len(items) != len(value):
        raise EnvelopeValidationError("requires 必须是非空字符串列表")
    return items


@dataclass(frozen=True)
class ReportVersion:
    """检查报告的一个修订版本。"""

    report_id: str
    revision: int
    state: str
    finding_code: str | None
    collected_at: datetime
    source_id: str


class ExamHistory:
    """一份检查报告的全部修订版本；有效版本 = 修订号最大的 final 版。"""

    def __init__(self, report_id: str, patient_hint: str | None = None) -> None:
        self.report_id = report_id
        self.patient_hint = patient_hint
        self.versions: list[ReportVersion] = []

    def get(self, revision: int) -> ReportVersion | None:
        for version in self.versions:
            if version.revision == revision:
                return version
        return None

    def add(self, version: ReportVersion) -> None:
        self.versions.append(version)
        self.versions.sort(key=lambda v: v.revision)

    @property
    def current_valid(self) -> ReportVersion | None:
        finals = [v for v in self.versions if v.state == "final"]
        return finals[-1] if finals else None


@dataclass
class ReferralEvent:
    """转诊时间线上的一个事件（仅来自已应用的记录）。"""

    collected_at: datetime
    source_id: str
    note: str


@dataclass
class ReferralAggregate:
    """一张转诊单的当前状态与历史。"""

    visit_id: str
    referral_id: str
    from_department: str | None
    to_department: str
    requires: tuple[str, ...] = ()
    state: str = "sent"
    revision: int = 0
    patient_hint: str | None = None
    # 接诊确认时钉住的依据版本 {report_id: revision}；未接诊为 None
    accepted_basis: dict[str, int] | None = None
    revisions: dict[int, str] = field(default_factory=dict)
    events: list[ReferralEvent] = field(default_factory=list)

    @property
    def outcome(self) -> str | None:
        """转诊结局：success / rejected / no_show；进行中为 None。"""
        if self.state == "completed":
            return "success"
        if self.state == "rejected":
            return "rejected"
        if self.state == "no_show":
            return "no_show"
        return None


@dataclass
class VisitAggregate:
    """一次就诊：首诊服务与费用落账都挂在这里。"""

    visit_id: str
    patient_ref: str
    department: str
    state: str
    services: list[str] = field(default_factory=list)
    events: list[ReferralEvent] = field(default_factory=list)


@dataclass
class FeeRecord:
    """一条费用记录；status 由对应环节的确认情况推导。"""

    receipt_id: str
    visit_id: str
    fee_kind: str
    reported_state: str
    referral_id: str | None
    collected_at: datetime
    status: str = "held"  # applied / held / void
    reason: str = ""


@dataclass
class SlotRecord:
    """一条号源占用申请；释放由转诊结局自动派生。"""

    slot_id: str
    visit_id: str
    referral_id: str
    department: str
    collected_at: datetime
    status: str = "held"  # held / reserved / consumed / released / dropped
    reason: str = ""


@dataclass(frozen=True)
class IngestResult:
    """一条记录的接入结果。"""

    source_id: str
    record_kind: str
    applied: bool
    note: str


class ReferralStore:
    """内存中的转诊资料存储，执行全部业务不变量。"""

    def __init__(self) -> None:
        self._seen_sources: set[str] = set()
        self._visits: dict[str, VisitAggregate] = {}
        self._referrals: dict[tuple[str, str], ReferralAggregate] = {}
        self._reports: dict[tuple[str, str], ExamHistory] = {}
        self._fees: dict[str, FeeRecord] = {}
        self._slots: dict[str, SlotRecord] = {}

    # ------------------------------------------------------------------ 接入

    def ingest(self, envelope: Envelope) -> IngestResult:
        if envelope.source_id in self._seen_sources:
            return IngestResult(
                envelope.source_id, envelope.record_kind, False, "重复导入，已忽略"
            )
        handler = {
            "visit": self._ingest_visit,
            "exam_report": self._ingest_exam,
            "referral": self._ingest_referral,
            "fee_receipt": self._ingest_fee,
            "slot_update": self._ingest_slot,
        }[envelope.record_kind]
        changed, note = handler(envelope)
        self._seen_sources.add(envelope.source_id)
        self._reevaluate_all()
        return IngestResult(envelope.source_id, envelope.record_kind, changed, note)

    def _ingest_visit(self, env: Envelope) -> tuple[bool, str]:
        p = env.payload
        visit_id = _require(p, "visit_id")
        patient_ref = _require(p, "patient_ref")
        department = _require(p, "department")
        state = _require(p, "state")
        existing = self._visits.get(visit_id)
        if existing is not None:
            if existing.patient_ref != patient_ref:
                raise ConflictError(
                    f"就诊 {visit_id} 已登记为 {existing.patient_ref}，不能并入 {patient_ref}"
                )
            if existing.state == state:
                return False, "首诊记录已存在且内容一致"
            existing.state = state
            if state == "seen" and not any("首诊科室接诊" in s for s in existing.services):
                existing.services.append(f"首诊科室接诊（{existing.department}）")
            existing.events.append(
                ReferralEvent(env.collected_at, env.source_id, f"首诊状态更新为 {state}")
            )
            return True, "首诊状态已更新"
        # 先校验早到的转诊/检查记录里的患者身份提示，再落库
        for ref in self._referrals_for_visit(visit_id):
            if ref.patient_hint and ref.patient_hint != patient_ref:
                raise ConflictError(
                    f"转诊单 {ref.referral_id} 的患者身份提示与就诊 {visit_id} 不一致"
                )
        for (vid, report_id), history in self._reports.items():
            if vid == visit_id and history.patient_hint and history.patient_hint != patient_ref:
                raise ConflictError(
                    f"检查报告 {report_id} 的患者身份提示与就诊 {visit_id} 不一致"
                )
        visit = VisitAggregate(visit_id, patient_ref, department, state)
        if state == "seen":
            visit.services.append(f"首诊科室接诊（{department}）")
        visit.events.append(
            ReferralEvent(env.collected_at, env.source_id, f"首诊登记：{department}，状态 {state}")
        )
        self._visits[visit_id] = visit
        return True, "首诊记录已登记"

    def _ingest_exam(self, env: Envelope) -> tuple[bool, str]:
        p = env.payload
        visit_id = _require(p, "visit_id")
        report_id = _require(p, "report_id")
        revision = _optional_revision(p)
        state = _require(p, "state")
        finding = p.get("finding_code")
        if finding is not None and not isinstance(finding, str):
            raise EnvelopeValidationError("finding_code 必须是字符串")
        patient_ref = p.get("patient_ref")
        visit = self._visits.get(visit_id)
        if visit is not None and patient_ref is not None and patient_ref != visit.patient_ref:
            raise ConflictError(f"检查报告 {report_id} 的患者身份与就诊 {visit_id} 不一致")
        key = (visit_id, report_id)
        history = self._reports.get(key)
        if history is None:
            history = ExamHistory(report_id, patient_hint=patient_ref)
            self._reports[key] = history
        existing = history.get(revision)
        if existing is not None:
            if (existing.state, existing.finding_code) == (state, finding):
                return False, "同一报告版本重复导入，已忽略"
            raise ConflictError(f"报告 {report_id} 第 {revision} 版已存在且内容不同")
        history.add(
            ReportVersion(report_id, revision, state, finding, env.collected_at, env.source_id)
        )
        # 报告更正只影响“依据”：给已接诊的转诊追加复核提示，绝不改变转诊状态
        if state == "final":
            for ref in self._referrals_for_visit(visit_id):
                if ref.accepted_basis is None:
                    continue
                pinned = ref.accepted_basis.get(report_id)
                if pinned is not None and revision > pinned:
                    ref.events.append(
                        ReferralEvent(
                            env.collected_at,
                            env.source_id,
                            f"依据报告 {report_id} 在接诊后更正"
                            f"（第 {pinned} 版 → 第 {revision} 版），需复核",
                        )
                    )
        return True, f"报告 {report_id} 第 {revision} 版已接入"

    def _ingest_referral(self, env: Envelope) -> tuple[bool, str]:
        p = env.payload
        referral_id = _require(p, "referral_id")
        visit_id = _require(p, "visit_id")
        state = _require(p, "state")
        revision = _optional_revision(p)
        patient_ref = p.get("patient_ref")
        visit = self._visits.get(visit_id)
        if visit is not None and patient_ref is not None and patient_ref != visit.patient_ref:
            raise ConflictError(f"转诊单 {referral_id} 的患者身份与就诊 {visit_id} 不一致")
        key = (visit_id, referral_id)
        ref = self._referrals.get(key)
        if ref is None:
            if state != "sent":
                raise InvalidTransitionError("转诊单的首条记录状态必须是 sent")
            ref = ReferralAggregate(
                visit_id=visit_id,
                referral_id=referral_id,
                from_department=p.get("from_department"),
                to_department=_require(p, "to_department"),
                requires=_optional_requires(p),
                revision=revision,
                patient_hint=patient_ref,
            )
            ref.revisions[revision] = state
            ref.events.append(
                ReferralEvent(env.collected_at, env.source_id, f"转诊单已发往{ref.to_department}")
            )
            self._referrals[key] = ref
            return True, "转诊单已登记"
        known_state = ref.revisions.get(revision)
        if known_state is not None:
            if known_state == state:
                return False, "同一转诊版本重复导入，已忽略"
            raise ConflictError(
                f"转诊单 {referral_id} 第 {revision} 版已记录为 {known_state}，不能改为 {state}"
            )
        if revision < ref.revision:
            raise ConflictError(
                f"转诊单 {referral_id} 收到更旧的版本 {revision}（当前第 {ref.revision} 版）"
            )
        if state == ref.state:
            if state == "sent":
                # 发出后、接诊前允许补充转诊单内容（如所需依据清单）
                ref.requires = _optional_requires(p) or ref.requires
                if p.get("to_department"):
                    ref.to_department = p["to_department"].strip()
                if p.get("from_department"):
                    ref.from_department = p["from_department"].strip()
                ref.revisions[revision] = state
                ref.revision = revision
                ref.events.append(
                    ReferralEvent(env.collected_at, env.source_id, "转诊单内容已补充")
                )
                return True, "转诊单内容已补充"
            ref.revisions[revision] = state
            ref.revision = revision
            return False, "重复状态记录，已忽略"
        self._transition(ref, state, env)
        ref.revisions[revision] = state
        ref.revision = revision
        return True, f"转诊状态更新为 {state}"

    def _transition(self, ref: ReferralAggregate, state: str, env: Envelope) -> None:
        if state not in _ALLOWED_TRANSITIONS.get(ref.state, frozenset()):
            raise InvalidTransitionError(
                f"转诊单 {ref.referral_id} 不能从 {ref.state} 流转到 {state}"
            )
        if state == "accepted":
            # 接诊确认：钉住医生当时所依据的有效报告版本
            ref.accepted_basis = {
                report_id: history.current_valid.revision
                for (vid, report_id), history in self._reports.items()
                if vid == ref.visit_id and history.current_valid is not None
            }
            ref.events.append(
                ReferralEvent(
                    env.collected_at, env.source_id,
                    f"{ref.to_department}已确认接诊，当时依据版本已留存",
                )
            )
        elif state == "rejected":
            ref.events.append(
                ReferralEvent(
                    env.collected_at, env.source_id,
                    f"{ref.to_department}退回转诊单（记为未成交，首诊已完成服务不受影响）",
                )
            )
        elif state == "no_show":
            ref.events.append(
                ReferralEvent(
                    env.collected_at, env.source_id,
                    "患者未到诊（记为未成交，首诊已完成服务不受影响）",
                )
            )
        elif state == "completed":
            ref.events.append(
                ReferralEvent(
                    env.collected_at, env.source_id, f"{ref.to_department}确认会诊完成"
                )
            )
        ref.state = state

    def _ingest_fee(self, env: Envelope) -> tuple[bool, str]:
        p = env.payload
        visit_id = _require(p, "visit_id")
        receipt_id = _require(p, "receipt_id")
        fee_kind = _require(p, "fee_kind")
        state = _require(p, "state")
        referral_id = p.get("referral_id")
        if fee_kind == "referral_consult" and not referral_id:
            raise EnvelopeValidationError("referral_consult 费用必须携带 referral_id")
        existing = self._fees.get(receipt_id)
        if existing is not None:
            same = (
                existing.visit_id,
                existing.fee_kind,
                existing.reported_state,
                existing.referral_id,
            ) == (visit_id, fee_kind, state, referral_id)
            if same:
                return False, "费用记录重复导入，已忽略"
            raise ConflictError(f"收据 {receipt_id} 已存在且内容不同")
        self._fees[receipt_id] = FeeRecord(
            receipt_id, visit_id, fee_kind, state, referral_id, env.collected_at
        )
        return True, "费用记录已接入（是否入账取决于对应环节是否确认）"

    def _ingest_slot(self, env: Envelope) -> tuple[bool, str]:
        p = env.payload
        visit_id = _require(p, "visit_id")
        referral_id = _require(p, "referral_id")
        department = _require(p, "department")
        action = _require(p, "action")
        if action != "reserve":
            raise EnvelopeValidationError("号源记录仅支持 reserve；释放由转诊结局自动派生")
        slot_id = p.get("slot_id") or env.source_id
        if not isinstance(slot_id, str) or not slot_id.strip():
            raise EnvelopeValidationError("slot_id 必须是非空字符串")
        slot_id = slot_id.strip()
        existing = self._slots.get(slot_id)
        if existing is not None:
            same = (existing.visit_id, existing.referral_id, existing.department) == (
                visit_id,
                referral_id,
                department,
            )
            if same:
                return False, "号源记录重复导入，已忽略"
            raise ConflictError(f"号源 {slot_id} 已存在且内容不同")
        self._slots[slot_id] = SlotRecord(
            slot_id, visit_id, referral_id, department, env.collected_at
        )
        return True, "号源申请已接入（是否占用取决于接诊确认）"

    # ---------------------------------------------------------- 费用/号源闸门

    def _reevaluate_all(self) -> None:
        for fee in self._fees.values():
            self._evaluate_fee(fee)
        for slot in self._slots.values():
            self._evaluate_slot(slot)

    def _evaluate_fee(self, fee: FeeRecord) -> None:
        if fee.reported_state != "settled":
            fee.status, fee.reason = "void", "来源未结算该费用"
            return
        visit = self._visits.get(fee.visit_id)
        if fee.fee_kind == "initial_registration":
            if visit is None:
                fee.status, fee.reason = "held", "首诊记录尚未确认，费用挂起"
                return
            fee.status, fee.reason = "applied", "首诊环节已确认"
            service = f"首次挂号费已结算（{fee.receipt_id}）"
            if service not in visit.services:
                visit.services.append(service)
            return
        if fee.fee_kind == "referral_consult":
            ref = self._referrals.get((fee.visit_id, fee.referral_id or ""))
            if ref is None:
                fee.status, fee.reason = "held", "转诊单尚未登记，费用挂起"
            elif ref.state in ("accepted", "completed"):
                fee.status, fee.reason = "applied", "接诊环节已确认"
            elif ref.state in TERMINAL_STATES:
                fee.status, fee.reason = "void", "转诊未成交，费用不得入账"
            else:
                fee.status, fee.reason = "held", "接诊确认前费用挂起"
            return
        fee.status, fee.reason = "held", f"未知费用类别 {fee.fee_kind}，费用挂起"

    def _evaluate_slot(self, slot: SlotRecord) -> None:
        ref = self._referrals.get((slot.visit_id, slot.referral_id))
        if ref is None:
            slot.status, slot.reason = "held", "转诊单尚未登记，号源挂起"
            return
        if ref.state == "accepted":
            slot.status, slot.reason = "reserved", "接诊确认后占用号源"
        elif ref.state == "completed":
            slot.status, slot.reason = "consumed", "会诊已完成，号源已使用"
        elif ref.state in ("rejected", "no_show"):
            if slot.status == "reserved":
                slot.status, slot.reason = "released", "转诊未成交，号源已释放"
            else:
                slot.status, slot.reason = "dropped", "转诊未成交，号源不再占用"
        else:
            slot.status, slot.reason = "held", "接诊确认前号源挂起"

    # ------------------------------------------------------------------ 查询

    def find_visit(self, visit_id: str) -> VisitAggregate | None:
        return self._visits.get(visit_id)

    def visit(self, visit_id: str) -> VisitAggregate:
        visit = self._visits.get(visit_id)
        if visit is None:
            raise UnknownVisitError(f"就诊 {visit_id} 尚未登记")
        return visit

    def referral(self, visit_id: str, referral_id: str) -> ReferralAggregate:
        ref = self._referrals.get((visit_id, referral_id))
        if ref is None:
            raise UnknownReferralError(f"就诊 {visit_id} 下不存在转诊单 {referral_id}")
        return ref

    def all_referrals(self) -> list[ReferralAggregate]:
        return list(self._referrals.values())

    def _referrals_for_visit(self, visit_id: str) -> list[ReferralAggregate]:
        return [r for (vid, _), r in self._referrals.items() if vid == visit_id]

    def referrals_for_visit(self, visit_id: str) -> list[ReferralAggregate]:
        return sorted(
            self._referrals_for_visit(visit_id), key=lambda r: r.referral_id
        )

    def valid_report(self, visit_id: str, report_id: str) -> ReportVersion | None:
        history = self._reports.get((visit_id, report_id))
        return history.current_valid if history else None

    def report_version(
        self, visit_id: str, report_id: str, revision: int
    ) -> ReportVersion | None:
        history = self._reports.get((visit_id, report_id))
        return history.get(revision) if history else None

    def missing_materials(self, ref: ReferralAggregate) -> tuple[str, ...]:
        """转诊单声明的依据中，仍没有有效版本的报告。"""
        return tuple(
            report_id
            for report_id in ref.requires
            if self.valid_report(ref.visit_id, report_id) is None
        )

    def rereview_flags(self, ref: ReferralAggregate) -> tuple[dict[str, Any], ...]:
        """已接诊转诊的依据在接诊后被更正/失效时，给出需复核提示。"""
        if ref.accepted_basis is None:
            return ()
        flags: list[dict[str, Any]] = []
        for report_id, pinned in sorted(ref.accepted_basis.items()):
            current = self.valid_report(ref.visit_id, report_id)
            if current is None:
                flags.append(
                    {
                        "report_id": report_id,
                        "accepted_revision": pinned,
                        "current_revision": None,
                        "reason": "接诊依据的报告已无有效版本，需复核",
                    }
                )
            elif current.revision > pinned:
                flags.append(
                    {
                        "report_id": report_id,
                        "accepted_revision": pinned,
                        "current_revision": current.revision,
                        "reason": f"接诊后报告更正为第 {current.revision} 版，需复核",
                    }
                )
        return tuple(flags)

    def evidence_for(self, ref: ReferralAggregate) -> tuple[dict[str, Any], ...]:
        """科室适用的依据版本：未接诊用当前有效版，已接诊用接诊时钉住的版本。"""
        items: list[dict[str, Any]] = []
        if ref.accepted_basis is None:
            report_ids = ref.requires or tuple(
                sorted(
                    report_id
                    for (vid, report_id), history in self._reports.items()
                    if vid == ref.visit_id and history.current_valid is not None
                )
            )
            for report_id in report_ids:
                version = self.valid_report(ref.visit_id, report_id)
                if version is not None:
                    items.append(
                        {
                            "report_id": report_id,
                            "revision": version.revision,
                            "finding_code": version.finding_code,
                            "basis": "current_valid",
                        }
                    )
        else:
            for report_id, revision in sorted(ref.accepted_basis.items()):
                version = self.report_version(ref.visit_id, report_id, revision)
                items.append(
                    {
                        "report_id": report_id,
                        "revision": revision,
                        "finding_code": version.finding_code if version else None,
                        "basis": "pinned_at_acceptance",
                    }
                )
        return tuple(items)

    def fees_for_visit(self, visit_id: str) -> list[FeeRecord]:
        return sorted(
            (f for f in self._fees.values() if f.visit_id == visit_id),
            key=lambda f: f.receipt_id,
        )

    def slots_for_visit(self, visit_id: str) -> list[SlotRecord]:
        return sorted(
            (s for s in self._slots.values() if s.visit_id == visit_id),
            key=lambda s: s.slot_id,
        )

    def known_visits(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted((v.patient_ref, v.visit_id) for v in self._visits.values())
        )

    def known_departments(self) -> tuple[str, ...]:
        departments = {v.department for v in self._visits.values()}
        for ref in self._referrals.values():
            if ref.from_department:
                departments.add(ref.from_department)
            departments.add(ref.to_department)
        return tuple(sorted(departments))
