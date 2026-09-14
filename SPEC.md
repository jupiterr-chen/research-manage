# Agents-Manage — TradingAgents 研究任务管理台 · 实现规格书 v1.1

> **文档性质**:实现契约 + 上下文交接。本文档面向**全新会话的实现者**,不依赖任何前置对话。
> **MUST** 为验收硬门槛;**SHOULD** 为强烈建议。
> 风格与硬约束体系对齐既有项目 `D:\2.Develop\5.Codex\stocks-alert\docs\WEBUI-SPEC.md`(可参考其实现),冲突时以本文档为准。
> v1.1 变更摘要见文末「附 B 修订记录」。

---

## 0. 一句话目标

在内网 NAS 上部署一个 Web 管理台:可视化配置"市场-标的"的研究调度(定时/手动),驱动 NAS 上已部署的 **TradingAgents** 多智能体分析框架执行,跟踪执行进度与状态,展示"报告是否已生成"。**纯任务管理台:不渲染任何报告内容,不涉及交易。**

明确不做(OUT OF SCOPE):报告内容展示(报告走 SMB 网络驱动器直接看原文件)、回测、实盘信号、多用户体系、移动端适配、WebSocket/SSE 推送、通知推送(v1 纯轮询)、**TradingAgents 自身的部署/升级/补丁/模型配置(由其自身运维体系负责,本系统只依赖 §1 列出的契约)**。

## 1. 依赖的既有环境与契约(实现前必读)

### 1.1 运行环境

| 项 | 值 |
|---|---|
| NAS | 飞牛 fnOS(Debian 系),`chen@192.168.1.150`,SSH 免密,chen(uid=1000)在 docker 组(gid=994),有免密 sudo |
| Docker | 28.5.2 / Compose v2.40.3 |
| Windows 工作机 | 与 NAS 同内网;用户通过 SMB 访问 NAS 文件 |

### 1.2 TradingAgents(被管理对象:本系统只读它的产物、只启动它的镜像)

- **镜像**:`tradingagents-tradingagents:latest`(ENTRYPOINT=`tradingagents` CLI;容器内用户 `appuser` uid=1000;工作目录 `/home/appuser/app`)。TradingAgents 升级后镜像 tag 不变,本系统无需改动
- **配置**:全部在 `/home/chen/docker/TradingAgents/.env`(模型、端点、代理、超时、断点开关)。`tradingagents` 包在 import 时自动 `load_dotenv(find_dotenv(usecwd=True))`,即**只要 `.env` 出现在容器工作目录,框架自己读取**——本系统 MUST NOT 解析该文件(见 §6.3)
- **数据目录**:宿主 `/home/chen/docker/TradingAgents/data`(owner 1000:1000)↔ 容器 `/home/appuser/.tradingagents`,内含:
  - `logs/<TICKER>/<YYYY-MM-DD>/reports/*.md`:分段报告(market/sentiment/news/fundamentals_report、investment_plan、trader_investment_plan、final_trade_decision)。`investment_plan.md` 为**增量写入**,中途内容是辩论陈词
  - `logs/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json`:全量终态
  - `memory/trading_memory.md`:决策记忆,每条以 `[YYYY-MM-DD | TICKER | 评级 | pending]` 开头;下一次同标的运行会把 `pending` 改写为 `resolved:...`
  - `cache/checkpoints/<TICKER>.db`(-shm/-wal 伴生):LangGraph 断点
- **执行性能参考**:四分析师全量一次约 30~45 分钟;单 LLM 调用有框架内超时与重试兜底

### 1.3 无头执行入口(⚠️ 核心事实,决定 runner 的形态)

TradingAgents 有两条执行路径,**各自只产出一半产物**(已在 NAS 源码与数据目录上核实):

| 路径 | 进度事件 | `logs/<T>/<D>/reports/*.md` | `trading_memory.md` 条目 + `full_states_log` |
|---|---|---|---|
| `cli/main.py`(交互 TUI) | 有(`graph.graph.stream`) | ✅ 由 CLI 自己按 section 写 | ❌ 不写 |
| `TradingAgentsGraph.propagate()`(Python API) | 无(`invoke`) | ❌ 不写 | ✅ `_log_state` + `memory_log.store_decision` |

