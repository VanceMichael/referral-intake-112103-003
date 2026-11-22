"""院内授权：只有被授权的院内人员才能接入记录或查看视图。"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import UnauthorizedError


@dataclass(frozen=True)
class Actor:
    """一次操作的执行人。"""

    staff_id: str
    role: str
    department: str | None = None


DEFAULT_POLICY: dict[str, dict[str, object]] = {
    # 病案室录入员：可接入全部类别，可查看全部视图
    "records_clerk": {
        "ingest": "*",
        "views": frozenset({"coordinator", "department", "referral", "visit"}),
    },
    # 门诊协调员：只读，面向患者解释就诊过程
    "coordinator": {
        "ingest": frozenset(),
        "views": frozenset({"coordinator", "referral", "visit"}),
    },
    # 科室人员：可回本科室的转诊/检查记录，只能看本科室视图
    "department_staff": {
        "ingest": frozenset({"referral", "exam_report"}),
        "views": frozenset({"department"}),
    },
    # 收费处：只接入费用记录
    "cashier": {
        "ingest": frozenset({"fee_receipt"}),
        "views": frozenset(),
    },
}


class Authorizer:
    """按角色策略校验操作权限。"""

    def __init__(self, policy: dict[str, dict[str, object]] | None = None) -> None:
        self._policy = policy or DEFAULT_POLICY

    def require_ingest(self, actor: Actor, record_kind: str) -> None:
        grant = self._policy.get(actor.role, {}).get("ingest", frozenset())
        if grant == "*" or record_kind in grant:
            return
        raise UnauthorizedError(
            f"{actor.staff_id}（{actor.role}）无权接入 {record_kind} 记录"
        )

    def require_view(self, actor: Actor, view: str, *, department: str | None = None) -> None:
        grant = self._policy.get(actor.role, {}).get("views", frozenset())
        if view not in grant:
            raise UnauthorizedError(f"{actor.staff_id}（{actor.role}）无权查看 {view} 视图")
        if view == "department" and actor.role == "department_staff":
            if actor.department != department:
                raise UnauthorizedError("科室人员只能查看本科室的视图")
