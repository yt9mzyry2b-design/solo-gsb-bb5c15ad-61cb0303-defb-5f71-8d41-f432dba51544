# Civil Schedule Expander

基于 **FastAPI + 标准库 zoneinfo** 的民用日程展开 API。输入 IANA 时区、日期区间、
星期集合与本地时刻，服务通过 **UTC 往返校验**区分正常 / 跳时（gap）/ 叠时（fold），
按指定策略生成**去重且按 UTC 稳定排序**的实例，并返回每个实例的本地时间、UTC 偏移、
UTC 时间与决策原因。`POST /overlaps` 在此基础上为两个日程各附加时长，
用**扫描线**检测它们在真实时间轴上的全部冲突；`POST /daily-occupancy`
则把单侧日程的占用区间合并后按**报告时区的本地日界**切分，逐日统计真实占用分钟数；
`POST /staffing-gaps` 接收一个 UTC 窗口、最低在岗人数与 1～20 名员工
（每人携带可空的日程列表），复用既有跳时 / 叠时展开把班次转为真实时长的
半开区间并裁剪到窗口，按时间扫描上下岗事件，输出人数不足的区间。

## 快速启动（单条命令）

```bash
docker compose up --build
```

依赖（FastAPI / uvicorn / tzdata）在镜像构建时安装完毕，容器启动即可服务。

- 服务地址： http://localhost:8000
- 交互式文档（Swagger UI）： http://localhost:8000/docs
- 健康检查： http://localhost:8000/health
- 接口（均为 POST，端口同上）： `/expand`、`/overlaps`、`/daily-occupancy`、`/staffing-gaps`

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 宿主机映射端口，如 `PORT=9000 docker compose up --build` 后访问 http://localhost:9000 |

容器内服务固定监听 8000 端口，无需其他配置；时区数据库由镜像内系统 tzdata
及 pip 包 `tzdata`（兜底）提供。

## API

### `POST /expand`

请求体：

