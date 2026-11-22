"""来源记录信封：各系统生成的首诊、检查、转诊、费用与号源记录。

不同系统（首诊登记、检查报告、转诊流转、收费、号源）各自生成记录，
本模块只负责信封层面的校验，业务规则在 store 中执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .errors import EnvelopeValidationError

RECORD_KINDS = frozenset(
    {
        "visit",  # 首诊记录
        "exam_report",  # 检查报告（可携带修订版本）
        "referral",  # 转诊单及其状态流转
        "fee_receipt",  # 费用记录
        "slot_update",  # 号源占用申请（释放由转诊结局自动派生）
    }
)


@dataclass(frozen=True)
class Envelope:
    """一条来源记录的标准信封。"""

    source_id: str
    record_kind: str
    collected_at: datetime
    payload: Mapping[str, Any]


def parse_envelope(raw: Mapping[str, Any]) -> Envelope:
    """把原始字典解析为信封，格式不合法时抛出 EnvelopeValidationError。"""
    if not isinstance(raw, Mapping):
        raise EnvelopeValidationError("记录必须是对象")
    source_id = raw.get("source_id")
    record_kind = raw.get("record_kind")
    collected_at = raw.get("collected_at")
    payload = raw.get("payload")
    if not isinstance(source_id, str) or not source_id.strip():
        raise EnvelopeValidationError("source_id 必须是非空字符串")
    if record_kind not in RECORD_KINDS:
        raise EnvelopeValidationError(f"未知记录类别：{record_kind!r}")
    if not isinstance(collected_at, str) or not collected_at.strip():
        raise EnvelopeValidationError("collected_at 必须是 ISO 时间字符串")
    try:
        parsed_at = datetime.fromisoformat(collected_at.strip())
    except ValueError as exc:
        raise EnvelopeValidationError(f"collected_at 无法解析：{collected_at!r}") from exc
    if not isinstance(payload, Mapping):
        raise EnvelopeValidationError("payload 必须是对象")
    return Envelope(source_id.strip(), record_kind, parsed_at, payload)
