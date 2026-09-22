# 验收手册(Acceptance Manual)

> 对应 [SPEC.md §10](../SPEC.md) 的 AM-01~18;AM-19~33 为 v1.2 财报原文获取(SPEC 附 B v1.2,本文件为唯一清单)。每条给出:环境、是否自动化、前置、步骤、预期、证据。
> **环境**:`LOCAL` = 本地 Docker + `tradingagents-sim:latest`;`NAS` = 192.168.1.150 真实镜像(受 [NAS-ACCESS.md](NAS-ACCESS.md) 约束,真实运行需用户确认)。
> **判定**:🔴 项任一 FAIL → 不可上线。非 🔴 项 FAIL → 记录为已知缺陷,由用户决定是否放行。
> **记录**:执行结果写入 `docs/acceptance-records/<YYYY-MM-DD>-<local|nas>.md`(模板见 §3)。

## 0. 通用准备

### 0.1 LOCAL 环境

```bash
scripts/build_sim.sh                         # 构建 tradingagents-sim:latest
mkdir -p .local/ta-data .local/am-data          # .local/ta.env 按 DESIGN §8 写(含代理 192.168.1.150:7890 与 NO_PROXY=llm-stub)
cp deploy/.env.example .env.local            # 按 DESIGN §8 填 AM_*,AM_TA_IMAGE=tradingagents-sim:latest
python -m app.main                           # 或 docker compose -f deploy/docker-compose.local.yml up
```
辅助:`sqlite3 .local/am-data/agents-manage.db`;`docker ps -a --filter label=am.run_id`。

### 0.2 NAS 环境

- 代码同步:`scripts/sync_to_nas.sh`(只写 `/home/chen/docker/agents-manage/`)
- 起管理台:`AM_BIND=127.0.0.1`,通过 `ssh -L 8090:127.0.0.1:8090` 访问
- **任何会启动执行容器的步骤前,向用户确认标的与日期**(会写入 TA `data/`)

### 0.3 常用断言命令

```bash
# run 状态
sqlite3 $DB "select id,status,exit_code,error,report_ready,status_stale from run order by created_at desc limit 5"
# 审计
sqlite3 $DB "select ts,actor,action,entity,entity_id from audit_log order by id desc limit 10"
# 密钥扫描(AM-07):把 .env 里的 key 值逐个 grep(本地 sim 的 .env 用假值 FAKEKEY_xxx)
grep -r "FAKEKEY" .local/am-data/ app.log || echo "clean"
```

## 1. 验收项

### AM-01 🔴 忙时 POST /api/v1/runs 返回 409,零副作用
- 环境 LOCAL · 自动化 ✅
- 前置:`SIM_MODE=slow`(`AM_SIM_ENV=SIM_MODE=slow`);标的 A、B 已添加
- 步骤:① `POST /api/v1/runs {code:A}` → 202/201;等待 status=running ② `POST /api/v1/runs {code:B}` ③ 记录容器数、run 行数 ④ 页面「手动发起」提交 B
- 预期:②返回 409,body `error=busy`,`current.id`=A 的 run,`queued=0`;③容器数=1、run 行数=1、audit 无 B 的 `run.create`;④页面内联显示当前任务与进度,不跳转
- 证据:响应体、`sqlite3` 查询输出、页面截图

### AM-02 🔴 调度 FIFO 串行,顺序=入队顺序
- 环境 LOCAL · 自动化 ✅(冻结时间触发)
- 前置:标的 A、B、C 各一条 `daily_trading` 调度,`at_time` 同为 T;`SIM_MODE=ok`,`SIM_STEP_SECONDS=1`
- 步骤:把时间推进到 T(或用测试钩子直接调用 3 次 `enqueue_scheduled`,schedule.id 顺序 A,B,C)
- 预期:3 条 run `queued`,`created_at` 顺序 A<B<C;执行期间任何时刻 `running` ≤1;最终 3 条 `succeeded`,`started_at` 顺序 A<B<C;audit 有 3 条 actor=schedule 的 `run.create`
- 证据:`select id,status,created_at,started_at from run order by created_at`

