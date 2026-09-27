"""核心展开逻辑测试（仅依赖标准库 + pytest）。"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.scheduler import (
    Instance,
    StaffingGap,
    classify,
    expand_schedule,
    find_overlaps,
    merge_intervals,
    occupancy_by_day,
    staffing_gaps,
)

NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")


def test_classify_normal():
    kind, payload = classify(datetime(2026, 3, 7, 2, 30), NY)
    assert kind == "normal"
    assert payload[0] == datetime(2026, 3, 7, 7, 30, tzinfo=timezone.utc)


def test_classify_gap():
    # 2026-03-08 02:30 在纽约不存在（02:00 -> 03:00）
    kind, gap = classify(datetime(2026, 3, 8, 2, 30), NY)
    assert kind == "gap"
    assert gap == timedelta(hours=1)


def test_classify_fold():
    # 2026-11-01 01:30 在纽约出现两次
    kind, payload = classify(datetime(2026, 11, 1, 1, 30), NY)
    assert kind == "fold"
    first, second = payload
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)  # EDT
    assert second == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)  # EST


def test_gap_skip():
    instances, skipped = expand_schedule(
        tz=NY,
        start_date=date(2026, 3, 8),
        end_date=date(2026, 3, 8),
        weekdays={0, 1, 2, 3, 4, 5, 6},
        local_time=time(2, 30),
        gap_strategy="skip",
        fold_strategy="first",
    )
    assert instances == []
    assert skipped == [date(2026, 3, 8)]


def test_gap_shift_forward():
    instances, skipped = expand_schedule(
        tz=NY,
        start_date=date(2026, 3, 8),
        end_date=date(2026, 3, 8),
        weekdays={6},
        local_time=time(2, 30),
        gap_strategy="shift_forward",
        fold_strategy="first",
    )
    assert skipped == []
    assert len(instances) == 1
    inst = instances[0]
    assert inst.reason == "gap_shifted_forward"
    # 02:30 + 1h = 03:30 EDT -> 07:30 UTC
    assert inst.utc == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)
    assert inst.utc.astimezone(NY).replace(tzinfo=None) == datetime(2026, 3, 8, 3, 30)


def test_fold_strategies():
    base = dict(
        tz=NY,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        weekdays={6},
        local_time=time(1, 30),
        gap_strategy="skip",
    )
    first, _ = expand_schedule(fold_strategy="first", **base)
    assert [i.reason for i in first] == ["fold_first"]
    assert first[0].utc == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)

    second, _ = expand_schedule(fold_strategy="second", **base)
    assert [i.reason for i in second] == ["fold_second"]
    assert second[0].utc == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)

    both, _ = expand_schedule(fold_strategy="both", **base)
    assert [i.reason for i in both] == ["fold_first", "fold_second"]
    assert both[0].utc < both[1].utc


def test_weekday_filter_and_utc_sort():
    # 2026-03-01 是周日；每周一、三 09:00，柏林时间
    instances, skipped = expand_schedule(
        tz=BERLIN,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 15),
        weekdays={0, 2},
        local_time=time(9, 0),
        gap_strategy="skip",
        fold_strategy="first",
    )
    assert skipped == []
    assert len(instances) == 4  # 3/2, 3/4, 3/9, 3/11
    utcs = [i.utc for i in instances]
    assert utcs == sorted(utcs)
    assert len(set(utcs)) == len(utcs)  # 已去重
    assert all(i.reason == "normal" for i in instances)


def test_berlin_gap_shift():
    # 柏林 2026-03-29 02:30 不存在（02:00 -> 03:00）
    instances, skipped = expand_instances = expand_schedule(
        tz=BERLIN,
        start_date=date(2026, 3, 29),
        end_date=date(2026, 3, 29),
        weekdays={6},
        local_time=time(2, 30),
        gap_strategy="shift_forward",
        fold_strategy="both",
    )
    assert skipped == []
    assert len(instances) == 1
    inst = instances[0]
    assert inst.reason == "gap_shifted_forward"
    # 02:30 + 1h = 03:30 CEST -> 01:30 UTC
    assert inst.utc == datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc)


def _inst(hh: int, mm: int = 0) -> Instance:
    return Instance(datetime(2026, 1, 1, hh, mm, tzinfo=timezone.utc), "normal")


def test_find_overlaps_basic():
    # A [08:00, 09:00)，B [08:30, 09:30) -> 重叠 [08:30, 09:00)
    [ov] = find_overlaps(
        a_instances=[_inst(8)],
        a_duration_minutes=60,
        b_instances=[_inst(8, 30)],
        b_duration_minutes=60,
    )
    assert ov.a_start == datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    assert ov.b_start == datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc)
    assert ov.start == datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc)
    assert ov.end == datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    assert ov.minutes == 30


def test_find_overlaps_boundary_touch_not_conflict():
    # 半开区间：A [08:00, 09:00) 与 B [09:00, 10:00) 边界相接，不算冲突
    assert (
        find_overlaps(
            a_instances=[_inst(8)],
            a_duration_minutes=60,
            b_instances=[_inst(9)],
            b_duration_minutes=60,
        )
        == []
    )


def test_find_overlaps_containment():
    # B 完全包含于 A
    [ov] = find_overlaps(
        a_instances=[_inst(8)],
        a_duration_minutes=120,
        b_instances=[_inst(8, 30)],
        b_duration_minutes=30,
    )
    assert ov.start == datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc)
    assert ov.end == datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    assert ov.minutes == 30


def test_find_overlaps_multiple_active_per_side():
    # A 侧两个并行区间都与同一 B 区间重叠，结果按重叠起点排序
    overlaps = find_overlaps(
        a_instances=[_inst(8), _inst(8, 30)],
        a_duration_minutes=60,
        b_instances=[_inst(8, 15)],
        b_duration_minutes=60,
    )
    assert len(overlaps) == 2
    assert [o.start for o in overlaps] == [
        datetime(2026, 1, 1, 8, 15, tzinfo=timezone.utc),
        datetime(2026, 1, 1, 8, 30, tzinfo=timezone.utc),
    ]
    assert all(o.minutes == 45 for o in overlaps)


def test_find_overlaps_empty_side():
    assert (
        find_overlaps(
            a_instances=[],
            a_duration_minutes=60,
            b_instances=[_inst(8)],
            b_duration_minutes=60,
        )
        == []
    )


# --- merge_intervals / occupancy_by_day ------------------------------------

UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _utc(day: int, hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(
        days=day - 1, hours=hh, minutes=mm
    )


def test_merge_intervals_overlapping_and_touching():
    merged = merge_intervals(
        [
            (_utc(1, 10), _utc(1, 11)),  # 与上一段相接 -> 并入
            (_utc(1, 8), _utc(1, 9)),
            (_utc(1, 8, 30), _utc(1, 10)),  # 与第一段相交 -> 并入
            (_utc(1, 12), _utc(1, 13)),  # 不相接，独立成段
        ]
    )
    assert merged == [(_utc(1, 8), _utc(1, 11)), (_utc(1, 12), _utc(1, 13))]


def test_merge_intervals_empty():
    assert merge_intervals([]) == []


def test_occupancy_by_day_basic():
    days = occupancy_by_day(
        instances=[Instance(_utc(1, 8), "normal")],
        duration_minutes=90,
        report_tz=ZoneInfo("UTC"),
    )
    assert days == [(date(2026, 1, 1), 90.0)]


def test_occupancy_by_day_cross_midnight_split():
    # 23:00 UTC + 120min -> 当日 60 分钟 + 次日 60 分钟
    days = occupancy_by_day(
        instances=[Instance(_utc(1, 23), "normal")],
        duration_minutes=120,
        report_tz=ZoneInfo("UTC"),
    )
    assert days == [(date(2026, 1, 1), 60.0), (date(2026, 1, 2), 60.0)]


def test_occupancy_by_day_report_timezone_shifts_date():
    # 2026-01-01 23:30 UTC + 60min，在上海（UTC+8）是 01-02 07:30-08:30
    days = occupancy_by_day(
        instances=[Instance(_utc(1, 23, 30), "normal")],
        duration_minutes=60,
        report_tz=SHANGHAI,
    )
    assert days == [(date(2026, 1, 2), 60.0)]


def test_occupancy_by_day_empty_instances():
    assert (
        occupancy_by_day(instances=[], duration_minutes=60, report_tz=ZoneInfo("UTC"))
        == []
    )


def test_occupancy_by_day_fold_both_counts_overlap_once():
    # 纽约 2026-11-01 01:30 出现两次：05:30 / 06:30 UTC；各 90 分钟
    # -> [05:30,07:00) 与 [06:30,08:00) 重叠，合并后 [05:30,08:00) = 150 分钟
    instances, _ = expand_schedule(
        tz=NY,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        weekdays={6},
        local_time=time(1, 30),
        gap_strategy="skip",
        fold_strategy="both",
    )
    days = occupancy_by_day(
        instances=instances, duration_minutes=90, report_tz=NY
    )
    assert days == [(date(2026, 11, 1), 150.0)]


def test_occupancy_by_day_spring_forward_real_minutes():
    # 纽约 2026-03-07 23:00 EST = 03-08 04:00 UTC，180 分钟
    # -> 03-08 03:00 EDT（春跳当日仅 23 小时，仍按真实 UTC 时长累计）
    # 本地午夜 03-08 00:00 EST = 05:00 UTC：03-07 得 60 分钟，03-08 得 120 分钟
    instances, _ = expand_schedule(
        tz=NY,
        start_date=date(2026, 3, 7),
        end_date=date(2026, 3, 7),
        weekdays={5},
        local_time=time(23, 0),
        gap_strategy="skip",
        fold_strategy="first",
    )
    days = occupancy_by_day(
        instances=instances, duration_minutes=180, report_tz=NY
    )
    assert days == [(date(2026, 3, 7), 60.0), (date(2026, 3, 8), 120.0)]


def test_occupancy_by_day_fall_back_day_real_minutes():
    # 纽约 2026-11-01 00:30 EDT = 04:30 UTC，120 分钟 -> 02:30 EST
    # 跨越秋回（当日 25 小时），仍按真实 120 分钟计入 11-01
    instances, _ = expand_schedule(
        tz=NY,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 1),
        weekdays={6},
        local_time=time(0, 30),
        gap_strategy="skip",
        fold_strategy="first",
    )
    days = occupancy_by_day(
        instances=instances, duration_minutes=120, report_tz=NY
    )
    assert days == [(date(2026, 11, 1), 120.0)]


# --- staffing_gaps ----------------------------------------------------------


def test_staffing_gaps_basic_sweep():
    # alice [08:00,09:00)，bob [08:30,09:30)；min_staff=2
    gaps = staffing_gaps(
        window_start=_utc(1, 0),
        window_end=_utc(2, 0),
        min_staff=2,
        employee_intervals={
            "alice": [(_utc(1, 8), _utc(1, 9))],
            "bob": [(_utc(1, 8, 30), _utc(1, 9, 30))],
        },
    )
    assert gaps == [
        StaffingGap(_utc(1, 0), _utc(1, 8), (), 2),
        StaffingGap(_utc(1, 8), _utc(1, 8, 30), ("alice",), 1),
        StaffingGap(_utc(1, 9), _utc(1, 9, 30), ("bob",), 1),
        StaffingGap(_utc(1, 9, 30), _utc(2, 0), (), 2),
    ]


def test_staffing_gaps_clips_to_window_and_merges_per_employee():
    gaps = staffing_gaps(
        window_start=_utc(1, 0),
        window_end=_utc(2, 0),
        min_staff=1,
        employee_intervals={
            "alice": [
                (_utc(0, 23), _utc(1, 1)),  # 裁剪到 [00:00, 01:00)
                (_utc(1, 8), _utc(1, 9)),
                (_utc(1, 8, 30), _utc(1, 10)),  # 与上一条相交 -> 合并
                (_utc(1, 10), _utc(1, 11)),  # 与合并段相接 -> 并入
            ],
        },
    )
    assert gaps == [
        StaffingGap(_utc(1, 1), _utc(1, 8), (), 1),
        StaffingGap(_utc(1, 11), _utc(2, 0), (), 1),
    ]


def test_staffing_gaps_same_instant_events_processed_together():
    # alice 09:00 下岗与 bob 09:00 上岗同一时刻一起处理，不产生零长缺口
    assert (
        staffing_gaps(
            window_start=_utc(1, 8),
            window_end=_utc(1, 10),
            min_staff=1,
            employee_intervals={
                "alice": [(_utc(1, 8), _utc(1, 9))],
                "bob": [(_utc(1, 9), _utc(1, 10))],
            },
        )
        == []
    )


def test_staffing_gaps_on_duty_sorted_and_missing():
    gaps = staffing_gaps(
        window_start=_utc(1, 8),
        window_end=_utc(1, 10),
        min_staff=3,
        employee_intervals={
            "zoe": [(_utc(1, 8), _utc(1, 10))],
            "amy": [(_utc(1, 9), _utc(1, 10))],
        },
    )
    assert gaps == [
        StaffingGap(_utc(1, 8), _utc(1, 9), ("zoe",), 2),
        StaffingGap(_utc(1, 9), _utc(1, 10), ("amy", "zoe"), 1),
    ]


def test_staffing_gaps_empty_when_staffed():
    # 单人在岗区间覆盖整个窗口（含越界部分被裁剪）
    assert (
        staffing_gaps(
            window_start=_utc(1, 0),
            window_end=_utc(2, 0),
            min_staff=1,
            employee_intervals={"alice": [(_utc(0, 12), _utc(2, 12))]},
        )
        == []
    )


def test_staffing_gaps_ignores_intervals_outside_window():
    gaps = staffing_gaps(
        window_start=_utc(1, 0),
        window_end=_utc(2, 0),
        min_staff=1,
        employee_intervals={"alice": [(_utc(3, 0), _utc(3, 1))]},
    )
    assert gaps == [StaffingGap(_utc(1, 0), _utc(2, 0), (), 1)]
