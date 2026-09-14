# Agents-Manage — TradingAgents 研究任务管理台 · 实现规格书 v1.0

> **文档性质**:实现契约 + 上下文交接。本文档面向**全新会话的实现者**,不依赖任何前置对话。
> **MUST** 为验收硬门槛;**SHOULD** 为强烈建议。
> 风格与硬约束体系对齐既有项目 `D:\2.Develop\5.Codex\stocks-alert\docs\WEBUI-SPEC.md`(可参考其实现),冲突时以本文档为准。

---

## 0. 一句话目标

在内网 NAS 上部署一个 Web 管理台:可视化配置"市场-标的"的研究调度(定时/手动),驱动 NAS 上已部署的 **TradingAgents** 多智能体分析框架执行,跟踪执行进度与状态,展示"报告是否已生成"。**纯任务管理台:不渲染任何报告内容,不涉及交易。**

明确不做(OUT OF SCOPE):报告内容展示(报告走 SMB 网络驱动器直接看原文件)、回测、实盘信号、多用户体系、移动端适配、WebSocket/SSE 推送、通知推送(v1 纯轮询)。

## 1. 背景与既有部署(上下文交接,实现前必读)

### 1.1 运行环境

| 项 | 值 |
|---|---|
| NAS | 飞牛 fnOS(Debian 系),`chen@192.168.1.150`,SSH 免密,chen 在 docker 组,有免密 sudo |
| Docker | 28.5.2 / Compose v2.40.3,Docker Root:`/vol1/docker` |
| 内网代理 | `http://192.168.1.150:1081`(sslocal,HTTP 模式;7890 已弃用) |
| Windows 工作机 | 与 NAS 同内网;用户通过 SMB 访问 NAS |

### 1.2 TradingAgents 既有部署(被管理对象,勿重复搭建)

- **源码**:`/home/chen/docker/TradingAgents`(GitHub main 分支,gh-proxy 下载;GitHub 直连不通)
- **镜像**:`tradingagents-tradingagents:latest`(约 570MB,ENTRYPOINT=`tradingagents` CLI)
- **定制层**:`/home/chen/docker/TradingAgents-custom/`,含:
  - `patches/0001~0006.patch`:对源码的 6 个补丁(舆情/新闻分析师独立 LLM 通道、relay 会话头注入、超时控制),**升级源码后必须重放**(`apply-patches.sh`)
  - `update.sh`:一键升级(下载源码→保留本地配置→重放补丁→重建镜像)
  - `HANDOFF.md`:TradingAgents 侧完整运维交接文档(问题史、配置含义、已知坑)——**实现本系统前建议通读**
- **compose 已改**:命名卷已替换为 bind mount:
  - `./data:/home/appuser/.tradingagents`(决策记忆/缓存/断点/报告)
  - `./reports:/home/appuser/app/reports`(CLI 手动保存目录)
- **数据落盘结构**(管理台要读的):
  - 报告:`data/logs/<TICKER>/<YYYY-MM-DD>/reports/*.md`(market/sentiment/news/fundamentals_report.md + investment_plan.md,后者为**增量写入**,中途内容是辩论陈词非最终版)
  - 决策记忆:`data/memory/trading_memory.md`(每完成一次分析追加一条 `[日期 | 代码 | 评级 | pending]`,**是"跑完"的可靠标志**)
  - 断点:`data/cache/checkpoints/<TICKER>.db`(-shm/-wal 伴生)
- **.env 关键项**(全部在 `/home/chen/docker/TradingAgents/.env`,**密钥不写入任何文档/页面**):
  - 主力 LLM:provider=openai_compatible,端点 opencode relay,模型 glm-5.3(deep)/glm-5.3-flash(quick),会话头经 `TRADINGAGENTS_LLM_DEFAULT_HEADERS` 注入(补丁 0006)
  - 舆情/新闻分析师独立通道:`TRADINGAGENTS_CONTENT_LLM_*` → deepseek-v4.1-flash 同一 relay(补丁 0001/0002/0005)
  - `TRADINGAGENTS_LLM_TIMEOUT=300`、`TRADINGAGENTS_LLM_MAX_RETRIES=3`、`TRADINGAGENTS_CHECKPOINT_ENABLED=true`
  - 代理:`HTTP(S)_PROXY=http://192.168.1.150:1081`,`NO_PROXY` 含 open.bigmodel.cn
