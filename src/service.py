"""公共标识纠错服务：线索归并、派单、审校与整改闭环。

设计要点：

- 一切业务动作（术语核对、责任单位确认、更换、驳回、重开、规范换版）
  只向事件流追加记录，案件状态由事件折叠而来，不做原地修改。
- 结论唯一性：状态机规定「已有结论（已办结/已驳回）的案件必须先重开
  才能继续推进」，配合事件流的乐观并发控制，离线补报或多人认领同一
  线索都不会产生两条互相矛盾的整改结论。
- 信息边界：照片先脱敏再入案件，原件进受限证据库；公众视图只投影
  白名单字段，内部证据链保留完整事件与原件引用。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .dispatch import choose_assignee, compute_stage_deadlines
from .errors import (
    ClaimConflictError,
    DomainError,
    InvalidTransitionError,
    NotFoundError,
    PermissionDeniedError,
)
from .events import Event, EventStore
from .merging import EntityResolver, sign_signature
from .privacy import EvidenceVault, PhotoUpload, SanitizedPhoto, sanitize_photo
from .reference import (
    Person,
    StandardRegistry,
    StandardVersion,
    Term,
    Unit,
    Venue,
    load_personnel,
    load_slas,
    load_standards,
    load_venues,
)

TERMINAL_STATUSES = frozenset({"closed", "rejected"})
ASSIGNMENT_ACTIVE_STATUSES = frozenset({"dispatched", "terminology_checked", "unit_confirmed", "replaced"})
CHECK_VERDICTS = frozenset({"needs_fix", "no_error", "unclear"})
REGISTRY_STREAM = "standards"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SubmissionResult:
    clue_id: str
    case_id: str
    merged: bool            # 是否归并到已有案件
    duplicate: bool         # 是否为重复提交（幂等命中，未产生新记录）
    after_conclusion: bool  # 归并时案件是否已有结论（仅作证据附着）


@dataclass
class ClueRecord:
    clue_id: str
    case_id: str
    reporter_ref: str
    source: str
    occurred_at: str
    venue_id: str
    sign_text: str
    merged: bool
    after_conclusion: bool
    photo_sanitized_ids: list[str]
    photo_original_refs: list[str]


@dataclass
class CaseState:
    """由案件事件流折叠出的当前状态。"""

    case_id: str
    venue_id: str
    sign_text: str
    signature: str
    unit_id: str
    status: str = "reported"
    assignee_id: str | None = None
    stream_seq: int = 0
    clue_ids: list[str] = field(default_factory=list)
    deadlines: dict[str, str] = field(default_factory=dict)
    checks: list[dict] = field(default_factory=list)
    confirmations: list[dict] = field(default_factory=list)
    replacements: list[dict] = field(default_factory=list)
    revisits: list[dict] = field(default_factory=list)
    rejections: list[dict] = field(default_factory=list)
    reopens: list[dict] = field(default_factory=list)
    rebases: list[dict] = field(default_factory=list)
    photos_sanitized: list[str] = field(default_factory=list)
    photos_original: list[str] = field(default_factory=list)
    merged_after_conclusion: bool = False

    @property
    def applied_standard(self) -> dict | None:
        return self.checks[-1]["standard"] if self.checks else None


class SignageCorrectionService:
    def __init__(
        self,
        *,
        store: EventStore,
        standards: StandardRegistry,
        venues: dict[str, Venue],
        units: dict[str, Unit],
        personnel: dict[str, Person],
        slas: dict[str, int],
        resolver: EntityResolver | None = None,
        vault: EvidenceVault | None = None,
    ):
        self._store = store
        self._standards = standards
        self._venues = venues
        self._units = units
        self._personnel = personnel
        self._slas = slas
        self._resolver = resolver or EntityResolver()
        self._vault = vault or EvidenceVault()

        self._cases: dict[str, CaseState] = {}
        self._clues: dict[str, ClueRecord] = {}
        self._signature_index: dict[str, str] = {}
        self._photo_index: dict[str, str] = {}            # content_hash -> sanitized_id
        self._sanitized_index: dict[str, SanitizedPhoto] = {}
        self._standards_seq = 0
        self._case_counter = 0
        self._rebuild()

    @classmethod
    def from_fixture_dir(cls, fixture_dir: str | Path, *, store: EventStore | None = None) -> "SignageCorrectionService":
        fixture_dir = Path(fixture_dir)
        venues, units = load_venues(fixture_dir / "venues.json")
        return cls(
            store=store if store is not None else EventStore(),
            standards=load_standards(fixture_dir / "standards.json"),
            venues=venues,
            units=units,
            personnel=load_personnel(fixture_dir / "personnel.json"),
            slas=load_slas(fixture_dir / "slas.json"),
        )

    # ------------------------------------------------------------------
    # 线索提交（含隐私处理与实体归并）
    # ------------------------------------------------------------------

    def submit_clue(
        self,
        *,
        venue_id: str,
        sign_text: str,
        reporter_ref: str,
        photos: Iterable[PhotoUpload] = (),
        clue_id: str | None = None,
        source: str = "online",
        occurred_at: str | None = None,
        now: datetime | None = None,
    ) -> SubmissionResult:
        """提交线索。离线补报由客户端生成 clue_id 与 occurred_at，
        重复提交同一 clue_id 幂等返回首次结果，不会产生第二条记录。"""
        now = now or _utcnow()
        venue = self._venues.get(venue_id)
        if venue is None:
            raise NotFoundError(f"未知场所 {venue_id}")
        if not sign_text or not sign_text.strip():
            raise DomainError("标识文字不能为空")

        clue_id = clue_id or f"clue-{secrets.token_hex(4)}"
        existing = self._clues.get(clue_id)
        if existing is not None:
            return SubmissionResult(
                clue_id=clue_id,
                case_id=existing.case_id,
                merged=existing.merged,
                duplicate=True,
                after_conclusion=existing.after_conclusion,
            )
        occurred_at = occurred_at or now.isoformat()

        # 1) 隐私处理：脱敏副本进入案件，原件进入受限证据库
        sanitized: list[SanitizedPhoto] = []
        for upload in photos:
            known = self._photo_index.get(upload.content_hash)
            if known is not None:
                sanitized.append(self._sanitized_index[known])
            else:
                sanitized.append(sanitize_photo(upload, self._vault))

        # 2) 实体归并：同一场所下文字一致或高度近似的线索归入同一案件
        signature = sign_signature(venue_id, sign_text)
        case_id = self._signature_index.get(signature)
        if case_id is None:
            case_id = self._resolver.find_match(
                venue_id=venue_id,
                sign_text=sign_text,
                candidates=[(c.case_id, c.venue_id, c.sign_text) for c in self._cases.values()],
            )
        merged = case_id is not None

        if not merged:
            case_id = f"case-{self._case_counter + 1:04d}"
            self._append(
                stream=case_id,
                expected=0,
                type_="case_created",
                actor="system",
                now=now,
                payload={
                    "venue_id": venue_id,
                    "sign_text": sign_text,
                    "signature": signature,
                    "unit_id": venue.owner_unit_id,
                },
            )
        state = self._cases[case_id]
        after_conclusion = state.status in TERMINAL_STATUSES

        for sp in sanitized:
            if sp.sanitized_id in state.photos_sanitized:
                continue  # 同内容照片已脱敏入库
            self._case_event(
                state,
                "photo_sanitized",
                actor=reporter_ref,
                now=now,
                payload={
                    "sanitized_id": sp.sanitized_id,
                    "content_hash": sp.content_hash,
                    "original_ref": sp.original_ref,
                    "blurred_regions": [dict(r) for r in sp.blurred_regions],
                    "stripped_metadata_keys": list(sp.stripped_metadata_keys),
                },
            )

        clue_payload = {
            "clue_id": clue_id,
            "reporter_ref": reporter_ref,
            "source": source,
            "occurred_at": occurred_at,
            "venue_id": venue_id,
            "sign_text": sign_text,
            "signature": signature,
            "photo_sanitized_ids": [s.sanitized_id for s in sanitized],
            "photo_original_refs": [s.original_ref for s in sanitized],
            "after_conclusion": after_conclusion,
        }
        self._case_event(
            state,
            "clue_merged" if merged else "clue_submitted",
            actor=reporter_ref,
            now=now,
            payload=clue_payload,
        )
        return SubmissionResult(
            clue_id=clue_id,
            case_id=case_id,
            merged=merged,
            duplicate=False,
            after_conclusion=after_conclusion,
        )

    # ------------------------------------------------------------------
    # 派单与认领
    # ------------------------------------------------------------------

    def dispatch_case(self, case_id: str, *, actor: str, now: datetime | None = None) -> Person:
        """协调员按权属与专业领域派单：责任单位取自场所权属，承办人按
        领域能力优先选专家、其次志愿者。"""
        self._require_coordinator(actor)
        state = self.case_state(case_id)
        self._require_status(state, {"reported"}, "只有待受理的案件可以派单")
        now = now or _utcnow()
        domain = self._venues[state.venue_id].category
        person = choose_assignee(self._personnel.values(), domain, self._active_counts())
        if person is None:
            raise DomainError(f"领域 {domain} 暂无可用专家或志愿者")
        self._case_event(
            state,
            "case_dispatched",
            actor=actor,
            now=now,
            payload={
                "assignee_id": person.person_id,
                "assignee_role": person.role,
                "domain": domain,
                "unit_id": state.unit_id,
                "deadlines": compute_stage_deadlines(self._slas, now),
            },
        )
        return person

    def claim_case(self, case_id: str, *, person_id: str, now: datetime | None = None) -> CaseState:
        """专家或志愿者认领。同一案件只允许一名承办人，后到认领被拒绝。"""
        person = self._personnel.get(person_id)
        if person is None:
            raise NotFoundError(f"未知人员 {person_id}")
        if person.role not in ("expert", "volunteer"):
            raise PermissionDeniedError("协调员不能认领案件")
        state = self.case_state(case_id)
        if state.assignee_id == person_id:
            return state  # 本人重复认领，幂等
        if state.assignee_id is not None:
            raise ClaimConflictError(f"案件 {case_id} 已由 {state.assignee_id} 承办，不能重复认领")
        self._require_status(state, {"reported"}, "只有待受理的案件可以认领")
        domain = self._venues[state.venue_id].category
        if domain not in person.domains:
            raise PermissionDeniedError(f"{person_id} 不具备 {domain} 领域能力")
        if self._active_counts().get(person_id, 0) >= person.max_active_cases:
            raise DomainError(f"{person_id} 已超出在办上限")
        now = now or _utcnow()
        self._case_event(
            state,
            "case_claimed",
            actor=person_id,
            now=now,
            payload={
                "assignee_id": person_id,
                "assignee_role": person.role,
                "domain": domain,
                "unit_id": state.unit_id,
                "deadlines": compute_stage_deadlines(self._slas, now),
            },
        )
        return state

    # ------------------------------------------------------------------
    # 审校与整改（全部只追加）
    # ------------------------------------------------------------------

    def submit_terminology_check(
        self,
        case_id: str,
        *,
        checker_id: str,
        standard_id: str,
        findings: Iterable[dict],
        verdict: str,
        suggested_text: str | None = None,
        standard_version: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """术语核对：记录所依据的规范版本与审校意见。"""
        state = self.case_state(case_id)
        self._require_status(state, {"dispatched"}, "只有已派单的案件可以核对术语")
        if state.assignee_id != checker_id:
            raise PermissionDeniedError("只有当前承办人可以提交术语核对")
        if verdict not in CHECK_VERDICTS:
            raise DomainError(f"未知核对结论 {verdict}")
        sv = (
            self._standards.get(standard_id, standard_version)
            if standard_version
            else self._standards.current(standard_id)
        )
        self._case_event(
            state,
            "terminology_checked",
            actor=checker_id,
            now=now or _utcnow(),
            payload={
                "checker_id": checker_id,
                "standard": {"standard_id": sv.standard_id, "version": sv.version},
                "findings": [dict(f) for f in findings],
                "verdict": verdict,
                "suggested_text": suggested_text,
            },
        )

    def confirm_by_unit(
        self,
        case_id: str,
        *,
        unit_id: str,
        confirmer_ref: str,
        decision: str = "accept",
        comment: str = "",
        now: datetime | None = None,
    ) -> None:
        """责任单位确认。只有场所权属单位可以确认；异议则退回待受理。"""
        state = self.case_state(case_id)
        self._require_status(state, {"terminology_checked"}, "只有已完成术语核对的案件可以确认")
        if unit_id != state.unit_id:
            raise PermissionDeniedError("只有权属责任单位可以确认整改")
        if decision not in ("accept", "dispute"):
            raise DomainError(f"未知确认结论 {decision}")
        self._case_event(
            state,
            "unit_confirmed",
            actor=confirmer_ref,
            now=now or _utcnow(),
            payload={
                "unit_id": unit_id,
                "confirmer_ref": confirmer_ref,
                "decision": decision,
                "comment": comment,
            },
        )

    def record_replacement(
        self,
        case_id: str,
        *,
        unit_id: str,
        new_text: str,
        after_photo: PhotoUpload | None = None,
        replaced_at: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """登记更换：记录新牌面文字、完成时间与更换后照片。"""
        state = self.case_state(case_id)
        self._require_status(state, {"unit_confirmed"}, "只有责任单位确认后才能登记更换")
        if unit_id != state.unit_id:
            raise PermissionDeniedError("只有权属责任单位可以登记更换")
        now = now or _utcnow()
        replaced_at = replaced_at or now.isoformat()

        sp = None
        if after_photo is not None:
            known = self._photo_index.get(after_photo.content_hash)
            sp = self._sanitized_index[known] if known else sanitize_photo(after_photo, self._vault)
            if sp.sanitized_id not in state.photos_sanitized:
                self._case_event(
                    state,
                    "photo_sanitized",
                    actor=unit_id,
                    now=now,
                    payload={
                        "sanitized_id": sp.sanitized_id,
                        "content_hash": sp.content_hash,
                        "original_ref": sp.original_ref,
                        "blurred_regions": [dict(r) for r in sp.blurred_regions],
                        "stripped_metadata_keys": list(sp.stripped_metadata_keys),
                    },
                )
        self._case_event(
            state,
            "replacement_recorded",
            actor=unit_id,
            now=now,
            payload={
                "unit_id": unit_id,
                "new_text": new_text,
                "replaced_at": replaced_at,
                "after_photo_sanitized": sp.sanitized_id if sp else None,
                "after_photo_original": sp.original_ref if sp else None,
            },
        )

    def record_revisit(
        self,
        case_id: str,
        *,
        visitor_ref: str,
        result: str,
        note: str = "",
        now: datetime | None = None,
    ) -> None:
        """现场回访记录（仅内部可见）。"""
        state = self.case_state(case_id)
        self._require_status(state, {"replaced"}, "只有已更换的案件可以回访")
        if result not in ("pass", "fail"):
            raise DomainError(f"未知回访结果 {result}")
        now = now or _utcnow()
        self._case_event(
            state,
            "revisit_recorded",
            actor=visitor_ref,
            now=now,
            payload={
                "visitor_ref": visitor_ref,
                "result": result,
                "note": note,
                "visited_at": now.isoformat(),
            },
        )

    def close_case(self, case_id: str, *, actor: str, now: datetime | None = None) -> None:
        """办结：要求已更换且至少一次回访通过。"""
        self._require_coordinator(actor)
        state = self.case_state(case_id)
        self._require_status(state, {"replaced"}, "只有已更换的案件可以办结")
        if not any(r["result"] == "pass" for r in state.revisits):
            raise DomainError("至少需要一次回访通过才能办结")
        self._case_event(state, "case_closed", actor=actor, now=now or _utcnow(), payload={})

    def reject_case(self, case_id: str, *, actor: str, reason: str, now: datetime | None = None) -> None:
        """驳回。已有结论的案件须先重开，保证结论链单线推进。"""
        state = self.case_state(case_id)
        if state.status in TERMINAL_STATUSES:
            raise InvalidTransitionError("案件已有结论，如需重新处理请先重开")
        if not (actor == state.assignee_id or self._is_coordinator(actor)):
            raise PermissionDeniedError("只有承办人或协调员可以驳回")
        if not reason or not reason.strip():
            raise DomainError("驳回必须填写理由")
        self._case_event(
            state,
            "case_rejected",
            actor=actor,
            now=now or _utcnow(),
            payload={"reason": reason, "from_status": state.status},
        )

    def reopen_case(self, case_id: str, *, actor: str, reason: str, now: datetime | None = None) -> None:
        """重开已驳回或已办结的案件，回到待受理重新走流程。"""
        self._require_coordinator(actor)
        state = self.case_state(case_id)
        if state.status not in TERMINAL_STATUSES:
            raise InvalidTransitionError("只有已办结或已驳回的案件可以重开")
        if not reason or not reason.strip():
            raise DomainError("重开必须填写理由")
        self._case_event(
            state,
            "case_reopened",
            actor=actor,
            now=now or _utcnow(),
            payload={"reason": reason, "from_status": state.status},
        )

    # ------------------------------------------------------------------
    # 规范换版
    # ------------------------------------------------------------------

    def publish_standard(
        self,
        *,
        actor: str,
        standard_id: str,
        version: str,
        effective_from: str,
        terms: Iterable[dict],
        supersedes: str | None = None,
        title: str | None = None,
        now: datetime | None = None,
    ) -> StandardVersion:
        """发布规范新版本。注册表只追加，历史版本不可改写。"""
        self._require_coordinator(actor)
        sv = StandardVersion(
            standard_id=standard_id,
            title=title or standard_id,
            version=version,
            effective_from=effective_from,
            supersedes=supersedes,
            terms=tuple(Term(zh=t["zh"], en=t["en"]) for t in terms),
        )
        self._standards.validate_new(sv)  # 先校验，写入由事件完成
        self._append(
            stream=REGISTRY_STREAM,
            expected=self._standards_seq,
            type_="standard_published",
            actor=actor,
            now=now or _utcnow(),
            payload={
                "standard_id": sv.standard_id,
                "title": sv.title,
                "version": sv.version,
                "effective_from": sv.effective_from,
                "supersedes": sv.supersedes,
                "terms": [{"zh": t.zh, "en": t.en} for t in sv.terms],
            },
        )
        return sv

    def rebase_case_standard(
        self,
        case_id: str,
        *,
        actor: str,
        new_version: str,
        now: datetime | None = None,
    ) -> None:
        """规范换版：把案件切换到新版规范。已核对/已确认的案件退回
        已派单，需按新版重新核对；已有结论的案件须先重开。"""
        self._require_coordinator(actor)
        state = self.case_state(case_id)
        if state.status in TERMINAL_STATUSES:
            raise InvalidTransitionError("案件已有结论，如需换版请先重开")
        if state.status not in {"dispatched", "terminology_checked", "unit_confirmed"}:
            raise InvalidTransitionError(f"当前状态 {state.status} 不支持规范换版")
        current = state.applied_standard
        if current is None:
            raise DomainError("案件尚未核对术语，无需换版")
        sv = self._standards.get(current["standard_id"], new_version)
        if sv.version == current["version"]:
            raise DomainError("案件已在该规范版本上")
        self._case_event(
            state,
            "case_standard_rebased",
            actor=actor,
            now=now or _utcnow(),
            payload={
                "standard_id": sv.standard_id,
                "from_version": current["version"],
                "to_version": sv.version,
            },
        )

    # ------------------------------------------------------------------
    # 读取接口（供视图投影使用）
    # ------------------------------------------------------------------

    def case_state(self, case_id: str) -> CaseState:
        try:
            return self._cases[case_id]
        except KeyError:
            raise NotFoundError(f"未知案件 {case_id}") from None

    def clue(self, clue_id: str) -> ClueRecord:
        try:
            return self._clues[clue_id]
        except KeyError:
            raise NotFoundError(f"未知线索 {clue_id}") from None

    def case_events(self, case_id: str) -> list[Event]:
        self.case_state(case_id)
        return self._store.events(case_id)

    def all_events(self) -> list[Event]:
        return self._store.events()

    def venue(self, venue_id: str) -> Venue:
        return self._venues[venue_id]

    def unit(self, unit_id: str) -> Unit:
        return self._units[unit_id]

    def standards(self) -> StandardRegistry:
        return self._standards

    def vault(self) -> EvidenceVault:
        return self._vault

    def sanitized_photo(self, sanitized_id: str) -> SanitizedPhoto:
        return self._sanitized_index[sanitized_id]

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _rebuild(self) -> None:
        for event in self._store.events():
            self._apply(event)

    def _append(self, *, stream, expected, type_, actor, now, payload) -> Event:
        occurred = now.isoformat() if isinstance(now, datetime) else now
        event = self._store.append(
            stream=stream,
            type=type_,
            actor=actor,
            occurred_at=occurred,
            payload=payload,
            expected_stream_seq=expected,
        )
        self._apply(event)
        return event

    def _case_event(self, state: CaseState, type_, *, actor, now, payload) -> Event:
        return self._append(
            stream=state.case_id,
            expected=state.stream_seq,
            type_=type_,
            actor=actor,
            now=now,
            payload=payload,
        )

    def _apply(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is None:
            raise DomainError(f"未知事件类型 {event.type}")
        handler(event)
        if event.stream == REGISTRY_STREAM:
            self._standards_seq = event.stream_seq
        elif event.stream in self._cases:
            self._cases[event.stream].stream_seq = event.stream_seq

    # -- 事件折叠 --

    def _on_case_created(self, event: Event) -> None:
        p = event.payload
        self._cases[event.stream] = CaseState(
            case_id=event.stream,
            venue_id=p["venue_id"],
            sign_text=p["sign_text"],
            signature=p["signature"],
            unit_id=p["unit_id"],
        )
        self._signature_index[p["signature"]] = event.stream
        self._case_counter += 1

    def _on_photo_sanitized(self, event: Event) -> None:
        p = event.payload
        state = self._cases[event.stream]
        state.photos_sanitized.append(p["sanitized_id"])
        state.photos_original.append(p["original_ref"])
        self._photo_index[p["content_hash"]] = p["sanitized_id"]
        self._sanitized_index[p["sanitized_id"]] = SanitizedPhoto(
            sanitized_id=p["sanitized_id"],
            content_hash=p["content_hash"],
            blurred_regions=tuple(p["blurred_regions"]),
            stripped_metadata_keys=tuple(p["stripped_metadata_keys"]),
            original_ref=p["original_ref"],
        )

    def _register_clue(self, event: Event, merged: bool) -> None:
        p = event.payload
        self._clues[p["clue_id"]] = ClueRecord(
            clue_id=p["clue_id"],
            case_id=event.stream,
            reporter_ref=p["reporter_ref"],
            source=p["source"],
            occurred_at=p["occurred_at"],
            venue_id=p["venue_id"],
            sign_text=p["sign_text"],
            merged=merged,
            after_conclusion=p["after_conclusion"],
            photo_sanitized_ids=list(p["photo_sanitized_ids"]),
            photo_original_refs=list(p["photo_original_refs"]),
        )
        self._cases[event.stream].clue_ids.append(p["clue_id"])

    def _on_clue_submitted(self, event: Event) -> None:
        self._register_clue(event, merged=False)

    def _on_clue_merged(self, event: Event) -> None:
        self._register_clue(event, merged=True)
        state = self._cases[event.stream]
        if state.status in TERMINAL_STATUSES:
            state.merged_after_conclusion = True

    def _assign(self, event: Event) -> None:
        state = self._cases[event.stream]
        state.status = "dispatched"
        state.assignee_id = event.payload["assignee_id"]
        state.deadlines = dict(event.payload["deadlines"])

    _on_case_dispatched = _assign
    _on_case_claimed = _assign

    def _on_terminology_checked(self, event: Event) -> None:
        state = self._cases[event.stream]
        state.status = "terminology_checked"
        state.checks.append(event.payload)

    def _on_unit_confirmed(self, event: Event) -> None:
        state = self._cases[event.stream]
        state.confirmations.append(event.payload)
        if event.payload["decision"] == "accept":
            state.status = "unit_confirmed"
        else:  # 异议：退回待受理，重新分派
            state.status = "reported"
            state.assignee_id = None
            state.deadlines = {}

    def _on_replacement_recorded(self, event: Event) -> None:
        state = self._cases[event.stream]
        state.status = "replaced"
        state.replacements.append(event.payload)

    def _on_revisit_recorded(self, event: Event) -> None:
        self._cases[event.stream].revisits.append(event.payload)

    def _on_case_closed(self, event: Event) -> None:
        self._cases[event.stream].status = "closed"

    def _on_case_rejected(self, event: Event) -> None:
        state = self._cases[event.stream]
        state.status = "rejected"
        state.rejections.append(event.payload)

    def _on_case_reopened(self, event: Event) -> None:
        state = self._cases[event.stream]
        state.status = "reported"
        state.assignee_id = None
        state.deadlines = {}
        state.reopens.append(event.payload)

    def _on_case_standard_rebased(self, event: Event) -> None:
        state = self._cases[event.stream]
        state.rebases.append(event.payload)
        if state.status in {"terminology_checked", "unit_confirmed"}:
            state.status = "dispatched"  # 需按新版规范重新核对、确认

    def _on_standard_published(self, event: Event) -> None:
        p = event.payload
        self._standards.publish(
            StandardVersion(
                standard_id=p["standard_id"],
                title=p["title"],
                version=p["version"],
                effective_from=p["effective_from"],
                supersedes=p["supersedes"],
                terms=tuple(Term(zh=t["zh"], en=t["en"]) for t in p["terms"]),
            )
        )

    # -- 辅助 --

    def _is_coordinator(self, actor: str) -> bool:
        person = self._personnel.get(actor)
        return person is not None and person.role == "coordinator"

    def _require_coordinator(self, actor: str) -> None:
        if not self._is_coordinator(actor):
            raise PermissionDeniedError("需要外事部门协调员权限")

    @staticmethod
    def _require_status(state: CaseState, allowed: set, message: str) -> None:
        if state.status not in allowed:
            raise InvalidTransitionError(f"{message}（当前状态 {state.status}）")

    def _active_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self._cases.values():
            if case.assignee_id and case.status in ASSIGNMENT_ACTIVE_STATUSES:
                counts[case.assignee_id] = counts.get(case.assignee_id, 0) + 1
        return counts
