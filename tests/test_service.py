import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.privacy import sanitize_photo
from src.refdata import (load_personnel, load_sla, load_standards,
                         load_venues)
from src.service import (CASE_OPENED, LATE_EVIDENCE, REJECTED, REPLACED,
                         REPORT_MERGED, ConflictError, CorrectionService,
                         NotFoundError, StateError)

FIX = Path("fixtures")
T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone(timedelta(hours=8)))

EXPERT_JT = "P-EXP-01"   # 交通/文化 专家
EXPERT_WL = "P-EXP-02"   # 文旅/医疗 专家
VOL_JT = "P-VOL-01"      # 交通/文旅 志愿者
UNIT_STATION = "中央车站运营部"
UNIT_PARK = "滨江公园管理处"


def make_service(clock):
    return CorrectionService(
        standards=load_standards(FIX / "standards.json"),
        venues=load_venues(FIX / "venues.json"),
        personnel=load_personnel(FIX / "personnel.json"),
        sla=load_sla(FIX / "sla.json"),
        now=lambda: clock[0],
    )


def photo(pid, gps=(31.23041, 121.47370)):
    return {
        "photo_id": pid,
        "taken_at": T0.isoformat(),
        "exif": {"gps": list(gps), "device": "Phone X"},
        "detections": [
            {"kind": "sign_text", "box": [10, 10, 200, 40],
             "text": "Carefully Slide"},
            {"kind": "face", "box": [50, 60, 30, 30]},
            {"kind": "plate", "box": [0, 100, 80, 20], "text": "沪A12345"},
        ],
    }


def report(svc, rid, sign="Carefully Slide", venue="V-1001", **kw):
    kw.setdefault("photos", [photo(f"PH-{rid}")])
    return svc.submit_report(report_id=rid, reporter_id=f"citizen-{rid}",
                             venue_id=venue, sign_text=sign, **kw)


def to_conclusion(svc, rid="R1", sign="Carefully Slide", venue="V-1001",
                  unit=UNIT_STATION, expert=EXPERT_JT):
    """立案 -> 核校 -> 确认 -> 更换，返回 case_id。"""
    case_id = report(svc, rid, sign=sign, venue=venue).case_id
    svc.terminology_check(case_id, actor=expert, verdict="error",
                          suggested_text="CAUTION: Wet Floor")
    svc.unit_confirm(case_id, actor="单位经办-01", unit_id=unit,
                     decision="accept")
    svc.record_replacement(case_id, actor="单位经办-01", unit_id=unit,
                           completed_at=T0 + timedelta(days=10))
    return case_id


class FixtureTest(unittest.TestCase):
    def test_refdata_loads(self):
        standards = load_standards(FIX / "standards.json")
        self.assertEqual([v["version"] for v in standards["versions"]], [1, 2])
        self.assertEqual(len(load_venues(FIX / "venues.json")["venues"]), 3)
        self.assertEqual(len(load_personnel(FIX / "personnel.json")["people"]), 4)
        self.assertEqual(len(load_sla(FIX / "sla.json")["stages"]), 4)


