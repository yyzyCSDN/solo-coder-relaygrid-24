from __future__ import annotations

from dataclasses import dataclass

from .records import EVIDENCE_SOURCES, RecordBook
from .timeline import FrozenTimeline


@dataclass(frozen=True)
class ExpectedEvidence:
    """复盘要求具备的一项证据。

    record_id 为 None 时表示“该类证据至少一条即可”的类别级要求。
    """

    source: str
    reason: str
    record_id: str | None = None


@dataclass(frozen=True)
class MissingEvidence:
    code: str
    source: str
    reason: str
    record_id: str | None = None


def declared_missing(expected: list[ExpectedEvidence], book: RecordBook) -> list[MissingEvidence]:
    out: list[MissingEvidence] = []
    for item in expected:
        if item.source not in EVIDENCE_SOURCES:
            raise ValueError(f"not an evidence source: {item.source}")
        if item.record_id is not None:
            if item.record_id not in book:
                out.append(MissingEvidence("declared_record_absent", item.source, item.reason, item.record_id))
        elif not book.by_source(item.source):
            out.append(MissingEvidence("declared_source_absent", item.source, item.reason, None))
    return out


def auto_missing(timeline: FrozenTimeline, book: RecordBook) -> list[MissingEvidence]:
    """根据冻结内容自动推断的证据缺口。

    - 时间线引用了但记录簿中没有的记录号（断链）
    - 有遥控却没有对应操作票（无票操作嫌疑）
    - 有停电证据却没有 AMI 量测佐证
    - 有 AMI 量测却没有天气/班组记录覆盖事故窗口
    """
    out: list[MissingEvidence] = []
    for ref in timeline.refs():
        if ref not in book:
            out.append(MissingEvidence("dangling_reference", "timeline", "时间线引用的原始记录不存在", ref))

    telecontrol = book.by_source("telecontrol")
    orders = book.by_source("switching_order")
    for record in telecontrol:
        order_id = record.payload.get("order_id")
        matched = (
            any(r.payload.get("order_id") == order_id for r in orders)
            if order_id is not None
            else False
        )
        if order_id is None:
            out.append(
                MissingEvidence("telecontrol_without_order", "switching_order",
                                "遥控指令未关联操作票", record.record_id)
            )
        elif not matched:
            out.append(
                MissingEvidence("telecontrol_without_order", "switching_order",
                                f"操作票 {order_id} 未封存", record.record_id)
            )

    if book.by_source("outage") and not book.by_source("ami"):
        out.append(MissingEvidence("outage_without_ami", "ami", "停电证据缺少 AMI 量测佐证"))
    if book.by_source("ami") and not book.by_source("weather"):
        out.append(MissingEvidence("ami_without_weather", "weather", "事故窗口缺少天气记录"))
    if book.by_source("ami") and not book.by_source("crew"):
        out.append(MissingEvidence("ami_without_crew", "crew", "事故窗口缺少班组值守/处置记录"))
    return out


def detect_missing(
    timeline: FrozenTimeline,
    book: RecordBook,
    expected: list[ExpectedEvidence] | None = None,
) -> list[MissingEvidence]:
    if not timeline.frozen or not book.frozen:
        raise RuntimeError("freeze timeline and record book before checking evidence")
    merged: dict[tuple[str, str | None], MissingEvidence] = {}
    for item in declared_missing(list(expected or []), book) + auto_missing(timeline, book):
        merged.setdefault((item.code, item.record_id), item)
    return [merged[k] for k in sorted(merged, key=lambda x: (x[0], x[1] or ""))]
