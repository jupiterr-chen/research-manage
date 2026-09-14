# 需求说明书(Requirements)

> 与 [SPEC.md](../SPEC.md) 的关系:SPEC 是**契约**(MUST/SHOULD、DDL、API、判定规则),本文把契约拆成**可分配、可验收的需求条目**,每条有唯一 ID、优先级、所属模块、验收映射。实现分支的 PR 标题/描述 MUST 引用本文的 R-ID。冲突时以 SPEC 为准。
>
> 优先级:**P0** = v1 上线必需;**P1** = v1 应有,可在集成阶段补;**P2** = v1 可选/v2。

## 0. 角色与场景

| 角色 | 场景 |
|---|---|
| 研究者(唯一用户) | 维护自选标的与调度;早上看总览确认昨夜/今早的研究是否跑完;报告通过 SMB 打开原文件;偶尔手动补跑某标的某日期;跑挂了取消/续跑 |
| 外部系统(stocks-alert,v1 只预留) | 通过 JSON API 触发一次研究并轮询结果 |
| 运维(同一人) | 部署到 NAS、看健康灯、NAS 重启后系统自恢复 |

## 1. 模块划分

| 模块 | 代号 | 内容 | 主要交付分支 |
|---|---|---|---|
| M1 基础设施 | FND | 配置、SQLite、迁移、鉴权、审计、应用骨架、测试夹具 | `feat/p0-foundation` |
| M2 领域服务 | SVC | 标的/档案/调度/run 的纯 DB 业务逻辑(忙拒、去重、force、resume) | `feat/p0-foundation` |
| M3 执行器 | EXE | 工作线程、docker 启动/监控/停止、status.json 读取、终态判定、看门狗、恢复、保留策略 | `feat/executor` |
| M4 Runner | RUN | 执行容器内的 runner.py、本地仿真镜像(sim) | `feat/runner` |
| M5 调度器 | SCH | APScheduler、交易日语义、入队去重、种子数据 | `feat/scheduler` |
| M6 JSON API | API | `/api/v1/runs*` | `feat/api-runs` |
| M7 Web 页面 | WEB | 3 页 + htmx 片段 + CSS | `feat/web-ui` |
| M8 部署 | DEP | Dockerfile、compose、部署/回滚文档、NAS 同步脚本 | `feat/deploy` |
| M9 验收 | ACC | 自动化验收用例、验收记录 | `feat/acceptance` |

## 2. 需求条目

### M1 基础设施(FND)

| ID | P | 需求 | 验收 |
|---|---|---|---|
| R-FND-01 | P0 | 配置全部来自环境变量 `AM_*`(清单见 DESIGN §3),启动时校验并打印**不含敏感值**的生效配置摘要 | AM-06 |
| R-FND-02 | P0 | 绑定门禁:`AM_BIND` 非回环且无 `AM_TOKEN`(或 <32 字符)→ 进程退出码非 0 并打印原因;判据 `is_loopback_bind()`,禁止字面量比较 | AM-06 |
| R-FND-03 | P0 | SQLite:WAL、`busy_timeout≥5000ms`、`foreign_keys=ON`;幂等迁移(`PRAGMA table_info` 后 CREATE/ALTER);DDL 与 SPEC §4 一致 | AM-13 |
| R-FND-04 | P0 | 鉴权:Bearer `AM_TOKEN` 或登录 cookie(HttpOnly、SameSite=Strict、`secure` 按是否 https);未鉴权页面 302 `/login`,API 401;token 不得出现在 URL | AM-06/07 |
| R-FND-05 | P0 | 登录限速:同 IP 5 次/分钟;失败文案不区分"token 错"与"未设置" | — |
| R-FND-06 | P0 | 审计:`audit(actor, action, entity, entity_id, detail)`;`detail_json` 序列化前经 `scrub()`(键名含 key/token/secret/password 的值替换为 `***`) | AM-13 |
| R-FND-07 | P0 | 常量唯一来源:`app.models` 定义 `MARKETS`、`ANALYSTS`、`RUN_STATUS`、`agents_total()`、`validate_code()`、`normalize_code()`、`validate_analysts()`;Web/API/调度器共用,禁止第二份 | AM-17 |
| R-FND-08 | P0 | 应用骨架:`create_app()`、lifespan 启停 worker 与 scheduler、全局异常边界(handler 异常 → 500 JSON/页面,不影响 worker) | AM-09 |
| R-FND-09 | P0 | 健康端点 `GET /healthz`(需鉴权):worker alive、queue depth、docker reachable、scheduler jobs 数、当前 run id | — |
| R-FND-10 | P0 | 测试夹具:临时 SQLite、TestClient、fake docker client、时间冻结 | — |
| R-FND-11 | P1 | 日志:结构化(时间/级别/模块/run_id),经 `scrub()`;日志中不出现 `.env` 路径以外的任何 `.env` 内容 | AM-07 |

