"""公众视图与内部证据链：同一事件流上的两个投影，各自维护信息边界。

- public_case_view：市民扫码所见。只含白名单字段——是否已归并、
  采用的规范版本、确认单位、完成更换时间等；不出现上报人、承办人、
  审校意见细节、回访记录与原件引用。
- internal_evidence_chain：外事部门内部使用。保留完整事件流，
  能从新牌面（更换记录）回到最初照片原件、每次审校意见与现场回访。
"""

from __future__ import annotations

from .service import SignageCorrectionService

PUBLIC_STATUS_LABELS = {
    "reported": "已受理",
    "dispatched": "已派单",
    "terminology_checked": "已核对术语",
    "unit_confirmed": "责任单位已确认",
    "replaced": "已更换",
    "closed": "已办结",
    "rejected": "已驳回",
}


def public_case_view(service: SignageCorrectionService, case_id: str) -> dict:
    state = service.case_state(case_id)
    venue = service.venue(state.venue_id)
    applied = state.applied_standard
    accepted = [c for c in state.confirmations if c["decision"] == "accept"]
    last_replacement = state.replacements[-1] if state.replacements else None
    return {
        "case_id": state.case_id,
        "venue_name": venue.name,
        "status": state.status,
        "status_label": PUBLIC_STATUS_LABELS[state.status],
        "clue_count": len(state.clue_ids),
        "merged": len(state.clue_ids) > 1,
        "standard_id": applied["standard_id"] if applied else None,
        "standard_version": applied["version"] if applied else None,
        "confirmed_by_unit": service.unit(accepted[-1]["unit_id"]).name if accepted else None,
        "replaced_at": last_replacement["replaced_at"] if last_replacement else None,
        "current_text": last_replacement["new_text"] if last_replacement else None,
        "photo_refs": list(state.photos_sanitized),  # 仅脱敏副本
    }


def internal_evidence_chain(service: SignageCorrectionService, case_id: str) -> dict:
    state = service.case_state(case_id)
    venue = service.venue(state.venue_id)
    unit = service.unit(state.unit_id)
    clues = [service.clue(cid) for cid in state.clue_ids]
    return {
        "case_id": state.case_id,
        "status": state.status,
        "venue": {"venue_id": venue.venue_id, "name": venue.name, "category": venue.category},
        "unit": {"unit_id": unit.unit_id, "name": unit.name},
        "assignee_id": state.assignee_id,
        "deadlines": dict(state.deadlines),
        "events": [e.to_dict() for e in service.case_events(case_id)],
        "clues": [
            {
                "clue_id": c.clue_id,
                "reporter_ref": c.reporter_ref,
                "source": c.source,
                "occurred_at": c.occurred_at,
                "photos": [
                    {"sanitized_id": sid, "original_ref": oref}
                    for sid, oref in zip(c.photo_sanitized_ids, c.photo_original_refs)
                ],
            }
            for c in clues
        ],
        "reviews": list(state.checks),
        "confirmations": list(state.confirmations),
        "replacements": list(state.replacements),
        "revisits": list(state.revisits),
        "rejections": list(state.rejections),
        "reopens": list(state.reopens),
        "standard_rebases": list(state.rebases),
        "original_photo_refs": list(state.photos_original),
        "merged_after_conclusion": state.merged_after_conclusion,
    }
