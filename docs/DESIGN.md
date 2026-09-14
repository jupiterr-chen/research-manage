# 设计说明书(Design)

> 面向各实现分支的**共同设计基线**。分支之间只通过本文 §4 定义的接口耦合;接口改动 MUST 先改本文再改代码,并在 PR 中标注 `[interface]`。与 [SPEC.md](../SPEC.md) 冲突时以 SPEC 为准。

## 1. 仓库布局

```
research-manage/
├── SPEC.md                    # 契约(唯一权威)
├── CLAUDE.md                  # 给 AI agent 的红线与工作方式
├── README.md
├── docs/
│   ├── REQUIREMENTS.md        # 需求条目 R-*
│   ├── DESIGN.md              # 本文
│   ├── ROADMAP.md             # 阶段/分支/交付
│   ├── ACCEPTANCE.md          # 验收手册 AM-*
│   ├── NAS-ACCESS.md          # 对 192.168.1.150 的操作边界
│   ├── DEPLOY.md              # 部署手册(feat/deploy 交付)
│   └── acceptance-records/    # 验收记录
├── app/                       # 管理台(FastAPI)
│   ├── __init__.py
│   ├── main.py                # uvicorn 入口:load settings → create_app
│   ├── config.py              # Settings、is_loopback_bind()、fail-closed
│   ├── db.py                  # connect()/migrate()/tx()
│   ├── models.py              # 常量与校验的唯一来源
│   ├── audit.py               # audit()、scrub()
│   ├── services/              # 纯 DB 业务逻辑(无 docker、无 HTTP)
│   │   ├── instruments.py
│   │   ├── profiles.py
│   │   ├── schedules.py
│   │   └── runs.py
│   ├── executor/
│   │   ├── worker.py          # Worker 线程
│   │   ├── launcher.py        # docker-py 封装(ContainerSpec → DockerLauncher)
│   │   ├── status_reader.py   # status.json 安全读取
│   │   ├── verdict.py         # 终态判定
│   │   └── retention.py
│   ├── scheduler.py           # APScheduler 封装
│   └── web/
│       ├── server.py          # create_app()、lifespan、异常边界
│       ├── auth.py            # token/cookie/限速
│       ├── deps.py            # 依赖注入(db、settings、current_actor)
│       ├── routes/
│       │   ├── pages.py       # /, /instruments, /runs/{id}, /login
│       │   ├── fragments.py   # /fragments/* (htmx 片段)
│       │   └── api_runs.py    # /api/v1/runs*
│       ├── templates/         # base.html + 页面 + fragments/
│       └── static/            # htmx.min.js、app.css
├── runner/
│   └── runner.py              # 执行容器内脚本(仅标准库 + tradingagents)
├── sim/                       # 本地仿真镜像
│   ├── Dockerfile
│   └── tradingagents/         # 假包,与真实 API 表面一致
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── docker-compose.local.yml
│   └── .env.example
├── scripts/
│   ├── sync_to_nas.sh
│   ├── build_sim.sh
│   └── gen_token.py
├── tests/
│   ├── conftest.py
│   ├── unit/                  # 各模块单测
│   ├── integration/           # 需要本地 Docker + sim 镜像
│   └── acceptance/            # 对应 ACCEPTANCE.md,`-m acceptance`
├── pyproject.toml
├── requirements.txt
└── requirements-dev.txt
```

## 2. 运行时拓扑与线程模型

```
uvicorn 主进程(单 worker)
├── asyncio 事件循环:FastAPI handlers(只读 SQLite / 写 SQLite;从不调 docker)
├── Worker 线程(threading.Thread, daemon=False):唯一的 docker-py 使用者
│     tick 1s:recover_once() → pick_queued() → launch → monitor loop → finalize
└── APScheduler BackgroundScheduler 线程:到点只调用 services.runs.enqueue_scheduled()
```

- 三者共用同一个 SQLite 文件,各自独立连接(`check_same_thread=False` 不共享连接对象);WAL + busy_timeout 解决并发
- Web 层需要 docker 状态时读 `Worker.health()` 的内存快照(由 Worker 每 tick 刷新),不直接碰 docker
- 进程退出:lifespan shutdown → `scheduler.shutdown(wait=False)` → `worker.stop()`(置停止标志,**不 stop 执行容器**;容器继续跑,下次启动由 recover 接管)

