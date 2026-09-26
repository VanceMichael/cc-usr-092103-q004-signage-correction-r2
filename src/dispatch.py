"""派单规则：按场所权属确定责任单位，按专业领域选择承办人。

承办人优先选专家，专家满负荷时选同领域志愿者；个人在办数不得超过
其能力上限。处理时限来自 slas 资料，在派单/认领时一次性写入案件。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from .reference import Person

_ROLE_RANK = {"expert": 0, "volunteer": 1}


def choose_assignee(
    people: Iterable[Person],
    domain: str,
    active_counts: dict[str, int],
) -> Person | None:
    candidates = [
        p
        for p in people
        if p.role in _ROLE_RANK
        and domain in p.domains
        and active_counts.get(p.person_id, 0) < p.max_active_cases
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda p: (_ROLE_RANK[p.role], active_counts.get(p.person_id, 0), p.person_id),
    )


def compute_stage_deadlines(slas: dict[str, int], start: datetime) -> dict[str, str]:
    return {stage: (start + timedelta(hours=hours)).isoformat() for stage, hours in slas.items()}