class PrivacyTest(unittest.TestCase):
    def test_sanitize_strips_sensitive_data(self):
        sanitized, vault = sanitize_photo(photo("PH-X"), "citizen-9", "salt")
        self.assertNotIn("device", sanitized["exif"])
        self.assertEqual(sanitized["exif"]["gps"], [31.23, 121.474])
        kinds = {d["kind"]: d for d in sanitized["detections"]}
        self.assertTrue(kinds["face"]["redacted"])
        self.assertTrue(kinds["plate"]["redacted"])
        self.assertNotIn("text", kinds["plate"])
        self.assertFalse(kinds["sign_text"].get("redacted", False))
        # 保险库保留原片与真实上报人，供内部回溯
        self.assertEqual(vault["reporter_id"], "citizen-9")
        self.assertEqual(vault["original"]["exif"]["device"], "Phone X")
        self.assertEqual(len(vault["redactions"]), 2)


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.clock = [T0]
        self.svc = make_service(self.clock)

    def test_same_sign_merges_into_one_case(self):
        r1 = report(self.svc, "R1")
        r2 = report(self.svc, "R2", sign="carefully slide")   # 大小写差异
        r3 = report(self.svc, "R3", sign="Carefully  Slide!")  # 标点空白差异
        self.assertEqual(r1.outcome, "opened")
        self.assertEqual(r2.outcome, "merged")
        self.assertEqual(r3.outcome, "merged")
        self.assertEqual(r1.case_id, r2.case_id)
        self.assertEqual(r1.case_id, r3.case_id)
        view = self.svc.public_view(r1.case_id)
        self.assertTrue(view["merged"])
        self.assertEqual(view["merged_reports"], 3)

    def test_different_sign_opens_new_case(self):
        r1 = report(self.svc, "R1")
        r2 = report(self.svc, "R2", sign="Exit Onlyy")
        self.assertNotEqual(r1.case_id, r2.case_id)

    def test_duplicate_report_id_is_idempotent(self):
        r1 = report(self.svc, "R1")
        n_events = len(self.svc.log)
        again = report(self.svc, "R1")
        self.assertTrue(again.duplicate)
        self.assertEqual(again.case_id, r1.case_id)
        self.assertEqual(len(self.svc.log), n_events)


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.clock = [T0]
        self.svc = make_service(self.clock)

    def test_auto_dispatch_by_domain(self):
        c1 = report(self.svc, "R1", venue="V-1001").case_id  # 交通
        c2 = report(self.svc, "R2", sign="No Somking", venue="V-2001").case_id  # 文旅
        self.assertEqual(self.svc._state(c1).assignee, EXPERT_JT)
        self.assertEqual(self.svc._state(c2).assignee, EXPERT_WL)

    def test_capacity_exhausted_goes_to_pool_then_claim(self):
        # 交通专家容量 3，第 4 件进入待认领池
        ids = [report(self.svc, f"R{i}", sign=f"Wrong Sign {i}").case_id
               for i in range(4)]
        pooled = ids[3]
        self.assertIsNone(self.svc._state(pooled).assignee)
        self.svc.claim(pooled, VOL_JT)
        self.assertEqual(self.svc._state(pooled).assignee, VOL_JT)
        # 同人重复认领幂等
        n = len(self.svc.log)
        self.svc.claim(pooled, VOL_JT)
        self.assertEqual(len(self.svc.log), n)

    def test_claim_domain_mismatch_rejected(self):
        ids = [report(self.svc, f"R{i}", sign=f"Wrong Sign {i}").case_id
               for i in range(4)]
        with self.assertRaises(StateError):
            self.svc.claim(ids[3], "P-VOL-02")  # 文化领域志愿者不能认领交通线索


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.clock = [T0]
        self.svc = make_service(self.clock)

    def test_full_flow_public_view(self):
        case_id = to_conclusion(self.svc)
        view = self.svc.public_view(case_id)
        self.assertEqual(view["status"], "replaced")
        self.assertEqual(view["standard_version"], 2)          # 采用的规范版本
        self.assertEqual(view["confirmed_by"]["unit"], UNIT_STATION)  # 由谁确认
        self.assertEqual(view["responsible_unit"], UNIT_STATION)
        self.assertEqual(view["replaced_at"],
                         (T0 + timedelta(days=10)).isoformat())  # 何时完成更换
        # 公众视图边界：不含个人标识、匿名、保险库引用
        blob = json.dumps(view, ensure_ascii=False)
        for leaked in (EXPERT_JT, "anon-", "vault://", "citizen-R1", "单位经办-01"):
            self.assertNotIn(leaked, blob)

    def test_internal_dossier_keeps_evidence_chain(self):
        case_id = to_conclusion(self.svc)
        self.svc.site_revisit(case_id, actor=VOL_JT, note="现场复核已换新牌",
                              photo=photo("PH-visit"))
        dossier = self.svc.internal_dossier(case_id)
        self.assertTrue(dossier["chain_valid"])
        self.assertEqual(dossier["assignee"], EXPERT_JT)
        self.assertEqual(len(dossier["reviews"]), 1)
        self.assertEqual(dossier["reviews"][0]["verdict"], "error")
        self.assertEqual(len(dossier["revisits"]), 1)
        # 可从新牌面回到最初照片：保险库引用与原片都在
        refs = {p["vault_ref"] for p in dossier["photos"]}
        self.assertIn("vault://PH-R1", refs)
        vault = self.svc.vault_record("PH-R1")
        self.assertEqual(vault["reporter_id"], "citizen-R1")
        types = [e["type"] for e in dossier["events"]]
        self.assertEqual(types[0], CASE_OPENED)
        self.assertIn(REPLACED, types)

    def test_wrong_unit_cannot_confirm_or_replace(self):
        case_id = report(self.svc, "R1").case_id
        self.svc.terminology_check(case_id, actor=EXPERT_JT, verdict="error")
        with self.assertRaises(StateError):
            self.svc.unit_confirm(case_id, actor="x", unit_id=UNIT_PARK,
                                  decision="accept")
        self.svc.unit_confirm(case_id, actor="x", unit_id=UNIT_STATION,
                              decision="accept")
        with self.assertRaises(StateError):
            self.svc.record_replacement(case_id, actor="x", unit_id=UNIT_PARK)

    def test_only_assignee_may_check(self):
        case_id = report(self.svc, "R1").case_id
        with self.assertRaises(StateError):
            self.svc.terminology_check(case_id, actor=EXPERT_WL,
                                       verdict="error")