### AM-03 🔴 succeeded 双重判定
- 环境 LOCAL(自动化 ✅)+ NAS(手动,一次)
- 步骤 a:`SIM_MODE=no_report` 发起 → 预期 `failed`,error 含 `investment_plan.md`
- 步骤 b:`SIM_MODE=no_memory` 发起 → 预期 `failed`,error 含 `trading_memory`
- 步骤 c:`SIM_MODE=ok` 发起 → 预期 `succeeded`,`report_ready=1`;`ta-data/logs/<code>/<date>/reports/investment_plan.md`、`memory/trading_memory.md` 含 `[<date> | <code> |`、`logs/<code>/TradingAgentsStrategy_logs/full_states_log_<date>.json` 三者齐全
- 步骤 d(LOCAL-REAL,自动化 ✅ `-m real`):`tradingagents-local` + llm-stub,3 分析师与 4 分析师各跑一次 → 三产物齐全,`agents_total` 11/12
- NAS:用户指定标的/日期真实跑一次,核对 c 的三产物 + 退出码 0
- 证据:run 行、`ls` 输出、memory 行

### AM-04 🔴 取消与断点续跑
- 环境 LOCAL(自动化 ✅)+ NAS(手动,一次,需确认)
- 前置:`SIM_MODE=slow`
- 步骤:① 发起,等 `agents_done≥2` ② `POST /runs/{id}/cancel` ③ 观察 ④ `POST /runs/{id}/resume` ⑤ 尝试 `resume` 时带不同 `analysts`
- 预期:② 20s 内 `status=cancelled`,`exit_code=130`,断点文件存在;④ 新 run `resumed_from`=原 id、`analysts_csv` 相同,status.json 首个 `agents_done` ≥ 取消时的值(跳过已完成节点),最终 succeeded;⑤ 400/409,不接受
- 证据:两条 run 行、status.json 序列(runs/<id>/status.json)、页面「断点续跑」按钮截图

### AM-05 🔴 看门狗
- 环境 LOCAL · 自动化 ✅
- 前置:`AM_WATCHDOG_MINUTES=1`(测试缩短),`SIM_MODE=hang`
- 预期:≈1 分钟后 `status=cancelled`,`error=watchdog_timeout`,容器已不存在,audit 有 `run.watchdog`
- 证据:run 行、`docker ps -a` 为空

### AM-06 🔴 fail-closed 绑定矩阵
- 环境 LOCAL · 自动化 ✅
- 步骤:分别以 `AM_BIND` = `0.0.0.0` / `::` / `192.168.1.150` / `"0.0.0.0 "` / `127.0.0.1`,`AM_TOKEN` 为空 或 31 字符,启动 `python -m app.main`
- 预期:前四个 × 两种 token → 退出码 2 并打印原因;`127.0.0.1` 无 token → 正常启动;任一绑定 + 32 字符 token → 正常启动。启动日志的配置摘要不含 token 值
- 证据:各组合退出码表

### AM-07 🔴 全站无密钥泄漏
- 环境 LOCAL(自动化 ✅)+ NAS(手动)
- 前置 LOCAL:`.local/ta.env` 写入 `OPENAI_API_KEY=FAKEKEY_abc123` 等 5 个假键
- 步骤:跑一次 ok + 一次 fail;抓取:所有页面 HTML、所有 API 响应、应用日志、`audit_log.detail_json`、`runs/<id>/container.log`、`docker inspect <执行容器>`(在 remove 前,或用 `AM_SIM_ENV` 触发 hang 时抓)
- 预期:上述全部 `grep FAKEKEY` 为空;`docker inspect` 的 `Config.Env` 只有 `AM_RUN_ID`、`TZ`(本地可额外有 `SIM_*`);`grep -rn "\.env" app/` 不出现读取 `.env` 的代码(只允许在 launcher 中作为挂载路径字符串)
- NAS:对真实 `.env` 中的每个 key 值(由用户在 NAS 上执行 grep,验收方不查看值)重复扫描
- 证据:grep 输出、inspect 的 Env 段

### AM-08 🔴 status.json 损坏不崩
- 环境 LOCAL · 自动化 ✅
- 前置:`SIM_MODE=corrupt_status`(第 3 节点写半截 JSON 并停 10s 后恢复)
- 预期:管理台进程不崩;损坏期间 `agents_done` 保持上次值且 `status_stale=1`;恢复后 `status_stale=0` 且继续更新;最终 succeeded。另:`AM_STALE_MINUTES=1` + `SIM_MODE=hang` → 1 分钟后 `status_stale=1`,页面显示 stale 标记
- 证据:progress 采样序列、页面截图