## 3. 配置(`app/config.py`)

| 变量 | 默认 | 说明 |
|---|---|---|
| `AM_BIND` | `127.0.0.1` | 绑定地址;非回环需 token |
| `AM_PORT` | `8090` | |
| `AM_TOKEN` | 空 | ≥32 字符;空则只允许回环绑定 |
| `AM_DB_PATH` | `{AM_DATA_DIR}/agents-manage.db` | SQLite 文件 |
| `AM_DATA_DIR` | `./data` | 管理台自有数据(容器内路径):db、`runs/<id>/` |
| `AM_DATA_HOST` | 同 `AM_DATA_DIR` | 同一目录的**宿主机路径**,供 docker 挂载 `runs/<id>` → `/ws` |
| `AM_TA_DATA_DIR` | 必填 | 管理台读 TA 产物的路径(容器内 `/ta-data`;本地开发=宿主路径) |
| `AM_TA_DATA_HOST` | 必填 | TA data 的宿主机路径,供执行容器挂载 |
| `AM_TA_ENV_HOST` | 必填 | `.env` 宿主机路径(管理台**不读**,只传给 docker 挂载) |
| `AM_RUNNER_HOST` | 必填 | `runner.py` 宿主机路径 |
| `AM_TA_IMAGE` | `tradingagents-tradingagents:latest` | 本地开发用 `tradingagents-sim:latest` |
| `AM_TA_CONTAINER_DATA` | `/home/appuser/.tradingagents` | 执行容器内 data 路径 |
| `AM_TA_WORKDIR` | `/home/appuser/app` | 执行容器 working_dir(`.env` 挂到这里) |
| `AM_SMB_PREFIX` | 空 | artifacts 路径前缀,如 `\\192.168.1.150\docker\TradingAgents\data` |
| `AM_WATCHDOG_MINUTES` | `120` | |
| `AM_STALE_MINUTES` | `20` | |
| `AM_RETENTION_DAYS` | `90` | |
| `AM_STATUS_POLL_SECONDS` | `5` | |
| `AM_STOP_TIMEOUT` | `20` | docker stop 宽限 |
| `AM_SIM_ENV` | 空 | **仅本地开发**:附加给执行容器的 env(如 `SIM_MODE=fail`),生产 MUST 为空 |
| `TZ` | `Asia/Shanghai` | |

`Settings.load()` 顺序:读 env → 类型转换 → `validate()`(绑定门禁、必填、数值范围)→ 失败 `SystemExit(2)` 并打印原因。`Settings.summary()` 返回不含 `AM_TOKEN` 的 dict 供启动日志。

## 4. 模块接口(分支间契约)

### 4.1 `app/models.py`

```python
MARKETS = ("us", "hk", "cn")
ANALYSTS = ("market", "social", "news", "fundamentals")
FIXED_AGENTS = ("Bull Researcher", "Bear Researcher", "Research Manager", "Trader",
                "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager")
ANALYST_AGENT = {"market": "Market Analyst", "social": "Social Analyst",
                 "news": "News Analyst", "fundamentals": "Fundamentals Analyst"}
RUN_STATUS = ("queued", "running", "succeeded", "failed", "cancelled")
TRIGGERS = ("web", "api", "schedule")

def agents_total(analysts: Sequence[str]) -> int            # len + 8
def agent_sequence(analysts) -> list[str]                    # 时间线顺序:分析师(按 ANALYSTS 顺序) + FIXED_AGENTS
def normalize_code(market: str, raw: str) -> str             # 去空白、大写;hk 补零到 4 位
def validate_code(market: str, code: str) -> None            # 不合法 raise ValueError(中文原因)
def parse_analysts(csv: str) -> tuple[str, ...]              # 去重、按 ANALYSTS 顺序;非法 raise ValueError
def validate_date(s: str) -> str                             # YYYY-MM-DD,不晚于今天(Asia/Shanghai)
def make_run_id(code: str, now: datetime) -> str
```

