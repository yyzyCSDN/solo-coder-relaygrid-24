from __future__ import annotations

import dataclasses
import json

import pytest

from relaygrid.review import (
    ExpectedEvidence,
    FrozenError,
    IncidentReview,
    Ledger,
    RawRecord,
    RecordBook,
    TimelineEvent,
    build_bundle,
    build_timeline,
    detect_missing,
    digest_of,
    make_record,
    open_review,
    record_from_assignment,
    record_from_command,
    record_from_meter_observation,
    record_from_outage_interval,
    record_from_shift,
    record_from_switching_order,
    record_from_weather,
    verify_bundle,
    write_bundle,
)
from relaygrid.operations.command_bus import Command
from relaygrid.switching.orders import OrderState, SwitchingOrder, SwitchingStep
from relaygrid.crew.shifts import Assignment, Shift
from relaygrid.metering.restoration import MeterObservation
from relaygrid.outage.reconciliation import Interval
from relaygrid.schemas.weather import Observation as WeatherObservation


def ledger_chain_at(revisions, key, freeze_at=300.0):
    """用前 N 个版本重放一条账本，用于验证历史版本摘要可独立重算。"""
    ledger = Ledger(key)
    for rev in revisions:
        ledger.append(rev.ts, rev.author, rev.summary, rev.data, freeze_at)
    return ledger.chain_digest()


def make_order() -> SwitchingOrder:
    order = SwitchingOrder("SO-100")
    order.create([SwitchingStep(1, "open", "SW-7", "open")], author="disp_li")
    order.check("checker_wang")
    order.approve("boss_zhao")
    order.start()
    order.acknowledge(1, "op_chen", 120.0)
    return order


def make_book(order: SwitchingOrder) -> tuple[RecordBook, dict]:
    command = Command("CMD-1", "SW-7", "open", 110.0, 200.0, "op_chen")
    shift = Shift("CREW-A", 0.0, 480.0, 360.0, 60.0)
    assignment = Assignment("CREW-A", "JOB-9", 60.0, 240.0, emergency=True)
    weather = WeatherObservation(
        "WX-1",
        attributes=(("wind_mps", 34.0), ("temp_c", -2.0), ("precip_mm", 12.0)),
    )
    meter = MeterObservation("MTR-9", "SEC-3", 130.0, None, False)
    interval = Interval("GRP-1", 65.0, 195.0, "breaker", "ami")
    records = [
        record_from_switching_order("rec-order", order, 300.0),
        record_from_command("rec-cmd", command, 300.0, order_id="SO-100"),
        record_from_shift("rec-shift", shift, 300.0),
        record_from_assignment("rec-assign", assignment, 300.0),
        record_from_weather("rec-wx", weather, 300.0),
        record_from_meter_observation("rec-ami", meter, 300.0),
        record_from_outage_interval("rec-outage", interval, 300.0),
    ]
    ids = {
        "order": "rec-order",
        "command": "rec-cmd",
        "shift": "rec-shift",
        "assign": "rec-assign",
        "weather": "rec-wx",
        "ami": "rec-ami",
        "outage": "rec-outage",
    }
    book = RecordBook()
    for record in records:
        book.append(record)
    book.freeze()
    return book, ids


def build_workspace():
    order = make_order()
    book, ids = make_book(order)
    timeline = build_timeline(
        0.0,
        300.0,
        [
            TimelineEvent(65.0, "馈线跳闸", "SW-7 保护动作", (ids["outage"], ids["order"])),
            TimelineEvent(110.0, "下发遥控", "拉开 SW-7", (ids["command"],)),
            TimelineEvent(120.0, "现场确认", "班组到达并操作", (ids["assign"], ids["shift"])),
            TimelineEvent(130.0, "AMI 最后上报", "MTR-9 失联", (ids["ami"],)),
            TimelineEvent(195.0, "恢复送电", "气象解除后送电", (ids["weather"], ids["outage"])),
        ],
    )
    review = open_review("INC-2026-001", "SW-7 跳闸事件", 300.0, timeline, book)
    return review, order, book, timeline, ids


def test_frozen_records_stable_when_source_objects_change():
    order = make_order()
    book, _ = make_book(order)
    before = book.get("rec-order").digest

    # 冻结后即便运行侧对象后续被替换/篡改，复盘快照也不变
    tampered = dataclasses.replace(
        order.current, steps=(SwitchingStep(1, "close", "SW-7", "closed"),)
    )
    order.revisions[-1] = tampered

    assert book.get("rec-order").digest == before
    assert book.root_digest() == digest_of(
        {"alg": "sha256", "records": [{"record_id": r.record_id, "digest": r.digest} for r in book.records()]}
    )


