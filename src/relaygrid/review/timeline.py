from __future__ import annotations

from dataclasses import dataclass, field

from .records import FrozenError, digest_of


@dataclass(frozen=True)
class TimelineEvent:
    """时间线上的一条事故节点，只引用原始记录，不复制其内容。"""

    ts: float
    title: str
    detail: str = ""
    refs: tuple[str, ...] = ()


@dataclass
class FrozenTimeline:
    """事故时间线：打开后追加，冻结后不可增删改。"""

    window_start: float
    window_end: float
    _events: list[TimelineEvent] = field(default_factory=list)
    frozen: bool = False
    digest: str = ""

    def add(self, event: TimelineEvent) -> TimelineEvent:
        if self.frozen:
            raise FrozenError("timeline frozen")
        if not self.window_start <= event.ts <= self.window_end:
            raise ValueError("event outside incident window")
        if self._events and event.ts < self._events[-1].ts:
            raise ValueError("events must be chronological")
        self._events.append(event)
        return event

    def events(self) -> tuple[TimelineEvent, ...]:
        return tuple(self._events)

    def refs(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for event in self._events:
            for ref in event.refs:
                seen.setdefault(ref, None)
        return tuple(seen)

    def freeze(self) -> str:
        if self.frozen:
            return self.digest
        self.digest = digest_of(
            {
                "window_start": self.window_start,
                "window_end": self.window_end,
                "events": [
                    {"ts": e.ts, "title": e.title, "detail": e.detail, "refs": list(e.refs)}
                    for e in self._events
                ],
            }
        )
        self.frozen = True
        return self.digest


def build_timeline(window_start: float, window_end: float, events: list[TimelineEvent]) -> FrozenTimeline:
    timeline = FrozenTimeline(window_start, window_end)
    for event in sorted(events, key=lambda e: e.ts):
        timeline.add(event)
    timeline.freeze()
    return timeline