代码格式:us `^[A-Z][A-Z0-9.\-]{0,9}$`;hk `^\d{4}\.HK$`;cn `^\d{6}\.(SS|SZ)$`。

### 4.2 `app/db.py`

```python
def connect(path: str) -> sqlite3.Connection      # row_factory=Row, WAL, busy_timeout=5000, foreign_keys=ON
def migrate(conn) -> None                          # 幂等;SPEC §4 DDL + 种子档案
@contextmanager
def tx(conn): ...                                  # BEGIN IMMEDIATE / COMMIT / ROLLBACK
```

### 4.3 `app/audit.py`

```python
def scrub(obj: Any) -> Any            # 递归;键名匹配 /key|token|secret|password|authorization/i 的值 → "***";字符串中 sk-[A-Za-z0-9]{8,} → "sk-***"
def audit(conn, actor: str, action: str, entity: str, entity_id: str | None, detail: dict | None) -> None
```

### 4.4 `app/services/runs.py`(核心)

```python
class BusyError(Exception): current: dict; queued: int
class AlreadyDoneError(Exception): run_id: str
class ConflictError(Exception): ...               # 状态不允许
class NotFound(Exception): ...

def create_run(conn, *, code: str, market: str | None, date: str | None, analysts: Sequence[str] | None,
               profile_id: int | None, trigger: str, actor: str, force: bool = False) -> dict
def enqueue_scheduled(conn, *, schedule_id: int, date: str) -> dict | None   # None = deduped
def request_cancel(conn, run_id: str, actor: str) -> dict
def resume(conn, run_id: str, actor: str) -> dict
def get(conn, run_id) -> dict; def list_runs(conn, *, status=None, code=None, limit=50) -> list[dict]
def current_and_queue(conn) -> tuple[dict | None, list[dict]]
def artifacts(conn, settings, run_id) -> list[str]
# 以下仅 Worker 调用
def pick_next_queued(conn) -> dict | None
def mark_running(conn, run_id, container_id) -> None
def update_progress(conn, run_id, *, current_agent, agents_done, tokens_in, tokens_out, stale: bool) -> None
def finalize(conn, run_id, *, status, exit_code, error, report_ready) -> None
def orphans_running(conn) -> list[dict]
```

`create_run` 事务内顺序:标的 → analysts → 忙判定 → already_done → 活动去重(`uq_run_active` 兜底)→ INSERT → audit。`profile_id` 与 `analysts` 二选一,都空时取标的第一条 enabled schedule 的档案,再取 `is_default` 档案。

### 4.5 `app/executor/launcher.py`

```python
@dataclass(frozen=True)
class ContainerSpec:
    image: str; run_id: str; ticker: str; date: str; analysts: tuple[str, ...]
    ta_data_host: str; ta_env_host: str; runner_host: str; workspace_host: str
    ta_container_data: str; workdir: str; stop_timeout: int; extra_env: dict[str, str]

class DockerLauncher:
    def __init__(self, client: docker.DockerClient | None = None)   # None → from_env()
    def ping(self) -> bool
    def image_exists(self, image: str) -> bool
    def start(self, spec: ContainerSpec) -> str                     # 返回 container id
    def wait(self, container_id: str, timeout: int) -> int | None   # None = 仍在运行
    def exists(self, container_id: str) -> bool
    def stop(self, container_id: str, timeout: int) -> None
    def logs_tail(self, container_id: str, n: int = 200) -> str
    def remove(self, container_id: str) -> None
```

`start()` 生成的 `containers.run(...)` 参数(**逐项对应 SPEC §6.3**):`image`, `name=f"am-{run_id}"`, `entrypoint=["python","/runner.py"]`, `command=["--ticker",...]`, `working_dir=workdir`, `volumes={ta_data_host: {bind: ta_container_data, mode: "rw"}, ta_env_host: {bind: f"{workdir}/.env", mode: "ro"}, runner_host: {bind: "/runner.py", mode: "ro"}, workspace_host: {bind: "/ws", mode: "rw"}}`, `environment={"AM_RUN_ID": run_id, "TZ": ..., **extra_env}`, `stop_signal="SIGINT"`, `detach=True`, `labels={"am.run_id": run_id}`。`extra_env` 生产为空(`AM_SIM_ENV` 只在本地)。