### AM-09 🔴 NAS/进程重启恢复
- 环境 LOCAL · 自动化 ✅(模拟)
- 步骤 a(孤儿):发起 `slow` 任务 → 强杀管理台进程 → `docker rm -f` 执行容器 → 重启管理台
- 步骤 b(接管):发起 `slow` 任务 → 强杀管理台 → 容器继续跑 → 重启管理台
- 步骤 c(队列):DB 中预置 2 条 queued → 重启
- 预期:a → 该 run `failed`,`error=host_restarted`;b → 继续监控直至 succeeded;c → 按序执行。任何情况 worker 线程存活,`/healthz` alive=true
- 证据:run 行、healthz 输出

### AM-10 🔴 去重与 force
- 环境 LOCAL · 自动化 ✅
- 步骤 a:标的 A 配 `daily_trading 08:30 三件套` + `weekly 周一 08:30 全量`;冻结时间到某周一 08:30 触发
- 步骤 b:A 某日 succeeded 后,`POST /api/v1/runs {code:A, date:同日}`;再带 `force:true`
- 预期:a → 只有 1 条 run,`analysts_csv=market,social,news,fundamentals`,audit 有 `run.deduped`;b → 先 409 `already_done`,force 后 201 新 run
- 证据:run 行、audit 行

### AM-11 🔴 runner 上游自检
- 环境 LOCAL(自动化 ✅)+ NAS 形状测试
- 步骤:`SIM_MODE=upstream_changed` 发起
- 预期:容器退出码 2,status.json `error=upstream_api_changed`,run `failed` 且 error 相同
- 对等(自动化 ✅):`scripts/verify_vendor.sh` 哈希一致 → `pytest tests/shape` 通过(8 个签名 + CLI 常量与 sim/models 一致)
- 证据:run 行、pytest 输出

### AM-12 调度交易日语义
- 环境 LOCAL · 自动化 ✅
- 步骤:冻结时间到周六/周日 `at_time`
- 预期:无 run 入队;周一~周五均入队
- 证据:pytest 输出

### AM-13 审计与报告 ✓ 实况一致
- 环境 LOCAL · 自动化 ✅
- 步骤:执行标的增/改/删、档案增、调度增/启停/删、手动发起、取消、resume、调度触发;succeeded 后手动删除 `investment_plan.md`,刷新详情页
- 预期:每个写操作各一条 audit,actor 与来源一致(页面=web,API=api,调度=schedule);删除文件后 `GET /api/v1/runs/{id}` 与页面 `report_ready` 变为 0(终态后以实况刷新,或 artifacts 列表不含该文件——二者至少其一,以实现为准并在记录中注明)
- 证据:audit 行、响应

### AM-14 无 npm/CDN,离线可用
- 环境 LOCAL · 自动化 ✅(部分)
- 步骤:`docker run --rm <管理台镜像> sh -c "which node npm; ls /usr/lib/node_modules"`;`grep -rn "https\?://" app/web/templates app/web/static`;浏览器断网(或 devtools offline)加载三页并操作
- 预期:镜像内无 node;模板/静态中无外链(允许注释与 `AM_SMB_PREFIX` 变量);离线下页面与 htmx 交互全部可用
- 证据:命令输出、截图

### AM-15 htmx 轮询 15s,无 WebSocket/SSE
- 环境 LOCAL · 自动化 ✅
- 预期:动态区域元素带 `hx-trigger="every 15s"`;`grep -rn "WebSocket\|EventSource\|text/event-stream" app/` 为空;devtools 网络面板 15s 一次片段请求
- 证据:grep、截图

### AM-16 status.json 原子写,agents_total 动态
- 环境 LOCAL · 自动化 ✅
- 步骤:`SIM_STEP_SECONDS=0.05` 跑一次,管理台以 `AM_STATUS_POLL_SECONDS=1` 高频读;并以 3 分析师与 4 分析师各跑一次
- 预期:`read_status` 从未因 JSON 错误返回 None(日志无 `status_corrupt`);`agents_total` 分别为 11 / 12,页面进度条分母一致
- 证据:日志、run 行

### AM-17 参数白名单
- 环境 LOCAL · 自动化 ✅
- 步骤:API 与页面分别提交 `analysts=["market","bogus"]`、`analysts=[]`、`code="1810.hk"`(应被 normalize 为 `1810.HK` 接受)、`code="ABC;rm"`、`date="2026-13-01"`、`date=明天`
- 预期:非法 → 400 `{"error":"invalid_*","message":中文}`;页面内联错误;runner 端对非法 `--analysts` 直接退出码 1(单测)
- 证据:响应体

