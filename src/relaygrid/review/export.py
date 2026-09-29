from __future__ import annotations

import json
from dataclasses import dataclass, field

from .missing import ExpectedEvidence, detect_missing
from .records import RawRecord, RecordBook, digest_of, to_jsonable
from .timeline import FrozenTimeline, TimelineEvent
from .workspace import Ledger

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class VerificationReport:
    ok: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def raise_if_invalid(self) -> None:
        if not self.ok:
            raise ValueError("export verification failed: " + "; ".join(self.errors))


def _ledger_entries(ledgers: dict[str, Ledger] | tuple[Ledger, ...]) -> dict:
    items = ledgers.items() if isinstance(ledgers, dict) else ((l.key, l) for l in ledgers)
    return {
        key: [
            {
                "revision": rev.revision,
                "ts": rev.ts,
                "author": rev.author,
                "summary": rev.summary,
                "data": rev.data,
                "digest": rev.digest,
            }
            for rev in ledger.revisions()
        ]
        for key, ledger in sorted(items)
    }


def build_bundle(review: IncidentReview, expected: list[ExpectedEvidence] | None = None) -> dict:
    """组装自包含快照包：原始记录全文都在包内，可离线重算全部摘要。"""
    missing = detect_missing(review.timeline, review.record_book, expected)
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "incident_id": review.incident_id,
        "title": review.title,
        "frozen_at": review.frozen_at,
        "timeline": {
            "window_start": review.timeline.window_start,
            "window_end": review.timeline.window_end,
            "events": [
                {"ts": e.ts, "title": e.title, "detail": e.detail, "refs": list(e.refs)}
                for e in review.timeline.events()
            ],
        },
        "timeline_digest": review.timeline.digest,
        "records": [
            {
                "record_id": r.record_id,
                "source": r.source,
                "captured_at": r.captured_at,
                "payload": to_jsonable(r.payload),
                "digest": r.digest,
            }
            for r in review.record_book.records()
        ],
        "record_book_root": review.record_book.root_digest(),
        "expected_evidence": [
            {"source": item.source, "reason": item.reason, "record_id": item.record_id}
            for item in (expected or [])
        ],
        "missing_evidence": [
            {"code": m.code, "source": m.source, "reason": m.reason, "record_id": m.record_id}
            for m in missing
        ],
        "ledgers": {
            "conclusions": _ledger_entries(review.conclusions()),
            "owners": _ledger_entries(review.owners()),
            "actions": _ledger_entries(review.actions()),
        },
        "ledger_chains": {
            "conclusions": {l.key: l.chain_digest() for l in review.conclusions()},
            "owners": {l.key: l.chain_digest() for l in review.owners()},
            "actions": {l.key: l.chain_digest() for l in review.actions()},
        },
        "snapshot_digest": review.snapshot_digest(),
    }
    bundle["bundle_digest"] = digest_of({k: v for k, v in bundle.items() if k != "bundle_digest"})
    return bundle