```json
{
  "timezone": "Europe/Berlin",
  "start_date": "2026-03-28",
  "end_date": "2026-03-30",
  "weekdays": [5, 6, 0],
  "local_time": "02:30",
  "gap_strategy": "shift_forward",
  "fold_strategy": "both"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `timezone` | string | IANA 时区名，如 `America/New_York` |
| `start_date` / `end_date` | date | 日期区间（两端均含） |
| `weekdays` | int[] | 星期集合，`0`=周一 … `6`=周日，至少 1 个 |
| `local_time` | string | 本地时刻，如 `02:30` |
| `gap_strategy` | string | 不存在时刻策略：`skip`（默认，跳过）/ `shift_forward`（按跳变差值前移） |
| `fold_strategy` | string | 重复时刻策略：`first`（默认，取第一次）/ `second`（取第二次）/ `both`（两次都保留） |

响应 `200`：

```json
{
  "timezone": "Europe/Berlin",
  "count": 3,
  "skipped_count": 0,
  "skipped_dates": [],
  "instances": [
    {"local": "2026-03-28T02:30:00+01:00", "offset": "+01:00", "utc": "2026-03-28T01:30:00Z", "reason": "normal"},
    {"local": "2026-03-29T03:30:00+02:00", "offset": "+02:00", "utc": "2026-03-29T01:30:00Z", "reason": "gap_shifted_forward"},
    {"local": "2026-03-30T02:30:00+02:00", "offset": "+02:00", "utc": "2026-03-30T00:30:00Z", "reason": "normal"}
  ]
}
```

决策原因（`reason`）：

| 值 | 含义 |
| --- | --- |
| `normal` | 正常时刻，本地时间与 UTC 一一对应 |
| `gap_shifted_forward` | 原时刻不存在（春跳），已按跳变差值前移 |
| `fold_first` | 重复时刻（秋回），取第一次出现（UTC 较早） |
| `fold_second` | 重复时刻（秋回），取第二次出现（UTC 较晚） |

被 `skip` 策略跳过的日期不计入 `instances`，汇总在 `skipped_count` / `skipped_dates`。

### `POST /overlaps`

判断两个民用周期日程在真实时间轴上的冲突。请求体的 `a` / `b` 两侧各自携带
与 `/expand` 相同的展开参数（含各自的 `gap_strategy` / `fold_strategy`），外加
`duration_minutes`；两侧独立展开后，把每个实例解释为**半开 UTC 区间**
`[start, start+duration)`，用扫描线枚举全部跨侧重叠。**边界相接不算冲突**
（一侧终点等于另一侧起点时重叠为空）。

请求体：

```json
{
  "a": {
    "timezone": "Europe/Berlin",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "09:00",
    "duration_minutes": 60
  },
  "b": {
    "timezone": "America/New_York",
    "start_date": "2026-03-02",
    "end_date": "2026-03-02",
    "weekdays": [0],
    "local_time": "03:30",
    "duration_minutes": 60
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `a` / `b` | object | 一侧日程：全部 `/expand` 请求字段 + `duration_minutes` |
| `duration_minutes` | int | 每次实例的时长（分钟），`1`～`1440` |

响应 `200`（`overlaps` 按 `overlap_start_utc` 排序）：

```json
{
  "count": 1,
  "overlaps": [
    {
      "a_start_utc": "2026-03-02T08:00:00Z",
      "b_start_utc": "2026-03-02T08:30:00Z",
      "overlap_start_utc": "2026-03-02T08:30:00Z",
      "overlap_end_utc": "2026-03-02T09:00:00Z",
      "minutes": 30.0
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `a_start_utc` / `b_start_utc` | 两侧实例起点（UTC，Z 结尾） |
| `overlap_start_utc` / `overlap_end_utc` | 重叠区间起止（半开，UTC） |
| `minutes` | 重叠时长（分钟） |

每侧的日期区间同样限制为不超过 366 天，校验失败返回 422。

### `POST /daily-occupancy`

统计单个日程在报告时区内的逐日占用时长。请求体与 `/overlaps` 单侧相同
（全部 `/expand` 展开参数 + `duration_minutes`），外加 `report_timezone`。
实例按既有策略展开后，每个实例解释为**半开 UTC 区间**
`[start, start+duration)`；先**合并相交或相接的区间**（重复时刻产生的
重叠只计一次），再按报告时区的**本地日界**切分，逐日返回占用的真实分钟数。
跨午夜自然落到相邻两日；夏令时切换日（23 / 25 小时）仍按实际 UTC 流逝累计。
无实例时 `days` 为空列表。

请求体：

```json
{
  "timezone": "America/New_York",
  "start_date": "2026-03-07",
  "end_date": "2026-03-07",
  "weekdays": [5],
  "local_time": "23:00",
  "duration_minutes": 180,
  "report_timezone": "America/New_York"
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| （展开参数） | — | 与 `/expand` 请求字段相同，校验规则一致 |
| `duration_minutes` | int | 每次实例的时长（分钟），`1`～`1440` |
| `report_timezone` | string | 报告时区（IANA 名），按该时区的本地日界切分 |

响应 `200`（`days` 按日期升序）：

```json
{
  "report_timezone": "America/New_York",
  "count": 2,
  "days": [
    {"date": "2026-03-07", "minutes": 60.0},
    {"date": "2026-03-08", "minutes": 120.0}
  ]
}
```

上例中实例为纽约 2026-03-07 23:00 EST（= 03-08 04:00 UTC）起 180 分钟，
跨越春跳（03-08 当日仅 23 小时），仍按真实 UTC 时长计入两日。

| 字段 | 说明 |
| --- | --- |
| `report_timezone` | 报告时区 |
| `count` | 有占用的天数 |
| `days[].date` | 报告时区的本地日期（YYYY-MM-DD） |
| `days[].minutes` | 当日占用的真实分钟数 |

### `POST /staffing-gaps`

查询一个 UTC 窗口内最低在岗人数不足的区间。请求包含窗口起止（均为
**以 `Z` 结尾的 UTC 时刻**，半开区间 `[window_start, window_end)`）、
`minimum_staff` 与 1～20 名员工。每名员工有唯一 `id` 和**可为空**的
`schedules` 日程列表（缺省、`null` 或 `[]` 表示全程不在岗）；列表中每条
日程的参数与 `/overlaps` 单侧完全一致（全部 `/expand` 展开参数 +
`duration_minutes`），校验规则也相同。

每条日程按既有 gap / fold 策略展开为实例，再按**真实时长**转成半开 UTC
区间 `[start, start+duration)`，统一**裁剪到窗口**；同一员工相交或相接的
区间先合并（重叠班段只计一人）。随后按时间扫描全部上岗 / 下岗事件，
**同一时刻的事件一起处理**（半开相接不产生瞬时缺口），输出在岗人数低于
`minimum_staff` 的非空区间；在岗集合相同的相邻缺员段合并，结果按起点排序，
全程达标时返回空列表。

请求体：

```json
{
  "window_start": "2026-03-02T08:00:00Z",
  "window_end": "2026-03-02T12:00:00Z",
  "minimum_staff": 2,
  "employees": [
    {
      "id": "alice",
      "schedules": [
        {
          "timezone": "Europe/Berlin",
          "start_date": "2026-03-02",
          "end_date": "2026-03-02",
          "weekdays": [0],
          "local_time": "09:00",
          "duration_minutes": 60
        }
      ]
    },
    {
      "id": "bob",
      "schedules": [
        {
          "timezone": "America/New_York",
          "start_date": "2026-03-02",
          "end_date": "2026-03-02",
          "weekdays": [0],
          "local_time": "04:00",
          "duration_minutes": 120
        }
      ]
    }
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `window_start` / `window_end` | string | UTC 窗口起止（起点含、终点不含），ISO 8601 且必须以 `Z` 结尾 |
| `minimum_staff` | int | 最低在岗人数，`1` 至员工总数 |
| `employees` | object[] | 员工列表，`1`～`20` 人 |
| `employees[].id` | string | 员工 ID，非空且在列表内唯一 |
| `employees[].schedules` | object[]\|null | 日程列表，可缺省 / 为 `null`；每条同 `/overlaps` 单侧参数与校验 |

上例中 alice 为柏林 09:00 CET 起 60 分钟（`[08:00, 09:00)` UTC），
bob 为纽约 04:00 EST 起 120 分钟（`[09:00, 11:00)` UTC），要求 2 人，
响应 `200`（`gaps` 按起点升序）：

```json
{
  "minimum_staff": 2,
  "count": 3,
  "gaps": [
    {
      "start_utc": "2026-03-02T08:00:00Z",
      "end_utc": "2026-03-02T09:00:00Z",
      "staff_ids": ["alice"],
      "shortage": 1
    },
    {
      "start_utc": "2026-03-02T09:00:00Z",
      "end_utc": "2026-03-02T11:00:00Z",
      "staff_ids": ["bob"],
      "shortage": 1
    },
    {
      "start_utc": "2026-03-02T11:00:00Z",
      "end_utc": "2026-03-02T12:00:00Z",
      "staff_ids": [],
      "shortage": 2
    }
  ]
}
```

`09:00` 的下岗（alice）与上岗（bob）同刻处理，因此不会产生零长度缺口；
相邻两段在岗集合不同（`["alice"]` → `["bob"]`）故不合并。

| 字段 | 说明 |
| --- | --- |
| `minimum_staff` | 回显请求的最低在岗人数 |
| `count` | 缺员区间数 |
| `gaps[].start_utc` / `end_utc` | 缺员区间起止（半开，UTC，Z 结尾） |
| `gaps[].staff_ids` | 该区间内在岗的员工 ID，按字典序排序 |
| `gaps[].shortage` | 缺少人数，等于 `minimum_staff - 实际在岗人数` |

### 错误（422）

以下情况返回 `422 Unprocessable Entity`：

- 非法 IANA 时区名（含 `/daily-occupancy` 的 `report_timezone`）
- `end_date` 早于 `start_date`（逆序区间）
- 区间超过 366 天（`(end_date - start_date).days > 366`，`/overlaps` 对每侧分别限制）
- `duration_minutes` 不在 1～1440（仅 `/overlaps`、`/daily-occupancy` 与 `/staffing-gaps` 的日程）
- 星期值越界（不在 0–6）或集合为空等其他字段校验失败
- `/staffing-gaps` 专属：窗口时刻不是以 `Z` 结尾的合法 UTC 时间；`window_end` 不晚于
  `window_start`；窗口长度超过 366 天；`minimum_staff` 不在 1～员工总数；
  员工数不在 1～20；员工 ID 为空或重复；任一员工日程不满足 `/overlaps` 单侧校验

## 判定原理

对每个候选本地时刻分别按 `fold=0` / `fold=1` 赋予时区，换算到 UTC 再换算回来
（UTC 往返校验）：能还原原时刻的解释才真实存在。两种解释均有效且 UTC 相同为正常；
均有效但 UTC 不同为叠时；均无效为跳时，跳变差值取两种解释对应的 UTC 之差，
`shift_forward` 即将本地时刻加上该差值后重新校验。所有实例按 UTC 去重
（保留先出现者）并按 UTC 稳定排序。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt -r requirements-dev.txt
uvicorn app.main:app --reload        # http://localhost:8000
pytest                               # 运行测试
```

## 项目结构

```
app/
  main.py        # FastAPI 入口，/expand、/overlaps、/daily-occupancy、/staffing-gaps 与 /health
  models.py      # 请求/响应模型与 422 校验规则
  scheduler.py   # UTC 往返校验分类 + 展开 / 去重 / 排序 + 扫描线重叠检测 + 逐日占用统计 + 在岗缺口扫描
tests/           # pytest 测试（核心逻辑 + API）
Dockerfile
docker-compose.yml
requirements.txt
requirements-dev.txt
```
