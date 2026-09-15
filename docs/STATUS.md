# 进度台账(STATUS)

> 由执行 agent 维护,协议见 [AGENT-PLAYBOOK.md](AGENT-PLAYBOOK.md)。状态:`todo` / `in_progress` / `awaiting_confirmation` / `blocked` / `done`。与 git 不一致时以 `git branch -r --merged origin/develop` 为准。

| 步 | 分支 | 状态 | 分支最新 commit | 合入 develop 的 sha | 备注 |
|---|---|---|---|---|---|
| S1 | feat/p0-foundation | done | 1d1ebf2 | b57c45c (merge) | 2026-09-15 用户确认;4 项假设与 ruff 豁免均获认可 |
| S2 | feat/runner | done | e89328f | 737d856 (merge) | 2026-09-15 用户确认;[interface] 常量修正获认可 |
| S3 | feat/executor | done | 05f91c1 | 9b7d48a (merge) | 2026-09-15 用户确认 |
| S4 | feat/scheduler | done | a04ea23 | 17e8a83 (merge) | 2026-09-15 用户确认 |
| S5 | feat/api-runs | done | ecbb630 | 253216a (merge) | 2026-09-15 用户确认 |
| S6 | feat/web-ui | done | 67301e9 | 4a3f932 (merge) | 2026-09-15 用户确认 |
| S7 | feat/deploy | done | 9d9fca3 | db949e7 (merge) | 2026-09-15 用户确认 |
| S8 | feat/acceptance | done | a66f444 | 86ec931 (merge) | 2026-09-15 用户确认;AM-05 审计留痕偏差经用户决定忽略 |
| S9 | 移交(tag v1.0.0-rc1) | done | 49e160d | tag v1.0.0-rc1 | 2026-09-15 开发阶段完成,移交 P3 |

## 汇报摘要(每步一条,最新在上)

### S9 移交 — done(2026-09-15)

- develop @ 49e160d 打 tag **v1.0.0-rc1** 并 push;S1~S9 全部 done。
- 开发阶段完成:P3(NAS 验收与生产部署)移交最终验收方,
  入口 docs/ACCEPTANCE.md §2 与 docs/ROADMAP.md §3 P3。
- 遗留(用户已决定忽略):worker 内部转移(看门狗/host_restarted)无审计留痕(SPEC actor 枚举限制)。

### S8 feat/acceptance — awaiting_confirmation(2026-09-15)

- 交付:tests/acceptance/ 34 项(LOCAL 全部自动化项 + AM-03d -m real);compose.local 全链路演练;
  验收记录 docs/acceptance-records/2026-09-15-local.md(每条附证据)。
- 测试:pytest -m acceptance 34 全绿(8:23,含真实栈);unit+shape 372 全绿;ruff 零告警。
- 随分支修复(记录缺陷清单 #2/#3):verdict 缺项文案补 trading_memory.md;resume 端点拒绝多余字段。
- 待用户拍板(#1):AM-05 手册预期 audit run.watchdog,但 SPEC 约束8 actor 枚举无系统角色——
  worker 内部转移(看门狗/host_restarted)未留审计,是否扩枚举属 SPEC 变更。

### S7 feat/deploy — awaiting_confirmation(2026-09-15)

- 交付:deploy/Dockerfile(python:3.12-slim、build-arg APP_UID/DOCKER_GID、非 root、无 node,
  pip --trusted-host 规避代理 MITM)、docker-compose.yml(SPEC §9+healthcheck)、
  docker-compose.local.yml(管理台+llm-stub)、.env.example、scripts/sync_to_nas.sh(排除
  data/vendor/.local/.git/.venv/.env)、scripts/gen_token.py、docs/DEPLOY.md 全 7 章
  (含 NAS 只读实测数值 1000:1000 / 994 / c85a52e…)。
- 验证:本地 compose.local up → /healthz docker_ok=true → API 发起 sim 任务 → succeeded、
  双重判定通过、artifacts 6 路径、container.log 落档;镜像内 which node 为空。
- 假设:容器内 AM_BIND=0.0.0.0(LAN 暴露由端口映射实现,SPEC §8 门禁仍生效);
  Docker Desktop 的 sock 为 root:root,本地 compose 以 root 跑(生产 1000:994)。

### S6 feat/web-ui — awaiting_confirmation(2026-09-15)

- 交付:routes/fragments.py(overview/run/instruments/schedules/run_detail 全套)+ pages.py 三页首屏
  + 全部模板(总览壳/overview 片段/run_result OOB/instruments_block/run_detail/时间线)+ CSS 扩展。
- 硬规则落实:无外链(模板/CSS 扫描测试)、every 15s 无 WS/SSE、忙时 200 内联不跳转、
  describe() 文案、周一冲突提示、agent_sequence 时间线、container.log 仅路径、取消 hx-confirm、
  续跑仅 cancelled/failed、全站无 |safe(扫描测试)。
- 测试:unit 372 全绿(新增 web-ui 26 项);ruff 零告警(Form 加入 immutable-calls);dev.sh 实跑三页 200。

### S5 feat/api-runs — awaiting_confirmation(2026-09-15)

- 交付:app/web/routes/api_runs.py 六端点(SPEC §5);业务全部委托 services.runs;
  409 busy 体含 current{7 字段}+queued;already_done 含 run_id;artifacts 只回路径;
  写操作 actor=api;models 增补 code_market()(400/404 分流,格式规则仍唯一来源)。
- 测试:unit 346 全绿(新增 API 28 项:busy 体/already_done+force/非法参数矩阵/401 全端点/
  cancel/resume 状态限制/artifacts 无内容);ruff 零告警(新增 flake8-bugbear
  extend-immutable-calls 声明 FastAPI Depends 惯用法)。

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