因此 runner MUST 自行组合两条路径(见 §6.1),不能把任一条当作薄封装。关键 API(`tradingagents/graph/trading_graph.py`):

- `TradingAgentsGraph(debug=False, config=DEFAULT_CONFIG.copy(), selected_analysts=(...))`:`DEFAULT_CONFIG` 已应用容器 env,本系统**不传**任何模型参数
- 调用序列:`graph._resolve_pending_entries(ticker)` → `graph.begin_checkpoint(ticker, date, asset_type)` → `graph.propagator.create_initial_state(...)` → `graph.graph.stream(graph.checkpoint_input(state0), **graph.propagator.get_graph_args())` → `graph._log_state(date, final_state)` → `graph.memory_log.store_decision(...)` → `graph.clear_checkpoint_on_success(...)` → finally `graph.end_checkpoint()`
- **断点签名** = `analysts + debate 轮数 + risk 轮数 + asset_type`,编进 thread_id:同 ticker+date+同分析师集合才会续跑;**分析师集合不同 = 从头跑**。成功完成后断点被清除,再次运行同 ticker+date 是完整重跑
- `stream` 是同步 API,runner 不需要 asyncio
- token 统计:CLI 通过 LangChain callback(`cli/main.py` 中 `stats_handler`)挂到 `args["config"]["callbacks"]`,runner MUST 同样挂载,否则 tokens 恒为 0
- 上游 CLI 的流式循环(`cli/main.py` `run_analysis` 内 `for chunk in graph.graph.stream(...)` 段)是 runner 的蓝本:节点→agent 状态映射、section→文件写入均照抄

## 2. 总体架构

```
浏览器(内网)                    NAS 192.168.1.150
──────────────                 ─────────────────────────────────────────
 │ HTTP + token                ┌────────────────────────────────────┐
 ├────────────────────────────▶│ agents-manage 容器 (uid 1000)       │
 │  htmx 轮询 15s              │  FastAPI + Jinja2 + htmx            │
 │                             │  SQLite(唯一事实源) + APScheduler   │
 │                             │  执行器: docker-py via              │
 │                             │          /var/run/docker.sock       │
 │                             └──────────────┬─────────────────────┘
 │                                             │ 每个任务创建兄弟容器
 │  文件直达(SMB)              ┌──────────────▼─────────────────────┐
 ├─────────────────────────    │ 执行容器(tradingagents 镜像)        │
 │  TA data/logs/.../reports/  │ runner.py(挂载进入) + stream       │
 │  AM data/runs/<id>/         │ 逐节点写 status.json + reports/*.md │
 └─────────────────────────    └────────────────────────────────────┘
```

- 管理台容器与执行容器**分离**:执行崩溃/挂死不影响管理台;执行容器直接复用既有镜像与 `.env`,TradingAgents 升级不经过本系统代码
- docker.sock 方案已获用户认可(内网+token 鉴权缓解)。SHOULD:经 `docker-socket-proxy`(仅放行 containers/images 相关端点)访问,而非直挂 sock

## 3. 硬约束

