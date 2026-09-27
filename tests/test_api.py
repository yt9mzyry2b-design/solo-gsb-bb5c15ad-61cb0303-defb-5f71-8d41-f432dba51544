"""API 层测试：正常展开与 422 校验。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BASE = {
    "timezone": "America/New_York",
    "start_date": "2026-03-08",
    "end_date": "2026-03-08",
    "weekdays": [6],
    "local_time": "02:30",
    "gap_strategy": "shift_forward",
    "fold_strategy": "both",
}


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_expand_gap_shift_forward():
    resp = client.post("/expand", json=BASE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["skipped_count"] == 0
    inst = body["instances"][0]
    assert inst["local"] == "2026-03-08T03:30:00-04:00"
    assert inst["offset"] == "-04:00"
    assert inst["utc"] == "2026-03-08T07:30:00Z"
    assert inst["reason"] == "gap_shifted_forward"


def test_expand_gap_skip():
    resp = client.post("/expand", json={**BASE, "gap_strategy": "skip"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["skipped_count"] == 1
    assert body["skipped_dates"] == ["2026-03-08"]


def test_expand_fold_both():
    payload = {
        **BASE,
        "start_date": "2026-11-01",
        "end_date": "2026-11-01",
        "local_time": "01:30",
    }
    resp = client.post("/expand", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    first, second = body["instances"]
    assert first["reason"] == "fold_first"
    assert first["offset"] == "-04:00"
    assert first["utc"] == "2026-11-01T05:30:00Z"
    assert second["reason"] == "fold_second"
    assert second["offset"] == "-05:00"
    assert second["utc"] == "2026-11-01T06:30:00Z"
    # 本地时钟面相同，但为两个不同的 UTC 实例
    assert first["local"] == "2026-11-01T01:30:00-04:00"
    assert second["local"] == "2026-11-01T01:30:00-05:00"


def test_invalid_timezone_422():
    resp = client.post("/expand", json={**BASE, "timezone": "Not/AZone"})
    assert resp.status_code == 422


def test_reversed_range_422():
    resp = client.post(
        "/expand",
        json={**BASE, "start_date": "2026-04-01", "end_date": "2026-03-01"},
    )
    assert resp.status_code == 422


def test_range_over_366_days_422():
    resp = client.post(
        "/expand",
        json={**BASE, "start_date": "2026-01-01", "end_date": "2027-01-03"},
    )
    assert resp.status_code == 422


def test_range_exactly_366_days_ok():
    resp = client.post(
        "/expand",
        json={
            **BASE,
            "start_date": "2026-01-01",
            "end_date": "2027-01-01",  # 相差 366 天
            "local_time": "09:00",
        },
    )
    assert resp.status_code == 200


def test_invalid_weekday_422():
    resp = client.post("/expand", json={**BASE, "weekdays": [7]})
    assert resp.status_code == 422


def test_empty_weekdays_422():
    resp = client.post("/expand", json={**BASE, "weekdays": []})
    assert resp.status_code == 422


# --- POST /overlaps ---------------------------------------------------------

# A 侧：柏林 2026-03-02（周一）09:00 CET = 08:00 UTC，持续 60 分钟
OVERLAP_A = {
    "timezone": "Europe/Berlin",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "09:00",
    "duration_minutes": 60,
}
# B 侧：纽约同日 03:30 EST = 08:30 UTC，持续 60 分钟
OVERLAP_B = {
    "timezone": "America/New_York",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "03:30",
    "duration_minutes": 60,
}


def test_overlaps_basic():
    resp = client.post("/overlaps", json={"a": OVERLAP_A, "b": OVERLAP_B})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    [ov] = body["overlaps"]
    assert ov["a_start_utc"] == "2026-03-02T08:00:00Z"
    assert ov["b_start_utc"] == "2026-03-02T08:30:00Z"
    assert ov["overlap_start_utc"] == "2026-03-02T08:30:00Z"
    assert ov["overlap_end_utc"] == "2026-03-02T09:00:00Z"
    assert ov["minutes"] == 30


def test_overlaps_boundary_touch_not_conflict():
    # B 改为 04:00 EST = 09:00 UTC，与 A 的终点 09:00 UTC 恰好相接
    resp = client.post(
        "/overlaps",
        json={"a": OVERLAP_A, "b": {**OVERLAP_B, "local_time": "04:00"}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "overlaps": []}


def test_overlaps_sorted_multiple():
    # 两侧都扩到周一、周二；A 09:00+120min，B 04:30 EST+60min = 09:30 UTC
    a = {
        **OVERLAP_A,
        "end_date": "2026-03-03",
        "weekdays": [0, 1],
        "duration_minutes": 120,
    }
    b = {
        **OVERLAP_B,
        "end_date": "2026-03-03",
        "weekdays": [0, 1],
        "local_time": "04:30",
    }
    resp = client.post("/overlaps", json={"a": a, "b": b})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    starts = [ov["overlap_start_utc"] for ov in body["overlaps"]]
    assert starts == sorted(starts)
    assert starts == ["2026-03-02T09:30:00Z", "2026-03-03T09:30:00Z"]
    assert all(ov["overlap_end_utc"].endswith("T10:00:00Z") for ov in body["overlaps"])
    assert all(ov["minutes"] == 30 for ov in body["overlaps"])


def test_overlaps_fold_both_counts_first_occurrence():
    # 纽约 2026-11-01 01:30 出现两次：05:30 UTC / 06:30 UTC
    a = {
        "timezone": "UTC",
        "start_date": "2026-11-01",
        "end_date": "2026-11-01",
        "weekdays": [6],
        "local_time": "05:45",
        "duration_minutes": 30,
    }
    b = {
        "timezone": "America/New_York",
        "start_date": "2026-11-01",
        "end_date": "2026-11-01",
        "weekdays": [6],
        "local_time": "01:30",
        "fold_strategy": "both",
        "duration_minutes": 60,
    }
    resp = client.post("/overlaps", json={"a": a, "b": b})
    assert resp.status_code == 200
    body = resp.json()
    # 仅第一次出现 [05:30, 06:30) 与 A [05:45, 06:15) 重叠
    assert body["count"] == 1
    [ov] = body["overlaps"]
    assert ov["b_start_utc"] == "2026-11-01T05:30:00Z"
    assert ov["overlap_start_utc"] == "2026-11-01T05:45:00Z"
    assert ov["overlap_end_utc"] == "2026-11-01T06:15:00Z"
    assert ov["minutes"] == 30

    # 只取第二次出现时不再重叠
    resp = client.post(
        "/overlaps", json={"a": a, "b": {**b, "fold_strategy": "second"}}
    )
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "overlaps": []}


def test_overlaps_gap_strategies():
    # 纽约 2026-03-08 02:30 不存在；shift_forward 后 03:30 EDT = 07:30 UTC
    a = {
        "timezone": "UTC",
        "start_date": "2026-03-08",
        "end_date": "2026-03-08",
        "weekdays": [6],
        "local_time": "07:30",
        "duration_minutes": 30,
    }
    b = {
        "timezone": "America/New_York",
        "start_date": "2026-03-08",
        "end_date": "2026-03-08",
        "weekdays": [6],
        "local_time": "02:30",
        "gap_strategy": "shift_forward",
        "duration_minutes": 60,
    }
    resp = client.post("/overlaps", json={"a": a, "b": b})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    [ov] = body["overlaps"]
    assert ov["b_start_utc"] == "2026-03-08T07:30:00Z"
    assert ov["overlap_start_utc"] == "2026-03-08T07:30:00Z"
    assert ov["overlap_end_utc"] == "2026-03-08T08:00:00Z"
    assert ov["minutes"] == 30

    # skip 策略下该次被跳过，无重叠
    resp = client.post(
        "/overlaps", json={"a": a, "b": {**b, "gap_strategy": "skip"}}
    )
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "overlaps": []}


def test_overlaps_duration_zero_422():
    resp = client.post(
        "/overlaps", json={"a": {**OVERLAP_A, "duration_minutes": 0}, "b": OVERLAP_B}
    )
    assert resp.status_code == 422


def test_overlaps_duration_over_1440_422():
    resp = client.post(
        "/overlaps",
        json={"a": OVERLAP_A, "b": {**OVERLAP_B, "duration_minutes": 1441}},
    )
    assert resp.status_code == 422


def test_overlaps_duration_1440_ok():
    resp = client.post(
        "/overlaps",
        json={"a": OVERLAP_A, "b": {**OVERLAP_B, "duration_minutes": 1440}},
    )
    assert resp.status_code == 200


def test_overlaps_side_range_over_366_days_422():
    resp = client.post(
        "/overlaps",
        json={
            "a": OVERLAP_A,
            "b": {**OVERLAP_B, "start_date": "2026-01-01", "end_date": "2027-01-03"},
        },
    )
    assert resp.status_code == 422


def test_overlaps_invalid_timezone_422():
    resp = client.post(
        "/overlaps", json={"a": {**OVERLAP_A, "timezone": "Not/AZone"}, "b": OVERLAP_B}
    )
    assert resp.status_code == 422


# --- POST /daily-occupancy --------------------------------------------------

# 柏林 2026-03-02（周一）09:00 CET = 08:00 UTC，持续 60 分钟
OCCUPANCY_BASE = {
    "timezone": "Europe/Berlin",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "09:00",
    "duration_minutes": 60,
    "report_timezone": "Europe/Berlin",
}


def test_daily_occupancy_basic():
    resp = client.post("/daily-occupancy", json=OCCUPANCY_BASE)
    assert resp.status_code == 200
    assert resp.json() == {
        "report_timezone": "Europe/Berlin",
        "count": 1,
        "days": [{"date": "2026-03-02", "minutes": 60.0}],
    }


def test_daily_occupancy_cross_midnight_split_sorted():
    # UTC 23:00 + 120min，跨两个本地日；结果按日期升序
    resp = client.post(
        "/daily-occupancy",
        json={
            "timezone": "UTC",
            "start_date": "2026-01-01",
            "end_date": "2026-01-02",
            "weekdays": [3, 4],
            "local_time": "23:00",
            "duration_minutes": 120,
            "report_timezone": "UTC",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    assert body["days"] == [
        {"date": "2026-01-01", "minutes": 60.0},
        {"date": "2026-01-02", "minutes": 120.0},
        {"date": "2026-01-03", "minutes": 60.0},
    ]


def test_daily_occupancy_report_timezone_shifts_date():
    # 柏林 09:00 CET = 08:00 UTC，在上海（UTC+8）是当日 16:00；
    # 而 UTC 23:30 的实例在上海落入次日
    resp = client.post(
        "/daily-occupancy",
        json={**OCCUPANCY_BASE, "report_timezone": "Asia/Shanghai"},
    )
    assert resp.status_code == 200
    assert resp.json()["days"] == [{"date": "2026-03-02", "minutes": 60.0}]

    resp = client.post(
        "/daily-occupancy",
        json={
            "timezone": "UTC",
            "start_date": "2026-01-01",
            "end_date": "2026-01-01",
            "weekdays": [3],
            "local_time": "23:30",
            "duration_minutes": 60,
            "report_timezone": "Asia/Shanghai",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["days"] == [{"date": "2026-01-02", "minutes": 60.0}]


def test_daily_occupancy_spring_forward_real_minutes():
    # 纽约 2026-03-07 23:00 EST + 180min，跨春跳（当日 23 小时）：
    # 03-07 得 60 分钟，03-08 得 120 分钟，按真实 UTC 时长累计
    resp = client.post(
        "/daily-occupancy",
        json={
            "timezone": "America/New_York",
            "start_date": "2026-03-07",
            "end_date": "2026-03-07",
            "weekdays": [5],
            "local_time": "23:00",
            "duration_minutes": 180,
            "report_timezone": "America/New_York",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["days"] == [
        {"date": "2026-03-07", "minutes": 60.0},
        {"date": "2026-03-08", "minutes": 120.0},
    ]


def test_daily_occupancy_fold_both_counts_overlap_once():
    # 纽约 2026-11-01 01:30 出现两次（05:30 / 06:30 UTC），各 90 分钟：
    # 重叠区间只计一次 -> 合并为 [05:30, 08:00) = 150 分钟
    resp = client.post(
        "/daily-occupancy",
        json={
            "timezone": "America/New_York",
            "start_date": "2026-11-01",
            "end_date": "2026-11-01",
            "weekdays": [6],
            "local_time": "01:30",
            "fold_strategy": "both",
            "duration_minutes": 90,
            "report_timezone": "America/New_York",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["days"] == [{"date": "2026-11-01", "minutes": 150.0}]


def test_daily_occupancy_no_instances_empty_list():
    # 纽约 2026-03-08 02:30 不存在，skip 策略下无实例 -> 空列表
    resp = client.post(
        "/daily-occupancy",
        json={
            "timezone": "America/New_York",
            "start_date": "2026-03-08",
            "end_date": "2026-03-08",
            "weekdays": [6],
            "local_time": "02:30",
            "gap_strategy": "skip",
            "duration_minutes": 60,
            "report_timezone": "America/New_York",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "report_timezone": "America/New_York",
        "count": 0,
        "days": [],
    }


def test_daily_occupancy_invalid_report_timezone_422():
    resp = client.post(
        "/daily-occupancy",
        json={**OCCUPANCY_BASE, "report_timezone": "Not/AZone"},
    )
    assert resp.status_code == 422


def test_daily_occupancy_reuses_schedule_validation():
    # 复用现有校验：非法时区 / 逆序区间 / 超 366 天 / 时长越界均 422
    resp = client.post(
        "/daily-occupancy", json={**OCCUPANCY_BASE, "timezone": "Not/AZone"}
    )
    assert resp.status_code == 422

    resp = client.post(
        "/daily-occupancy",
        json={
            **OCCUPANCY_BASE,
            "start_date": "2026-04-01",
            "end_date": "2026-03-01",
        },
    )
    assert resp.status_code == 422

    resp = client.post(
        "/daily-occupancy",
        json={
            **OCCUPANCY_BASE,
            "start_date": "2026-01-01",
            "end_date": "2027-01-03",
        },
    )
    assert resp.status_code == 422

    resp = client.post(
        "/daily-occupancy", json={**OCCUPANCY_BASE, "duration_minutes": 0}
    )
    assert resp.status_code == 422


# --- POST /staffing-gaps ----------------------------------------------------

# 窗口：2026-03-02 全天（UTC）
STAFF_WINDOW = {
    "window_start_utc": "2026-03-02T00:00:00Z",
    "window_end_utc": "2026-03-03T00:00:00Z",
}
# 柏林 2026-03-02（周一）09:00 CET = 08:00 UTC，60 分钟
STAFF_SCHED_BERLIN = {
    "timezone": "Europe/Berlin",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "09:00",
    "duration_minutes": 60,
}
# 纽约同日 03:30 EST = 08:30 UTC，60 分钟
STAFF_SCHED_NY = {
    "timezone": "America/New_York",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "03:30",
    "duration_minutes": 60,
}


def _staff_payload(**overrides):
    payload = {
        **STAFF_WINDOW,
        "min_staff": 2,
        "employees": [
            {"id": "alice", "schedules": [STAFF_SCHED_BERLIN]},
            {"id": "bob", "schedules": [STAFF_SCHED_NY]},
        ],
    }
    payload.update(overrides)
    return payload


def _utc_schedule(local_time: str, duration: int = 60) -> dict:
    return {
        "timezone": "UTC",
        "start_date": "2026-03-02",
        "end_date": "2026-03-02",
        "weekdays": [0],
        "local_time": local_time,
        "duration_minutes": duration,
    }


def test_staffing_gaps_basic():
    resp = client.post("/staffing-gaps", json=_staff_payload())
    assert resp.status_code == 200
    body = resp.json()
    # alice [08:00,09:00)，bob [08:30,09:30)；min_staff=2
    assert body["count"] == 4
    assert body["gaps"] == [
        {
            "start_utc": "2026-03-02T00:00:00Z",
            "end_utc": "2026-03-02T08:00:00Z",
            "on_duty": [],
            "missing": 2,
        },
        {
            "start_utc": "2026-03-02T08:00:00Z",
            "end_utc": "2026-03-02T08:30:00Z",
            "on_duty": ["alice"],
            "missing": 1,
        },
        {
            "start_utc": "2026-03-02T09:00:00Z",
            "end_utc": "2026-03-02T09:30:00Z",
            "on_duty": ["bob"],
            "missing": 1,
        },
        {
            "start_utc": "2026-03-02T09:30:00Z",
            "end_utc": "2026-03-03T00:00:00Z",
            "on_duty": [],
            "missing": 2,
        },
    ]


def test_staffing_gaps_fully_staffed_returns_empty():
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1,
            employees=[
                {"id": "alice", "schedules": [_utc_schedule("00:00", 1440)]}
            ],
        ),
    )
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "gaps": []}


def test_staffing_gaps_null_schedules_never_on_duty():
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1,
            employees=[{"id": "alice", "schedules": None}, {"id": "bob"}],
        ),
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "count": 1,
        "gaps": [
            {
                "start_utc": "2026-03-02T00:00:00Z",
                "end_utc": "2026-03-03T00:00:00Z",
                "on_duty": [],
                "missing": 1,
            }
        ],
    }


def test_staffing_gaps_clips_to_window():
    # 实例 2026-03-01 23:00 UTC 起 120 分钟，仅 [00:00, 01:00) 落在窗口内
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1,
            employees=[
                {
                    "id": "alice",
                    "schedules": [
                        {
                            "timezone": "UTC",
                            "start_date": "2026-03-01",
                            "end_date": "2026-03-01",
                            "weekdays": [6],
                            "local_time": "23:00",
                            "duration_minutes": 120,
                        }
                    ],
                }
            ],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["gaps"] == [
        {
            "start_utc": "2026-03-02T01:00:00Z",
            "end_utc": "2026-03-03T00:00:00Z",
            "on_duty": [],
            "missing": 1,
        }
    ]


def test_staffing_gaps_same_employee_touching_intervals_count_once():
    # alice 两段相接日程 08:00+60 / 09:00+60 -> 连续在岗 [08:00,10:00)，只计一人
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1,
            employees=[
                {
                    "id": "alice",
                    "schedules": [
                        _utc_schedule("08:00"),
                        _utc_schedule("09:00"),
                    ],
                }
            ],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["gaps"] == [
        {
            "start_utc": "2026-03-02T00:00:00Z",
            "end_utc": "2026-03-02T08:00:00Z",
            "on_duty": [],
            "missing": 1,
        },
        {
            "start_utc": "2026-03-02T10:00:00Z",
            "end_utc": "2026-03-03T00:00:00Z",
            "on_duty": [],
            "missing": 1,
        },
    ]


def test_staffing_gaps_same_instant_handoff_no_zero_gap():
    # alice [08:00,09:00)，bob [09:00,10:00)：09:00 的上下岗一起处理
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1,
            employees=[
                {"id": "alice", "schedules": [_utc_schedule("08:00")]},
                {"id": "bob", "schedules": [_utc_schedule("09:00")]},
            ],
        ),
    )
    assert resp.status_code == 200
    assert resp.json()["gaps"] == [
        {
            "start_utc": "2026-03-02T00:00:00Z",
            "end_utc": "2026-03-02T08:00:00Z",
            "on_duty": [],
            "missing": 1,
        },
        {
            "start_utc": "2026-03-02T10:00:00Z",
            "end_utc": "2026-03-03T00:00:00Z",
            "on_duty": [],
            "missing": 1,
        },
    ]


def test_staffing_gaps_on_duty_ids_sorted():
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=3,
            employees=[
                {"id": "zoe", "schedules": [_utc_schedule("08:00", 120)]},
                {"id": "amy", "schedules": [_utc_schedule("08:00", 120)]},
                {"id": "bob", "schedules": None},
            ],
        ),
    )
    assert resp.status_code == 200
    gaps = resp.json()["gaps"]
    assert gaps[1] == {
        "start_utc": "2026-03-02T08:00:00Z",
        "end_utc": "2026-03-02T10:00:00Z",
        "on_duty": ["amy", "zoe"],
        "missing": 1,
    }


def test_staffing_gaps_reuses_fold_expansion():
    # 纽约 2026-11-01 01:30 出现两次（05:30 / 06:30 UTC），各 60 分钟且相接
    # -> 同一员工合并为连续在岗 [05:30, 07:30)，只计一人
    resp = client.post(
        "/staffing-gaps",
        json={
            "window_start_utc": "2026-11-01T00:00:00Z",
            "window_end_utc": "2026-11-02T00:00:00Z",
            "min_staff": 1,
            "employees": [
                {
                    "id": "alice",
                    "schedules": [
                        {
                            "timezone": "America/New_York",
                            "start_date": "2026-11-01",
                            "end_date": "2026-11-01",
                            "weekdays": [6],
                            "local_time": "01:30",
                            "fold_strategy": "both",
                            "duration_minutes": 60,
                        }
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["gaps"] == [
        {
            "start_utc": "2026-11-01T00:00:00Z",
            "end_utc": "2026-11-01T05:30:00Z",
            "on_duty": [],
            "missing": 1,
        },
        {
            "start_utc": "2026-11-01T07:30:00Z",
            "end_utc": "2026-11-02T00:00:00Z",
            "on_duty": [],
            "missing": 1,
        },
    ]


def test_staffing_gaps_reuses_gap_shift_forward():
    # 纽约 2026-03-08 02:30 不存在；shift_forward 后 03:30 EDT = 07:30 UTC
    resp = client.post(
        "/staffing-gaps",
        json={
            "window_start_utc": "2026-03-08T00:00:00Z",
            "window_end_utc": "2026-03-09T00:00:00Z",
            "min_staff": 1,
            "employees": [
                {
                    "id": "alice",
                    "schedules": [
                        {
                            "timezone": "America/New_York",
                            "start_date": "2026-03-08",
                            "end_date": "2026-03-08",
                            "weekdays": [6],
                            "local_time": "02:30",
                            "gap_strategy": "shift_forward",
                            "duration_minutes": 60,
                        }
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["gaps"] == [
        {
            "start_utc": "2026-03-08T00:00:00Z",
            "end_utc": "2026-03-08T07:30:00Z",
            "on_duty": [],
            "missing": 1,
        },
        {
            "start_utc": "2026-03-08T08:30:00Z",
            "end_utc": "2026-03-09T00:00:00Z",
            "on_duty": [],
            "missing": 1,
        },
    ]


def test_staffing_gaps_window_must_be_z_utc_422():
    # 缺少 Z 后缀 / 用 +00:00 代替 Z 均非法
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(window_start_utc="2026-03-02T00:00:00"),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(window_start_utc="2026-03-02T00:00:00+00:00"),
    )
    assert resp.status_code == 422


def test_staffing_gaps_window_not_increasing_422():
    # 相等与逆序都非法
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(window_end_utc="2026-03-02T00:00:00Z"),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            window_start_utc="2026-03-03T00:00:00Z",
            window_end_utc="2026-03-02T00:00:00Z",
        ),
    )
    assert resp.status_code == 422


def test_staffing_gaps_window_over_366_days_422():
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            window_start_utc="2026-01-01T00:00:00Z",
            window_end_utc="2027-01-03T00:00:00Z",
        ),
    )
    assert resp.status_code == 422


def test_staffing_gaps_window_exactly_366_days_ok():
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            window_start_utc="2026-01-01T00:00:00Z",
            window_end_utc="2027-01-02T00:00:00Z",  # 恰好 366 天
        ),
    )
    assert resp.status_code == 200


def test_staffing_gaps_min_staff_bounds_422():
    resp = client.post("/staffing-gaps", json=_staff_payload(min_staff=0))
    assert resp.status_code == 422

    # 超过员工总数（2 名员工）
    resp = client.post("/staffing-gaps", json=_staff_payload(min_staff=3))
    assert resp.status_code == 422


def test_staffing_gaps_employee_count_bounds():
    resp = client.post("/staffing-gaps", json=_staff_payload(employees=[]))
    assert resp.status_code == 422

    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1, employees=[{"id": f"e{i}"} for i in range(21)]
        ),
    )
    assert resp.status_code == 422

    # 20 名合法
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            min_staff=1, employees=[{"id": f"e{i}"} for i in range(20)]
        ),
    )
    assert resp.status_code == 200


def test_staffing_gaps_duplicate_employee_ids_422():
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(employees=[{"id": "alice"}, {"id": "alice"}]),
    )
    assert resp.status_code == 422


def test_staffing_gaps_reuses_schedule_validation():
    # 日程沿用 /overlaps 单侧校验：非法时区 / 时长越界 / 区间超 366 天均 422
    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            employees=[
                {
                    "id": "alice",
                    "schedules": [{**STAFF_SCHED_BERLIN, "timezone": "Not/AZone"}],
                },
                {"id": "bob"},
            ]
        ),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            employees=[
                {
                    "id": "alice",
                    "schedules": [{**STAFF_SCHED_BERLIN, "duration_minutes": 0}],
                },
                {"id": "bob"},
            ]
        ),
    )
    assert resp.status_code == 422

    resp = client.post(
        "/staffing-gaps",
        json=_staff_payload(
            employees=[
                {
                    "id": "alice",
                    "schedules": [
                        {
                            **STAFF_SCHED_BERLIN,
                            "start_date": "2026-01-01",
                            "end_date": "2027-01-03",
                        }
                    ],
                },
                {"id": "bob"},
            ]
        ),
    )
    assert resp.status_code == 422
