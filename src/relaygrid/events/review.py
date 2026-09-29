"""Event review workspace for post-incident analysis.

Freezes an incident timeline into an immutable snapshot, associates
operational evidence (operation tickets, remote control, crew, weather,
AMI, outage records), and version-controls review conclusions, responsible
parties, and rectification items. Raw operational records are never
rewritten; the frozen timeline and evidence references are append-only.
Exports carry snapshot hashes so the originating snapshot can be verified.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

# Evidence kinds that can be associated with a review.
EVIDENCE_KINDS = (
    "operation_ticket",  # 操作票
    "remote_control",    # 遥控
    "crew",              # 班组
    "weather",           # 天气
    "ami",               # AMI 计量
    "outage",            # 停电证据
)

# Workspace lifecycle states.
REVIEW_STATES = ("draft", "in_review", "finalized")


def _hash(payload: Any) -> str:
    """Deterministic SHA-256 hash of a JSON-serializable payload."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TimelineEvent:
    """A single raw operational event. Immutable once created."""
    ts: float
    kind: str
    entity: str
    detail: str


@dataclass(frozen=True)
class FrozenTimeline:
    """An immutable snapshot of an incident timeline.

    The snapshot hash binds the frozen events so any later tampering is
    detectable. Raw records are referenced, never rewritten.
    """
    events: tuple[TimelineEvent, ...]
    frozen_at: float
    snapshot_hash: str

    def verify(self) -> bool:
        """Recompute the snapshot hash and confirm it matches."""
        payload = [
            {"ts": e.ts, "kind": e.kind, "entity": e.entity, "detail": e.detail}
            for e in self.events
        ]
        return _hash(payload) == self.snapshot_hash


def freeze_timeline(events: list[TimelineEvent], frozen_at: float) -> FrozenTimeline:
    """Freeze raw events into an immutable timeline snapshot.

    The input events are copied into a tuple; the original list is never
    modified. The snapshot hash is computed over the ordered events.
    """
    ordered = sorted(events, key=lambda e: (e.ts, e.kind, e.entity, e.detail))
    payload = [
        {"ts": e.ts, "kind": e.kind, "entity": e.entity, "detail": e.detail}
        for e in ordered
    ]
    return FrozenTimeline(tuple(ordered), frozen_at, _hash(payload))


@dataclass(frozen=True)
class Evidence:
    """A reference to an operational evidence record.

    The evidence points at a raw record by (kind, ref_id); the raw record
    itself is never copied or modified.
    """
    kind: str
    ref_id: str
    payload: tuple[tuple[str, Any], ...] = ()
    collected_at: float = 0.0

    def get(self, key: str, default=None):
        for k, v in self.payload:
            if k == key:
                return v
        return default


@dataclass(frozen=True)
class ReviewVersion:
    """An immutable version of the review findings.

    Conclusions, responsible parties, and rectification items are versioned;
    each version is append-only so the review history is auditable.
    """
    version: int
    ts: float
    conclusion: str
    responsible: tuple[str, ...]
    rectification: tuple[str, ...]
    note: str = ""