### 4.6 `app/executor/status_reader.py`

```python
@dataclass
class Status: phase: str; current_agent: str | None; agents_done: int; agents_total: int
              tokens_in: int; tokens_out: int; updated_at: datetime | None; error: str | None
def read_status(path: Path) -> Status | None      # 文件不存在/JSON 损坏/字段缺失 → None,绝不抛
```

### 4.7 `app/executor/verdict.py`

```python
@dataclass
class Verdict: status: str; error: str | None; report_ready: bool
def decide(*, exit_code: int | None, cancelled_by_us: bool, status: Status | None,
           ta_data_dir: Path, code: str, date: str) -> Verdict
def report_ready(ta_data_dir, code, date) -> bool           # investment_plan.md 存在
def memory_has_entry(ta_data_dir, code, date) -> bool       # 任一行 startswith(f"[{date} | {code} |")
```

### 4.8 `app/executor/worker.py`

```python
class Worker(threading.Thread):
    def __init__(self, settings, launcher: DockerLauncher, db_factory: Callable[[], Connection])
    def start(self); def stop(self); def health(self) -> dict   # {alive, docker_ok, queue_depth, current_run_id, last_tick}
    # 内部:run() → self_check() → recover_once() → loop(tick=1s)
```

监控子循环(每 tick):
1. `exit = launcher.wait(cid, timeout=5)`
2. 每 `AM_STATUS_POLL_SECONDS` 读 status.json → `update_progress`(stale 规则见 R-EXE-05)
3. 若 `cancel_requested_at` 或超过看门狗 → `launcher.stop(cid, AM_STOP_TIMEOUT)`,记 `cancelled_by_us=True`(看门狗时 error 预置 `watchdog_timeout`)
4. `exit is not None` → `verdict.decide(...)` → 写 container.log → `remove` → `finalize`

### 4.9 `app/scheduler.py`

```python
class Scheduler:
    def __init__(self, settings, db_factory); def start(self); def shutdown(self)
    def rebuild_jobs(self) -> int                       # 从 schedule 表全量重建,返回 job 数
    def next_fires(self, today: date) -> list[dict]     # [{schedule_id, code, at, analysts}]
```
`services.schedules` 的每个写操作结束后调用 `app.state.scheduler.rebuild_jobs()`。

### 4.10 Web 路由

| 路径 | 类型 | 说明 |
|---|---|---|
| `GET /login`, `POST /login`, `POST /logout` | 页面 | |
| `GET /` | 页面 | 壳 + 首屏数据 |
| `GET /fragments/overview` | 片段 | 当前任务卡+队列+最近 20 条+今日预告;`hx-trigger="every 15s"` |
| `POST /fragments/run` | 片段 | 手动发起;成功 → 返回刷新后的 overview 片段;409 → 返回 busy 提示片段(HTTP 200 + 内联) |
| `GET /instruments` | 页面 | |
| `POST /fragments/instruments`、`POST /fragments/instruments/{id}`、`DELETE ...` | 片段 | 行内编辑返回该行片段 |
| `POST /fragments/schedules`、`POST /fragments/schedules/{id}`、`DELETE ...`、`POST .../toggle` | 片段 | |
| `GET /runs/{id}` | 页面 | |
| `GET /fragments/runs/{id}` | 片段 | 时间线+进度,`every 15s`,终态后停止轮询(`hx-trigger` 由模板按状态决定) |
| `POST /fragments/runs/{id}/cancel`、`/resume` | 片段 | |
| `GET /healthz` | JSON | |
| `/api/v1/runs*` | JSON | 见 SPEC §5 |

htmx 约定:片段接口只返回 `<div id=...>` 可 `hx-swap="outerHTML"` 的块;错误信息渲染在片段内 `.flash-error`;破坏性按钮用 `hx-confirm`。

### 4.11 鉴权(`app/web/auth.py`)

