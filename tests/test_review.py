from __future__ import annotations

import pytest

from relaygrid.events.review import (
    EVIDENCE_KINDS,
    Evidence,
    FrozenTimeline,
    ReviewVersion,
    ReviewWorkspace,
    TimelineEvent,
    freeze_timeline,
)


def make_events():
    return [
        TimelineEvent(0.0, "fault", "feeder-1", "overcurrent trip"),
        TimelineEvent(5.0, "remote_control", "rc-1", "open switch"),
        TimelineEvent(12.0, "ami", "meter-9", "last gasp"),
        TimelineEvent(20.0, "restore", "feeder-1", "reclose success"),
    ]


def make_workspace():
    ws = ReviewWorkspace(incident_id="INC-2026-001")
    ws.freeze(make_events(), frozen_at=100.0)
    return ws


# ---- timeline freezing ----------------------------------------------

def test_freeze_produces_immutable_snapshot():
    raw = make_events()
    ws = ReviewWorkspace("INC-1")
    frozen = ws.freeze(raw, frozen_at=50.0)
    assert isinstance(frozen, FrozenTimeline)
    assert frozen.frozen_at == 50.0
    assert len(frozen.events) == 4
    # The frozen timeline is a tuple copy; the original list is untouched.
    assert isinstance(frozen.events, tuple)
    assert len(raw) == 4


def test_freeze_sorts_events_by_ts():
    raw = [
        TimelineEvent(20.0, "restore", "f", "x"),
        TimelineEvent(0.0, "fault", "f", "y"),
        TimelineEvent(10.0, "ami", "m", "z"),
    ]
    frozen = freeze_timeline(raw, frozen_at=1.0)
    assert [e.ts for e in frozen.events] == [0.0, 10.0, 20.0]


def test_frozen_timeline_verifies_own_hash():
    frozen = freeze_timeline(make_events(), frozen_at=1.0)
    assert frozen.verify()


def test_tampered_timeline_fails_verification():
    frozen = freeze_timeline(make_events(), frozen_at=1.0)
    # Build a tampered copy with a different event but the same hash.
    tampered = FrozenTimeline(
        events=frozen.events[:-1] + (TimelineEvent(99.0, "fake", "x", "y"),),
        frozen_at=frozen.frozen_at,
        snapshot_hash=frozen.snapshot_hash,
    )
    assert not tampered.verify()


def test_cannot_freeze_twice():
    ws = make_workspace()
    with pytest.raises(ValueError, match="already frozen"):
        ws.freeze(make_events(), frozen_at=200.0)


def test_frozen_events_are_immutable():
    frozen = freeze_timeline(make_events(), frozen_at=1.0)
    with pytest.raises(Exception):
        frozen.events[0] = TimelineEvent(1.0, "x", "y", "z")


# ---- evidence association --------------------------------------------

def test_add_evidence_for_every_kind():
    ws = make_workspace()
    for kind in EVIDENCE_KINDS:
        ws.add_evidence(Evidence(kind=kind, ref_id=f"ref-{kind}", collected_at=10.0))
    assert ws.collected_kinds() == set(EVIDENCE_KINDS)
    assert ws.evidence_complete()


def test_unknown_evidence_kind_rejected():
    ws = make_workspace()
    with pytest.raises(ValueError, match="unknown evidence kind"):
        ws.add_evidence(Evidence(kind="bogus", ref_id="x"))


def test_missing_evidence_listed_separately():
    ws = make_workspace()
    ws.add_evidence(Evidence(kind="weather", ref_id="w-1"))
    missing = ws.missing_evidence()
    assert "weather" not in missing
    assert "operation_ticket" in missing
    assert "outage" in missing
    assert len(missing) == len(EVIDENCE_KINDS) - 1


def test_evidence_by_kind_filters():
    ws = make_workspace()
    ws.add_evidence(Evidence(kind="ami", ref_id="m-1"))
    ws.add_evidence(Evidence(kind="ami", ref_id="m-2"))
    assert len(ws.evidence_by_kind("ami")) == 2
    assert ws.evidence_by_kind("crew") == []


def test_evidence_payload_accessor():
    ev = Evidence(kind="weather", ref_id="w-1", payload=(("temp_c", 32.0), ("wind_mps", 14.0)))
    assert ev.get("temp_c") == 32.0
    assert ev.get("missing", "default") == "default"


# ---- review versioning -----------------------------------------------

def test_review_requires_frozen_timeline():
    ws = ReviewWorkspace("INC-1")
    with pytest.raises(ValueError, match="freeze timeline"):
        ws.review(ts=1.0, conclusion="x", responsible=["a"], rectification=["b"])


