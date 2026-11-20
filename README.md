# 线上展馆观众参观路径 API

从浏览器上报的 `open` / `close` 事件还原匿名观众的实际参观路径，避免把未关闭页面
计作完整参观；支持乱序补报、重传去重与数据同意撤回。

在此基础上提供 **A/B 线上导览对比试验** 的分组能力：按曝光版别与 UTC 曝光时间把
匿名观众分入 A 组、B 组或串组，并保存每人在最早曝光后 30 分钟窗口内打开的浏览项
及停留状态，供馆员直接查询。

技术栈：Python 3.11 + FastAPI + SQLite（WAL 模式），Docker Compose 一键启动。

## 快速启动

```bash
docker compose up --build
# 打开交互文档：
#   Swagger UI  http://localhost:8000/docs
#   ReDoc       http://localhost:8000/redoc
# 健康检查     http://localhost:8000/health
```

SQLite 数据文件存放在命名卷 `exhibition-data`（容器内 `/data/exhibition.db`），
容器重建不丢数据。

本地（无 Docker）运行：

```bash
pip install -r requirements.txt
EXHIBITION_DB=./exhibition.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/events` | 上报单个事件 |
| POST | `/events/batch` | 批量上报，单事务原子提交 |
| GET | `/visitors/{visitor_id}/path` | 查询观众参观路径 |
| POST | `/visitors/{visitor_id}/withdraw` | 撤回数据使用同意（body 可省略，或传 `{"visitor_id": "..."}`） |
| POST | `/experiment/exposures` | 上报曝光（匿名观众号 + 版别 A/B + UTC 曝光时间） |
| GET | `/experiment/groups` | 查询已存的分组样本与串组名单（可选 `?group=A/B/cross` 过滤样本） |
| GET | `/experiment/cross-group` | 查询串组名单（两版都看过的观众号） |
| GET | `/experiment/visitors/{visitor_id}` | 查询单个观众的已存试验结果（撤回后 404） |
| GET | `/health` | 健康检查 |

事件字段：`event_id`（全局唯一，重传复用同一号）、`visitor_id`（匿名观众号）、
`page_view_id`（页面浏览号，同一次浏览的开关共享）、`exhibit_id`（展品号）、
`event_type`（`open` / `close`）、`event_time`（**带时区的 UTC ISO-8601**，
如 `2026-11-18T08:00:00Z`；无时区时间戳返回 400）。

### 上报响应语义

- `stored`：首次入库
- `duplicate`：`event_id` 已存在，幂等忽略（即使重传内容被篡改，也以首次内容为准）
- `rejected_withdrawn`：该观众已撤回同意，事件不入库，路径不会因此恢复

HTTP 状态码：成功受理固定 `201`，具体结果看响应体 `status`；参数/时间非法为
`422` / `400`。

### 路径响应

按 `page_view_id` 配对开关，同一观众按 **open 时间** 排序（`sequence` 从 1 连续编号）：

- 有关闭：`dwell_known=true`，`dwell_seconds` 为停留秒数
- 缺关闭：仍计为一次参观，但 `closed_at=null`、`dwell_known=false`、`dwell_seconds=null`（停留未知）
- 只有 close 没有 open 的孤儿事件不产生参观项
- close 早于 open 的异常数据：保留关闭时间，但停留时长标记未知

### A/B 试验分组规则

- **曝光**：`POST /experiment/exposures`，body
  `{"visitor_id": "...", "variant": "A", "exposed_at": "2026-11-18T08:00:00Z"}`。
  同一 `(visitor_id, variant, exposed_at)` 重传幂等返回 `duplicate`；无时区时间 400。
- **观察窗**：以该观众 **所有版别中最早的曝光时间** 为起点，取接下来 30 分钟
  `[起点, 起点+30min)`：含起点，截止时刻本身不计（“到时即止”）。
- **入组**：两个版别都曝光过的观众标为 `cross`（串组）并进入串组名单；否则按所见
  版别进入 `A` 或 `B` 组。入组只取决于曝光，“符合入组条件的人即使没在窗内打开过
  展品，也保留在组内”（`views=[]`、`view_count=0`）。
- **窗内浏览项**：窗口内 `open` 的每个 `page_view_id` 都记入样本，按 open 时间排序；
  窗口内没有对应 close（或 close 在截止时刻之后）时 `dwell_known=false`、
  `closed_at=null`、`dwell_seconds=null`。窗口之前打开的页面即使跨过窗口也不算。
- **只刷新受影响的观众**：每条新曝光的写入与样本重算在同一事务内完成，只重算该
  观众；补报浏览事件同理只重算事件所属观众。曝光重传（`duplicate`）不触发重算。
- **查询读已存样本**：`GET /experiment/groups` 与 `GET /experiment/visitors/{id}`
  直接读取物化的 `experiment_samples` / `experiment_sample_views` /
  `experiment_cross_group`，查询时不重新配对事件。样本由写入侧维护，因此查询快且
  结果与写入事务一致。
- **撤回**：撤回在同一个事务里删除该观众的曝光、分组样本、窗内浏览项与串组名单
  条目（连同原有事件），随后 `GET /experiment/visitors/{id}` 返回 404、
  `GET /experiment/groups` 不再包含此人；撤回后补传曝光或浏览均为
  `rejected_withdrawn`，不会恢复任何试验结果。