1. 🔴 **SQLite 唯一事实源**。MUST NOT 引入第二存储(含 APScheduler 持久化 JobStore——MUST 用 MemoryJobStore,启动及 schedule 变更时从 SQLite 重建);status.json 是执行侧产物,两者不一致时以容器退出码 + 文件系统实况仲裁
2. 🔴 **无构建步骤**:MUST NOT 引入 npm/node/webpack/vite/React/Vue/CDN。Jinja2 服务端渲染 + htmx(单文件随仓库)+ 手写 CSS。页面离线可用
3. 🔴 **执行逻辑不在 Web 层**:管理台只做 发起/跟踪/落档;所有 TradingAgents 调用发生在 runner.py(执行容器内)
4. 🔴 **并发 = 1,单工作线程消费队列**。手动发起:存在 running 或 queued 任务时返回 409(含当前任务信息),不入队;调度触发:一律入队(FIFO)
5. 🔴 **同一 (标的, 分析日期) 在 queued/running 中唯一**(部分唯一索引)。调度入队遇已存在:保留分析师集合更大的一个,审计 `deduped`;已 succeeded 的 (标的,日期) 再跑 MUST 显式 `force=true`(成功后断点已清,会是完整重跑)
6. 🔴 **鉴权 fail-closed**:默认绑 127.0.0.1;绑定任何非回环地址(含 0.0.0.0/::/具体网卡 IP/带空格变体)且未配置 token 时拒绝启动;token ≥32 字符;登录限速同 IP 5 次/分钟
7. 🔴 **凭据零接触**:管理台 MUST NOT 读取、解析、注入 `.env`;`.env` 只以只读 bind mount 交给执行容器(§6.3)。任何页面/响应/日志/审计/`docker inspect` 中不得出现密钥
8. 🔴 **写操作全审计**(actor: web/api/schedule),幂等可追溯
9. 🔴 Web 层异常 MUST NOT 影响正在执行的 run(执行在兄弟容器;handler 仍需异常边界)
10. 🔴 **宿主路径与容器路径分离**:传给 docker-py 的所有挂载源 MUST 是宿主机路径(来自 `AM_*_HOST` 配置),不得复用管理台容器内路径

## 4. 数据模型(SQLite DDL)

```sql
CREATE TABLE IF NOT EXISTS instrument (
  id       INTEGER PRIMARY KEY,
  market   TEXT NOT NULL CHECK(market IN ('us','hk','cn')),   -- crypto 见 v2
  code     TEXT NOT NULL,              -- 如 1810.HK / NVDA / 600519.SS
  name     TEXT NOT NULL DEFAULT '',
  enabled  INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(market, code)
);

CREATE TABLE IF NOT EXISTS profile (               -- 分析师组合模板(仅作表单预填)
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,                       -- 如 "日频三件套" / "全量"
  analysts_csv TEXT NOT NULL,                      -- 逗号分隔: market,social,news,fundamentals 子集
  is_default INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schedule (
  id INTEGER PRIMARY KEY,
  instrument_id INTEGER NOT NULL REFERENCES instrument(id),
  profile_id    INTEGER NOT NULL REFERENCES profile(id),
  kind     TEXT NOT NULL CHECK(kind IN ('daily_trading','weekly')),
  at_time  TEXT NOT NULL DEFAULT '08:30',          -- HH:MM,Asia/Shanghai
  weekday  INTEGER,                                -- weekly 用:1=周一
  enabled  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS run (
  id TEXT PRIMARY KEY,                             -- r-YYYYMMDD-HHMMSS-<code 去点>
  instrument_id INTEGER NOT NULL REFERENCES instrument(id),
  profile_id    INTEGER REFERENCES profile(id),    -- 可空(临时勾选分析师时)
  analysts_csv  TEXT NOT NULL,                     -- 不可变快照,resume/断点匹配以此为准
  analysis_date TEXT NOT NULL,                     -- YYYY-MM-DD
  status TEXT NOT NULL CHECK(status IN
    ('queued','running','succeeded','failed','cancelled')),
  trigger TEXT NOT NULL CHECK(trigger IN ('web','api','schedule')),
  resumed_from TEXT REFERENCES run(id),
  container_id TEXT, exit_code INTEGER,
  current_agent TEXT, agents_done INTEGER DEFAULT 0, agents_total INTEGER NOT NULL,  -- = 分析师数 + 8
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  report_ready INTEGER DEFAULT 0,                  -- 见 §6.4
  status_stale INTEGER DEFAULT 0,                  -- status.json 超时未更新/损坏
  cancel_requested_at TEXT,                        -- Web/API 只置此列,docker stop 由工作线程执行
  error TEXT, started_at TEXT, finished_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_status ON run(status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_active
  ON run(instrument_id, analysis_date) WHERE status IN ('queued','running');

CREATE TABLE IF NOT EXISTS audit_log (             -- 同 WEBUI-SPEC §5.2
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  actor TEXT NOT NULL CHECK(actor IN ('web','api','schedule')),
  action TEXT NOT NULL, entity TEXT NOT NULL, entity_id TEXT,
  detail_json TEXT                                  -- MUST NOT 含凭据
);
```