def test_record_book_and_timeline_are_append_only_after_freeze():
    order = make_order()
    book, ids = make_book(order)
    with pytest.raises(FrozenError):
        book.append(make_record("rec-new", "logbook", 301.0, {}))

    timeline = build_timeline(0.0, 300.0, [TimelineEvent(10.0, "x")])
    with pytest.raises(FrozenError):
        timeline.add(TimelineEvent(20.0, "y"))
    with pytest.raises(ValueError):
        build_timeline(0.0, 10.0, [TimelineEvent(50.0, "out of window")])


def test_review_refuses_unfrozen_or_dangling_inputs():
    order = make_order()
    book, ids = make_book(order)
    loose_timeline = build_timeline(0.0, 300.0, [TimelineEvent(10.0, "x", refs=("ghost",))])
    with pytest.raises(ValueError, match="absent records"):
        open_review("INC", "t", 300.0, loose_timeline, book)

    unfrozen = RecordBook()
    unfrozen.append(make_record("r", "ami", 1.0, {}))
    with pytest.raises(ValueError, match="frozen"):
        open_review("INC", "t", 300.0, build_timeline(0.0, 1.0, []), unfrozen)


def test_conclusions_owners_actions_are_versioned_history_retained():
    review, _, _, _, _ = build_workspace()

    r1 = review.record_conclusion("c1", "root_cause", "初步：雷暴导致闪络", 320.0, "lead_wu", "初版结论")
    r2 = review.record_conclusion("c1", "root_cause", "复核：树木倒伏引发短路", 400.0, "lead_wu", "现场巡视后修订")
    assert r1.revision == 1 and r2.revision == 2
    ledger = review.conclusions()[0]
    assert ledger.current.data["text"].startswith("复核")
    assert ledger.revisions()[0].data["text"].startswith("初步")  # 历史版本未被改写

    review.assign_owner("o-dispatch", "调度班", "遥控执行", 330.0, "lead_wu", "初判调度责任")
    review.assign_owner("o-dispatch", "运检班", "树障巡检", 410.0, "lead_wu", "改派责任班组")
    owners = review.owners()[0]
    assert owners.current.data["owner"] == "运检班"
    assert owners.revisions()[0].data["owner"] == "调度班"

    review.update_action("a1", "修剪 SW-7 走廊树障", "open", 420.0, "lead_wu", "立项", due_at=1000.0, assignee="运检班")
    review.update_action("a1", "修剪 SW-7 走廊树障", "in_progress", 500.0, "foreman", "已开工")
    review.update_action("a1", "修剪 SW-7 走廊树障", "verified", 900.0, "lead_wu", "验收通过")
    with pytest.raises(ValueError, match="terminal state"):
        review.update_action("a1", "修剪 SW-7 走廊树障", "open", 950.0, "lead_wu", "尝试重开")
    assert review.open_actions() == ()

    # 版本链随每次推进而变化，但旧链摘要仍可由前 N 个版本独立重算
    assert owners.chain_digest() != ledger_chain_at(owners.revisions()[:1], "o-dispatch")

    with pytest.raises(ValueError):
        review.record_conclusion("c2", "finding", "x", 100.0, "lead_wu", "早于冻结时刻")
    with pytest.raises(ValueError):
        review.update_action("a2", "t", "bogus_state", 500.0, "lead_wu", "bad state")


def test_coverage_and_missing_evidence():
    review, _, book, timeline, ids = build_workspace()
    assert review.coverage() == {
        "switching_order": 1,
        "telecontrol": 1,
        "crew": 2,
        "weather": 1,
        "ami": 1,
        "outage": 1,
    }
    assert detect_missing(timeline, book) == []

    # 缺天气：只封存操作票+遥控的最小工作区
    tiny_timeline = build_timeline(
        0.0, 100.0,
        [TimelineEvent(10.0, "操作", refs=("ro", "rc"))],
    )
    tiny_book = RecordBook()
    tiny_book.append(make_record("ro", "switching_order", 100.0, {"order_id": "SO-1"}))
    tiny_book.append(
        record_from_command(
            "rc", Command("C2", "SW-1", "open", 5.0, 90.0, "op"), 100.0, order_id="SO-1"
        )
    )
    tiny_book.freeze()
    gaps = detect_missing(tiny_timeline, tiny_book)
    codes = {g.code for g in gaps}
    assert {"ami_without_crew", "ami_without_weather"}.isdisjoint(codes)  # 没有 AMI 不报这两条
    # 声明式要求：天气和指定 AMI 记录缺失
    gaps2 = detect_missing(
        tiny_timeline,
        tiny_book,
        [
            ExpectedEvidence("weather", "事故窗口需天气"),
            ExpectedEvidence("ami", "需要 MTR-1 量测", record_id="rec-ami-missing"),
        ],
    )
    assert {g.code for g in gaps2} == {"declared_source_absent", "declared_record_absent"}


