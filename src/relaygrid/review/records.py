from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

# 复盘关联的六类运行原始证据
EVIDENCE_SOURCES = (
    "switching_order",  # 操作票
    "telecontrol",      # 遥控
    "crew",             # 班组
    "weather",          # 天气
    "ami",              # AMI 量测
    "outage",           # 停电证据
)
EXTRA_SOURCES = ("scada", "logbook", "clearance")
ALL_SOURCES = EVIDENCE_SOURCES + EXTRA_SOURCES


class FrozenError(RuntimeError):
    """封存后的对象不允许再写入。"""


def _canonical(obj: Any) -> Any:
    if isinstance(obj, Enum):
        obj = obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = dataclasses.asdict(obj)
    if isinstance(obj, dict):
        return {str(k): _canonical(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_canonical(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        converted = [_canonical(v) for v in obj]
        return sorted(converted, key=lambda v: json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    if obj is None or isinstance(obj, (str, bool, int, float)):
        return obj
    raise TypeError(f"cannot canonicalize value of type {type(obj).__name__}")


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(
        _canonical(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def to_jsonable(obj: Any) -> Any:
    """返回可直接 json 序列化的规范化表示（dataclass/Enum/集合均已展开）。"""
    return _canonical(obj)


def digest_of(obj: Any) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


@dataclass(frozen=True)
class RawRecord:
    """一条运行原始记录在冻结时刻的快照。"""

    record_id: str
    source: str
    captured_at: float
    payload: dict
    digest: str = field(init=False)

    def __post_init__(self):
        if not self.record_id:
            raise ValueError("record_id required")
        if not isinstance(self.payload, dict):
            raise TypeError("payload must be a dict")
        envelope = {
            "record_id": self.record_id,
            "source": self.source,
            "captured_at": self.captured_at,
            "payload": self.payload,
        }
        # 构造时立即规范化，发现不可序列化内容应早失败
        object.__setattr__(self, "digest", digest_of(envelope))


def make_record(record_id: str, source: str, captured_at: float, payload: dict) -> RawRecord:
    return RawRecord(record_id, source, captured_at, payload)


@dataclass
class RecordBook:
    """只追加的原始记录簿；freeze 后任何写入都抛 FrozenError。"""

    _records: dict[str, RawRecord] = field(default_factory=dict)
    frozen: bool = False

    def append(self, record: RawRecord) -> RawRecord:
        if self.frozen:
            raise FrozenError(f"record book frozen; cannot add {record.record_id}")
        if record.record_id in self._records:
            raise ValueError(f"duplicate record {record.record_id}")
        self._records[record.record_id] = record
        return record

    def freeze(self) -> str:
        self.frozen = True
        return self.root_digest()

    def get(self, record_id: str) -> RawRecord:
        return self._records[record_id]

    def __contains__(self, record_id: str) -> bool:
        return record_id in self._records

    def __iter__(self):
        return iter(self._records.values())

    def __len__(self) -> int:
        return len(self._records)

    def records(self) -> tuple[RawRecord, ...]:
        return tuple(self._records[k] for k in sorted(self._records))

    def by_source(self, source: str) -> tuple[RawRecord, ...]:
        return tuple(r for r in self.records() if r.source == source)

    def root_digest(self) -> str:
        entries = [{"record_id": r.record_id, "digest": r.digest} for r in self.records()]
        return digest_of({"alg": "sha256", "records": entries})


# --------------------------------------------------------------------------- #
# 适配器：把各领域对象在冻结时刻的状态固化为 RawRecord，之后原对象再怎么变化
# 都不会影响复盘快照。
# --------------------------------------------------------------------------- #

def record_from_switching_order(record_id: str, order, captured_at: float) -> RawRecord:
    current = order.current
    return RawRecord(
        record_id,
        "switching_order",
        captured_at,
        {
            "order_id": order.order_id,
            "revision": current.revision,
            "state": current.state,
            "steps": current.steps,
            "author": current.author,
            "checker": current.checker,
            "approver": current.approver,
            "acknowledgements": order.acknowledgements,
        },
    )


def record_from_command(record_id: str, command, captured_at: float, order_id: str | None = None) -> RawRecord:
    payload = dataclasses.asdict(command)
    if order_id is not None:
        payload["order_id"] = order_id
    return RawRecord(record_id, "telecontrol", captured_at, payload)


def record_from_command_result(record_id: str, result, captured_at: float) -> RawRecord:
    return RawRecord(record_id, "telecontrol", captured_at, dataclasses.asdict(result))


def record_from_shift(record_id: str, shift, captured_at: float) -> RawRecord:
    return RawRecord(record_id, "crew", captured_at, dataclasses.asdict(shift))


def record_from_assignment(record_id: str, assignment, captured_at: float) -> RawRecord:
    return RawRecord(record_id, "crew", captured_at, dataclasses.asdict(assignment))


def record_from_weather(record_id: str, observation, captured_at: float) -> RawRecord:
    return RawRecord(record_id, "weather", captured_at, dataclasses.asdict(observation))


def record_from_meter_observation(record_id: str, observation, captured_at: float) -> RawRecord:
    return RawRecord(record_id, "ami", captured_at, dataclasses.asdict(observation))


def record_from_outage_evidence(record_id: str, evidence, captured_at: float) -> RawRecord:
    return RawRecord(record_id, "outage", captured_at, dataclasses.asdict(evidence))


def record_from_outage_interval(record_id: str, interval, captured_at: float) -> RawRecord:
    return RawRecord(record_id, "outage", captured_at, dataclasses.asdict(interval))


def record_from_telemetry(record_id: str, sample, captured_at: float) -> RawRecord:
    return RawRecord(record_id, "scada", captured_at, dataclasses.asdict(sample))


def record_from_logbook_event(record_id: str, event, captured_at: float) -> RawRecord:
    return RawRecord(record_id, "logbook", captured_at, dataclasses.asdict(event))


def record_from_clearance(record_id: str, clearance, captured_at: float) -> RawRecord:
    return RawRecord(record_id, "clearance", captured_at, dataclasses.asdict(clearance))


def seal_records(records: Iterable[RawRecord]) -> RecordBook:
    book = RecordBook()
    for record in records:
        book.append(record)
    book.freeze()
    return book
