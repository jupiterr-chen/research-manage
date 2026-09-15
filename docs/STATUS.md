# 进度台账(STATUS)

> 由执行 agent 维护,协议见 [AGENT-PLAYBOOK.md](AGENT-PLAYBOOK.md)。状态:`todo` / `in_progress` / `awaiting_confirmation` / `blocked` / `done`。与 git 不一致时以 `git branch -r --merged origin/develop` 为准。

| 步 | 分支 | 状态 | 分支最新 commit | 合入 develop 的 sha | 备注 |
|---|---|---|---|---|---|
| S1 | feat/p0-foundation | done | 1d1ebf2 | b57c45c (merge) | 2026-09-15 用户确认;4 项假设与 ruff 豁免均获认可 |
| S2 | feat/runner | done | e89328f | 737d856 (merge) | 2026-09-15 用户确认;[interface] 常量修正获认可 |
| S3 | feat/executor | done | 05f91c1 | 9b7d48a (merge) | 2026-09-15 用户确认 |
| S4 | feat/scheduler | awaiting_confirmation | (本次提交) | | 保留策略 03:30 任务一并接线 |
| S5 | feat/api-runs | todo | | | |
| S6 | feat/web-ui | todo | | | |
| S7 | feat/deploy | todo | | | |
| S8 | feat/acceptance | todo | | | |
| S9 | 移交(tag v1.0.0-rc1) | todo | | | |

## 汇报摘要(每步一条,最新在上)

### S4 feat/scheduler — awaiting_confirmation(2026-09-15)

- 交付:app/scheduler.py 真实实现(BackgroundScheduler + MemoryJobStore + Asia/Shanghai;
  单线程执行器 + id 升序注册保证同 tick 顺序;触发只调 enqueue_scheduled;
  next_fires 今日预告);schedules 写操作经 on_change 钩子触发 rebuild_jobs(server lifespan 接线);
  每日 03:30 保留策略任务(retention.purge,R-EXE-11 接线)。
- 测试:unit 318 全绿(新增 scheduler 19 项:周末不触发/周一去重全量/同 tick id 顺序/
  next_fires/trigger 字段/钩子/生命周期);ruff 零告警。
- 假设:APScheduler 3.11 的 Job.next_run_time 未调度时不可读,job_next_fire 回退 trigger 求值。

### S3 feat/executor — awaiting_confirmation(2026-09-15)

- 交付:app/executor/{launcher,status_reader,verdict,worker,retention}.py(DESIGN §4.5~4.8 签名);
  替换 S1 Worker stub;lifespan 自检失败拒绝启动;container.log scrub 落档;AM_SIM_ENV 注入;
  FakeLauncher 补全(内存模拟容器生命周期 + status.json + TA 产物)。
- 内部调整:runs.finalize 去掉 status='running' 守卫(launch_failed/host_restarted 需终结 queued);
  DockerLauncher 增加可选 network 构造参数(AM_TA_NETWORK)。
- 测试:unit 299(新增 worker 全链路 36 项)+ docker 集成 21(runner 11 + worker 10)全绿;
  dev.sh 实跑 /healthz docker_ok=true。
- 关键决策:监控循环改为每 tick wait(1s) 切片,使进度/取消/看门狗检查粒度=tick(否则 wait(5)
  阻塞会错过 status.json 轮询窗口);sim 的断点开关经 AM_SIM_ENV 注入(生产走挂载 .env)。

### S2 feat/runner — awaiting_confirmation(2026-09-15)

- 交付:runner/runner.py(SPEC §6.1 八步,合并 CLI 流式与 propagate 收尾)、sim/ 假包(8 种 SIM_MODE + 断点模拟)+ build_sim.sh、fetch/verify_vendor.sh(vendor/ 与 NAS 镜像 80 文件哈希一致)、tests/shape(AST 签名对等 13 项)、sim/llm_stub + build_local.sh(tradingagents-local 不改源码构建)+ compose.local llm-stub 服务、单测/集成/real 三层测试。
- [interface]:ANALYST_AGENT["social"] Social→Sentiment Analyst;FIXED_AGENTS 顺序 Neutral/Conservative 对调——按 NAS 真实 cli/main.py 修正,DESIGN §4.1 已同步。
- 测试:unit 249 + shape 13 + docker 11 + real 3 全绿;vendor 镜像一致性校验通过。
- 关键结论:真实图在 llm-stub 下能完整走完(3/4 分析师均 succeeded、三产物齐全、取消后 resume 续跑);未对数据工具打 monkeypatch(yfinance 经代理真实调用,失败按上游 fail-open)。

### S1 feat/p0-foundation — awaiting_confirmation(2026-09-15)

- 交付:config(含 is_loopback_bind 门禁)、db(SPEC §4 DDL+种子档案)、models、audit、services 四模块(DESIGN §4.4 全接口)、web 骨架(create_app/lifespan+worker/scheduler stub/auth+login/base.html+htmx 本地文件)、conftest(含 FakeLauncher 骨架)、scripts/dev.sh。
- 测试:pytest tests/unit 234 通过;ruff check/format 零告警;services 覆盖 98%;dev.sh 实跑 /healthz、/login 均 200。
- 待确认事项:本地模式(AM_TOKEN 未配置且回环绑定时放行)等 4 项假设,见汇报「假设与自行决定」。
