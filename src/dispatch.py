"""按权属与专业领域派单。

候选人为角色与场所专业领域都匹配、且在手案件未达容量上限的人员；
负载相同按人员标识排序，保证结果可复现。
"""


def choose_assignee(people: list[dict], *, domain: str, role: str,
                    loads: dict[str, int]) -> dict | None:
    """返回最合适的承办人；无人可派时返回 None（进入待认领池）。"""
    candidates = [
        p for p in people
        if p["role"] == role
        and domain in p["domains"]
        and loads.get(p["person_id"], 0) < p["capacity"]
    ]
    candidates.sort(key=lambda p: (loads.get(p["person_id"], 0), p["person_id"]))
    return candidates[0] if candidates else None