- **执行性能参考**:四分析师全量一次约 15~30 分钟(思考模型生成为主要耗时);单 LLM 调用正常 3~5 秒内发出响应(超时 300s 兜底)

### 1.3 无头执行入口(管理台如何驱动它)

TradingAgents 支持非交互 Python API(**不使用**交互式 CLI):

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
ta = TradingAgentsGraph(debug=False, config=config, selected_analysts=("market","social","news"))
_, decision = ta.propagate("1810.HK", "2026-09-14")
```

- `config = DEFAULT_CONFIG.copy()`:自动继承容器 env(.env 注入),无需管理台传模型配置
- `selected_analysts`:本次要跑的分析师子集 —— **这是管理台唯一的执行参数**(加上 ticker 与日期)
- 进度:图支持流式(`astream`,与官方 CLI 同源),逐节点产出事件(当前 agent、state 增量)
- 断点:同 ticker+日期 重跑自动从断点继续(checkpoint 已全局启用);`config` 变化(如分析师集合不同)会导致断点不匹配、从头跑
- 历史:09-13 曾因 LLM 端点问题出现挂死,已根治(详见 HANDOFF.md 第 9 节);现执行容器卡死风险低,但管理台仍**必须**实现看门狗与取消

## 2. 总体架构

```
浏览器(内网)                    NAS 192.168.1.150
──────────────                 ─────────────────────────────────────────
 │ HTTP + token                ┌────────────────────────────────────┐
 ├────────────────────────────▶│ agents-manage 容器                  │
 │  htmx 轮询 15s              │  FastAPI + Jinja2 + htmx            │
 │                             │  SQLite(唯一事实源) + APScheduler   │
 │                             │  执行器: docker-py via              │
 │                             │          /var/run/docker.sock       │
 │                             └──────────────┬─────────────────────┘
 │                                             │ 每个任务创建兄弟容器
 │  文件直达(SMB)              ┌──────────────▼─────────────────────┐
 ├─────────────────────────    │ 执行容器(tradingagents 镜像)        │
 │  data/logs/.../reports/     │ runner.py(挂载进入) + astream      │
 │  data/runs/<id>/status.json │ 逐节点写 status.json               │
 └─────────────────────────    └────────────────────────────────────┘
```

- 管理台容器与执行容器**分离**:执行崩溃/挂死不影响管理台;执行容器直接复用既有镜像(继承全部补丁与配置),TradingAgents 升级不经过本系统代码
- docker.sock 方案已获用户认可(内网+token 鉴权缓解;参考 stocks-alert WEBUI-SPEC §3.1/S1 的安全范式)

## 3. 硬约束

1. 🔴 **SQLite 唯一事实源**。MUST NOT 引入第二存储;status.json 是执行侧产物,管理台以 SQLite 为准(两者不一致时以容器退出码 + 文件系统实况仲裁)
2. 🔴 **无构建步骤**:MUST NOT 引入 npm/node/webpack/vite/React/Vue/CDN。Jinja2 服务端渲染 + htmx(单文件随仓库)+ 手写 CSS。页面离线可用
3. 🔴 **执行逻辑不在 Web 层**:管理台只做 发起/跟踪/落档;所有 TradingAgents 调用发生在 runner.py(执行容器内)
4. 🔴 **并发 = 1,单工作线程消费队列**。手动发起:存在 running 或 queued 任务时返回 409(含当前任务信息),不入队;调度触发:一律入队(FIFO)
5. 🔴 **鉴权 fail-closed**:默认绑 127.0.0.1;绑定任何非回环地址(含 0.0.0.0/::/具体网卡 IP/带空格变体)且未配置 token 时拒绝启动;token ≥32 字符;登录限速同 IP 5 次/分钟
6. 🔴 **凭据零渲染**:.env 内容、API key、relay 地址密钥部分不得出现在任何页面/响应/日志
7. 🔴 **写操作全审计**(actor: web/api/schedule),幂等可追溯
8. 🔴 Web 层异常 MUST NOT 影响正在执行的 run(天然满足:执行在兄弟容器;仍需 handler 异常边界)

## 4. 数据模型(SQLite DDL)

```sql
CREATE TABLE IF NOT EXISTS instrument (
  id       INTEGER PRIMARY KEY,
  market   TEXT NOT NULL CHECK(market IN ('us','hk','cn','crypto')),
  code     TEXT NOT NULL,              -- 如 1810.HK / NVDA / 600519.SS / BTC-USD
  name     TEXT NOT NULL DEFAULT '',
  enabled  INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(market, code)
);

