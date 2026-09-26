"""只追加的事件存储。

所有业务事实（术语核对、责任单位确认、更换、驳回、重开、规范换版……）
都以事件形式追加到日志中，不做任何原地修改或删除。每条流（一个案件或
规范注册表）维护单调递增的 stream_seq，追加时可声明期望版本，从而实现
乐观并发控制：离线补报与多人同时操作同一线索时，后到者会收到冲突错误，
而不是悄悄覆盖出第二条结论。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .errors import ConcurrencyConflictError


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Event:
    """一条不可变事实。occurred_at 是业务发生时间（离线补报可能早于
    追加时刻），recorded_at 是入库时间，seq 是全局追加顺序。"""

    seq: int
    stream: str
    stream_seq: int
    type: str
    actor: str
    occurred_at: str
    recorded_at: str
    payload: dict

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        return cls(**data)


class EventStore:
    """全局有序、按流版本化的只追加日志，可选 JSONL 落盘。"""

    def __init__(self, path: str | Path | None = None, clock: Callable[[], str] | None = None):
        self._path = Path(path) if path else None
        self._clock = clock or _utcnow_iso
        self._events: list[Event] = []
        self._stream_seqs: dict[str, int] = {}
        if self._path and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._register(Event.from_dict(json.loads(line)))

    def append(
        self,
        *,
        stream: str,
        type: str,
        actor: str,
        occurred_at: str,
        payload: dict,
        expected_stream_seq: int | None = None,
    ) -> Event:
        """追加事件。expected_stream_seq 不为空时，若流已被推进则抛冲突。"""
        current = self._stream_seqs.get(stream, 0)
        if expected_stream_seq is not None and expected_stream_seq != current:
            raise ConcurrencyConflictError(
                f"流 {stream} 已推进到版本 {current}，期望 {expected_stream_seq}，请重读后重试"
            )
        event = Event(
            seq=len(self._events) + 1,
            stream=stream,
            stream_seq=current + 1,
            type=type,
            actor=actor,
            occurred_at=occurred_at,
            recorded_at=self._clock(),
            payload=dict(payload),
        )
        if self._path:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        self._register(event)
        return event

    def events(self, stream: str | None = None) -> list[Event]:
        if stream is None:
            return list(self._events)
        return [e for e in self._events if e.stream == stream]

    def stream_seq(self, stream: str) -> int:
        return self._stream_seqs.get(stream, 0)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def _register(self, event: Event) -> None:
        self._events.append(event)
        self._stream_seqs[event.stream] = event.stream_seq