class ConflictTest(unittest.TestCase):
    """离线补报与多人认领都不能制造第二条矛盾结论。"""

    def setUp(self):
        self.clock = [T0]
        self.svc = make_service(self.clock)

    def test_no_second_conclusion_after_terminal(self):
        case_id = to_conclusion(self.svc)
        with self.assertRaises(ConflictError):
            self.svc.reject(case_id, actor="platform", reason="重复线索")
        with self.assertRaises(ConflictError):
            self.svc.record_replacement(case_id, actor="x",
                                        unit_id=UNIT_STATION)

    def test_second_claim_conflict(self):
        ids = [report(self.svc, f"R{i}", sign=f"Wrong Sign {i}").case_id
               for i in range(4)]
        pooled = ids[3]
        self.svc.claim(pooled, VOL_JT)
        with self.assertRaises(ConflictError):
            self.svc.claim(pooled, EXPERT_WL)

    def test_offline_backfill_merges_without_new_conclusion(self):
        case_id = to_conclusion(self.svc)
        n_cases = len(self.svc._case_ids)
        # 离线补报：拍摄于更换之前，延迟入库
        late = report(self.svc, "R-late",
                      occurred_at=T0 + timedelta(days=5))
        self.assertEqual(late.outcome, "late_evidence")
        self.assertEqual(late.case_id, case_id)
        self.assertEqual(len(self.svc._case_ids), n_cases)  # 没有另立新案
        st = self.svc._state(case_id)
        self.assertEqual(st.conclusion["type"], "replaced")  # 结论未被撼动
        self.assertFalse(st.needs_review)
        # 拍摄于更换之后：仍归并，但标记复核，而不是直接推翻结论
        after = report(self.svc, "R-after",
                       occurred_at=T0 + timedelta(days=20))
        self.assertEqual(after.case_id, case_id)
        self.assertTrue(self.svc._state(case_id).needs_review)
        types = [e.type for e in self.svc.log.for_case(case_id)]
        self.assertEqual(types.count(REPLACED), 1)
        self.assertIn(LATE_EVIDENCE, types)

    def test_reopen_starts_new_episode(self):
        case_id = report(self.svc, "R1").case_id
        self.svc.terminology_check(case_id, actor=EXPERT_JT, verdict="unrelated")
        self.svc.reject(case_id, actor="platform", reason="非外语标识问题")
        self.svc.reopen(case_id, actor="外事办-督导", reason="复核认定为标识错误")
        st = self.svc._state(case_id)
        self.assertEqual(st.episode, 2)
        self.assertIsNone(st.conclusion)
        self.assertEqual(st.assignee, EXPERT_JT)  # 重开后重新派单
        self.svc.terminology_check(case_id, actor=EXPERT_JT, verdict="error",
                                   suggested_text="CAUTION: Wet Floor")
        self.svc.unit_confirm(case_id, actor="单位经办-01",
                              unit_id=UNIT_STATION, decision="accept")
        self.svc.record_replacement(case_id, actor="单位经办-01",
                                    unit_id=UNIT_STATION)
        view = self.svc.public_view(case_id)
        self.assertEqual(view["status"], "replaced")
        self.assertEqual(view["reopened_count"], 1)
        # 历史只追加：驳回与更换两条结论都留在证据链里
        types = [e.type for e in self.svc.log.for_case(case_id)]
        self.assertIn(REJECTED, types)
        self.assertIn(REPLACED, types)

    def test_reopen_without_conclusion_rejected(self):
        case_id = report(self.svc, "R1").case_id
        with self.assertRaises(StateError):
            self.svc.reopen(case_id, actor="x", reason="尚无结论")


