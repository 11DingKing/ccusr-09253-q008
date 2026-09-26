"""外校事件到本校培养方案的确定性映射。

设计要点：

* 映射后的本地事件编号形如 ``ext:<sender>:<padded-seq>:<external-id>``，
  在同一 plan 内稳定且可重放；发送方乱序送达不改变映射结果。
* 跨校同一活动可能被两校各自上报（activity_id 相同或时间区间重叠），
  映射时把这类事件标记为 ``duplicate`` 且不写入本地事件流，由 replay
  的区间并集语义保证“同一活动不得在两校重复计时”。
* 规则快照（版本、活动类型映射、秒数上限）在批次接收时固化；规则升级
  后重放旧批次仍使用旧快照，只影响新批次。
* 无法采信的事件（未知活动类型、载荷非法、规则不覆盖）进入
  ``quarantined``，在裁决前保持待定，绝不计入学时。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..core.clock import to_utc

LOCAL_PREFIX = "ext"
_SEQ_WIDTH = 10

# 映射后允许进入本地 replay 的事件类型（与 EventType 对齐）。
_MAPPABLE_TYPES = frozenset({"checkin"})


@dataclass(frozen=True)
class RuleSnapshot:
    """批次接收时刻固化的互认规则。"""

    rule_id: str
    version: int
    sender_id: str
    receiver_id: str
    plan_version: str
    activity_map: dict[str, str]
    cap_seconds: int | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleSnapshot":
        return cls(
            rule_id=data["rule_id"],
            version=int(data["version"]),
            sender_id=data["sender_id"],
            receiver_id=data["receiver_id"],
            plan_version=data["plan_version"],
            activity_map=dict(data["activity_map"]),
            cap_seconds=data.get("cap_seconds"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "version": self.version,
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "plan_version": self.plan_version,
            "activity_map": dict(self.activity_map),
            "cap_seconds": self.cap_seconds,
        }


@dataclass
class MappedEvent:
    external_event_id: str
    sender_seq: int
    student_id: str
    status: str  # accepted / duplicate / quarantined
    local_event_id: str | None = None
    local_event: dict[str, Any] | None = None
    reason: str = ""
    occurred_at_utc: datetime | None = None


@dataclass
class MappingResult:
    mapped: list[MappedEvent] = field(default_factory=list)

    @property
    def accepted(self) -> list[MappedEvent]:
        return [m for m in self.mapped if m.status == "accepted"]

    @property
    def duplicates(self) -> list[MappedEvent]:
        return [m for m in self.mapped if m.status == "duplicate"]

    @property
    def quarantined(self) -> list[MappedEvent]:
        return [m for m in self.mapped if m.status == "quarantined"]


def local_event_id(sender_id: str, sender_seq: int, external_event_id: str) -> str:
    """生成确定性的本地命名空间事件编号。"""
    return f"{LOCAL_PREFIX}:{sender_id}:{sender_seq:0{_SEQ_WIDTH}d}:{external_event_id}"


def _parse_aware(value: Any) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("外校事件时间戳必须包含时区偏移")
    return to_utc(dt)


def _cap_interval(
    start_utc: datetime, end_utc: datetime, cap_seconds: int | None
) -> tuple[datetime, datetime, int]:
    """按秒数上限从区间起点截断，返回 (新起点, 新终点, 削去秒数)。"""
    total = int((end_utc - start_utc).total_seconds())
    if cap_seconds is None or total <= cap_seconds:
        return start_utc, end_utc, 0
    from datetime import timedelta

    return (
        start_utc,
        start_utc + timedelta(seconds=cap_seconds),
        total - cap_seconds,
    )


def _registered_interval(existing: dict[str, Any]) -> tuple[datetime, datetime] | None:
    try:
        return (
            _parse_aware(existing["payload"]["check_in_at"]),
            _parse_aware(existing["payload"]["check_out_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _subtract_intervals(
    start: datetime,
    end: datetime,
    blockers: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """从 [start, end) 中扣除 blockers 覆盖的部分，返回剩余区间列表。"""
    remaining = [(start, end)]
    for b_start, b_end in sorted(blockers):
        tail: list[tuple[datetime, datetime]] = []
        for r_start, r_end in remaining:
            if b_end <= r_start or b_start >= r_end:
                tail.append((r_start, r_end))
                continue
            if b_start > r_start:
                tail.append((r_start, min(r_end, b_start)))
            if b_end < r_end:
                tail.append((max(r_start, b_end), r_end))
        remaining = tail
        if not remaining:
            break
    return remaining


def map_batch(
    envelope_events: list[dict[str, Any]],
    rule: RuleSnapshot,
    *,
    already_registered: list[dict[str, Any]] | None = None,
) -> MappingResult:
    """把一个批次的外校事件映射为本地事件（纯函数）。

    ``already_registered`` 为该生已登记的本地/外校事件（含 payload），
    用于跨校同一活动判重；批次内部也互相判重。乱序输入安全：输出按
    ``sender_seq`` 排序，判重以已登记区间为阻塞集，结果与送达顺序无关
    （区间并集的交换律）。
    """
    registered: list[dict[str, Any]] = list(already_registered or [])
    result = MappingResult()

    ordered = sorted(
        enumerate(envelope_events),
        key=lambda pair: (int(pair[1].get("sender_seq", pair[0])), pair[0]),
    )

    for _, event in ordered:
        external_id = str(event.get("event_id", ""))
        seq = int(event.get("sender_seq", 0))
        student_id = str(event.get("student_id", ""))
        event_type = str(event.get("event_type", ""))
        payload = event.get("payload") or {}

        if not external_id or not student_id:
            result.mapped.append(
                MappedEvent(
                    external_event_id=external_id or f"<seq-{seq}>",
                    sender_seq=seq,
                    student_id=student_id,
                    status="quarantined",
                    reason="事件缺少 event_id 或 student_id",
                )
            )
            continue

        if event_type not in _MAPPABLE_TYPES:
            result.mapped.append(
                MappedEvent(
                    external_event_id=external_id,
                    sender_seq=seq,
                    student_id=student_id,
                    status="quarantined",
                    reason=f"事件类型 {event_type!r} 不在互认范围内",
                )
            )
            continue

        sender_activity_type = str(payload.get("activity_type", "regular"))
        mapped_type = rule.activity_map.get(sender_activity_type)
        if mapped_type is None:
            result.mapped.append(
                MappedEvent(
                    external_event_id=external_id,
                    sender_seq=seq,
                    student_id=student_id,
                    status="quarantined",
                    reason=(
                        f"规则 {rule.rule_id}v{rule.version} 未覆盖活动类型 "
                        f"{sender_activity_type!r}"
                    ),
                )
            )
            continue

        try:
            start_utc = _parse_aware(payload["check_in_at"])
            end_utc = _parse_aware(payload["check_out_at"])
        except (KeyError, TypeError, ValueError) as exc:
            result.mapped.append(
                MappedEvent(
                    external_event_id=external_id,
                    sender_seq=seq,
                    student_id=student_id,
                    status="quarantined",
                    reason=f"时间戳无法解析: {exc}",
                )
            )
            continue

        if end_utc <= start_utc:
            result.mapped.append(
                MappedEvent(
                    external_event_id=external_id,
                    sender_seq=seq,
                    student_id=student_id,
                    status="quarantined",
                    reason="check_out_at 必须晚于 check_in_at",
                )
            )
            continue

        incoming_activity_id = str(payload.get("activity_id", ""))
        same_activity_clash = next(
            (
                ex
                for ex in registered
                if ex["student_id"] == student_id
                and incoming_activity_id
                and str(ex.get("payload", {}).get("activity_id", ""))
                == incoming_activity_id
            ),
            None,
        )
        if same_activity_clash is not None:
            result.mapped.append(
                MappedEvent(
                    external_event_id=external_id,
                    sender_seq=seq,
                    student_id=student_id,
                    status="duplicate",
                    reason=(
                        "同一活动已由 "
                        f"{same_activity_clash.get('local_event_id') or same_activity_clash.get('event_id', '')} "
                        "计时"
                    ),
                    occurred_at_utc=start_utc,
                )
            )
            continue

        blockers = [
            interval
            for ex in registered
            if ex["student_id"] == student_id
            for interval in [_registered_interval(ex)]
            if interval is not None
        ]
        remainder = _subtract_intervals(start_utc, end_utc, blockers)
        if not remainder:
            result.mapped.append(
                MappedEvent(
                    external_event_id=external_id,
                    sender_seq=seq,
                    student_id=student_id,
                    status="duplicate",
                    reason="签到区间与已登记活动完全重叠",
                    occurred_at_utc=start_utc,
                )
            )
            continue

        # 多个剩余段时取最长段，保持“一个签到事件一个区间”的本地语义。
        clipped_start, clipped_end = max(
            remainder, key=lambda pair: int((pair[1] - pair[0]).total_seconds())
        )
        overlap_trimmed = int((end_utc - start_utc).total_seconds()) - int(
            (clipped_end - clipped_start).total_seconds()
        )

        capped_start, capped_end, cap_trimmed = _cap_interval(
            clipped_start, clipped_end, rule.cap_seconds
        )
        local_id = local_event_id(rule.sender_id, seq, external_id)
        reasons = []
        if overlap_trimmed:
            reasons.append(f"扣除跨校重叠 {overlap_trimmed} 秒")
        if cap_trimmed:
            reasons.append(f"按规则上限削去 {cap_trimmed} 秒")
        local_payload = {
            "activity_id": payload.get("activity_id", ""),
            "activity_type": mapped_type,
            "check_in_at": capped_start.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "check_out_at": capped_end.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            # 保留原始来源，供来源解释 API 使用。
            "source": {
                "sender_id": rule.sender_id,
                "external_event_id": external_id,
                "sender_timezone_hint": payload.get("timezone_hint", ""),
                "rule_id": rule.rule_id,
                "rule_version": rule.version,
                "overlap_trimmed_seconds": overlap_trimmed,
                "cap_trimmed_seconds": cap_trimmed,
            },
        }
        mapped = MappedEvent(
            external_event_id=external_id,
            sender_seq=seq,
            student_id=student_id,
            status="accepted",
            local_event_id=local_id,
            local_event={
                "event_id": local_id,
                "event_type": event_type,
                "student_id": student_id,
                "payload": local_payload,
            },
            reason="；".join(reasons),
            occurred_at_utc=start_utc,
        )
        result.mapped.append(mapped)
        registered.append(
            {
                "student_id": student_id,
                "payload": local_payload,
                "event_id": external_id,
                "local_event_id": local_id,
            }
        )

    return result