- `Authorization: Bearer <token>` 或 cookie `am_session`(值 = `hmac(token, "session")` 的十六进制,不存 token 本身)
- 依赖 `require_auth`:失败时 API 路径 401 JSON,页面路径 302 `/login?next=`
- 登录限速:内存 `{ip: deque[timestamps]}`,5 次/60s;超限 429
- `secrets.compare_digest` 比较

## 5. 状态机

```
                 ┌──────────┐ pick_next_queued ┌─────────┐
  create_run ───▶│  queued  │─────────────────▶│ running │
                 └────┬─────┘                  └──┬──┬──┬┘
        request_cancel│                           │  │  │
                      ▼                           │  │  │ exit=0 且双重判定通过
                 ┌───────────┐  exit=130 / 主动stop│  │  ▼
                 │ cancelled │◀────────────────────┘  │ ┌───────────┐
                 └─────┬─────┘                        │ │ succeeded │
                       │ resume(新 run)               │ └───────────┘
                       ▼                              ▼ exit≠0 / 判定失败 / host_restarted / worker_exception
                   queued(新)                      ┌────────┐
                                                   │ failed │──resume──▶ queued(新)
                                                   └────────┘
```

看门狗触发 = 主动 stop,终态 cancelled,error=`watchdog_timeout`。

## 6. Runner 设计(`runner/runner.py`)

```
main()
 ├ parse_args + 白名单校验(ticker 正则、date、analysts ⊆ ANALYSTS)
 ├ StatusWriter(workspace/status.json):write(phase, current_agent, agents_done, ...) 原子写
 ├ self_check()  → 缺属性:write(failed, error=upstream_api_changed); exit 2
 ├ 安装 SIGINT 处理:默认行为(KeyboardInterrupt),不覆盖
 ├ graph = TradingAgentsGraph(debug=False, config=DEFAULT_CONFIG.copy(), selected_analysts=analysts)
 ├ graph._resolve_pending_entries(ticker)
 ├ tid = graph.begin_checkpoint(ticker, date, "stock")
 ├ try:
 │   state0 = graph.propagator.create_initial_state(ticker, date, asset_type="stock",
 │              past_context=graph.memory_log.get_past_context(ticker, as_of=graph._memory_as_of(date)),
 │              instrument_context=graph.resolve_instrument_context(ticker, "stock"))
 │   args = graph.propagator.get_graph_args(); 注入 thread_id、callbacks=[TokenCounter()]
 │   for chunk in graph.graph.stream(graph.checkpoint_input(state0), **args):
 │       tracker.consume(chunk)   # 更新 agent 状态、写 reports/<section>.md、写 status.json
 │       trace.append(chunk)
 │   final_state = merge(trace)
 │   graph._log_state(date, final_state)
 │   graph.memory_log.store_decision(ticker=ticker, trade_date=date, final_trade_decision=final_state["final_trade_decision"])
 │   graph.clear_checkpoint_on_success(ticker, date, "stock")
 │   write(succeeded); exit 0
 │ except KeyboardInterrupt: write(phase=running, error="interrupted"); exit 130
 │ except Exception as e: write(failed, error=f"{type(e).__name__}: {str(e)[:200]}"); exit 1
 └ finally: graph.end_checkpoint()
```

- `tracker` 的节点→agent 映射与 section→文件名映射**照抄** `cli/main.py`(`ANALYST_MAPPING`、`REPORT_SECTIONS`、`update_research_team_status` 等逻辑);报告目录 `Path(config["results_dir"]) / ticker / date / "reports"`
- `TokenCounter`:LangChain `BaseCallbackHandler.on_llm_end` 累加 `usage_metadata`;不可用时静默为 0
- `create_initial_state` 的精确参数以 `trading_graph.py::_run_graph` 当前实现为准,实现时核对一次

## 7. 仿真镜像 `sim/`

目的:让**runner 本身**和执行器在本地 Docker 上端到端跑通,无需 NAS。