CREATE TABLE IF NOT EXISTS profile (               -- 执行档案(分析师组合)
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,                       -- 如 "日频三件套" / "全量"
  analysts_csv TEXT NOT NULL,                      -- 逗号分隔: market,social,news,fundamentals 子集
  is_default INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schedule (
  id INTEGER PRIMARY KEY,
  instrument_id INTEGER NOT NULL REFERENCES instrument(id),
  profile_id    INTEGER NOT NULL REFERENCES profile(id),
  kind     TEXT NOT NULL CHECK(kind IN ('daily_trading','weekly','manual_only')),
  at_time  TEXT NOT NULL DEFAULT '08:30',          -- HH:MM,Asia/Shanghai
  weekday  INTEGER,                                -- weekly 用:1=周一
  enabled  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS run (
  id INTEGER PRIMARY KEY,                          -- r-YYYYMMDD-HHMMSS-<code>
  instrument_id INTEGER NOT NULL REFERENCES instrument(id),
  profile_id    INTEGER NOT NULL REFERENCES profile(id),
  analysis_date TEXT NOT NULL,                     -- YYYY-MM-DD
  status TEXT NOT NULL CHECK(status IN
    ('queued','running','succeeded','failed','cancelled')),
  trigger TEXT NOT NULL CHECK(trigger IN ('web','api','schedule')),
  container_id TEXT, workspace TEXT,               -- data/runs/<id>/
  current_agent TEXT, agents_done INTEGER DEFAULT 0, agents_total INTEGER DEFAULT 12,
  tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
  report_ready INTEGER DEFAULT 0,                  -- 见 §6.4 判定
  error TEXT, started_at TEXT, finished_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_status ON run(status);

CREATE TABLE IF NOT EXISTS audit_log (             -- 同 WEBUI-SPEC §5.2
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
  actor TEXT NOT NULL CHECK(actor IN ('web','api','schedule')),
  action TEXT NOT NULL, entity TEXT NOT NULL, entity_id TEXT,
  detail_json TEXT                                  -- MUST NOT 含凭据
);
```

迁移 MUST 用幂等方式(`PRAGMA table_info` 检查后 CREATE/ALTER)。

## 5. 对外 API(`/api/v1`,与页面同 token 鉴权)

供本系统页面与外部系统(如 stocks-alert)共用。全部 JSON;写操作 POST/PUT/DELETE/PATCH。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/instruments?market=&enabled=` | 列表 |
| POST | `/api/v1/instruments` | `{market,code,name}`;**MUST 校验代码格式**(与 TradingAgents/yfinance 约定一致:us 裸代码、hk `####.HK`、cn `######.SS/.SZ`、crypto `XXX-USD`) |
| PUT/DELETE | `/api/v1/instruments/{id}` | 改名/启停/删除(有关联 schedule 时 409 并列出) |
| GET/POST | `/api/v1/profiles` | 档案列表/新建(analysts_csv MUST ⊆ {market,social,news,fundamentals} 且非空) |
| GET/POST/PATCH/DELETE | `/api/v1/schedules[/{id}]` | 调度 CRUD;PATCH `/enabled` 启停 |
| POST | `/api/v1/runs` | `{code, date?, profile_id?}`(date 缺省=当天;profile 缺省=标的第一条 schedule 的档案或系统默认)。**忙时(存在 running/queued)返回 409**:`{"error":"busy","current":{...当前任务与进度...},"queued":n}`,不产生任何副作用 |
| GET | `/api/v1/runs?status=&code=&limit=` | 历史列表 |
| GET | `/api/v1/runs/{id}` | 详情:status/current_agent/agents_done/tokens/report_ready/error/时间戳 |
| GET | `/api/v1/runs/{id}/artifacts` | 报告文件路径列表(SMB 可达路径形式返回,MUST NOT 返回文件内容) |
| POST | `/api/v1/runs/{id}/cancel` | 仅 running 可取消:docker stop 容器(SIGINT,20s 宽限)→ status=cancelled;断点保留 |
| POST | `/api/v1/runs/{id}/resume` | 仅 cancelled/failed 可续:新建 run 记录(带 resumed_from 标记),同 code+date 走断点 |

错误码约定:400 参数非法 / 401 未鉴权 / 404 不存在 / 409 忙或冲突;错误体 `{"error": "<machine_readable>", "message": "<中文人话>"}`。

## 6. 执行链路契约(核心)

### 6.1 runner.py(随本仓库维护,挂载进执行容器)

CLI:`python /runner.py --ticker 1810.HK --date 2026-09-14 --analysts market,social,news --workspace /ws --run-id <id>`

职责(按序):
1. `DEFAULT_CONFIG.copy()`(继承容器 env,模型/代理/超时全来自 .env,管理台**不传**任何模型参数)
2. `TradingAgentsGraph(config=..., selected_analysts=tuple(...))`;通过 `astream` 逐节点消费事件(参考 `cli/main.py` 的流式循环写法)
3. 每个节点事件 → 原子写 `<workspace>/status.json`(临时文件+rename)
4. 结束:写终态(成功/失败+一句话原因),退出码 0/1

### 6.2 status.json schema(执行侧唯一产物,管理台每 5s 轮询读)

```json
{
  "run_id": "r-20260914-0830-1810HK", "ticker": "1810.HK", "date": "2026-09-14",
  "phase": "running",
  "current_agent": "Bear Researcher",
  "agents_done": 6, "agents_total": 12,
  "tokens_in": 210000, "tokens_out": 98000,
  "updated_at": "2026-09-14T08:51:12+08:00",
  "error": null
}
```

### 6.3 执行容器启动参数(等价于现有 compose run)

镜像 `tradingagents-tradingagents:latest`;挂载:
- `/home/chen/docker/TradingAgents/data:/home/appuser/.tradingagents`
- `<repo>/runner/runner.py:/runner.py:ro`
- `/home/chen/docker/TradingAgents/data/runs/<run_id>:/ws`(工作区)
- env:由管理台启动时解析 `/home/chen/docker/TradingAgents/.env` 逐项注入(docker-py 无 env_file 概念;**MUST NOT** 回显或落日志)
- entrypoint 覆盖:`python /runner.py ...`;`stop_signal=SIGINT`,`stop_timeout=20`(给 LangGraph 落盘断点的机会)

### 6.4 状态机与判定

- 工作循环(单线程):取最早 queued → 置 running(记 container_id、started_at)→ 监听容器退出 + 轮询 status.json 同步进度到 SQLite
- **succeeded 判定 MUST 双重**:容器退出码 0 **且** `data/logs/<code>/<date>/reports/investment_plan.md` 存在 **且** `data/memory/trading_memory.md` 追加了当日该 code 条目;不满足 → failed(error 写明缺哪个)
- **report_ready**:investment_plan.md 存在即为 1(按用户拍板,只展示"报告已生成 ✓",**不展示评级/决策内容**)
- **看门狗**:running 超过 120 分钟 → 自动 cancel + error="watchdog_timeout"(300s×3 重试最坏叠加约 90 分钟,留余量)
- **队列**:调度触发一律 INSERT status=queued;手动触发遇 running 或 queued 非空 → 409 拒绝
- **NAS 重启恢复**:启动时扫描 running 记录 → 容器已不存在则置 failed(error="host_restarted");queued 保留继续消费

### 6.5 调度器

- APScheduler(与 FastAPI 同进程);时区 Asia/Shanghai
- `daily_trading`:周一至周五 at_time 触发(v1 不做交易所节假日历;v2 对接 stocks-alert 交易日历)
- `weekly`:指定 weekday 的 at_time
- 触发动作 = 入队(status=queued,trigger=schedule)+ 审计
- 种子数据(首次初始化):档案"日频三件套"(market,social,news)与"全量"(四项);无默认标的(用户自己加)

## 7. 页面(4 页,Jinja2 + htmx 轮询 15s)

1. **总览 `/`**:当前任务卡(代码、当前 agent、agents x/12 进度条、token 计数、已耗时);队列列表;最近 20 条运行(状态灯 + 报告 ✓/✗);今日调度预告
2. **标的与调度 `/instruments`**:一张表:标的(市场/代码/名称/启停)| 档案 | 调度;行内编辑;**MUST 自然语言复述**(如「小米集团:每交易日 08:30 · 技术+舆情+新闻」);删除二次确认,有关联调度时提示
3. **手动发起 `/run`**:下拉标的 → 日期(默认今天,可选历史)→ 档案(或临时勾分析师)→ 提交;忙时页面内联提示当前任务与进度(不跳转)
4. **运行详情 `/runs/{id}`**:agent 时间线(完成/进行/待跑)、tokens、耗时、报告已生成 ✓、error 全文、[取消](二次确认)、cancelled/failed 时 [断点续跑]

通用:顶部导航 + 常驻状态灯(工作循环健康/队列深度);破坏性操作二次确认;移动端不适配。

## 8. 安全(对齐 WEBUI-SPEC §6/§8)

- token:`AM_TOKEN`(≥32 字符),Bearer 或登录后 HttpOnly+SameSite=Strict cookie;MUST NOT 进 URL
- 绑定:默认 127.0.0.1:8090;非回环绑定无 token 拒绝启动(判据用 is-loopback,禁止 `== "0.0.0.0"` 字面量比较)
- 局域网部署:绑 `192.168.1.150` + token
- Jinja2 自动转义;用户输入(标的名称等)禁 `|safe`
- 登录限速 5 次/分钟/IP;失败不区分"token 错"与"未设置"
- 无任意文件读写/命令执行端点;runner 参数全部白名单枚举校验

## 9. 部署

```yaml
# 独立 compose,放 /home/chen/docker/agents-manage/,build context 为本仓库同步到 NAS 的目录
services:
  agents-manage:
    build: .
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock        # 启停执行容器(已获用户认可)
      - /home/chen/docker/TradingAgents/.env:/ta/.env:ro  # 启动时解析注入执行容器,不落盘副本
      - /home/chen/docker/TradingAgents/data:/ta-data      # 读 status.json/报告;写 runs/<id>/
      - ./data:/app-data                                    # SQLite 本体(管理台自有状态)
    environment:
      - AM_BIND=192.168.1.150
      - AM_PORT=8090
      - AM_TOKEN=${AM_TOKEN}
      - TZ=Asia/Shanghai
    ports: ["8090:8090"]
```

注意:管理台容器内以非 root 跑,但 docker.sock 需要宿主 docker 组权限(镜像内固定 gid=宿主 docker 组号,部署文档写明查询命令 `stat -c %g /var/run/docker.sock`)。

## 10. 验收清单

| ID | 验收项 |
|---|---|
| AM-01 | 🔴 忙时(存在 running 或 queued)POST /api/v1/runs 返回 409,零副作用(无容器、无 run 记录) |
| AM-02 | 🔴 调度任务 FIFO 串行执行,顺序=入队顺序,全部完成 |
| AM-03 | 🔴 succeeded 双重判定:人为删除 investment_plan.md 后,容器退出码 0 仍判 failed 并写明原因 |
| AM-04 | 🔴 cancel:docker stop(SIGINT+20s)后 status=cancelled,断点完好;resume 后从断点继续(跳过已完成节点) |
| AM-05 | 🔴 看门狗:running 超时(测试中缩短阈值)自动 cancel,error=watchdog_timeout |
| AM-06 | 🔴 fail-closed 绑定矩阵:0.0.0.0 / :: / 192.168.1.150 / "0.0.0.0 "(带空格) 无 token 均拒绝启动 |
| AM-07 | 🔴 全站(页面+API 响应+日志)无 .env 内容与密钥泄漏 |
| AM-08 | 🔴 status.json 半截/损坏时管理台不崩,进度保持上次值并标 stale |
| AM-09 | 🔴 NAS 重启后:孤儿 running→failed(error=host_restarted),queued 保留并继续消费 |
| AM-10 | 调度交易日语义:周六日不产生 run |
| AM-11 | 所有写操作入 audit_log(actor 正确);报告 ✓ 与文件系统实况一致 |
| AM-12 | 无 npm/CDN:镜像内无 node,断网加载页面全部功能可用 |
| AM-13 | htmx 轮询 15s 刷新进度,无 WebSocket/SSE |
| AM-14 | runner 原子写 status.json(并发读不解析失败) |
| AM-15 | 分析师参数白名单:非法 analysts 值返回 400 |

## 11. 分期

- **v1(本期)**:本规格全部 —— 标的/档案/调度 CRUD、手动发起(忙拒)、FIFO 队列、进度跟踪、取消/续跑、看门狗、审计、API v1、四页面
- **v2**:交易日历接入(stocks-alert 对接)、财报日触发、完成通知(Bark/webhook)、批量补历史日期
- **v3**:多模型档案(按任务指定不同 LLM 配置)、并发>1(需多 relay 账号)

## 12. 参考资料(实现时按需查阅)

| 材料 | 位置 |
|---|---|
| TradingAgents 运维交接(问题史/配置/坑) | NAS:`/home/chen/docker/TradingAgents-custom/HANDOFF.md` |
| TradingAgents 使用速查 | NAS:`/home/chen/docker/TradingAgents/USAGE.md` |
| 上游项目 README/API 说明 | NAS:`/home/chen/docker/TradingAgents/README.md` |
| Web UI 风格与安全范式参照 | 本机:`D:\2.Develop\5.Codex\stocks-alert\docs\WEBUI-SPEC.md` |
| 无头执行参考(Python API/astream 流式循环) | NAS:`/home/chen/docker/TradingAgents/cli/main.py`(CLI 的流式消费即 runner 蓝本) |
| 补丁链(勿手动改 TradingAgents 源码) | NAS:`/home/chen/docker/TradingAgents-custom/patches/` |

## 附:已定决策记录(2026-09-13 讨论定稿,不再重新讨论)

1. 前端:沿用 stocks-alert 范式(Jinja2 + htmx,无构建步骤),不过度设计
2. 并发=1;**手动发起遇忙直接拒绝(409 + 当前任务信息)**;**调度触发一律排队(FIFO)**
3. 执行容器操控:docker.sock 挂载方案(内网 + token 缓解其权限风险)
4. 结果展示:仅"报告已生成 ✓",不展示评级/决策内容
5. 调度默认值:交易日 08:30 三件套(技术+舆情+新闻),每周一 08:30 全量;基本面周频的依据 = 财报数据季度级更新(数据半衰期分析结论)
6. v1 不做通知,纯 API 轮询