def write_bundle(path: str, review: IncidentReview, expected: list[ExpectedEvidence] | None = None) -> str:
    bundle = build_bundle(review, expected)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(bundle, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return bundle["bundle_digest"]


def read_bundle(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _rebuild_book(bundle: dict) -> RecordBook:
    book = RecordBook()
    for entry in bundle["records"]:
        record = RawRecord(entry["record_id"], entry["source"], entry["captured_at"], entry["payload"])
        if record.digest != entry["digest"]:
            raise ValueError(f"record digest mismatch: {entry['record_id']}")
        book.append(record)
    book.freeze()
    if book.root_digest() != bundle["record_book_root"]:
        raise ValueError("record book root digest mismatch")
    return book


def _rebuild_timeline(bundle: dict) -> FrozenTimeline:
    raw = bundle["timeline"]
    timeline = FrozenTimeline(raw["window_start"], raw["window_end"])
    for entry in raw["events"]:
        timeline.add(
            TimelineEvent(entry["ts"], entry["title"], entry.get("detail", ""), tuple(entry.get("refs", ())))
        )
    timeline.freeze()
    if timeline.digest != bundle["timeline_digest"]:
        raise ValueError("timeline digest mismatch")
    return timeline


def _rebuild_ledgers(bundle: dict, freeze_at: float) -> dict[str, dict[str, Ledger]]:
    rebuilt: dict[str, dict[str, Ledger]] = {}
    for name, entries in bundle["ledgers"].items():
        group: dict[str, Ledger] = {}
        for key, revisions in entries.items():
            ledger = Ledger(key)
            for item in revisions:
                ledger.append(item["ts"], item["author"], item["summary"], item["data"], freeze_at)
                revision = ledger.current
                if revision.digest != item["digest"]:
                    raise ValueError(f"{name}/{key} r{item['revision']} digest mismatch")
            group[key] = ledger
        rebuilt[name] = group
    return rebuilt


def verify_bundle(
    bundle: dict,
    expected_snapshot_digest: str | None = None,
    live_records: list[RawRecord] | None = None,
) -> VerificationReport:
    """重算包内所有摘要并比对。

    expected_snapshot_digest: 调用方持有的快照指纹，防止整包被整体替换。
    live_records: 可选地把包内记录与源系统当前记录比对（digest 不一致说明
                  冻结后运行原始记录被改动）。注意：本工具本身绝不改写源记录。
    """
    errors: list[str] = []
    warnings: list[str] = []
    try:
        if bundle.get("schema_version") != SCHEMA_VERSION:
            errors.append("schema_version mismatch")

        recomputed_bundle_digest = digest_of(
            {k: v for k, v in bundle.items() if k != "bundle_digest"}
        )
        if recomputed_bundle_digest != bundle.get("bundle_digest"):
            errors.append("bundle digest mismatch")

        book = _rebuild_book(bundle)
        timeline = _rebuild_timeline(bundle)
        ledgers = _rebuild_ledgers(bundle, bundle["frozen_at"])

        chains = {
            name: {key: ledger.chain_digest() for key, ledger in group.items()}
            for name, group in ledgers.items()
        }
        if chains != bundle.get("ledger_chains"):
            errors.append("ledger chain digest mismatch")

        expected = [
            ExpectedEvidence(item["source"], item["reason"], item.get("record_id"))
            for item in bundle.get("expected_evidence", [])
        ]
        missing = detect_missing(timeline, book, expected)
        packed = [
            {"code": m.code, "source": m.source, "reason": m.reason, "record_id": m.record_id}
            for m in missing
        ]
        if packed != bundle.get("missing_evidence"):
            errors.append("missing-evidence list stale or tampered")

        # 用重算出的链式摘要再算一次快照指纹
        snapshot = {
            "incident_id": bundle["incident_id"],
            "frozen_at": bundle["frozen_at"],
            "timeline_digest": timeline.digest,
            "record_book_root": book.root_digest(),
            "conclusions": chains["conclusions"],
            "owners": chains["owners"],
            "actions": chains["actions"],
        }
        if digest_of(snapshot) != bundle.get("snapshot_digest"):
            errors.append("snapshot digest mismatch")
        if expected_snapshot_digest is not None and expected_snapshot_digest != bundle.get("snapshot_digest"):
            errors.append("snapshot digest differs from caller-held digest")

        if live_records is not None:
            live = {r.record_id: r for r in live_records}
            for record in book.records():
                current = live.get(record.record_id)
                if current is None:
                    warnings.append(f"live record missing: {record.record_id}")
                elif current.digest != record.digest:
                    errors.append(f"raw record changed after freeze: {record.record_id}")

        if bundle.get("missing_evidence"):
            warnings.append(f"{len(bundle['missing_evidence'])} evidence gap(s) listed in bundle")
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"malformed bundle: {exc}")
    return VerificationReport(not errors, tuple(errors), tuple(warnings))


def verify_file(path: str, expected_snapshot_digest: str | None = None) -> VerificationReport:
    return verify_bundle(read_bundle(path), expected_snapshot_digest)
