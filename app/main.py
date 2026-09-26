"""FastAPI 入口：民用日程展开服务。"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from fastapi import FastAPI

from .models import (
    DailyOccupancyRequest,
    DailyOccupancyResponse,
    ExpandRequest,
    ExpandResponse,
    OverlapRequest,
    OverlapResponse,
)
from .scheduler import (
    expand_schedule,
    find_overlaps,
    instance_payload,
    occupancy_by_day,
    utc_iso,
)

app = FastAPI(
    title="Civil Schedule Expander",
    version="1.0.0",
    description=(
        "基于标准库 zoneinfo 的民用日程展开 API："
        "通过 UTC 往返校验区分正常 / 跳时 / 叠时，"
        "按策略生成去重且按 UTC 稳定排序的实例，"
        "并可检测两个日程在真实时间轴上的重叠、按报告时区统计逐日占用。"
    ),
)


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.post("/expand", response_model=ExpandResponse, summary="展开日程")
def expand(req: ExpandRequest) -> ExpandResponse:
    tz = ZoneInfo(req.timezone)
    instances, skipped = expand_schedule(
        tz=tz,
        start_date=req.start_date,
        end_date=req.end_date,
        weekdays=req.weekdays,
        local_time=req.local_time,
        gap_strategy=req.gap_strategy.value,
        fold_strategy=req.fold_strategy.value,
    )
    return ExpandResponse(
        timezone=req.timezone,
        count=len(instances),
        skipped_count=len(skipped),
        skipped_dates=[d.isoformat() for d in skipped],
        instances=[instance_payload(i, tz) for i in instances],
    )


@app.post("/overlaps", response_model=OverlapResponse, summary="检测两个日程的重叠")
def overlaps(req: OverlapRequest) -> OverlapResponse:
    # 两侧各自按原 gap/fold 策略展开（实例已按 UTC 去重、排序）
    expanded = []
    for side in (req.a, req.b):
        tz = ZoneInfo(side.timezone)
        instances, _ = expand_schedule(
            tz=tz,
            start_date=side.start_date,
            end_date=side.end_date,
            weekdays=side.weekdays,
            local_time=side.local_time,
            gap_strategy=side.gap_strategy.value,
            fold_strategy=side.fold_strategy.value,
        )
        expanded.append(instances)
    found = find_overlaps(
        a_instances=expanded[0],
        a_duration_minutes=req.a.duration_minutes,
        b_instances=expanded[1],
        b_duration_minutes=req.b.duration_minutes,
    )
    return OverlapResponse(
        count=len(found),
        overlaps=[
            {
                "a_start_utc": utc_iso(o.a_start),
                "b_start_utc": utc_iso(o.b_start),
                "overlap_start_utc": utc_iso(o.start),
                "overlap_end_utc": utc_iso(o.end),
                "minutes": o.minutes,
            }
            for o in found
        ],
    )


@app.post(
    "/daily-occupancy",
    response_model=DailyOccupancyResponse,
    summary="按报告时区统计逐日占用分钟数",
)
def daily_occupancy(req: DailyOccupancyRequest) -> DailyOccupancyResponse:
    tz = ZoneInfo(req.timezone)
    instances, _ = expand_schedule(
        tz=tz,
        start_date=req.start_date,
        end_date=req.end_date,
        weekdays=req.weekdays,
        local_time=req.local_time,
        gap_strategy=req.gap_strategy.value,
        fold_strategy=req.fold_strategy.value,
    )
    days = occupancy_by_day(
        instances=instances,
        duration_minutes=req.duration_minutes,
        report_tz=ZoneInfo(req.report_timezone),
    )
    return DailyOccupancyResponse(
        report_timezone=req.report_timezone,
        count=len(days),
        days=[{"date": d.isoformat(), "minutes": m} for d, m in days],
    )
