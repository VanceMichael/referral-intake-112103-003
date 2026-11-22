"""院内转诊资料接收与跟踪能力。

一次挂号后，首诊、检查、转诊、费用、号源记录从各自系统接入；
本能力负责幂等归并、版本化依据、状态流转与按角色的查询视图。
所有演练数据均为合成数据，不含真实医疗信息。
"""

from .actors import Actor, Authorizer
from .errors import (
    ConflictError,
    EnvelopeValidationError,
    InvalidTransitionError,
    KeyMismatchError,
    ReferralTrackerError,
    UnauthorizedError,
    UnknownReferralError,
    UnknownVisitError,
)
from .identity import ReferralKey
from .store import IngestResult
from .tracker import ReferralTracker
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

__all__ = [
    "Actor",
    "Authorizer",
    "ConflictError",
    "CoordinatorVisitView",
    "DepartmentReferralView",
    "DepartmentView",
    "EnvelopeValidationError",
    "EvidenceItem",
    "FeeView",
    "IngestResult",
    "InvalidTransitionError",
    "KeyMismatchError",
    "ReferralKey",
    "ReferralStatus",
    "ReferralTracker",
    "ReferralTrackerError",
    "RereviewFlag",
    "SlotView",
    "UnauthorizedError",
    "UnknownReferralError",
    "UnknownVisitError",
]