### AM-18 container.log 落档不渲染
- 环境 LOCAL · 自动化 ✅
- 步骤:`SIM_MODE=fail` 发起
- 预期:`runs/<id>/container.log` 存在且含 sim 的异常栈;详情页只显示该文件**路径**;`GET /api/v1/runs/{id}` 不含日志内容
- 证据:`ls`、页面截图、响应体

### AM-19 🔴 财报获取 happy path:提交→进度→终态→归档→下载→304
- 环境 LOCAL(Docker + `integration-kit` mock)· 自动化 ✅
- 前置:`docker compose -f integration-kit/compose.yaml up -d mock`(127.0.0.1:18765);默认(success)场景;管理台 `REPORTS_API_BASE_URL=http://127.0.0.1:18765`
- 步骤:① `POST /api/v1/report-jobs {code:"700.hk",last_n:2}` ② 轮询任务至终态 ③ 同标的再次发起 ④ `GET /api/v1/archive/reports?code=0700.HK` ⑤ `GET /api/v1/archive/reports/{rid}` ⑥ `GET .../file` 核对 `X-Checksum-SHA256`/`ETag`/sha256 ⑦ 带 `If-None-Match` 重下
- 预期:① 202 status=pending、symbol=`0700.HK`、instrument_id 已绑定 ② succeeded、remote_job_id 非空、report_ids 2 份、submit_attempts=1 ③ 第二次 409(同标的互斥,零副作用) ④ 归档含这些 report_ids ⑤ 详情含 `artifacts[].sha256` ⑥ 200 且 sha256 一致、`ETag`=`"<sha256>"`、Content-Disposition attachment ⑦ 304、无 body
- 测试:`tests/integration/test_reports_mock.py::TestAppEndToEnd::test_submit_progress_terminal_archive_download`、`TestClientContract::test_happy_path_submit_poll_list_detail_download_checksum`、`TestClientContract::test_health`
- 证据:响应体、任务行与审计、下载响应头

### AM-20 进度序列(queued/running→终态)
- 环境 LOCAL · 自动化 ✅
- 前置:mock 可达;slow 场景
- 步骤:提交 `MSFT`,`wait_for_terminal(on_poll=记录每次状态)`
- 预期:去重后的状态序列首项 ∈ {queued,running}、末项 ∈ {succeeded,partial,failed};至少出现一次 queued 或 running
- 测试:`TestClientContract::test_progression_queued_running_terminal`
- 证据:状态序列

### AM-21 幂等重放(同键 202/202→200)
- 环境 LOCAL · 自动化 ✅
- 前置:mock 可达;slow 场景
- 步骤:同一 `Idempotency-Key` 连续提交两次 → 等终态 → 再提交一次;另用新 key 分别以「省略 last_n/refresh」与「显式 last_n=4,refresh=false」提交
- 预期:两次提交均 202 且 job_id 相同;终态后重放 200 且 job_id 相同;省略默认值与显式默认值视为同一请求(job_id 相同)
- 测试:`TestClientContract::test_idempotent_replay_same_job_pending_202_then_terminal_200`
- 证据:响应状态与 job_id

### AM-22 409 idempotency_conflict
- 环境 LOCAL · 自动化 ✅
- 前置:mock 可达
- 步骤:同一 key 先以 `last_n=1` 提交,再以 `last_n=2` 提交;管理台侧同键冲突由轮询线程映射
- 预期:第二次 409、code=`idempotency_conflict`、不可重试;管理台侧该错误落 `error`(不可重试)
- 测试:`TestClientContract::test_idempotency_conflict_409`;本地映射见 `tests/unit/test_reports_unit.py::TestPoller::test_conflict_and_validation_are_terminal_errors`
- 证据:problem+json、任务行

### AM-23 参数错误矩阵
- 环境 LOCAL · 自动化 ✅
- 前置:mock 可达
- 步骤:客户端侧:空 key / 超长 key / 空 symbols / `last_n=21` / 未知字段 / 缺 key / 非法 key / `text/plain` / 非法 JSON;应用侧:`code="ABC;rm"`、`last_n=21`、`last_n=0`、归档 `code=bad;`、未知 job id、页面片段非法 code
- 预期:客户端侧 ValueError 或 problem+json(422 `invalid_request`、400 `missing_idempotency_key`、400 `invalid_idempotency_key`、415 `unsupported_media_type`);应用侧 400 `invalid_request`、404 `not_found`;页面片段内联 `flash-error`
- 测试:`TestClientContract::test_validation_errors_problem_json`、`TestAppEndToEnd::test_invalid_params_400`
- 证据:响应体