class StandardVersionTest(unittest.TestCase):
    def setUp(self):
        self.clock = [T0]
        self.svc = make_service(self.clock)

    def test_reversion_keeps_concluded_version(self):
        case_id = to_conclusion(self.svc, venue="V-2001", sign="No Somking",
                                unit=UNIT_PARK, expert=EXPERT_WL)
        self.svc.publish_standard(
            actor="外事办", version=3, effective_from="2026-10-01",
            terms=[{"source": "出口", "approved": "EXIT"},
                   {"source": "售票处", "approved": "Ticket Office"}])
        # 已办结案件仍显示当时采用的第 2 版
        self.assertEqual(self.svc.public_view(case_id)["standard_version"], 2)
        # 新案件默认采用第 3 版
        c2 = report(self.svc, "R-new", sign="Tiket Offce", venue="V-3001").case_id
        self.svc.terminology_check(c2, actor=EXPERT_JT, verdict="error")
        self.assertEqual(self.svc.public_view(c2)["standard_version"], 3)
        with self.assertRaises(ConflictError):
            self.svc.publish_standard(actor="外事办", version=3,
                                      effective_from="2026-11-01",
                                      terms=[{"source": "入口",
                                              "approved": "ENTRANCE"}])


class SlaTest(unittest.TestCase):
    def test_overdue_stages_follow_clock(self):
        clock = [T0]
        svc = make_service(clock)
        case_id = report(svc, "R1").case_id
        self.assertEqual(svc.overdue_stages(case_id), [])
        clock[0] = T0 + timedelta(days=4)  # 术语核对时限 3 天
        self.assertEqual(svc.overdue_stages(case_id), ["terminology_review"])
        svc.terminology_check(case_id, actor=EXPERT_JT, verdict="error")
        self.assertEqual(svc.overdue_stages(case_id), [])
        clock[0] = T0 + timedelta(days=9)  # 单位确认时限 5 天
        self.assertEqual(svc.overdue_stages(case_id), ["unit_confirm"])

    def test_urgent_report_uses_override(self):
        clock = [T0]
        svc = make_service(clock)
        case_id = report(svc, "R1", priority="urgent").case_id
        st = svc._state(case_id)
        due = datetime.fromisoformat(st.deadlines["replacement"])
        self.assertEqual((due - T0).days, 3)  # 紧急件更换时限 3 天


class LogTest(unittest.TestCase):
    def test_hash_chain_verifies(self):
        clock = [T0]
        svc = make_service(clock)
        to_conclusion(svc)
        self.assertTrue(svc.log.verify())
        # 事件不可变
        event = next(iter(svc.log))
        with self.assertRaises(Exception):
            event.payload = {}

    def test_unknown_case_raises(self):
        svc = make_service([T0])
        with self.assertRaises(NotFoundError):
            svc.public_view("CASE-9999")


if __name__ == "__main__":
    unittest.main()