def test_telecontrol_without_order_flagged():
    timeline = build_timeline(0.0, 100.0, [TimelineEvent(10.0, "无票遥控", refs=("rc",))])
    book = RecordBook()
    book.append(record_from_command("rc", Command("C3", "SW-9", "trip", 5.0, 90.0, "op"), 100.0))
    book.freeze()
    gaps = detect_missing(timeline, book)
    assert gaps[0].code == "telecontrol_without_order"


def test_export_roundtrip_and_tamper_detection(tmp_path):
    review, _, _, _, _ = build_workspace()
    expected = [ExpectedEvidence("weather", "事故窗口天气记录")]
    bundle = build_bundle(review, expected)

    report = verify_bundle(bundle)
    assert report.ok

    # 调用方持有的快照指纹可防止整包替换
    snap = bundle["snapshot_digest"]
    tampered = dict(bundle)
    tampered["title"] = "被改写的标题"
    bad = verify_bundle(tampered, expected_snapshot_digest=snap)
    # 标题不进 snapshot_digest，但会破坏 bundle_digest
    assert not bad.ok and "bundle digest mismatch" in bad.errors

    # 直接改一条原始记录的载荷 -> 记录摘要失配（深拷贝，不污染原包）
    tampered_records = json.loads(json.dumps(bundle["records"]))
    tampered_records[0]["payload"]["author"] = "forged"
    bundle2 = dict(bundle)
    bundle2["records"] = tampered_records
    assert not verify_bundle(bundle2).ok
    assert verify_bundle(bundle).ok

    path = tmp_path / "incident.json"
    written = write_bundle(str(path), review, expected)
    assert written == bundle["bundle_digest"]

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert verify_bundle(on_disk).ok

    # 源系统记录在冻结后发生变化 -> live 比对报错（复盘侧记录不变）
    live = [RawRecord(r.record_id, r.source, r.captured_at, dict(r.payload)) for r in review.record_book]
    live[0] = RawRecord(
        live[0].record_id, live[0].source, live[0].captured_at,
        {**live[0].payload, "author": "someone_else"},
    )
    report_live = verify_bundle(bundle, live_records=live)
    assert not report_live.ok
    assert any("raw record changed after freeze" in e for e in report_live.errors)


def test_export_detects_stripped_missing_evidence_list():
    # 一个有缺失证据（无票遥控）的工作区
    timeline = build_timeline(0.0, 100.0, [TimelineEvent(10.0, "无票", refs=("rc",))])
    book = RecordBook()
    book.append(record_from_command("rc", Command("C3", "SW-9", "trip", 5.0, 90.0, "op"), 100.0))
    book.freeze()
    review = open_review("INC-X", "无票事件", 100.0, timeline, book)

    bundle = build_bundle(review)
    assert bundle["missing_evidence"]
    assert any("gap" in w for w in verify_bundle(bundle).warnings)

    # 掏空缺失清单并重算外层摘要伪装自洽 -> 仍应被发现
    stripped = dict(bundle)
    stripped["missing_evidence"] = []
    stripped["bundle_digest"] = digest_of(
        {k: v for k, v in stripped.items() if k != "bundle_digest"}
    )
    report = verify_bundle(stripped)
    assert not report.ok
    assert any("missing-evidence list stale" in e for e in report.errors)


def test_review_cannot_mutate_raw_records():
    review, _, book, _, ids = build_workspace()
    original = book.get(ids["command"]).digest
    # 工作区只暴露读取视图
    linked = review.linked_records("telecontrol")
    assert len(linked) == 1 and linked[0].digest == original
    with pytest.raises(FrozenError):
        review.record_book.append(make_record("x", "ami", 999.0, {}))
    assert isinstance(review, IncidentReview)
