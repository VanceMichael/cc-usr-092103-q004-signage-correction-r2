"""公共外语标识纠错闭环服务。

约定：
- 所有业务动作只追加事件，历史不改写；
- 同一归并指纹（场所 + 牌面文字）只存在一个案件，不同角度照片、
  离线补报、多人重复上报都归并为该案件的证据；
- 每个办理周期（episode）最多产生一条整改结论（更换或驳回），
  结论之后的异议只能先重开再办理，因此不会出现两条互相矛盾的结论；
- 上报人身份、原片、审校意见只入内部证据链，公众视图只给结论级事实。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .dispatch import choose_assignee
from .log import Event, EventLog
from .merge import merge_key, normalize_text
from .privacy import pseudonym, sanitize_photo

# 事件类型
CASE_OPENED = "CASE_OPENED"
REPORT_MERGED = "REPORT_MERGED"
LATE_EVIDENCE = "LATE_EVIDENCE"
ASSIGNED = "ASSIGNED"
TERMINOLOGY_CHECKED = "TERMINOLOGY_CHECKED"
UNIT_CONFIRMED = "UNIT_CONFIRMED"
REPLACED = "REPLACED"
REJECTED = "REJECTED"
REOPENED = "REOPENED"
SITE_REVISITED = "SITE_REVISITED"
STANDARD_PUBLISHED = "STANDARD_PUBLISHED"

STANDARDS_CASE = "__standards__"  # 规范换版的审计载体

# 状态 -> 当前待办环节（用于时限计算）
PENDING_STAGE = {
    "accepted": "dispatch",
    "dispatched": "terminology_review",
    "terminology_checked": "unit_confirm",
    "unit_confirmed": "replacement",
}

TERMINOLOGY_VERDICTS = {"conform", "error", "unrelated"}
CONFIRM_DECISIONS = {"accept", "dispute"}


class ConflictError(Exception):
    """与既有记录冲突（重复结论、重复认领等）。"""


class NotFoundError(Exception):
    """案件或资料不存在。"""


class StateError(Exception):
    """当前状态不允许该操作。"""


@dataclass(frozen=True)
class SubmitResult:
    case_id: str
    report_id: str
    outcome: str  # opened | merged | late_evidence
    duplicate: bool = False


@dataclass
class CaseState:
    """由事件折叠出的案件当前形态，本身不落库、可随时重放。"""

    case_id: str
    venue_id: str = ""
    merge_key: str = ""
    sign_text: str = ""
    priority: str = "normal"
    episode: int = 1
    status: str = "accepted"
    opened_at: str = ""
    assignee: str | None = None
    standard_version: int | None = None
    report_ids: list = field(default_factory=list)
    photo_ids: list = field(default_factory=list)
    reviews: list = field(default_factory=list)
    confirmations: list = field(default_factory=list)
    revisits: list = field(default_factory=list)
    conclusion: dict | None = None
    needs_review: bool = False
    deadlines: dict = field(default_factory=dict)


class CorrectionService:
    def __init__(self, *, standards: dict, venues: dict, personnel: dict,
                 sla: dict, now=None, salt: str = "signage-demo") -> None:
        self.log = EventLog()
        self._venues = {v["venue_id"]: v for v in venues["venues"]}
        self._people = {p["person_id"]: p for p in personnel["people"]}
        self._sla = {s["stage"]: s["days"] for s in sla["stages"]}
        self._overrides = sla.get("priority_overrides", {})
        # 规范版本库：只增不改
        self._standards = sorted((dict(v) for v in standards["versions"]),
                                 key=lambda v: v["version"])
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._salt = salt
        self._seq = 0
        self._case_ids: list[str] = []
        self._merge_index: dict[str, str] = {}   # merge_key -> case_id（永久）
        self._reports: dict[str, SubmitResult] = {}  # report_id -> 受理结果（幂等）
        self._photos: dict[str, dict] = {}       # photo_id -> 脱敏副本
        self._vault: dict[str, dict] = {}        # photo_id -> 保险库记录（内部）

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _iso(self, value) -> str:
        if value is None:
            return self._now().isoformat()
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def _state(self, case_id: str) -> CaseState:
        events = self.log.for_case(case_id)
        if not events:
            raise NotFoundError(f"案件不存在: {case_id}")
        return self._fold(case_id, events)

    def _fold(self, case_id: str, events: list[Event]) -> CaseState:
        st = CaseState(case_id=case_id)
        for e in events:
            p = e.payload
            if e.type == CASE_OPENED:
                st.venue_id = p["venue_id"]
                st.merge_key = p["merge_key"]
                st.sign_text = p["sign_text"]
                st.priority = p["priority"]
                st.deadlines = dict(p["deadlines"])
                st.opened_at = e.occurred_at
                st.report_ids.append(p["report_id"])
                st.photo_ids.extend(p["photo_ids"])
            elif e.type in (REPORT_MERGED, LATE_EVIDENCE):
                st.report_ids.append(p["report_id"])
                st.photo_ids.extend(p["photo_ids"])
                if p.get("post_conclusion"):
                    st.needs_review = True
            elif e.type == ASSIGNED:
                st.assignee = p["person_id"]
                st.status = "dispatched"
            elif e.type == TERMINOLOGY_CHECKED:
                st.standard_version = p["standard_version"]
                st.reviews.append({**p, "actor": e.actor, "at": e.at})
                st.status = "terminology_checked"
            elif e.type == UNIT_CONFIRMED:
                st.confirmations.append({**p, "actor": e.actor, "at": e.at})
                st.status = ("unit_confirmed" if p["decision"] == "accept"
                             else "terminology_checked")
            elif e.type == REPLACED:
                st.conclusion = {"type": "replaced", "at": e.occurred_at,
                                 "by": p["unit_id"]}
                st.status = "replaced"
            elif e.type == REJECTED:
                st.conclusion = {"type": "rejected", "at": e.occurred_at,
                                 "by": e.actor, "reason": p["reason"]}
                st.status = "rejected"
            elif e.type == REOPENED:
                st.episode = p["episode"]
                st.conclusion = None
                st.assignee = None
                st.status = "accepted"
            elif e.type == SITE_REVISITED:
                st.revisits.append({**p, "actor": e.actor, "at": e.at})
                st.photo_ids.extend(p.get("photo_ids", []))
        return st

    def _loads(self) -> dict[str, int]:
        loads: dict[str, int] = {}
        for cid in self._case_ids:
            st = self._state(cid)
            if st.assignee and st.status not in ("replaced", "rejected"):
                loads[st.assignee] = loads.get(st.assignee, 0) + 1
        return loads

    def _deadlines(self, opened: str, priority: str) -> dict:
        base = datetime.fromisoformat(opened)
        days = dict(self._sla)
        days.update(self._overrides.get(priority, {}))
        return {stage: (base + timedelta(days=d)).isoformat()
                for stage, d in days.items()}

    def _append(self, case_id: str, type: str, actor: str, payload: dict,
                occurred_at=None) -> Event:
        return self.log.append(case_id=case_id, type=type, actor=actor,
                               at=self._now().isoformat(),
                               occurred_at=self._iso(occurred_at),
                               payload=payload)

    def _require_open_episode(self, st: CaseState) -> None:
        if st.conclusion is not None:
            raise ConflictError(
                f"案件 {st.case_id} 本周期已有结论（{st.conclusion['type']}），"
                "须先重开才能再办理，不能追加第二条结论")

    # ------------------------------------------------------------------
    # 线索上报：隐私处理 + 实体归并 + 自动派单
    # ------------------------------------------------------------------
    def submit_report(self, *, report_id: str, reporter_id: str,
                      venue_id: str, sign_text: str, photos=(),
                      occurred_at=None, priority: str = "normal",
                      note: str = "") -> SubmitResult:
        # 幂等：同一 report_id 重复提交（含离线重试）不产生重复事件
        if report_id in self._reports:
            prev = self._reports[report_id]
            return SubmitResult(prev.case_id, prev.report_id, prev.outcome,
                                duplicate=True)
        venue = self._venues.get(venue_id)
        if venue is None:
            raise NotFoundError(f"场所不存在: {venue_id}")
        if priority not in ("normal", "urgent"):
            raise ValueError(f"未知优先级: {priority}")

        key = merge_key(venue_id, sign_text)
        actor = pseudonym(reporter_id, self._salt)

        photo_ids = []
        for photo in photos:
            sanitized, vault = sanitize_photo(photo, reporter_id, self._salt)
            pid = sanitized["photo_id"]
            self._photos[pid] = sanitized
            self._vault[pid] = vault
            photo_ids.append(pid)

        occurred = self._iso(occurred_at)
        case_id = self._merge_index.get(key)

        if case_id is None:
            # 新实体：立案
            self._seq += 1
            case_id = f"CASE-{self._seq:04d}"
            self._case_ids.append(case_id)
            self._merge_index[key] = case_id
            self._append(case_id, CASE_OPENED, actor, {
                "venue_id": venue_id,
                "merge_key": key,
                "sign_text": sign_text,
                "sign_text_norm": normalize_text(sign_text),
                "priority": priority,
                "note": note,
                "report_id": report_id,
                "photo_ids": photo_ids,
                "deadlines": self._deadlines(occurred, priority),
            }, occurred_at=occurred)
            outcome = "opened"
            self._auto_dispatch(case_id, venue)
        else:
            # 已有同实体案件：归并为证据，绝不另立新案
            st = self._state(case_id)
            post_conclusion = (
                st.conclusion is not None and occurred > st.conclusion["at"])
            event_type = LATE_EVIDENCE if st.conclusion else REPORT_MERGED
            self._append(case_id, event_type, actor, {
                "report_id": report_id,
                "photo_ids": photo_ids,
                "note": note,
                "post_conclusion": post_conclusion,
            }, occurred_at=occurred)
            outcome = "late_evidence" if st.conclusion else "merged"

        result = SubmitResult(case_id, report_id, outcome)
        self._reports[report_id] = result
        return result

    def _auto_dispatch(self, case_id: str, venue: dict) -> None:
        person = choose_assignee(list(self._people.values()),
                                 domain=venue["domain"], role="expert",
                                 loads=self._loads())
        if person:
            self._append(case_id, ASSIGNED, "system", {
                "person_id": person["person_id"],
                "role": "expert",
                "domain": venue["domain"],
                "via": "auto_dispatch",
            })

    # ------------------------------------------------------------------
    # 认领（待认领池）
    # ------------------------------------------------------------------
    def claim(self, case_id: str, person_id: str) -> None:
        person = self._people.get(person_id)
        if person is None:
            raise NotFoundError(f"人员不存在: {person_id}")
        st = self._state(case_id)
        self._require_open_episode(st)
        if st.assignee == person_id:
            return  # 同人重复认领：幂等
        if st.assignee is not None:
            raise ConflictError(
                f"案件 {case_id} 已由 {st.assignee} 承办，"
                "同一线索只保留一条办理线，其材料应归并为证据")
        venue = self._venues[st.venue_id]
        if venue["domain"] not in person["domains"]:
            raise StateError(
                f"{person_id} 的专业领域不含 {venue['domain']}，不能认领")
        self._append(case_id, ASSIGNED, person_id, {
            "person_id": person_id,
            "role": person["role"],
            "domain": venue["domain"],
            "via": "claim",
        })

    # ------------------------------------------------------------------
    # 办理环节
    # ------------------------------------------------------------------
    def terminology_check(self, case_id: str, *, actor: str, verdict: str,
                          suggested_text: str = "",
                          standard_version: int | None = None,
                          note: str = "") -> None:
        st = self._state(case_id)
        self._require_open_episode(st)
        if st.status != "dispatched":
            raise StateError(f"当前状态 {st.status} 不能提交术语核对")
        if actor != st.assignee:
            raise StateError("仅承办人可提交术语核对")
        if verdict not in TERMINOLOGY_VERDICTS:
            raise ValueError(f"未知核对结论: {verdict}")
        version = (standard_version if standard_version is not None
                   else self.current_standard()["version"])
        if not any(v["version"] == version for v in self._standards):
            raise NotFoundError(f"规范版本不存在: {version}")
        self._append(case_id, TERMINOLOGY_CHECKED, actor, {
            "verdict": verdict,
            "suggested_text": suggested_text,
            "standard_version": version,
            "note": note,
        })

    def unit_confirm(self, case_id: str, *, actor: str, unit_id: str,
                     decision: str, note: str = "") -> None:
        st = self._state(case_id)
        self._require_open_episode(st)
        if st.status != "terminology_checked":
            raise StateError(f"当前状态 {st.status} 不能进行责任单位确认")
        venue = self._venues[st.venue_id]
        if unit_id != venue["responsible_unit"]:
            raise StateError(
                f"权属不符：{unit_id} 不是 {venue['name']} 的责任单位")
        if decision not in CONFIRM_DECISIONS:
            raise ValueError(f"未知确认结论: {decision}")
        self._append(case_id, UNIT_CONFIRMED, actor, {
            "unit_id": unit_id,
            "decision": decision,
            "note": note,
        })

    def record_replacement(self, case_id: str, *, actor: str, unit_id: str,
                           completed_at=None, note: str = "") -> None:
        st = self._state(case_id)
        self._require_open_episode(st)
        if st.status != "unit_confirmed":
            raise StateError(f"当前状态 {st.status} 不能登记更换")
        venue = self._venues[st.venue_id]
        if unit_id != venue["responsible_unit"]:
            raise StateError(
                f"权属不符：{unit_id} 不是 {venue['name']} 的责任单位")
        self._append(case_id, REPLACED, actor, {
            "unit_id": unit_id,
            "note": note,
        }, occurred_at=completed_at)

    def reject(self, case_id: str, *, actor: str, reason: str) -> None:
        st = self._state(case_id)
        self._require_open_episode(st)
        if st.status not in PENDING_STAGE:
            raise StateError(f"当前状态 {st.status} 不能驳回")
        if not reason:
            raise ValueError("驳回须说明理由")
        self._append(case_id, REJECTED, actor, {"reason": reason})

    def reopen(self, case_id: str, *, actor: str, reason: str) -> None:
        st = self._state(case_id)
        if st.conclusion is None:
            raise StateError("案件尚无结论，无需重开")
        if not reason:
            raise ValueError("重开须说明理由")
        self._append(case_id, REOPENED, actor, {
            "reason": reason,
            "episode": st.episode + 1,
        })
        self._auto_dispatch(case_id, self._venues[st.venue_id])

    def site_revisit(self, case_id: str, *, actor: str, note: str,
                     photo: dict | None = None) -> None:
        if actor not in self._people:
            raise NotFoundError(f"人员不存在: {actor}")
        self._state(case_id)  # 确认案件存在
        photo_ids = []
        if photo is not None:
            sanitized, vault = sanitize_photo(photo, actor, self._salt)
            pid = sanitized["photo_id"]
            self._photos[pid] = sanitized
            self._vault[pid] = vault
            photo_ids.append(pid)
        self._append(case_id, SITE_REVISITED, actor, {
            "note": note,
            "photo_ids": photo_ids,
        })

    # ------------------------------------------------------------------
    # 规范换版：只追加，已办结案件保留当时版本
    # ------------------------------------------------------------------
    def publish_standard(self, *, actor: str, version: int,
                         effective_from: str, terms: list[dict]) -> None:
        if version <= self.current_standard()["version"]:
            raise ConflictError(
                f"规范版本 {version} 不大于现行版，换版只能追加更新版本")
        if not terms:
            raise ValueError("规范版本须包含词条")
        record = {"version": version, "effective_from": effective_from,
                  "status": "current", "terms": [dict(t) for t in terms]}
        for v in self._standards:
            v["status"] = "superseded"
        self._standards.append(record)
        self._append(STANDARDS_CASE, STANDARD_PUBLISHED, actor, {
            "version": version,
            "effective_from": effective_from,
            "term_count": len(terms),
        })

    def current_standard(self) -> dict:
        return self._standards[-1]

    # ------------------------------------------------------------------
    # 时限
    # ------------------------------------------------------------------
    def overdue_stages(self, case_id: str, at=None) -> list[str]:
        st = self._state(case_id)
        stage = PENDING_STAGE.get(st.status)
        if stage is None:
            return []
        now = self._iso(at)
        return [stage] if now > st.deadlines[stage] else []

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------
    def public_view(self, case_id: str) -> dict:
        from .views import build_public_view
        st = self._state(case_id)
        return build_public_view(st, self._venues[st.venue_id])

    def internal_dossier(self, case_id: str) -> dict:
        from .views import build_internal_dossier
        st = self._state(case_id)
        return build_internal_dossier(
            st, self._venues[st.venue_id], self.log.for_case(case_id),
            self._photos, chain_valid=self.log.verify())

    def vault_record(self, photo_id: str) -> dict:
        """保险库记录（原片、真实上报人、打码明细），仅限内部调取。"""
        if photo_id not in self._vault:
            raise NotFoundError(f"照片不存在: {photo_id}")
        return self._vault[photo_id]