### AM-24 queue_full 429 + Retry-After(管理台同键重试)
- 环境 LOCAL · 自动化 ✅
- 前置:mock 场景 queue_full
- 步骤:客户端提交观察 429 与 `Retry-After`;应用侧以 queue_full 起任务,待 `submit_attempts≥1` 后切正常 client 并清 `next_attempt_at`,等终态;对照前后 `idempotency_key`
- 预期:客户端 `ProblemError` status=429、code=`queue_full`、`retry_after>0`;应用任务保持 pending、`submit_attempts≥1`、error 以 queue_full 开头;恢复后 succeeded、`submit_attempts≥2`,前后 idempotency_key 相同
- 测试:`TestClientContract::test_queue_full_429_retry_after`、`TestAppEndToEnd::test_queue_full_then_recovers_with_same_key`
- 证据:响应、任务行

### AM-25 partial 保留可用文件 + report_period=null 不推测
- 环境 LOCAL · 自动化 ✅
- 前置:mock 场景 partial
- 步骤:提交 `0700.HK` `last_n=2`;取终态;逐个 `get_report` 并下载
- 预期:任务 status=partial 且有 report_ids 与 warnings;`results[0].status=partial`;至少一个报告 `report_period is None` 且 `period_source=unknown`;每个可用文件仍可下载并通过 sha256 校验
- 测试:`TestClientContract::test_partial_keeps_usable_files_and_unknown_period`
- 证据:终态文档、报告详情、下载

### AM-26 failed 是 HTTP 200 + 可重试 error
- 环境 LOCAL · 自动化 ✅
- 前置:mock 场景 failed
- 步骤:提交 `600519`;取终态;再原始 `GET /api/v1/fetch-jobs/{id}`
- 预期:HTTP 200 且 status=failed;`results[0].status=failed`、report_ids=[];`error.code=source_unavailable`、`retryable=true`
- 测试:`TestClientContract::test_failed_job_is_http_200_with_retryable_error`
- 证据:响应

### AM-27 no_reports 空结果合法
- 环境 LOCAL · 自动化 ✅
- 前置:mock 场景 no_reports
- 步骤:提交 `AAPL`;取终态
- 预期:任务 status=succeeded、`results[0].status=no_reports`、report_ids=[];warnings 含 `no_matching_reports`
- 测试:`TestClientContract::test_no_reports_is_success_with_empty_result`
- 证据:终态文档

### AM-28 未知 ID → 404
- 环境 LOCAL · 自动化 ✅
- 前置:mock 可达
- 步骤:`get_job("job_nope")`、`get_report("r_nope")`、下载未知报告;对真实报告传不属于它的 `artifact_id`
- 预期:均 404、code=`not_found`(artifact 不属于该报告同样 404);应用侧归档代理透传 404
- 测试:`TestClientContract::test_unknown_ids_404`;应用侧透传见 `TestAppEndToEnd::test_submit_progress_terminal_archive_download`
- 证据:problem+json

### AM-29 有界超时 + 刷新
- 环境 LOCAL · 自动化 ✅
- 前置:slow 场景;缩短等待预算
- 步骤:客户端以 `max_wait=1.5` 跑 slow 观察 `WaitTimeout`;应用侧 `reports_max_wait=1.5` 起任务至 timeout,再恢复预算并 `POST /api/v1/report-jobs/{id}/refresh`
- 预期:客户端 ≤4s 抛 `WaitTimeout`,last_document.status ∈ {queued,running};应用任务 timeout、error_code=`wait_timeout`、保留 remote_job_id;页面出现「刷新服务端状态」;refresh 后 running,再至 succeeded
- 测试:`TestClientContract::test_bounded_timeout_on_slow`、`TestAppEndToEnd::test_bounded_timeout_then_refresh`
- 证据:异常、任务行、页面

### AM-30 归档分页 cursor 无重复
- 环境 LOCAL · 自动化 ✅
- 前置:mock 可达
- 步骤:提交 `TSLA` `last_n=5` 等终态;`limit=2` 逐页翻到 `next_cursor` 为空;再以 `cursor=garbage` 查询
- 预期:各页 report_id 无重复、累计 ≥5、页数 ≥3;非法 cursor → 400 `invalid_cursor`
- 测试:`TestClientContract::test_pagination_cursor_no_duplicates`
- 证据:各页 report_id

