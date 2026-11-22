"""转诊资料接收与跟踪能力的异常类型。"""


class ReferralTrackerError(Exception):
    """本能力内所有业务异常的基类。"""


class EnvelopeValidationError(ReferralTrackerError):
    """接入记录的信封或载荷格式不合法。"""


class UnauthorizedError(ReferralTrackerError):
    """操作者没有执行该动作的院内授权。"""


class ConflictError(ReferralTrackerError):
    """记录与已接入内容矛盾（例如同一版本号出现不同内容）。"""


class InvalidTransitionError(ReferralTrackerError):
    """转诊状态流转不符合流程。"""


class UnknownVisitError(ReferralTrackerError):
    """就诊号尚未登记。"""


class UnknownReferralError(ReferralTrackerError):
    """转诊单不存在。"""


class KeyMismatchError(ReferralTrackerError):
    """患者身份、就诊号、转诊单号的组合不一致。"""
