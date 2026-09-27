"""核心展开逻辑：UTC 往返校验区分正常 / 跳时（gap）/ 叠时（fold）。

往返校验原理（PEP 495）：对一个朴素本地时刻分别按 fold=0 / fold=1
赋予时区，换算到 UTC 后再换算回来；能还原原时刻的解释才是真实存在的。

- 两种 fold 都有效且 UTC 相同 -> 正常时刻
- 两种 fold 都有效但 UTC 不同 -> 叠时（秋回，本地时刻出现两次）
- 仅一种 fold 有效          -> 正常时刻（罕见过渡边界）
- 两种 fold 都无效          -> 跳时（春跳，本地时刻不存在）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

Kind = Literal["normal", "fold", "gap"]


@dataclass(frozen=True)
class Instance:
    """一个展开出的日程实例（以 UTC 时刻为唯一标识）。"""

    utc: datetime
    reason: str


@dataclass(frozen=True)
class Overlap:
    """一次跨侧重叠：双方实例起点与重叠区间（均为 UTC）。"""

    a_start: datetime
    b_start: datetime
    start: datetime
    end: datetime

    @property
    def minutes(self) -> float:
        """重叠时长（分钟）。"""
        return (self.end - self.start).total_seconds() / 60


def _valid_interpretations(naive: datetime, tz: ZoneInfo) -> dict[int, datetime]:
    """返回 {fold: UTC 时刻}，仅保留能通过 UTC 往返校验的解释。"""
    valid: dict[int, datetime] = {}
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(tz).replace(tzinfo=None) == naive:
            valid[fold] = utc
    return valid


def classify(
    naive: datetime, tz: ZoneInfo
) -> tuple[Literal["normal", "fold"], list[datetime]] | tuple[Literal["gap"], timedelta]:
    """对朴素本地时刻分类。

    返回 ("normal", [utc])、("fold", [first_utc, second_utc])
    或 ("gap", gap_delta)。gap_delta 为跳变差值（前移量），
    即 naive + gap_delta 是政策上“顺延”后的本地时刻。
    """
    valid = _valid_interpretations(naive, tz)
    if len(valid) == 2:
        if valid[0] == valid[1]:
            return "normal", [valid[0]]
        # fold=0 对应过渡前偏移，UTC 更早，即“第一次出现”
        return "fold", [valid[0], valid[1]]
    if len(valid) == 1:
        return "normal", [next(iter(valid.values()))]

    # 跳时：fold=0 按过渡前偏移解释，fold=1 按过渡后偏移解释，
    # 两者之差即为时钟向前跳变的差值。
    utc_pre = naive.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
    utc_post = naive.replace(tzinfo=tz, fold=1).astimezone(timezone.utc)
    return "gap", utc_pre - utc_post


def expand_schedule(
    *,
    tz: ZoneInfo,
    start_date: date,
    end_date: date,
    weekdays: set[int],
    local_time: time,
    gap_strategy: str,
    fold_strategy: str,
) -> tuple[list[Instance], list[date]]:
    """展开日程。

    返回 (实例列表, 被跳过的本地日期列表)。实例按 UTC 去重（保留先出现者）
    并按 UTC 稳定排序。
    """
    instances: list[Instance] = []
    skipped: list[date] = []

    day = start_date
    one_day = timedelta(days=1)
    while day <= end_date:
        if day.weekday() in weekdays:
            naive = datetime.combine(day, local_time)
            kind, payload = classify(naive, tz)

            if kind == "normal":
                instances.append(Instance(payload[0], "normal"))
            elif kind == "fold":
                first_utc, second_utc = payload
                if fold_strategy in ("first", "both"):
                    instances.append(Instance(first_utc, "fold_first"))
                if fold_strategy in ("second", "both"):
                    instances.append(Instance(second_utc, "fold_second"))
            else:  # gap
                gap = payload
                if gap_strategy == "shift_forward" and gap > timedelta(0):
                    shifted = naive + gap
                    kind2, payload2 = classify(shifted, tz)
                    if kind2 == "gap":  # 极端情况下前移后仍不存在，放弃
                        skipped.append(day)
                    else:
                        instances.append(
                            Instance(payload2[0], "gap_shifted_forward")
                        )
                else:
                    skipped.append(day)
        day += one_day

    # 按 UTC 去重（保留先出现者），再按 UTC 稳定排序
    deduped: dict[datetime, Instance] = {}
    for inst in instances:
        deduped.setdefault(inst.utc, inst)
    ordered = sorted(deduped.values(), key=lambda i: i.utc)
    return ordered, skipped


def find_overlaps(
    *,
    a_instances: list[Instance],
    a_duration_minutes: int,
    b_instances: list[Instance],
    b_duration_minutes: int,
) -> list[Overlap]:
    """扫描线枚举两个日程的全部跨侧重叠。

    每个实例解释为半开 UTC 区间 [start, start+duration)；边界相接
    （一侧终点 == 另一侧起点）不算重叠。返回按重叠起点稳定排序的结果。
    """
    a_span = timedelta(minutes=a_duration_minutes)
    b_span = timedelta(minutes=b_duration_minutes)
    # (起点, 终点, 侧)：0=A，1=B；按起点排序，起点相同则终点早的、A 侧的在前
    events = [(i.utc, i.utc + a_span, 0) for i in a_instances]
    events += [(i.utc, i.utc + b_span, 1) for i in b_instances]
    events.sort()

    # 每侧的活动区间（起点, 终点）；新区间到达时先淘汰 end <= 起点的区间
    active: list[list[tuple[datetime, datetime]]] = [[], []]
    overlaps: list[Overlap] = []
    for start, end, side in events:
        active[0] = [iv for iv in active[0] if iv[1] > start]
        active[1] = [iv for iv in active[1] if iv[1] > start]
        for other_start, other_end in active[1 - side]:
            ov_start = max(start, other_start)
            ov_end = min(end, other_end)
            if ov_start < ov_end:  # 半开区间：相接（ov_start == ov_end）不算冲突
                a_start, b_start = (start, other_start) if side == 0 else (other_start, start)
                overlaps.append(Overlap(a_start, b_start, ov_start, ov_end))
        active[side].append((start, end))

    overlaps.sort(key=lambda o: (o.start, o.end, o.a_start, o.b_start))
    return overlaps


def merge_intervals(
    intervals: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """合并相交或相接的半开区间，返回按起点排序的两两不相交区间。"""
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:  # 相交或相接：并入上一段
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def occupancy_by_day(
    *,
    instances: list[Instance],
    duration_minutes: int,
    report_tz: ZoneInfo,
) -> list[tuple[date, float]]:
    """按报告时区的本地日界统计逐日占用分钟数。

    每个实例解释为半开 UTC 区间 [utc, utc+duration)，先合并相交或相接的
    区间（重复时刻产生的重叠只计一次），再按 report_tz 的本地日界切分。
    时长按真实 UTC 流逝累计：跨午夜自然落到相邻两日，夏令时切换日仍按
    实际经过的分钟数计。返回 [(本地日期, 分钟数)]，按日期升序。
    """
    span = timedelta(minutes=duration_minutes)
    merged = merge_intervals((i.utc, i.utc + span) for i in instances)

    minutes_by_day: dict[date, float] = {}
    one_day = timedelta(days=1)
    for start, end in merged:
        cursor = start
        while cursor < end:
            local = cursor.astimezone(report_tz)
            # 下一个本地午夜对应的 UTC 时刻（切换日不足/超过 24h 均按实际）
            next_midnight = datetime.combine(
                local.date() + one_day, time.min, tzinfo=report_tz
            ).astimezone(timezone.utc)
            segment_end = min(end, next_midnight)
            minutes = (segment_end - cursor).total_seconds() / 60
            minutes_by_day[local.date()] = (
                minutes_by_day.get(local.date(), 0.0) + minutes
            )
            cursor = segment_end
    return sorted(minutes_by_day.items())


@dataclass(frozen=True)
class StaffingGap:
    """一段人数不足的区间：UTC 起止、在岗员工 ID（升序）与缺少人数。"""

    start: datetime
    end: datetime
    on_duty: tuple[str, ...]
    missing: int


def staffing_gaps(
    *,
    window_start: datetime,
    window_end: datetime,
    min_staff: int,
    employee_intervals: dict[str, list[tuple[datetime, datetime]]],
) -> list[StaffingGap]:
    """扫描线统计窗口 [window_start, window_end) 内的在岗人数缺口。

    每名员工的半开区间先裁剪到窗口，相交或相接者合并（同一员工只计一人）；
    同一时刻的上下岗事件一起处理。返回人数不足的非空区间：在岗集合相同的
    相邻缺员段已合并，结果按起点排序。
    """
    events: list[tuple[datetime, bool, str]] = []  # (时刻, 是否下岗, 员工 ID)
    for emp_id, intervals in employee_intervals.items():
        clipped = [
            (max(start, window_start), min(end, window_end))
            for start, end in intervals
        ]
        for start, end in merge_intervals(iv for iv in clipped if iv[0] < iv[1]):
            events.append((start, False, emp_id))  # 上岗
            events.append((end, True, emp_id))  # 下岗
    events.sort()

    gaps: list[StaffingGap] = []
    on_duty: set[str] = set()
    prev = window_start
    i = 0
    while i < len(events):
        t = events[i][0]
        if prev < t and len(on_duty) < min_staff:
            gaps.append(
                StaffingGap(prev, t, tuple(sorted(on_duty)), min_staff - len(on_duty))
            )
        while i < len(events) and events[i][0] == t:  # 同一时刻的事件一起处理
            _, is_off, emp_id = events[i]
            if is_off:
                on_duty.discard(emp_id)
            else:
                on_duty.add(emp_id)
            i += 1
        prev = t
    if prev < window_end and len(on_duty) < min_staff:
        gaps.append(
            StaffingGap(
                prev, window_end, tuple(sorted(on_duty)), min_staff - len(on_duty)
            )
        )

    # 在岗集合相同的相邻缺员段合并（分组扫描下的防御性收尾）
    merged: list[StaffingGap] = []
    for gap in gaps:
        last = merged[-1] if merged else None
        if last and last.end == gap.start and last.on_duty == gap.on_duty:
            merged[-1] = StaffingGap(last.start, gap.end, last.on_duty, last.missing)
        else:
            merged.append(gap)
    return merged


def utc_iso(dt: datetime) -> str:
    """把 UTC 时刻格式化为 Z 结尾的 ISO 8601 字符串。"""
    return dt.isoformat().replace("+00:00", "Z")


def _format_offset(td: timedelta) -> str:
    total = int(td.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    if ss:
        return f"{sign}{hh:02d}:{mm:02d}:{ss:02d}"
    return f"{sign}{hh:02d}:{mm:02d}"


def instance_payload(inst: Instance, tz: ZoneInfo) -> dict:
    """把实例格式化为 API 响应字段（本地时间 / 偏移 / UTC / 决策原因）。"""
    local = inst.utc.astimezone(tz)
    return {
        "local": local.isoformat(),
        "offset": _format_offset(local.utcoffset()),
        "utc": utc_iso(inst.utc),
        "reason": inst.reason,
    }