### AM-31 业务代码无 mock 控制项
- 环境 LOCAL · 自动化 ✅
- 步骤:构造生产 client 断言 `default_headers` 无场景头;AST 扫描 `app/**/*.py` 的字符串常量
- 预期:无 `X-Mock-Scenario` / `/__mock/` 常量(仅 docstring 说明性提及不算)
- 测试:`TestClientContract::test_no_mock_headers_in_production_client`
- 证据:pytest 输出

### AM-32 应用侧 partial/failed/no_reports 端到端(页面文案 + 审计)
- 环境 LOCAL · 自动化 ✅
- 前置:mock 可达
- 步骤:分别以 partial(`NVDA`)、failed(`600519.SS`)、no_reports(`MU`)经应用发起,等终态,核对页面与审计
- 预期:状态与 report_ids 符合场景;partial 页面含「部分完成」与「下载原文」;failed `error_code=source_unavailable`、`error_retryable=true`;no_reports `results[0].status=no_reports`;审计含 create+finalize
- 测试:`TestAppEndToEnd::test_partial_and_failed_and_no_reports_via_app`
- 证据:响应、页面、审计

### AM-33 功能关闭态 + 未鉴权
- 环境 LOCAL · 自动化 ✅
- 前置:不配 `REPORTS_API_BASE_URL`(默认关闭)
- 步骤:`GET /reports`;`POST /api/v1/report-jobs`;`GET /api/v1/archive/reports`;`GET /healthz`;去掉 `Authorization` 再访问
- 预期:`/reports` 200 且提示 `REPORTS_API_BASE_URL`;两个 API 返回 503 `reports_disabled`;`/healthz` 的 `reports.enabled=false`;未鉴权时 API 401、页面 302 跳 `/login`
- 测试:`tests/unit/test_reports_unit.py::TestDisabledFeature::test_page_shows_notice_and_api_503`、`TestDisabledFeature::test_unauth`
- 证据:响应体、`/healthz`

## 2. NAS 真实环境验收流程(P3)

| 步 | 操作 | 需用户确认 |
|---|---|---|
| 1 | `scripts/fetch_vendor.sh` + `scripts/verify_vendor.sh`(哈希一致)→ `pytest tests/shape` → `pytest -m real` | 否 |
| 2 | `scripts/sync_to_nas.sh` 同步仓库到 `/home/chen/docker/agents-manage/` | 否(不触碰 TA 目录) |
| 3 | NAS 上 `docker compose build`;`AM_BIND=127.0.0.1` 启动;`/healthz` 检查 docker/镜像/挂载 | 否 |
| 4 | 添加用户指定的 1 个标的;手动发起(用户指定日期、分析师集合) | **是** |
| 5 | 全程观察:进度更新、tokens 增长、最终 succeeded、三产物齐全(AM-03 c)、AM-07 密钥扫描(由用户执行 grep) | — |
| 6 | 第二次发起同标的,中途取消,再 resume(AM-04) | **是** |
| 7 | 切 `AM_BIND=192.168.1.150` + token,从 Windows 浏览器访问三页 | 否 |
| 8 | 配置正式调度;观察 ≥3 个交易日 | 否(调度会自动跑,启用前告知用户) |
| 9 | 填写 `docs/acceptance-records/<date>-nas.md`,合并 release → main,tag | 否 |
| 10 | 财报服务只读健康检查 + 一次小任务(reports-fetcher,见 REPORTS-FETCHER.md §6:先 `GET /health/ready`、`GET /api/v1/reports` 只读,再 `last_n=1, refresh=false` 并读 warnings) | **是** |

## 3. 验收记录模板

```markdown
# 验收记录 <YYYY-MM-DD> <LOCAL|NAS>
- 执行者:
- 代码版本(commit/tag):
- 环境:sim 镜像 tag / NAS 镜像 image id / 管理台镜像 id
| AM | 结果(PASS/FAIL/N/A) | 证据路径 | 备注 |
|---|---|---|---|
| AM-01 | | | |
| … | | | |
## 缺陷清单
| # | AM | 描述 | 分支 | 状态 |
## 结论
可上线 / 不可上线(原因)
```