def test_review_versions_are_append_only():
    ws = make_workspace()
    ws.review(ts=1.0, conclusion="v1", responsible=["张三"], rectification=["r1"], note="初版")
    ws.review(ts=2.0, conclusion="v2", responsible=["张三", "李四"], rectification=["r1", "r2"], note="更新")
    history = ws.review_history()
    assert len(history) == 2
    assert history[0].version == 1
    assert history[1].version == 2
    assert history[1].conclusion == "v2"
    assert ws.current_version().version == 2


def test_review_version_is_frozen():
    ws = make_workspace()
    rv = ws.review(ts=1.0, conclusion="x", responsible=["a"], rectification=["b"])
    assert isinstance(rv, ReviewVersion)
    with pytest.raises(Exception):
        rv.conclusion = "mutated"


def test_responsible_and_rectification_stored_as_tuples():
    ws = make_workspace()
    rv = ws.review(ts=1.0, conclusion="x", responsible=["a", "b"], rectification=["r"])
    assert isinstance(rv.responsible, tuple)
    assert isinstance(rv.rectification, tuple)


# ---- finalization ----------------------------------------------------

def test_finalize_requires_timeline():
    ws = ReviewWorkspace("INC-1")
    with pytest.raises(ValueError, match="without a frozen timeline"):
        ws.finalize()


def test_finalize_requires_review():
    ws = ReviewWorkspace("INC-1")
    ws.freeze(make_events(), frozen_at=1.0)
    with pytest.raises(ValueError, match="without a review version"):
        ws.finalize()


def test_finalize_blocks_mutations():
    ws = make_workspace()
    ws.review(ts=1.0, conclusion="x", responsible=["a"], rectification=["b"])
    ws.finalize()
    assert ws.state == "finalized"
    with pytest.raises(ValueError, match="finalized"):
        ws.freeze(make_events(), frozen_at=2.0)
    with pytest.raises(ValueError, match="finalized"):
        ws.add_evidence(Evidence(kind="weather", ref_id="w"))
    with pytest.raises(ValueError, match="finalized"):
        ws.review(ts=2.0, conclusion="y", responsible=["a"], rectification=["b"])


# ---- export & verification ------------------------------------------

def test_export_contains_snapshot_hashes():
    ws = make_workspace()
    ws.add_evidence(Evidence(kind="weather", ref_id="w-1"))
    ws.review(ts=1.0, conclusion="x", responsible=["a"], rectification=["b"])
    export = ws.export(exported_at=500.0)
    assert export["incident_id"] == "INC-2026-001"
    assert export["state"] == "draft"
    assert len(export["timeline_hash"]) == 64
    assert len(export["evidence_hash"]) == 64
    assert len(export["workspace_hash"]) == 64
    assert export["review_version"] == 1
    assert export["exported_at"] == 500.0


def test_export_verifies_against_live_state():
    ws = make_workspace()
    ws.add_evidence(Evidence(kind="ami", ref_id="m-1"))
    ws.review(ts=1.0, conclusion="x", responsible=["a"], rectification=["b"])
    export = ws.export()
    assert ws.verify(export)


def test_export_fails_verification_after_tampering():
    ws = make_workspace()
    ws.review(ts=1.0, conclusion="x", responsible=["a"], rectification=["b"])
    export = ws.export()
    # Tamper with the export.
    export["conclusion"] = "tampered"
    assert not ws.verify(export)


def test_export_fails_verification_after_state_change():
    ws = make_workspace()
    ws.review(ts=1.0, conclusion="x", responsible=["a"], rectification=["b"])
    export = ws.export()
    # Add evidence after export; the evidence hash must no longer match.
    ws.add_evidence(Evidence(kind="weather", ref_id="w-1"))
    assert not ws.verify(export)


def test_export_fails_verification_with_wrong_incident():
    ws = make_workspace()
    ws.review(ts=1.0, conclusion="x", responsible=["a"], rectification=["b"])
    export = ws.export()
    export["incident_id"] = "WRONG"
    assert not ws.verify(export)


def test_export_fails_verification_with_wrong_workspace_hash():
    ws = make_workspace()
    ws.review(ts=1.0, conclusion="x", responsible=["a"], rectification=["b"])
    export = ws.export()
    export["workspace_hash"] = "0" * 64
    assert not ws.verify(export)


def test_finalized_export_verifies():
    ws = make_workspace()
    ws.review(ts=1.0, conclusion="x", responsible=["a"], rectification=["b"])
    ws.finalize()
    export = ws.export()
    assert ws.verify(export)
    assert export["state"] == "finalized"


def test_evidence_hash_is_deterministic_regardless_of_insertion_order():
    ws1 = make_workspace()
    ws2 = make_workspace()
    ws1.add_evidence(Evidence(kind="ami", ref_id="m-1"))
    ws1.add_evidence(Evidence(kind="weather", ref_id="w-1"))
    ws2.add_evidence(Evidence(kind="weather", ref_id="w-1"))
    ws2.add_evidence(Evidence(kind="ami", ref_id="m-1"))
    assert ws1.evidence_hash() == ws2.evidence_hash()