- 迁移 MUST 幂等(`PRAGMA table_info` 检查后 CREATE/ALTER)
- 连接 MUST 启用 WAL + `busy_timeout`(工作线程与 Web 线程共写)
- `agents_total` = `len(analysts) + 8`(Bull/Bear Researcher、Research Manager、Trader、Aggressive/Conservative/Neutral Analyst、Portfolio Manager);四分析师 = 12,三件套 = 11

## 5. 对外 JSON API(`/api/v1`,与页面同 token 鉴权)

v1 只暴露 **runs**(供 stocks-alert 等外部系统触发/查询);标的/档案/调度的增删改走页面 HTML 表单(htmx),JSON 版本放 v2。全部 JSON;写操作 POST。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/runs` | `{code, date?, analysts?, profile_id?, force?}`(date 缺省=当天;analysts/profile 缺省=标的第一条 schedule 的档案或系统默认档案)。**忙时(存在 running/queued)返回 409**:`{"error":"busy","current":{...当前任务与进度...},"queued":n}`,零副作用。同 (标的,日期) 已 succeeded 且无 `force` → 409 `already_done` |
| GET | `/api/v1/runs?status=&code=&limit=` | 历史列表 |
| GET | `/api/v1/runs/{id}` | 详情:status/current_agent/agents_done/agents_total/tokens/report_ready/status_stale/error/时间戳 |
| GET | `/api/v1/runs/{id}/artifacts` | 报告文件路径列表(以 `AM_SMB_PREFIX` 拼成 SMB 路径返回,MUST NOT 返回文件内容) |
| POST | `/api/v1/runs/{id}/cancel` | 仅 queued/running 可取消:queued 直接置 cancelled;running 置 `cancel_requested_at`,由工作线程 docker stop(SIGINT,20s 宽限)→ status=cancelled;断点保留。所有 docker 调用 MUST 只发生在工作线程 |
| POST | `/api/v1/runs/{id}/resume` | 仅 cancelled/failed 可续:新建 run(`resumed_from`=原 id,`analysts_csv` **MUST 复制原 run**,否则断点签名不匹配会从头跑),同 code+date 走断点。忙时同样 409 |

参数校验:代码格式 MUST 校验(与 yfinance 约定一致:us 裸代码、hk `####.HK`、cn `######.SS/.SZ`);`analysts` MUST ⊆ {market,social,news,fundamentals} 且非空。
错误码:400 参数非法 / 401 未鉴权 / 404 不存在 / 409 忙或冲突;错误体 `{"error": "<machine_readable>", "message": "<中文人话>"}`。

## 6. 执行链路契约(核心)

### 6.1 runner.py(随本仓库维护,挂载进执行容器)

CLI:`python /runner.py --ticker 1810.HK --date 2026-09-14 --analysts market,social,news --workspace /ws --run-id <id>`

职责(按序,对应 §1.3 两条路径的合并):
1. **启动自检**:断言 `TradingAgentsGraph` 具备 `_resolve_pending_entries`、`begin_checkpoint`、`checkpoint_input`、`propagator`、`_log_state`、`memory_log.store_decision`、`clear_checkpoint_on_success`、`end_checkpoint`;缺任一项 → 退出码 2 + status.json `error="upstream_api_changed"`。目的:上游升级后**响亮失败**而非静默少写产物
2. `config = DEFAULT_CONFIG.copy()`(继承容器 env;不传任何模型参数);`graph = TradingAgentsGraph(debug=False, config=config, selected_analysts=tuple(analysts))`
3. `graph._resolve_pending_entries(ticker)`(记忆反思,保持 propagate 语义)
4. `tid = graph.begin_checkpoint(ticker, date, "stock")`;`state0 = graph.propagator.create_initial_state(...)`(参数照抄 `_run_graph`:past_context / instrument_context);`args = graph.propagator.get_graph_args()`,注入 thread_id 与 token 统计 callback
5. `for chunk in graph.graph.stream(graph.checkpoint_input(state0), **args)`:每个 chunk →
   - 更新 agent 状态(映射照抄 `cli/main.py`)并**原子写** `<workspace>/status.json`(临时文件 + rename)
   - 按 section 写 `logs/<T>/<D>/reports/<section>.md`(写法照抄 CLI)
