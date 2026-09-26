"""公众视图与内部证据链：同一案件，两种信息边界。

公众视图只给结论级事实：是否归并、采用哪版规范、由哪个单位确认、
何时完成更换；不含任何个人标识、原片与审校意见。
内部证据链保留全部事件、原片保险库引用、每次审校与回访记录。
"""

STATUS_LABELS = {
    "accepted": "已受理",
    "dispatched": "已派单",
    "terminology_checked": "术语已核校",
    "unit_confirmed": "责任单位已确认",
    "replaced": "已完成更换",
    "rejected": "已驳回",
}


def build_public_view(state, venue: dict) -> dict:
    confirmed_by = None
    accepted = [c for c in state.confirmations if c["decision"] == "accept"]
    if accepted:
        confirmed_by = {
            "unit": accepted[-1]["unit_id"],
            "role": "责任单位确认人",
        }
    return {
        "case_id": state.case_id,
        "status": state.status,
        "status_label": STATUS_LABELS[state.status],
        "venue": {
            "venue_id": venue["venue_id"],
            "name": venue["name"],
            "district": venue["district"],
        },
        "merged": len(state.report_ids) > 1,
        "merged_reports": len(state.report_ids),
        "standard_version": state.standard_version,
        "confirmed_by": confirmed_by,
        "responsible_unit": venue["responsible_unit"],
        "replaced_at": (state.conclusion["at"]
                        if state.conclusion
                        and state.conclusion["type"] == "replaced" else None),
        "reopened_count": state.episode - 1,
    }


def build_internal_dossier(state, venue: dict, events, photos: dict,
                           chain_valid: bool) -> dict:
    return {
        "case_id": state.case_id,
        "episode": state.episode,
        "status": state.status,
        "venue": dict(venue),
        "merge_key": state.merge_key,
        "sign_text": state.sign_text,
        "priority": state.priority,
        "assignee": state.assignee,
        "needs_review": state.needs_review,
        "report_ids": list(state.report_ids),
        "photos": [
            {
                "photo_id": pid,
                "vault_ref": f"vault://{pid}",
                "redactions": photos[pid].get("detections") and [
                    d for d in photos[pid]["detections"] if d.get("redacted")
                ] or [],
            }
            for pid in state.photo_ids if pid in photos
        ],
        "reviews": list(state.reviews),
        "confirmations": list(state.confirmations),
        "revisits": list(state.revisits),
        "conclusion": state.conclusion,
        "deadlines": dict(state.deadlines),
        "events": [e.as_dict() for e in events],
        "chain_valid": chain_valid,
    }
