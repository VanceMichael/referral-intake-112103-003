"""转诊的复合身份标识。

患者身份、就诊号、转诊单号是三个相互独立的字段：同一次就诊可以
发往多个专科（多张转诊单），同一患者也有多次就诊。任何查询都必须
同时给出三者，绝不拼接成单一键使用。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReferralKey:
    """转诊单的唯一身份：患者身份 + 就诊号 + 转诊单号。"""

    patient_ref: str
    visit_id: str
    referral_id: str

    def __post_init__(self) -> None:
        for name in ("patient_ref", "visit_id", "referral_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空字符串")