6. 合并 chunk 为 `final_state`;`graph._log_state(date, final_state)`;`graph.memory_log.store_decision(ticker=..., trade_date=..., final_trade_decision=final_state["final_trade_decision"])`;`graph.clear_checkpoint_on_success(...)`
7. `finally: graph.end_checkpoint()`。runner MUST NOT 吞 `KeyboardInterrupt`
8. **退出码契约**:`0` 成功 / `1` 失败(异常,status.json 写一句话原因)/ `2` 自检失败 / `130` 收到 SIGINT

### 6.2 status.json schema(执行侧唯一进度产物,管理台每 5s 轮询读)

```json
{
  "run_id": "r-20260914-083000-1810HK", "ticker": "1810.HK", "date": "2026-09-14",
  "phase": "running",
  "current_agent": "Bear Researcher",
  "agents_done": 6, "agents_total": 11,
  "tokens_in": 210000, "tokens_out": 98000,
  "updated_at": "2026-09-14T08:51:12+08:00",
  "error": null
}
```

`phase` ∈ `starting|running|succeeded|failed`。管理台读到半截/损坏 JSON 时 MUST 保持上次值并置 `status_stale=1`;`updated_at` 超过 `AM_STALE_MINUTES`(默认 20)未变且容器仍在运行 → 同样标 stale(不取消,只提示)。

### 6.3 执行容器启动参数

镜像 `AM_TA_IMAGE`(默认 `tradingagents-tradingagents:latest`);`working_dir=/home/appuser/app`;`user` 不覆盖(镜像内 appuser=1000);挂载(源均为**宿主路径**):

| 宿主(配置项) | 容器内 | 模式 |
|---|---|---|
| `AM_TA_DATA_HOST`=`/home/chen/docker/TradingAgents/data` | `/home/appuser/.tradingagents` | rw |
| `AM_TA_ENV_HOST`=`/home/chen/docker/TradingAgents/.env` | `/home/appuser/app/.env` | **ro**(框架自行 load_dotenv;管理台不读) |
| `AM_RUNNER_HOST`=`/home/chen/docker/agents-manage/runner/runner.py` | `/runner.py` | ro |
| `AM_DATA_HOST/runs/<run_id>` | `/ws` | rw |

- `entrypoint=["python","/runner.py", ...]`;`stop_signal=SIGINT`,`stop_timeout=20`(给 LangGraph 落盘断点的机会);`network_mode` 沿用默认 bridge(代理地址来自 `.env`)
- MUST NOT 通过 `environment` 传任何来自 `.env` 的值(`docker inspect` 会明文暴露)
- 容器退出后 MUST 把 `docker logs` 尾部(最多 200 行)写入 `runs/<id>/container.log` 文件(不渲染到页面,页面只给路径),然后 `remove` 容器

### 6.4 状态机与判定

- 工作循环(单线程):取最早 queued → 置 running(记 container_id、started_at)→ `container.wait(timeout=5)` 循环 + 轮询 status.json 同步进度到 SQLite
- **终态判定**(按顺序):
  - 退出码 130 或本系统主动 stop → `cancelled`
  - 退出码 ≠ 0 → `failed`(error 取 status.json.error,缺省 `exit_<code>`)
  - 退出码 0 时 **succeeded 判定 MUST 双重**:`logs/<code>/<date>/reports/investment_plan.md` 存在 **且** `memory/trading_memory.md` 中存在以 `[<date> | <code> |` 开头的行(不要求 `pending`,后续运行会改写为 `resolved:`);不满足 → `failed`(error 写明缺哪个)