### M2 领域服务(SVC)

| ID | P | 需求 | 验收 |
|---|---|---|---|
| R-SVC-01 | P0 | 标的 CRUD:`(market, code)` 唯一;`code` 经 `normalize_code()`+`validate_code()`;删除有关联调度 → 409 并列出 | AM-17 |
| R-SVC-02 | P0 | 档案 CRUD:`analysts_csv` ⊆ ANALYSTS 且非空;`is_default` 唯一;种子"日频三件套"/"全量" | — |
| R-SVC-03 | P0 | 调度 CRUD:`kind ∈ {daily_trading, weekly}`;`weekly` 必填 `weekday 1..7`;`at_time` HH:MM;`describe(schedule)` 输出自然语言(「小米集团:每交易日 08:30 · 技术+舆情+新闻」) | — |
| R-SVC-04 | P0 | `create_run(code, date, analysts, trigger, force=False)`:一次事务内完成 ①标的存在且 enabled ②analysts 校验 ③忙判定(trigger∈{web,api} 且存在 running/queued → `BusyError(current, queued_n)`) ④已 succeeded 同(标的,日期) 且非 force → `AlreadyDoneError` ⑤活动去重(约束 5) ⑥INSERT status=queued + 审计。任何错误 → 零副作用 | AM-01/10 |
| R-SVC-05 | P0 | 调度入队 `enqueue_scheduled(instrument, profile, date)`:不受忙判定;命中活动去重时保留分析师集合更大者(相等则保留已存在),审计 `deduped` | AM-02/10 |
| R-SVC-06 | P0 | `request_cancel(run_id)`:queued → 直接 cancelled;running → 置 `cancel_requested_at`;其他 → 409 | AM-04 |
| R-SVC-07 | P0 | `resume(run_id)`:仅 cancelled/failed;新 run 复制 `analysts_csv`、`analysis_date`、`instrument_id`,`resumed_from`=原 id;忙判定同手动 | AM-04 |
| R-SVC-08 | P0 | run 查询:列表(status/code/limit 过滤,按 created_at desc)、详情、当前任务(running)与队列(queued 按 created_at asc) | — |
| R-SVC-09 | P0 | `artifacts(run_id)`:列出 `AM_TA_DATA_DIR/logs/<code>/<date>/reports/*.md` 实际存在的文件,返回 `AM_SMB_PREFIX` 拼接的路径,不读内容 | AM-13 |
| R-SVC-10 | P0 | run id 生成:`r-YYYYMMDD-HHMMSS-<code 去掉非字母数字>`,同秒冲突加 `-2`… | — |

### M3 执行器(EXE)

| ID | P | 需求 | 验收 |
|---|---|---|---|
| R-EXE-01 | P0 | 单工作线程,tick 1s;所有 docker-py 调用只在该线程 | AM-02 |
| R-EXE-02 | P0 | 启动恢复:running 且容器不存在 → failed(`host_restarted`);容器仍存在 → 继续接管监控;queued 保留 | AM-09 |
| R-EXE-03 | P0 | 启动自检:docker 可达、镜像存在、`AM_TA_DATA_DIR` 可读、`AM_DATA_DIR` 可写;失败 → 进程退出 | — |
| R-EXE-04 | P0 | 启动执行容器:按 SPEC §6.3 的镜像/挂载/entrypoint/stop_signal/stop_timeout/working_dir;`environment` 只允许 `AM_RUN_ID`、`TZ`;挂载源全部来自 `AM_*_HOST` | AM-07 |
| R-EXE-05 | P0 | 监控循环:`wait(timeout=5)`;每 5s 读 status.json 同步 `current_agent/agents_done/tokens_*`;半截/损坏 JSON → 保持上次值 + `status_stale=1`;`updated_at` 超过 `AM_STALE_MINUTES` → `status_stale=1`;恢复正常后清 0 | AM-08 |
| R-EXE-06 | P0 | 取消:发现 `cancel_requested_at` → `container.stop(timeout=20)`(镜像 stop_signal=SIGINT)→ 终态 cancelled | AM-04 |
| R-EXE-07 | P0 | 看门狗:`started_at` 超过 `AM_WATCHDOG_MINUTES` → 同取消流程,error=`watchdog_timeout`,status=cancelled | AM-05 |
| R-EXE-08 | P0 | 终态判定(SPEC §6.4 顺序):130/主动 stop → cancelled;≠0 → failed;=0 → 双重判定(investment_plan.md 存在 **且** memory 行前缀 `[<date> \| <code> \|`)不满足 → failed 并写明缺项 | AM-03 |
| R-EXE-09 | P0 | `report_ready` 以文件系统实况为准,终态时最后一次刷新 | AM-13 |
| R-EXE-10 | P0 | 容器退出后:`logs(tail=200)` 写 `AM_DATA_DIR/runs/<id>/container.log`(经 `scrub()`),然后 `remove()`;失败不影响终态 | AM-18 |
| R-EXE-11 | P1 | 保留策略:每日 03:30 删除 `finished_at` 早于 `AM_RETENTION_DAYS` 的 run 记录与 `runs/<id>/`,审计 `purged` | — |
| R-EXE-12 | P0 | worker 任何异常:记录日志、当前 run 置 failed(error=`worker_exception:<类名>`)、线程不退出 | AM-09 |

