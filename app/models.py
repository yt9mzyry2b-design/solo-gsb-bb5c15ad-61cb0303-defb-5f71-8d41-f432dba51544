"""Pydantic 请求/响应模型。

所有校验失败（非法时区、逆序区间、超过 366 天的区间、非法星期等）
都会由 FastAPI 统一转换为 HTTP 422。
"""

from __future__ import annotations

from datetime import date, time
from enum import Enum
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_RANGE_DAYS = 366


def _check_iana_timezone(v: str) -> str:
    """校验 IANA 时区名，非法时抛 ValueError（由 FastAPI 转为 422）。"""
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        raise ValueError(f"unknown IANA timezone: {v!r}") from None
    return v


class GapStrategy(str, Enum):
    """本地时刻不存在（春跳）时的策略。"""

    skip = "skip"  # 跳过该次
    shift_forward = "shift_forward"  # 按跳变差值前移


class FoldStrategy(str, Enum):
    """本地时刻重复（秋回）时的策略。"""

    first = "first"  # 取第一次出现（UTC 较早）
    second = "second"  # 取第二次出现（UTC 较晚）
    both = "both"  # 两次都保留


# 0=周一 ... 6=周日（与 datetime.date.weekday() 一致）
Weekday = Annotated[int, Field(ge=0, le=6)]


class ExpandRequest(BaseModel):
    timezone: str = Field(..., description="IANA 时区名，如 Europe/Berlin")
    start_date: date = Field(..., description="区间起始日期（含）")
    end_date: date = Field(..., description="区间结束日期（含）")
    weekdays: set[Weekday] = Field(
        ..., min_length=1, description="星期集合，0=周一 ... 6=周日"
    )
    local_time: time = Field(..., description="本地时刻，如 02:30")
    gap_strategy: GapStrategy = Field(
        default=GapStrategy.skip, description="不存在时刻策略：skip / shift_forward"
    )
    fold_strategy: FoldStrategy = Field(
        default=FoldStrategy.first, description="重复时刻策略：first / second / both"
    )

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        return _check_iana_timezone(v)

    @model_validator(mode="after")
    def _validate_range(self) -> "ExpandRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if (self.end_date - self.start_date).days > MAX_RANGE_DAYS:
            raise ValueError(
                f"date range must not exceed {MAX_RANGE_DAYS} days"
            )
        return self


class InstanceOut(BaseModel):
    local: str = Field(..., description="带偏移的本地时间，ISO 8601")
    offset: str = Field(..., description="UTC 偏移，如 +02:00")
    utc: str = Field(..., description="对应的 UTC 时间，ISO 8601（Z 结尾）")
    reason: str = Field(
        ...,
        description=(
            "决策原因：normal / gap_shifted_forward / fold_first / fold_second"
        ),
    )


class ExpandResponse(BaseModel):
    timezone: str
    count: int = Field(..., description="生成的实例数")
    skipped_count: int = Field(..., description="因 skip 策略被跳过的次数")
    skipped_dates: list[str] = Field(..., description="被跳过实例的本地日期")
    instances: list[InstanceOut]


class OverlapSide(ExpandRequest):
    """一侧日程：完整的展开参数 + 每次实例时长。

    继承 ExpandRequest 的全部校验（合法时区、顺序区间、不超过 366 天等）。
    """

    duration_minutes: int = Field(
        ..., ge=1, le=1440, description="每次实例的时长（分钟），1～1440"
    )


class OverlapRequest(BaseModel):
    a: OverlapSide = Field(..., description="A 侧日程")
    b: OverlapSide = Field(..., description="B 侧日程")


class OverlapOut(BaseModel):
    a_start_utc: str = Field(..., description="A 侧实例起点，ISO 8601（Z 结尾）")
    b_start_utc: str = Field(..., description="B 侧实例起点，ISO 8601（Z 结尾）")
    overlap_start_utc: str = Field(..., description="重叠区间起点（含），UTC")
    overlap_end_utc: str = Field(..., description="重叠区间终点（不含），UTC")
    minutes: float = Field(..., description="重叠时长（分钟）")


class OverlapResponse(BaseModel):
    count: int = Field(..., description="跨侧重叠次数")
    overlaps: list[OverlapOut] = Field(
        ..., description="全部跨侧重叠，按 overlap_start_utc 排序"
    )


class DailyOccupancyRequest(OverlapSide):
    """逐日占用统计请求：与 /overlaps 单侧相同的日程 + 报告时区。

    继承 OverlapSide 的全部校验（合法时区、顺序区间、不超过 366 天、
    duration_minutes 1～1440 等）。
    """

    report_timezone: str = Field(
        ..., description="报告时区（IANA 名），按该时区的本地日界切分"
    )

    @field_validator("report_timezone")
    @classmethod
    def _validate_report_timezone(cls, v: str) -> str:
        return _check_iana_timezone(v)


class DayOccupancyOut(BaseModel):
    date: str = Field(..., description="报告时区的本地日期，YYYY-MM-DD")
    minutes: float = Field(..., description="当日占用的真实分钟数")


class DailyOccupancyResponse(BaseModel):
    report_timezone: str = Field(..., description="报告时区")
    count: int = Field(..., description="有占用的天数")
    days: list[DayOccupancyOut] = Field(
        ..., description="逐日占用分钟数，按日期升序；无实例时为空列表"
    )
