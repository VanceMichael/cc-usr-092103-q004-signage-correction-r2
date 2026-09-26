"""公共标识纠错服务的闭环与一致性测试。"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.errors import (
    ClaimConflictError,
    ConcurrencyConflictError,
    DomainError,
    DuplicateError,
    InvalidTransitionError,
    PermissionDeniedError,
)
from src.events import EventStore
from src.privacy import PhotoUpload
from src.service import SignageCorrectionService
from src.views import internal_evidence_chain, public_case_view

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
T0 = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def service():
    return SignageCorrectionService.from_fixture_dir(FIXTURES)


def photo(tag):
    return PhotoUpload(
        content_hash=f"hash-{tag}",
        metadata={"gps": "31.23,121.47", "device_id": "phone-9", "owner_name": "路人甲"},
        sensitive_regions=({"kind": "face", "box": [1, 2, 3, 4]},),
        captured_at="2026-03-31T08:00:00+00:00",
    )


def submit(service, text="EXIT", venue="venue-metro-whgc", clue_id="clue-1", photos=("angle-1",), **kw):
    return service.submit_clue(
        venue_id=venue,
        sign_text=text,
        reporter_ref="reporter-001",
        photos=[photo(t) for t in photos],
        clue_id=clue_id,
        now=T0,
        **kw,
    )


def check(service, case_id, **kw):
    kwargs = dict(
        checker_id="exp-01",
        standard_id="std-public-signs",
        findings=[{"term_zh": "出口", "current_en": "EXIT WAY", "suggested_en": "Exit"}],
        verdict="needs_fix",
        suggested_text="Exit",
        now=T0,
    )
    kwargs.update(kw)
    service.submit_terminology_check(case_id, **kwargs)


def drive_to_closed(service, case_id):
    service.dispatch_case(case_id, actor="coord-01", now=T0)
    check(service, case_id)
    service.confirm_by_unit(case_id, unit_id="unit-metro", confirmer_ref="unit-staff-1", now=T0)
    service.record_replacement(
        case_id,
        unit_id="unit-metro",
        new_text="Exit",
        after_photo=photo("after"),
        replaced_at="2026-04-10T10:00:00+00:00",
        now=T0,
    )
    service.record_revisit(case_id, visitor_ref="coord-01", result="pass", note="现场复核无误", now=T0)
    service.close_case(case_id, actor="coord-01", now=T0)


# ----------------------------------------------------------------------
# 端到端闭环与公众视图
# ----------------------------------------------------------------------

def test_public_view_shows_merge_standard_confirmer_and_replacement_time(service):
    r1 = submit(service, clue_id="c1", photos=("angle-1",))
    r2 = submit(service, text="exit ", clue_id="c2", photos=("angle-2",))
    assert r2.merged and r2.case_id == r1.case_id

    drive_to_closed(service, r1.case_id)
    view = public_case_view(service, r1.case_id)

    assert view["status"] == "closed"
    assert view["merged"] is True and view["clue_count"] == 2       # 线索已归并
    assert view["standard_version"] == "2026.1"                     # 采用的译写规范版本
    assert view["confirmed_by_unit"] == "市轨道交通集团"             # 由谁确认
    assert view["replaced_at"] == "2026-04-10T10:00:00+00:00"       # 何时完成更换
    assert view["current_text"] == "Exit"


def test_internal_chain_reaches_originals_reviews_and_revisits(service):
    r = submit(service, clue_id="c1", photos=("angle-1",))
    drive_to_closed(service, r.case_id)
    chain = internal_evidence_chain(service, r.case_id)

    # 从新牌面回到最初照片：更换记录与首张照片原件引用都在链上
    assert chain["replacements"][0]["after_photo_original"].startswith("orig-")
    assert any(ref.startswith("orig-") for ref in chain["original_photo_refs"])
    # 每次审校意见与现场回访
    assert chain["reviews"][0]["findings"][0]["current_en"] == "EXIT WAY"
    assert chain["revisits"][0]["note"] == "现场复核无误"
    # 完整事件序列
    types = [e["type"] for e in chain["events"]]
    assert types == [
        "case_created", "photo_sanitized", "clue_submitted",
        "case_dispatched", "terminology_checked", "unit_confirmed",
        "photo_sanitized", "replacement_recorded",
        "revisit_recorded", "case_closed",
    ]


# ----------------------------------------------------------------------
# 隐私处理与信息边界
# ----------------------------------------------------------------------

def test_uploaded_photos_are_sanitized_before_storage(service):
    r = submit(service, clue_id="c1", photos=("raw",))
    state = service.case_state(r.case_id)
    sp = service.sanitized_photo(state.photos_sanitized[0])

    assert sp.stripped_metadata_keys == ("device_id", "gps", "owner_name")
    assert sp.blurred_regions[0]["action"] == "blurred"
    # 原件只在受限证据库中
    original = service.vault().get(sp.original_ref)
    assert original.metadata["gps"] == "31.23,121.47"


def test_public_view_hides_internal_evidence(service):
    r = submit(service, clue_id="c1")
    drive_to_closed(service, r.case_id)

    blob = json.dumps(public_case_view(service, r.case_id), ensure_ascii=False)
    for leaked in ["reporter-001", "orig-", "gps", "owner_name",
                   "exp-01", "EXIT WAY", "现场复核无误", "unit-staff-1"]:
        assert leaked not in blob

    internal = json.dumps(internal_evidence_chain(service, r.case_id), ensure_ascii=False)
    for kept in ["reporter-001", "orig-", "exp-01", "EXIT WAY", "现场复核无误"]:
        assert kept in internal


# ----------------------------------------------------------------------
# 实体归并
# ----------------------------------------------------------------------

def test_clues_of_same_sign_from_different_angles_merge(service):
    r1 = submit(service, text="EXIT", clue_id="c1", photos=("front",))
    r2 = submit(service, text="exit", clue_id="c2", photos=("side",))
    r3 = submit(service, text="Exit", clue_id="c3", venue="venue-hospital-01")

    assert r2.merged and r2.case_id == r1.case_id          # 同牌不同角度归并
    assert not r3.merged and r3.case_id != r1.case_id      # 不同场所不归并
    assert len(service.case_state(r1.case_id).clue_ids) == 2


def test_near_identical_text_merges(service):
    r1 = submit(service, text="Visitor Center", clue_id="c1")
    r2 = submit(service, text="Visitors  Center", clue_id="c2")
    assert r2.merged and r2.case_id == r1.case_id


# ----------------------------------------------------------------------
# 离线补报与幂等
# ----------------------------------------------------------------------

def test_offline_resubmission_is_idempotent(service):
    kw = dict(clue_id="offline-1", source="offline", occurred_at="2026-03-20T08:00:00+00:00")
    r1 = submit(service, **kw)
    before = len(service.all_events())
    r2 = submit(service, text="被改过的文字", **kw)  # 重试即使内容变化也不生效

    assert r2.duplicate and r2.case_id == r1.case_id
    assert len(service.all_events()) == before


def test_offline_report_after_conclusion_adds_evidence_not_new_conclusion(service):
    r1 = submit(service, clue_id="c1")
    drive_to_closed(service, r1.case_id)

    r2 = submit(service, clue_id="offline-late", source="offline",
                occurred_at="2026-03-01T00:00:00+00:00", photos=("late",))
    state = service.case_state(r1.case_id)

    assert r2.merged and r2.after_conclusion and r2.case_id == r1.case_id
    assert state.status == "closed"          # 不自动推翻已有结论
    assert len(state.replacements) == 1      # 没有第二条整改结论
    assert state.merged_after_conclusion


# ----------------------------------------------------------------------
# 多人认领与结论唯一性
# ----------------------------------------------------------------------

def test_only_one_person_can_claim_a_case(service):
    r = submit(service, clue_id="c1")
    service.claim_case(r.case_id, person_id="exp-01", now=T0)

    with pytest.raises(ClaimConflictError):
        service.claim_case(r.case_id, person_id="vol-01", now=T0)

    claimed = [e for e in service.case_events(r.case_id) if e.type == "case_claimed"]
    assert len(claimed) == 1
    service.claim_case(r.case_id, person_id="exp-01", now=T0)  # 本人重复认领幂等
    assert len([e for e in service.case_events(r.case_id) if e.type == "case_claimed"]) == 1


def test_contradictory_conclusions_require_reopen(service):
    r = submit(service, clue_id="c1")
    service.dispatch_case(r.case_id, actor="coord-01", now=T0)
    check(service, r.case_id, verdict="no_error", suggested_text=None)
    service.reject_case(r.case_id, actor="coord-01", reason="译写无误", now=T0)

    # 驳回后不能直接确认或更换，也不能重复驳回
    with pytest.raises(InvalidTransitionError):
        service.confirm_by_unit(r.case_id, unit_id="unit-metro", confirmer_ref="u1", now=T0)
    with pytest.raises(InvalidTransitionError):
        service.record_replacement(r.case_id, unit_id="unit-metro", new_text="Exit", now=T0)
    with pytest.raises(InvalidTransitionError):
        service.reject_case(r.case_id, actor="coord-01", reason="重复驳回", now=T0)

    # 重开后重新走流程，结论链单线推进
    service.reopen_case(r.case_id, actor="coord-01", reason="补充照片显示确有错误", now=T0)
    service.dispatch_case(r.case_id, actor="coord-01", now=T0)
    check(service, r.case_id)
    service.confirm_by_unit(r.case_id, unit_id="unit-metro", confirmer_ref="unit-staff-1", now=T0)
    service.record_replacement(r.case_id, unit_id="unit-metro", new_text="Exit", now=T0)
    service.record_revisit(r.case_id, visitor_ref="coord-01", result="pass", now=T0)
    service.close_case(r.case_id, actor="coord-01", now=T0)

    types = [e.type for e in service.case_events(r.case_id)]
    assert types.index("case_rejected") < types.index("case_reopened") < types.index("case_closed")
    # 办结后同样不能直接改写结论
    with pytest.raises(InvalidTransitionError):
        service.record_replacement(r.case_id, unit_id="unit-metro", new_text="Way Out", now=T0)


def test_store_rejects_stale_stream_writes(service):
    store = EventStore()
    store.append(stream="s", type="case_created", actor="a", occurred_at="t",
                 payload={}, expected_stream_seq=0)
    with pytest.raises(ConcurrencyConflictError):
        store.append(stream="s", type="case_closed", actor="a", occurred_at="t",
                     payload={}, expected_stream_seq=0)


# ----------------------------------------------------------------------
# 派单规则与处理时限
# ----------------------------------------------------------------------

def test_dispatch_prefers_expert_in_domain_and_respects_capacity(service):
    cases = [submit(service, text=f"Sign {i}", clue_id=f"c{i}").case_id for i in range(4)]
    assignees = [service.dispatch_case(cid, actor="coord-01", now=T0).person_id for cid in cases]
    assert assignees == ["exp-01", "exp-01", "exp-01", "vol-01"]  # 专家优先，满负荷后志愿者

    event = [e for e in service.case_events(cases[0]) if e.type == "case_dispatched"][0]
    assert event.payload["unit_id"] == "unit-metro"               # 按权属确定责任单位
    assert event.payload["domain"] == "transport"


def test_dispatch_uses_domain_of_venue(service):
    r = submit(service, text="Emergency", venue="venue-hospital-01", clue_id="c1")
    assert service.dispatch_case(r.case_id, actor="coord-01", now=T0).person_id == "exp-02"


def test_sla_deadlines_set_on_dispatch(service):
    r = submit(service, clue_id="c1")
    service.dispatch_case(r.case_id, actor="coord-01", now=T0)
    deadlines = service.case_state(r.case_id).deadlines
    assert deadlines["terminology_check"] == (T0 + timedelta(hours=72)).isoformat()
    assert deadlines["replacement"] == (T0 + timedelta(hours=240)).isoformat()


def test_unit_cannot_confirm_or_replace_for_other_units_case(service):
    r = submit(service, clue_id="c1")  # 地铁场所，权属 unit-metro
    service.dispatch_case(r.case_id, actor="coord-01", now=T0)
    check(service, r.case_id)
    with pytest.raises(PermissionDeniedError):
        service.confirm_by_unit(r.case_id, unit_id="unit-park", confirmer_ref="u1", now=T0)
    service.confirm_by_unit(r.case_id, unit_id="unit-metro", confirmer_ref="u1", now=T0)
    with pytest.raises(PermissionDeniedError):
        service.record_replacement(r.case_id, unit_id="unit-hospital", new_text="Exit", now=T0)


def test_non_assignee_cannot_submit_check(service):
    r = submit(service, clue_id="c1")
    service.dispatch_case(r.case_id, actor="coord-01", now=T0)
    with pytest.raises(PermissionDeniedError):
        check(service, r.case_id, checker_id="vol-01")


# ----------------------------------------------------------------------
# 规范换版
# ----------------------------------------------------------------------

def test_standard_rebase_is_append_only_and_requires_recheck(service):
    r = submit(service, clue_id="c1")
    service.dispatch_case(r.case_id, actor="coord-01", now=T0)
    check(service, r.case_id, standard_version="2024.1")  # 按旧版核对
    assert public_case_view(service, r.case_id)["standard_version"] == "2024.1"

    before = [e.to_dict() for e in service.all_events()]
    service.rebase_case_standard(r.case_id, actor="coord-01", new_version="2026.1", now=T0)
    assert service.case_state(r.case_id).status == "dispatched"  # 需按新版重新核对

    check(service, r.case_id)  # 默认采用当前版 2026.1
    assert public_case_view(service, r.case_id)["standard_version"] == "2026.1"

    after = [e.to_dict() for e in service.all_events()]
    assert after[: len(before)] == before  # 历史记录未被改写

    chain = internal_evidence_chain(service, r.case_id)
    assert len(chain["reviews"]) == 2
    assert chain["standard_rebases"][0]["from_version"] == "2024.1"


def test_rebase_closed_case_requires_reopen(service):
    r = submit(service, clue_id="c1")
    drive_to_closed(service, r.case_id)
    with pytest.raises(InvalidTransitionError):
        service.rebase_case_standard(r.case_id, actor="coord-01", new_version="2024.1", now=T0)


def test_publish_standard_appends_registry_version(service):
    service.publish_standard(
        actor="coord-01", standard_id="std-public-signs", version="2026.2",
        effective_from="2026-09-01", supersedes="2026.1",
        terms=[{"zh": "出口", "en": "Exit"}],
    )
    registry = service.standards()
    assert registry.current("std-public-signs").version == "2026.2"
    assert [v.version for v in registry.versions("std-public-signs")] == ["2024.1", "2026.1", "2026.2"]

    with pytest.raises(DuplicateError):
        service.publish_standard(actor="coord-01", standard_id="std-public-signs",
                                 version="2026.2", effective_from="2026-09-02", terms=[])
    with pytest.raises(DomainError):
        service.publish_standard(actor="coord-01", standard_id="std-public-signs",
                                 version="2027.1", effective_from="2027-01-01",
                                 supersedes="1999.0", terms=[])
    with pytest.raises(PermissionDeniedError):
        service.publish_standard(actor="exp-01", standard_id="std-public-signs",
                                 version="2027.1", effective_from="2027-01-01", terms=[])


# ----------------------------------------------------------------------
# 持久化与重建
# ----------------------------------------------------------------------

def test_service_rebuilds_from_jsonl_store(tmp_path):
    path = tmp_path / "events.jsonl"
    s1 = SignageCorrectionService.from_fixture_dir(FIXTURES, store=EventStore(path))
    r = submit(s1, clue_id="c1")
    drive_to_closed(s1, r.case_id)

    s2 = SignageCorrectionService.from_fixture_dir(FIXTURES, store=EventStore(path))
    assert public_case_view(s2, r.case_id) == public_case_view(s1, r.case_id)
    assert len(internal_evidence_chain(s2, r.case_id)["events"]) == 10
