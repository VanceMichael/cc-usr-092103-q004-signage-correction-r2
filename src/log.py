"""只追加的事件日志。

所有业务动作（受理、归并、派单、核校、确认、更换、驳回、重开、
规范换版、回访）都以事件形式追加，不提供修改或删除接口；
每条事件携带前序哈希形成链，供内部证据链校验完整性。
"""

import hashlib
import json
from dataclasses import asdict, dataclass


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


@dataclass(frozen=True)
class Event:
    seq: int
    case_id: str
    type: str
    actor: str
    at: str           # 入库时间（系统受理时刻）
    occurred_at: str  # 业务发生时间（离线补报时可能早于入库时间）
    payload: dict
    prev_hash: str
    hash: str

    def as_dict(self) -> dict:
        return asdict(self)


class EventLog:
    """只追加：没有 update/delete，事件为不可变数据。"""

    GENESIS = "0" * 64

    def __init__(self) -> None:
        self._events: list[Event] = []

    def append(self, *, case_id: str, type: str, actor: str, at: str,
               occurred_at: str, payload: dict) -> Event:
        prev_hash = self._events[-1].hash if self._events else self.GENESIS
        body = {
            "seq": len(self._events) + 1,
            "case_id": case_id,
            "type": type,
            "actor": actor,
            "at": at,
            "occurred_at": occurred_at,
            "payload": payload,
            "prev_hash": prev_hash,
        }
        digest = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
        event = Event(hash=digest, **body)
        self._events.append(event)
        return event

    def for_case(self, case_id: str) -> list[Event]:
        return [e for e in self._events if e.case_id == case_id]

    def verify(self) -> bool:
        """重放哈希链，任一环节被篡改都会校验失败。"""
        prev = self.GENESIS
        for e in self._events:
            if e.prev_hash != prev:
                return False
            body = e.as_dict()
            body.pop("hash")
            if hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest() != e.hash:
                return False
            prev = e.hash
        return True

    def __iter__(self):
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)