- **report_ready**:investment_plan.md 存在即为 1(只展示"报告已生成 ✓",**不展示评级/决策内容**)
- **看门狗**:running 超过 `AM_WATCHDOG_MINUTES`(默认 120)→ 自动 cancel + error="watchdog_timeout"
- **队列**:调度触发一律 INSERT status=queued(受约束 5 去重);手动触发遇 running 或 queued 非空 → 409
- **NAS 重启恢复**:启动时扫描 running 记录 → 容器已不存在则置 failed(error="host_restarted");queued 保留继续消费
- **启动自检**:docker.sock 可达、`AM_TA_IMAGE` 存在、`AM_TA_DATA_HOST` 在管理台内的对应挂载可读;任一失败 → 拒绝启动并打印原因
- **保留策略**:`runs/<id>/` 与 run 记录保留 `AM_RETENTION_DAYS`(默认 90)后由每日任务清理(审计 `purged`)

### 6.5 调度器

- APScheduler(与 FastAPI 同进程,MemoryJobStore);时区 Asia/Shanghai
- `daily_trading`:周一至周五 at_time 触发(v1 不做交易所节假日历;v2 对接 stocks-alert 交易日历)
- `weekly`:指定 weekday 的 at_time
- 触发动作 = 入队(status=queued,trigger=schedule,analysts_csv 取自档案快照)+ 审计;命中约束 5 去重时审计 `deduped` 而不入队
- `analysis_date` = 触发时刻的 Asia/Shanghai 日期。注意:08:30 对 us/hk/cn 均为盘前,报告实际反映**上一交易日收盘**;页面 SHOULD 在日期旁注明「盘前」
- 种子数据(首次初始化):档案"日频三件套"(market,social,news)与"全量"(四项);无默认标的(用户自己加)

## 7. 页面(3 页,Jinja2 + htmx 轮询 15s)

1. **总览 `/`**:顶部「手动发起」卡(下拉标的 → 日期默认今天 → 档案或临时勾分析师 → 提交;忙时内联提示当前任务与进度,不跳转);当前任务卡(代码、当前 agent、agents x/N 进度条、token 计数、已耗时、stale 标记);队列列表;最近 20 条运行(状态灯 + 报告 ✓/✗);今日调度预告
2. **标的与调度 `/instruments`**:一张表:标的(市场/代码/名称/启停)| 档案 | 调度;行内编辑;**MUST 自然语言复述**(如「小米集团:每交易日 08:30 · 技术+舆情+新闻」);删除二次确认,有关联调度时提示;周一同时命中日频与周频时 SHOULD 提示「周一将只跑全量」
3. **运行详情 `/runs/{id}`**:agent 时间线(完成/进行/待跑)、tokens、耗时、报告已生成 ✓、error 全文、container.log 路径、[取消](二次确认)、cancelled/failed 时 [断点续跑]

通用:顶部导航 + 常驻状态灯(工作循环健康/队列深度/docker 可达);破坏性操作二次确认;移动端不适配。

## 8. 安全(对齐 WEBUI-SPEC §6/§8)

- token:`AM_TOKEN`(≥32 字符),Bearer 或登录后 HttpOnly+SameSite=Strict cookie;MUST NOT 进 URL
- 绑定:默认 127.0.0.1:8090;非回环绑定无 token 拒绝启动(判据用 is-loopback,禁止 `== "0.0.0.0"` 字面量比较)
- 局域网部署:绑 `192.168.1.150` + token
- Jinja2 自动转义;用户输入(标的名称等)禁 `|safe`
- 登录限速 5 次/分钟/IP;失败不区分"token 错"与"未设置"
- 无任意文件读写/命令执行端点;runner 参数全部白名单枚举校验;`/artifacts` 只返回受控前缀下的路径
- 管理台不接触 `.env`(约束 7);container.log 落文件不渲染

## 9. 部署