- `sim/tradingagents/`:`__init__.py`(读 `.env` 存在性,写入 marker 以验证挂载)、`default_config.py`(`DEFAULT_CONFIG` 含 `results_dir/data_cache_dir/memory_log_path` 指向 `~/.tradingagents`)、`graph/trading_graph.py`(`TradingAgentsGraph` 提供 8 个属性;`graph.stream()` 按 `agent_sequence` 逐节点 yield 与真实同形的 chunk,每节点 sleep `SIM_STEP_SECONDS`;`_log_state` 写 json;`memory_log.store_decision` 追加 `[date | ticker | Hold | pending]`;断点用一个 json 文件模拟 `begin/clear/end_checkpoint`,`checkpoint_input` 在有断点时从已完成节点之后继续)
- `SIM_MODE`:`ok`(默认)| `fail`(第 3 节点抛异常)| `hang`(第 3 节点后 sleep 1h,SIGINT 可中断)| `slow`(每节点 30s)| `no_memory`(不写 memory)| `no_report`(不写 investment_plan.md)| `upstream_changed`(缺 `_log_state` 属性)| `corrupt_status`(写半截 status.json 一次)
- 镜像用户 `appuser` uid 1000,`WORKDIR /home/appuser/app`,ENTRYPOINT `tradingagents`(占位 shell),与真实镜像同形
- `scripts/build_sim.sh` → `docker build -t tradingagents-sim:latest sim/`

## 8. 本地开发环境

```
# 一次性
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt -r requirements-dev.txt
scripts/build_sim.sh
mkdir -p .local/ta-data .local/am-data && echo "SIM=1" > .local/ta.env
# .env.local(不入库)
AM_BIND=127.0.0.1  AM_TA_IMAGE=tradingagents-sim:latest
AM_TA_DATA_DIR=D:/.../.local/ta-data   AM_TA_DATA_HOST=D:/.../.local/ta-data
AM_DATA_DIR=D:/.../.local/am-data      AM_DATA_HOST=D:/.../.local/am-data
AM_TA_ENV_HOST=D:/.../.local/ta.env    AM_RUNNER_HOST=D:/.../runner/runner.py
# 运行(应用只读 OS 环境变量;dev.sh 负责 source .env.local)
scripts/dev.sh
```
Docker Desktop(Windows)的 bind 源路径用 `D:/x/y` 形式;docker-py 直接接受。

## 9. 测试策略

| 层 | 位置 | 依赖 | 覆盖 |
|---|---|---|---|
| 单元 | `tests/unit/` | 无 | models/config/db/services/verdict/status_reader/scheduler(冻结时间)/auth/runner(mock graph) |
| 集成 | `tests/integration/` | 本地 Docker + sim 镜像 | Worker 真启容器:ok/fail/hang+cancel/no_memory/corrupt_status/resume |
| 验收 | `tests/acceptance/` | 同上 | 一一对应 ACCEPTANCE.md 的自动化项,`pytest -m acceptance` |
| 形状 | `tests/shape/` | `vendor/tradingagents`(从 NAS 只读拷贝) | runner.self_check 对真实包通过 |

fake docker:`tests/conftest.py` 提供 `FakeLauncher`(实现 `DockerLauncher` 同接口,内存模拟容器生命周期与 status.json 写入),供 Worker 单测。

## 10. 设计决策记录(ADR)

| # | 决策 | 理由 |
|---|---|---|
| ADR-1 | docker 调用集中在 Worker 线程;Web 取消只置列 | 单点串行化,避免 Web 与 Worker 对同一容器竞态;Web 异常与 docker 隔离 |
| ADR-2 | runner 合并 CLI 流式与 propagate 收尾 | 上游无单一路径同时产出 reports 与 memory(SPEC §1.3 实证) |
| ADR-3 | `.env` 挂载而非解析注入 | 密钥零接触;`docker inspect` 不暴露 |
| ADR-4 | `AM_*_HOST` 与 `AM_*_DIR` 分离 | 管理台在容器内运行,docker 挂载需要宿主路径 |
| ADR-5 | 仿真镜像复刻上游 API 表面 | 让 runner 与执行器在本地端到端验证,NAS 只做最终验收 |
| ADR-6 | 纯 `sqlite3`,不用 ORM | 与 stocks-alert 一致;表少、查询简单 |
| ADR-7 | 工作区放在管理台自有 `data/runs/` | 不向被管理对象的数据目录写入本系统产物 |
| ADR-8 | run.analysts_csv 快照 | 档案可变;断点签名与审计需要不可变输入 |