@dataclass
class ReviewWorkspace:
    """A post-incident review workspace.

    The workspace freezes the incident timeline, associates evidence, tracks
    missing evidence, and version-controls the review findings. Raw
    operational records are never rewritten; exports carry snapshot hashes
    for verification.
    """
    incident_id: str
    expected_evidence: tuple[str, ...] = EVIDENCE_KINDS
    timeline: FrozenTimeline | None = None
    evidence: list[Evidence] = field(default_factory=list)
    versions: list[ReviewVersion] = field(default_factory=list)
    state: str = "draft"

    # ---- timeline -----------------------------------------------------
    def freeze(self, events: list[TimelineEvent], frozen_at: float) -> FrozenTimeline:
        """Freeze the incident timeline. Raises if already frozen or finalized."""
        if self.state == "finalized":
            raise ValueError("workspace finalized")
        if self.timeline is not None:
            raise ValueError("timeline already frozen")
        self.timeline = freeze_timeline(events, frozen_at)
        return self.timeline

    # ---- evidence -----------------------------------------------------
    def add_evidence(self, evidence: Evidence) -> Evidence:
        """Associate an evidence reference. Raises if the workspace is finalized."""
        if self.state == "finalized":
            raise ValueError("workspace finalized")
        if evidence.kind not in EVIDENCE_KINDS:
            raise ValueError(f"unknown evidence kind: {evidence.kind}")
        self.evidence.append(evidence)
        return evidence

    def evidence_by_kind(self, kind: str) -> list[Evidence]:
        return [e for e in self.evidence if e.kind == kind]

    def collected_kinds(self) -> set[str]:
        return {e.kind for e in self.evidence}

    def missing_evidence(self) -> list[str]:
        """Evidence kinds that are expected but not yet collected."""
        return [k for k in self.expected_evidence if k not in self.collected_kinds()]

    def evidence_complete(self) -> bool:
        """Whether all expected evidence kinds have been collected."""
        return not self.missing_evidence()

    def evidence_hash(self) -> str:
        """Deterministic hash of the collected evidence."""
        payload = [
            {
                "kind": e.kind,
                "ref_id": e.ref_id,
                "collected_at": e.collected_at,
                "payload": [list(pair) for pair in e.payload],
            }
            for e in sorted(self.evidence, key=lambda x: (x.kind, x.ref_id, x.collected_at))
        ]
        return _hash(payload)

    # ---- review versions ---------------------------------------------
    def review(
        self,
        ts: float,
        conclusion: str,
        responsible: list[str],
        rectification: list[str],
        note: str = "",
    ) -> ReviewVersion:
        """Create a new review version. Append-only; history is preserved."""
        if self.state == "finalized":
            raise ValueError("workspace finalized")
        if self.timeline is None:
            raise ValueError("freeze timeline before reviewing")
        version = len(self.versions) + 1
        rv = ReviewVersion(
            version, ts, conclusion, tuple(responsible), tuple(rectification), note
        )
        self.versions.append(rv)
        return rv

    def current_version(self) -> ReviewVersion | None:
        return self.versions[-1] if self.versions else None

    def review_history(self) -> list[ReviewVersion]:
        """All review versions, oldest first."""
        return list(self.versions)

    def finalize(self) -> str:
        """Finalize the workspace. No further mutations are allowed."""
        if self.timeline is None:
            raise ValueError("cannot finalize without a frozen timeline")
        if not self.versions:
            raise ValueError("cannot finalize without a review version")
        self.state = "finalized"
        return self.state

    # ---- export & verification ---------------------------------------
    def export(self, exported_at: float = 0.0) -> dict[str, Any]:
        """Export the workspace state with snapshot hashes.

        The export binds the frozen timeline, the collected evidence, and the
        current review version so the originating snapshot can be verified.
        """
        current = self.current_version()
        timeline_hash = self.timeline.snapshot_hash if self.timeline else ""
        payload = {
            "incident_id": self.incident_id,
            "state": self.state,
            "timeline_hash": timeline_hash,
            "evidence_hash": self.evidence_hash(),
            "review_version": current.version if current else 0,
            "exported_at": exported_at,
        }
        payload["workspace_hash"] = _hash(payload)
        return payload

    def verify(self, export: dict[str, Any]) -> bool:
        """Verify that an export corresponds to the current workspace state.

        Recomputes the timeline, evidence, and workspace hashes from the
        live state and compares them against the export.
        """
        if export.get("incident_id") != self.incident_id:
            return False
        if export.get("state") != self.state:
            return False
        timeline_hash = self.timeline.snapshot_hash if self.timeline else ""
        if export.get("timeline_hash") != timeline_hash:
            return False
        if export.get("evidence_hash") != self.evidence_hash():
            return False
        current = self.current_version()
        if export.get("review_version") != (current.version if current else 0):
            return False
        body = {k: v for k, v in export.items() if k != "workspace_hash"}
        return _hash(body) == export.get("workspace_hash", "")