```yaml
# /home/chen/docker/agents-manage/docker-compose.yml,build context 为本仓库同步到 NAS 的目录
services:
  agents-manage:
    build: .
    user: "1000:994"                                      # uid=宿主 chen(与 TA data 同 owner),gid=宿主 docker 组
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock        # 启停执行容器(已获用户认可;SHOULD 改经 socket-proxy)
      - /home/chen/docker/TradingAgents/data:/ta-data:ro  # 只读:判定报告/记忆实况
      - ./data:/app-data                                  # SQLite + runs/<id>/(status.json、container.log)
      - ./runner:/runner:ro
    environment:
      - AM_BIND=192.168.1.150
      - AM_PORT=8090
      - AM_TOKEN=${AM_TOKEN}
      - AM_TA_IMAGE=tradingagents-tradingagents:latest
      - AM_TA_DATA_HOST=/home/chen/docker/TradingAgents/data
      - AM_TA_ENV_HOST=/home/chen/docker/TradingAgents/.env
      - AM_RUNNER_HOST=/home/chen/docker/agents-manage/runner/runner.py
      - AM_DATA_HOST=/home/chen/docker/agents-manage/data
      - AM_TA_DATA_DIR=/ta-data                            # 管理台容器内读 TA 产物的路径(对应上面的挂载)
      - AM_DATA_DIR=/app-data                              # 管理台容器内自有数据路径
      - AM_SMB_PREFIX=\\192.168.1.150\docker\TradingAgents\data
      - AM_WATCHDOG_MINUTES=120
      - AM_STALE_MINUTES=20
      - AM_RETENTION_DAYS=90
      - TZ=Asia/Shanghai
    ports: ["8090:8090"]
```

部署文档 MUST 写明查询命令 `stat -c %u:%g /home/chen/docker/TradingAgents/data` 与 `stat -c %g /var/run/docker.sock`,`user:` 按查询结果填写。

## 10. 验收清单