- **失败保留原状**：曝光上报、单笔/批量浏览上报、撤回都与对应的样本更新一并提交；
  任一校验失败（400/422）整笔/整批回滚，已存样本不变。

`GET /experiment/groups` 响应含 `window_minutes=30`、`counts`（A/B/cross 人数）、
`groups`（各组的已存样本，`?group=` 可只取一组）以及 `cross_group`（串组名单）。
`GET /experiment/visitors/{id}` 返回该观众的 `group`、`variants`、`first_exposed_at`、
`window_end`、`views`（含停留状态）与 `view_count`。

## 设计要点（如何满足需求）

1. **重传不重复计数**：`events.event_id` 为主键；插入前先查存在性，存在即返回
   `duplicate`，整段逻辑在同一事务内。
2. **乱序补报照常处理**：参观路径不做物化存储，每次查询从已提交事件实时配对、排序。
   后补的较早 open 会在下次查询时自然改变路径顺序；后补的 close 会把“停留未知”
   变成已知时长。试验样本则相反，是写入时物化的：新事件/新曝光到达时只重算该观众
   的样本，提前的曝光会立刻把窗口起点前移并重新入组。
3. **撤回后不可恢复**：`withdrawals` 表持久化撤回标记；撤回在一个 `BEGIN IMMEDIATE`
   事务内完成「删除该观众全部事件 + 曝光 + 试验样本/浏览项/串组条目 + 写撤回标记」。
   此后任何上传（包括旧事件或旧曝光重放）在同一事务内先查撤回标记，一律
   `rejected_withdrawn`。
4. **交错操作基于同一已提交状态、不露半份结果**：
   - SQLite 开 WAL，写请求统一 `BEGIN IMMEDIATE` 立即拿写锁，配合 `busy_timeout`
     串行化所有写事务；
   - 读请求在单个 `BEGIN DEFERRED` 快照事务里完成「撤回检查 + 样本读取」，
     要么看到撤回前的完整样本，要么看到撤回后的空结果；
   - 样本在写入事务内整份替换（先删后插），查询不会看到“更新一半”的样本；
   - 批量上报为单事务，任一事件非法则整批回滚。
5. **线程/进程并发**：每请求一个独立 SQLite 连接（`check_same_thread=False`
   以适配 uvicorn 线程池，连接不在请求间共享）；多个 uvicorn worker 共享同一
   数据卷时由 SQLite 文件锁保证安全。

## 目录结构

```
app/
  config.py    环境变量配置（DB 路径、busy timeout）
  db.py        连接、schema、WAL/pragma、事务管理
  models.py    Pydantic 请求/响应模型
  crud.py      领域逻辑：入库去重、开关配对、路径推导、撤回、A/B 试验分组
  main.py      FastAPI 路由与生命周期
tests/
  conftest.py
  test_api.py        15 项功能/并发测试
  test_experiment.py A/B 试验分组测试（窗口边界、重新入组、串组、撤回、原子性）
  stress_http.py     针对运行中服务的真实 HTTP 并发压测（手动运行）
Dockerfile
docker-compose.yml
```

## 自测

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

覆盖：正常配对与停留时长、乱序（close 先到）与更早事件补报导致的重排、
缺关闭标记未知及补关后转已知、孤儿 close、`event_id` 重传（含篡改内容）、
批量去重与整批原子回滚、撤回后路径消失/重复撤回幂等/旧事件重放与新事件均被拒、
多线程上传-查询-撤回交错（进程内 TestClient 与真实 HTTP 两种）、
无时区时间拒绝、时区偏移归一化、负停留异常、WAL 与主键的存储层校验；
以及试验分组：无浏览仍入组、窗口含起点/截止即止、首曝提前后的重新入组、
串组与名单、停留状态、只刷新受影响观众、查询只读已存样本、撤回后不可查且不可恢复、
非法曝光/坏批次不改动已存样本。

## 已知限制 / 未完成项

- 本开发沙箱没有 Docker 可执行文件/守护进程（无 `docker` 命令、无 socket、无 root，
  `unshare` 被禁用），`Dockerfile` 与 `docker-compose.yml` 已编写但**未在真实
  Docker 中构建运行**；容器外已用 uvicorn 单进程与 `--workers 2` 两种方式完成功能、
  并发与 `/docs`（OpenAPI）验证。
- 试验固定为一场（不区分试验编号）；A/B 只支持两个版别，窗口固定 30 分钟。
- 同一 `page_view_id` 若出现多个 open，取最早 open；多个 close 取最早 close，
  异常上报不报错但只保留一组配对（试验样本沿用同一规则）。
- 窗口内只有 open 没有 close、或 close 落在截止时刻之后时，试验样本记为停留未知；
  不把窗口外观测到的 close 拿来补算时长。
- 试验样本是写入时物化的派生数据：若有人直接改动 `events` 而不经过 API，样本不会
  自动重算（需重新上报该观众的事件/曝光才会刷新）。
- 没有鉴权/限流与分页；观众号、事件号均按字符串处理，未做格式白名单。
- 撤回是硬删除事件与曝光数据（仅保留撤回标记本身），不做审计留档。