### M4 Runner(RUN)

| ID | P | 需求 | 验收 |
|---|---|---|---|
| R-RUN-01 | P0 | `runner.py` 单文件、仅依赖标准库 + 镜像内已有的 `tradingagents`;参数 `--ticker --date --analysts --workspace --run-id`,全部白名单校验 | AM-17 |
| R-RUN-02 | P0 | 启动自检 8 个上游属性(SPEC §6.1 步骤 1);缺失 → status.json `phase=failed, error=upstream_api_changed`,退出码 2 | AM-11 |
| R-RUN-03 | P0 | 执行序列严格按 SPEC §6.1 步骤 2~7;`finally: end_checkpoint()` | AM-03 |
| R-RUN-04 | P0 | 每个 stream chunk:更新 agent 状态(映射照抄 CLI)→ 原子写 status.json(tmp+`os.replace`);按 section 写 `reports/<section>.md` | AM-16 |
| R-RUN-05 | P0 | token 统计 callback 挂载;无 callback 可用时 tokens 保持 0 但不报错 | — |
| R-RUN-06 | P0 | 信号:SIGINT → 不吞,`finally` 落盘断点后退出码 130;未捕获异常 → status.json `phase=failed, error=<一句话>`,退出码 1 | AM-04 |
| R-RUN-07 | P0 | status.json 字段与 SPEC §6.2 完全一致;`agents_total = len(analysts)+8` | AM-16 |
| R-RUN-08 | P0 | **仿真镜像 `sim/`**:`python:3.12-slim` + 假 `tradingagents` 包,API 表面与真实一致(8 个属性、`stream` 逐 chunk、写 memory/full_states_log、清断点);行为由 env `SIM_MODE` 控制:`ok | fail | hang | slow | no_memory | no_report | upstream_changed`,`SIM_STEP_SECONDS` 控制每节点耗时;镜像 tag `tradingagents-sim:latest` | 全部本地验收 |
| R-RUN-09 | P1 | runner 在真实镜像上的形状测试:从 NAS 只读拷贝 `tradingagents/` 包到本地 `vendor/`(gitignore),`import` 后断言 8 个属性存在(不执行分析) | AM-11 |

### M5 调度器(SCH)

| ID | P | 需求 | 验收 |
|---|---|---|---|
| R-SCH-01 | P0 | APScheduler BackgroundScheduler,MemoryJobStore,时区 Asia/Shanghai;启动与每次 schedule 变更后 `rebuild_jobs()` 全量重建 | — |
| R-SCH-02 | P0 | `daily_trading` → `cron(day_of_week='mon-fri', hour, minute)`;`weekly` → `cron(day_of_week=weekday)` | AM-12 |
| R-SCH-03 | P0 | 触发动作:`analysis_date` = 触发时刻 Asia/Shanghai 日期;调用 `enqueue_scheduled`;disabled 的标的/调度不触发 | AM-02/10 |
| R-SCH-04 | P0 | 同一 tick 内多条调度按 schedule.id 顺序入队,保证 FIFO 可预测 | AM-02 |
| R-SCH-05 | P0 | `next_fires(today)`:今日剩余触发预告(总览页用) | — |
| R-SCH-06 | P0 | 首次初始化种子档案;不种标的 | — |

### M6 JSON API(API)

| ID | P | 需求 | 验收 |
|---|---|---|---|
| R-API-01 | P0 | `POST /api/v1/runs` 按 SPEC §5;409 体 `{"error":"busy","message":...,"current":{id,code,date,current_agent,agents_done,agents_total,started_at},"queued":n}`;`already_done` 同理 | AM-01/10 |
| R-API-02 | P0 | `GET /api/v1/runs`、`GET /api/v1/runs/{id}`、`GET /api/v1/runs/{id}/artifacts` | — |
| R-API-03 | P0 | `POST /api/v1/runs/{id}/cancel`、`/resume`,actor=`api` | AM-04 |
| R-API-04 | P0 | 统一错误体 `{"error","message"}`;400/401/404/409;pydantic 校验错误映射为 400 | AM-17 |
| R-API-05 | P0 | 响应中不含任何路径以外的文件内容、不含 `.env` 相关字段 | AM-07 |

