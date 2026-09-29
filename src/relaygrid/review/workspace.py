from __future__ import annotations

from dataclasses import dataclass, field

from .records import FrozenError, RecordBook, digest_of
from .timeline import FrozenTimeline

CONCLUSION_KINDS = ("root_cause", "contributing_factor", "finding")
ACTION_STATES = ("open", "in_progress", "done", "verified", "dropped")


@dataclass(frozen=True)
class Revision:
    """评审产物的一个版本；只追加，不改写历史。"""

    revision: int
    ts: float
    author: str
    summary: str
    data: dict
    digest: str = field(init=False)

    def __post_init__(self):
        if not self.author:
            raise ValueError("author required")
        if not self.summary:
            raise ValueError("summary required")
        object.__setattr__(
            self,
            "digest",
            digest_of(
                {
                    "revision": self.revision,
                    "ts": self.ts,
                    "author": self.author,
                    "summary": self.summary,
                    "data": self.data,
                }
            ),
        )


@dataclass
class Ledger:
    """同一对象（一条结论/责任人指派/整改项）的版本链，链式摘要可验完整性。"""

    key: str
    _revisions: list[Revision] = field(default_factory=list)

    @property
    def current(self) -> Revision:
        if not self._revisions:
            raise KeyError(self.key)
        return self._revisions[-1]

    @property
    def revision_count(self) -> int:
        return len(self._revisions)

    def append(self, ts: float, author: str, summary: str, data: dict, freeze_at: float) -> Revision:
        revision = Revision(len(self._revisions) + 1, ts, author, summary, data)
        if ts < freeze_at:
            raise ValueError("revision timestamp predates workspace freeze")
        if self._revisions and ts < self._revisions[-1].ts:
            raise ValueError("revisions must be chronological")
        self._revisions.append(revision)
        return revision

    def revisions(self) -> tuple[Revision, ...]:
        return tuple(self._revisions)

    def chain_digest(self) -> str:
        chain: list[str] = []
        for revision in self._revisions:
            chain.append(digest_of({"key": self.key, "prev": chain[-1] if chain else None, "revision": revision.digest}))
        return chain[-1] if chain else digest_of({"key": self.key, "empty": True})


class IncidentReview:
    """事件复盘工作区。

    时间线与原始记录簿在打开工作区时即冻结；评审结论、责任人、整改项
    通过追加版本推进，历史版本全部保留，任何情况下都不回写原始记录。
    """

    def __init__(
        self,
        incident_id: str,
        title: str,
        frozen_at: float,
        timeline: FrozenTimeline,
        record_book: RecordBook,
    ):
        if not timeline.frozen:
            raise ValueError("timeline must be frozen before opening review")
        if not record_book.frozen:
            raise ValueError("record book must be frozen before opening review")
        unknown = [ref for ref in timeline.refs() if ref not in record_book]
        if unknown:
            raise ValueError(f"timeline references absent records: {sorted(unknown)}")
        self.incident_id = incident_id
        self.title = title
        self.frozen_at = frozen_at
        self.timeline = timeline
        self.record_book = record_book
        self._conclusions: dict[str, Ledger] = {}
        self._owners: dict[str, Ledger] = {}
        self._actions: dict[str, Ledger] = {}

    # -- 评审结论：版本化推进 ---------------------------------------------- #
    def record_conclusion(self, key: str, kind: str, text: str, ts: float, author: str, summary: str) -> Revision:
        if kind not in CONCLUSION_KINDS:
            raise ValueError(f"unknown conclusion kind: {kind}")
        ledger = self._conclusions.setdefault(key, Ledger(key))
        data = {"kind": kind, "text": text}
        return ledger.append(ts, author, summary, data, self.frozen_at)

    def conclusions(self) -> tuple[Ledger, ...]:
        return tuple(self._conclusions[k] for k in sorted(self._conclusions))

    # -- 责任人：可改派，改派历史保留 -------------------------------------- #
    def assign_owner(self, key: str, owner: str, scope: str, ts: float, author: str, summary: str) -> Revision:
        ledger = self._owners.setdefault(key, Ledger(key))
        return ledger.append(ts, author, summary, {"owner": owner, "scope": scope}, self.frozen_at)

    def owners(self) -> tuple[Ledger, ...]:
        return tuple(self._owners[k] for k in sorted(self._owners))

    # -- 整改项：状态推进，整改内容只追加版本 ------------------------------ #
    def update_action(
        self,
        action_id: str,
        title: str,
        state: str,
        ts: float,
        author: str,
        summary: str,
        due_at: float | None = None,
        assignee: str | None = None,
    ) -> Revision:
        if state not in ACTION_STATES:
            raise ValueError(f"unknown action state: {state}")
        ledger = self._actions.setdefault(action_id, Ledger(action_id))
        prev_state = ledger.current.data["state"] if ledger.revision_count else None
        if prev_state in {"verified", "dropped"} and state != prev_state:
            raise ValueError(f"action already {prev_state}; terminal state cannot change")
        data = {"title": title, "state": state, "due_at": due_at, "assignee": assignee}
        return ledger.append(ts, author, summary, data, self.frozen_at)

    def actions(self) -> tuple[Ledger, ...]:
        return tuple(self._actions[k] for k in sorted(self._actions))

    def open_actions(self) -> tuple[Ledger, ...]:
        return tuple(ledger for ledger in self.actions() if ledger.current.data["state"] not in {"verified", "dropped"})

    # -- 证据关联查询 ------------------------------------------------------ #
    def linked_records(self, source: str) -> tuple:
        linked = {ref for ref in self.timeline.refs() if self.record_book.get(ref).source == source}
        return tuple(self.record_book.get(rid) for rid in sorted(linked))

    def coverage(self) -> dict[str, int]:
        counts = dict.fromkeys(
            ("switching_order", "telecontrol", "crew", "weather", "ami", "outage"), 0
        )
        for ref in self.timeline.refs():
            source = self.record_book.get(ref).source
            if source in counts:
                counts[source] += 1
        return counts

    # -- 快照摘要：原始内容不在工作区侧产生，只汇总摘要 -------------------- #
    def snapshot_digest(self) -> str:
        return digest_of(
            {
                "incident_id": self.incident_id,
                "frozen_at": self.frozen_at,
                "timeline_digest": self.timeline.digest,
                "record_book_root": self.record_book.root_digest(),
                "conclusions": {k: v.chain_digest() for k, v in sorted(self._conclusions.items())},
                "owners": {k: v.chain_digest() for k, v in sorted(self._owners.items())},
                "actions": {k: v.chain_digest() for k, v in sorted(self._actions.items())},
            }
        )


def open_review(
    incident_id: str,
    title: str,
    frozen_at: float,
    timeline: FrozenTimeline,
    record_book: RecordBook,
) -> IncidentReview:
    """打开复盘工作区并再次确认时间线/记录簿均已冻结。"""
    if not isinstance(timeline, FrozenTimeline) or not isinstance(record_book, RecordBook):
        raise TypeError("timeline and record_book required")
    return IncidentReview(incident_id, title, frozen_at, timeline, record_book)


def assert_raw_records_sealed(review: IncidentReview) -> None:
    """复核守卫：原始记录簿必须仍处于冻结状态。"""
    if not review.record_book.frozen:
        raise FrozenError("record book is not frozen")
    if not review.timeline.frozen:
        raise FrozenError("timeline is not frozen")