| ID | 验收项 |
|---|---|
| AM-01 | 🔴 忙时(存在 running 或 queued)POST /api/v1/runs 返回 409,零副作用(无容器、无 run 记录) |
| AM-02 | 🔴 调度任务 FIFO 串行执行,顺序=入队顺序,全部完成 |
| AM-03 | 🔴 succeeded 双重判定:人为删除 investment_plan.md 后,容器退出码 0 仍判 failed 并写明原因;正常跑完后 reports/*.md **与** memory 条目 **与** full_states_log 三者齐全 |
| AM-04 | 🔴 cancel:docker stop(SIGINT+20s)后 exit_code=130、status=cancelled,断点完好;resume(相同 analysts)后从断点继续(跳过已完成节点);resume 端点不接受改动 analysts |
| AM-05 | 🔴 看门狗:running 超时(测试中缩短阈值)自动 cancel,error=watchdog_timeout |
| AM-06 | 🔴 fail-closed 绑定矩阵:0.0.0.0 / :: / 192.168.1.150 / "0.0.0.0 "(带空格) 无 token 均拒绝启动 |
| AM-07 | 🔴 全站(页面+API 响应+日志+审计+执行容器 `docker inspect`)无 .env 内容与密钥;管理台代码库中不存在读取 `.env` 的代码路径 |
| AM-08 | 🔴 status.json 半截/损坏时管理台不崩,进度保持上次值并标 stale |
| AM-09 | 🔴 NAS 重启后:孤儿 running→failed(error=host_restarted),queued 保留并继续消费 |
| AM-10 | 🔴 去重:同一标的配置日频+周一全量,周一只产生 1 个 run 且为全量;同 (标的,日期) 已 succeeded 时无 force 返回 409 |
| AM-11 | 🔴 runner 自检:模拟缺失上游方法时退出码 2、error=upstream_api_changed,run 判 failed |
| AM-12 | 调度交易日语义:周六日不产生 run |
| AM-13 | 所有写操作入 audit_log(actor 正确);报告 ✓ 与文件系统实况一致 |
| AM-14 | 无 npm/CDN:镜像内无 node,断网加载页面全部功能可用 |
| AM-15 | htmx 轮询 15s 刷新进度,无 WebSocket/SSE |
| AM-16 | runner 原子写 status.json(并发读不解析失败);agents_total 随分析师数变化(3→11,4→12) |
| AM-17 | 分析师/代码格式白名单:非法值返回 400 |
| AM-18 | 失败 run 的 `runs/<id>/container.log` 存在且页面不渲染其内容 |

## 11. 分期

- **v1(本期)**:本规格全部 —— 标的/档案/调度 CRUD(页面)、手动发起(忙拒)、FIFO 队列 + 去重、进度跟踪、取消/续跑、看门狗、审计、runs JSON API、三页面
- **v2**:交易日历接入(stocks-alert 对接)、财报日触发、完成通知(Bark/webhook)、批量补历史日期、instruments/schedules JSON API、crypto 市场(需 `asset_type="crypto"` 与对应分析师集合)
- **v3**:多模型档案(按任务指定不同 LLM 配置)、并发>1

## 12. 参考资料(实现时按需查阅)

| 材料 | 位置 |
|---|---|
| runner 蓝本:CLI 流式循环、agent 状态映射、section→文件写入、token callback | NAS:`/home/chen/docker/TradingAgents/cli/main.py`(`run_analysis`) |
| propagate 收尾三步、断点签名、`_run_graph` 初始化参数 | NAS:`/home/chen/docker/TradingAgents/tradingagents/graph/trading_graph.py` |
| 记忆文件格式 | NAS:`/home/chen/docker/TradingAgents/tradingagents/agents/utils/memory.py` |
| 上游 README / 使用速查 | NAS:`/home/chen/docker/TradingAgents/README.md`、`USAGE.md` |
| Web UI 风格与安全范式参照 | 本机:`D:\2.Develop\5.Codex\stocks-alert\docs\WEBUI-SPEC.md` |

## 附 A:已定决策记录(2026-09-13 讨论定稿)

1. 前端:沿用 stocks-alert 范式(Jinja2 + htmx,无构建步骤),不过度设计
2. 并发=1;**手动发起遇忙直接拒绝(409 + 当前任务信息)**;**调度触发一律排队(FIFO)**
3. 执行容器操控:docker.sock 挂载方案(内网 + token 缓解其权限风险)
4. 结果展示:仅"报告已生成 ✓",不展示评级/决策内容
5. 调度默认值:交易日 08:30 三件套(技术+舆情+新闻),每周一 08:30 全量;基本面周频的依据 = 财报数据季度级更新。**v1.1 补充**:周一两条调度同时命中同一标的,按约束 5 去重为只跑全量(超集覆盖子集),无需用户手动错开
6. v1 不做通知,纯 API 轮询

## 附 B:修订记录

**v1.1(2026-09-14)** —— 基于对 NAS 上 TradingAgents 源码与数据实况的核对:
- §1.3 重写:确认 CLI 路径只写 reports、`propagate()` 路径只写 memory/full_states_log,runner 必须合并两条路径(v1.0 的 `propagate()` 薄封装无法产出 `investment_plan.md`,AM-03 必然失败)
- 新增约束 5(同标的+日期去重)、约束 7 改为「凭据零接触」(`.env` 挂载给执行容器,管理台不解析)、约束 10(宿主路径)
- run 表:id 改 TEXT;新增 `analysts_csv` 快照、`resumed_from`、`exit_code`、`status_stale`;`agents_total` 改为按分析师数计算
- 新增 runner 退出码契约与上游 API 自检;新增 container.log 落档、stale 判定、保留策略、启动自检
- 部署:管理台以 uid 1000 + docker gid 运行;新增 `AM_*_HOST`(宿主路径,供 docker-py)与 `AM_*_DIR`(容器内路径,供管理台读写)配置项
- run 表新增 `cancel_requested_at`:Web/API 不直接调 docker,取消由工作线程执行
- 精简:v1 JSON API 收敛为 runs;删除 `schedule.kind=manual_only`;`/run` 页并入总览(4 页→3 页);crypto 市场移至 v2
- 移除 v1.0 中 TradingAgents 侧的部署/补丁/端点/问题史内容(不属本系统范围)