### M7 Web 页面(WEB)

| ID | P | 需求 | 验收 |
|---|---|---|---|
| R-WEB-01 | P0 | `base.html`:顶部导航(总览/标的与调度)、常驻状态灯(worker/队列深度/docker),htmx 由本地 `static/htmx.min.js` 提供,手写 `app.css`;无外链 | AM-14 |
| R-WEB-02 | P0 | 总览 `/`:手动发起卡、当前任务卡(进度条 x/N、tokens、耗时、stale 标记)、队列、最近 20 条、今日调度预告;动态区域为 htmx 片段 `every 15s` 轮询 | AM-15 |
| R-WEB-03 | P0 | 手动发起:忙时片段内联显示 409 信息,不跳转;已完成需勾选 force;临时勾分析师与档案二选一 | AM-01 |
| R-WEB-04 | P0 | `/instruments`:标的行内编辑(名称/启停)、新增、删除(二次确认;有调度时列出);调度行内增删改、启停;每条调度显示 `describe()` 文案;周一日频+周频同标的提示「周一将只跑全量」 | — |
| R-WEB-05 | P0 | `/runs/{id}`:agent 时间线(完成/进行/待跑)、tokens、耗时、报告 ✓、error 全文、container.log **路径**、取消(二次确认)、断点续跑按钮(仅 cancelled/failed) | AM-04/18 |
| R-WEB-06 | P0 | 全部用户输入自动转义;禁 `\|safe` 于任何用户数据;表单 POST 走 htmx,返回片段或 `HX-Redirect` | AM-07 |
| R-WEB-07 | P0 | 登录页 `/login`(token 输入 → cookie);登出 | AM-06 |
| R-WEB-08 | P1 | 页面在断网(无外网)环境下完整可用;所有 `<script>/<link>` 指向本站 | AM-14 |

### M8 部署(DEP)

| ID | P | 需求 | 验收 |
|---|---|---|---|
| R-DEP-01 | P0 | `deploy/Dockerfile`:`python:3.12-slim`,非 root(uid/gid 由 build-arg `APP_UID`/`DOCKER_GID` 指定),镜像内无 node/npm | AM-14 |
| R-DEP-02 | P0 | `deploy/docker-compose.yml` 与 SPEC §9 一致;`.env.example` 列全 `AM_*`;`AM_TOKEN` 生成命令写进文档 | — |
| R-DEP-03 | P0 | `deploy/docker-compose.local.yml`:本地全链路(管理台容器 + sim 镜像),Windows Docker Desktop 路径写法说明 | — |
| R-DEP-04 | P0 | `scripts/sync_to_nas.sh`:rsync/scp 仓库到 `/home/chen/docker/agents-manage/`(排除 data、vendor、.git);**只同步本仓库,不触碰 TradingAgents 目录** | — |
| R-DEP-05 | P0 | `docs/DEPLOY.md`:首次部署、升级、回滚、备份(SQLite 文件)、uid/gid 查询、docker-socket-proxy 可选方案 | — |
| R-DEP-06 | P1 | 管理台 `healthz` 接入 compose `healthcheck` | — |

### M9 验收(ACC)

| ID | P | 需求 | 验收 |
|---|---|---|---|
| R-ACC-01 | P0 | `tests/acceptance/` 覆盖 [ACCEPTANCE.md](ACCEPTANCE.md) 中标注「自动化」的用例,以 sim 镜像 + 本地 Docker 执行;`pytest -m acceptance` 一键运行 | 全部 |
| R-ACC-02 | P0 | 验收记录模板 `docs/acceptance-records/<date>.md`:每条 AM 的执行者、环境、结果、证据路径 | — |

## 3. 非功能需求

| ID | 需求 |
|---|---|
| R-NFR-01 | 单用户内网工具,并发请求 ≤5;页面响应 <500ms(不含首次 docker 调用) |
| R-NFR-02 | 管理台内存 <200MB;SQLite 单文件;无外部服务依赖(除 docker.sock) |
| R-NFR-03 | Python ≥3.11(本地),镜像 3.12;依赖清单固定在 `requirements.txt`,不引入 ORM(纯 `sqlite3`) |
| R-NFR-04 | 单元测试覆盖:services/executor/scheduler/runner ≥80% 行覆盖;`ruff` 零告警 |
| R-NFR-05 | 所有时间存 ISO-8601 带时区(`+08:00`);显示用 Asia/Shanghai |

## 4. 明确不做(v1)

报告内容展示 / 通知 / 交易日历 / crypto / 并发>1 / 多用户 / 移动端 / WebSocket / instruments 与 schedules 的 JSON API / TradingAgents 的任何运维操作。
