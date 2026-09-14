# Agent 执行手册(AGENT-PLAYBOOK)

> 本文给**执行开发的 agent** 用。你不需要用户告诉你做什么:每次启动都按 §1 的协议自行判断当前该做哪一步,做完按 §3 的格式汇报,然后**停下等用户确认**。用户只会说"确认"或提出修改意见,不会替你记进度。
> 进度的唯一记录是 [docs/STATUS.md](STATUS.md),由你维护。

## 0. 你必须遵守的

1. 先读 [CLAUDE.md](../CLAUDE.md) 与 [docs/NAS-ACCESS.md](NAS-ACCESS.md)。NAS(192.168.1.150)上的 TradingAgents 只读,不改、不重启、不在上面跑分析;需要写 NAS 的事一律停下问用户。
2. 一次只推进**一个步骤**(§2 的 S1~S9)。步骤做完 → 汇报 → 等确认。用户未确认前不得合并到 develop、不得开始下一步。
3. 汇报如实:测试没跑写没跑,失败贴输出,DoD 没达到就写"未达到"并说明原因。
4. 不改 SPEC.md 的契约;若实现中发现契约有错,写进汇报的「需要用户决定」栏,等用户拍板。
5. 外网访问经代理 `http://192.168.1.150:7890`。

## 1. 每次启动的协议

```
1. cd D:\2.Develop\5.Codex\researcher-agent\Agents-Manage
   git fetch --all --prune
2. 读 docs/STATUS.md,找到第一个状态不是 done 的步骤 = 当前步骤 S。
   - 若 S 的状态是 awaiting_confirmation:说明上次已汇报、尚未被确认。
       · 用户这次的消息是"确认"类 → 执行 §4 的「确认后动作」,把 S 置 done,然后进入下一步骤的 todo(同一轮里只做到"宣布下一步骤开始",不继续实现)。
       · 用户提出修改 → 在原分支修改,重新汇报,状态仍为 awaiting_confirmation。
   - 若 S 的状态是 todo 或 in_progress:执行 S(§2 对应小节),完成后按 §3 汇报,状态置 awaiting_confirmation。
3. 每次改动 STATUS.md 都随分支一起 commit + push。
4. 全部 done → 按 S9 通知用户。
```

判断"步骤已合入"的客观依据:`git branch -r --merged origin/develop | grep <分支名>`。STATUS.md 与 git 不一致时以 git 为准并修正 STATUS.md。

## 2. 步骤清单(顺序固定)

| 步 | 分支 | 前置 | 简述 |
|---|---|---|---|
| S1 | `feat/p0-foundation` | — | 配置/DB/鉴权/审计/services/应用骨架 |
| S2 | `feat/runner` | — | runner.py、sim 镜像、llm-stub、tradingagents-local、契约对等测试 |
| S3 | `feat/executor` | S1 合入;S2 的 sim 镜像 | Worker 线程、docker 启停、终态判定、看门狗、恢复 |
| S4 | `feat/scheduler` | S1 合入 | APScheduler、交易日语义、去重入队 |
| S5 | `feat/api-runs` | S1 合入 | `/api/v1/runs*` |
| S6 | `feat/web-ui` | S1 合入 | 三页 + htmx 片段 |
| S7 | `feat/deploy` | S1、S2 合入 | Dockerfile、compose、部署手册、同步脚本 |
| S8 | `feat/acceptance` | S1~S7 全部合入 | 自动化验收 + 本地验收记录 + fix/* |
| S9 | — | S8 合入 | 打 tag `v1.0.0-rc1`,通知用户移交最终验收(P3 由验收方执行,不是你) |

S1 与 S2 无相互依赖,但为了让用户每次只确认一件事,**仍按 S1 → S2 顺序做**。S3~S7 同理按序做。如果你的运行环境支持子代理且用户明确允许并行,可以把 S3~S7 并行,但汇报与确认仍按步骤逐个进行。

每一步的通用流程:
```
git checkout develop && git pull
git checkout -b <分支> 2>/dev/null || git checkout <分支> && git rebase develop
…实现…
pytest / ruff 按该步 DoD
更新 docs/STATUS.md(状态、commit、汇报摘要)
git push -u origin <分支>
按 §3 汇报,停。
```

### S1 `feat/p0-foundation`

读:SPEC.md → docs/DESIGN.md → docs/REQUIREMENTS.md M1/M2 → docs/ROADMAP.md §3 "P0"。
交付:ROADMAP §3 P0 表格全部;需求 R-FND-01~11、R-SVC-01~10、R-SCH-06、R-WEB-01/07。
要点:
- 接口签名严格按 DESIGN §4.1~4.4、§4.11;后续步骤都依赖这些签名。确需改动先改 DESIGN.md,汇报中标注 `[interface]`。
- Worker 与 Scheduler 只提供 DESIGN §4.8/§4.9 的 stub,lifespan 能注入即可。
- `services/runs.py::create_run` 在一个事务内完成 R-SVC-04 六步,任何失败零副作用;单测覆盖忙拒、already_done、去重、force。
- 绑定门禁 `is_loopback_bind()`,写 AM-06 完整测试矩阵。
- `htmx.min.js` 下载后放 `app/web/static/` 随仓库提交。
- 提供 `scripts/dev.sh`。
DoD:`pytest tests/unit` 绿;`ruff check . && ruff format --check .` 零告警;services 覆盖 ≥90%;`scripts/dev.sh` 能起服务,`/healthz`、`/login` 可访问。

### S2 `feat/runner`

读:docs/NAS-ACCESS.md → SPEC.md §1.3、§6 → docs/DESIGN.md §6、§7、§7.1、§8 → docs/REQUIREMENTS.md M4(R-RUN-01~12)→ docs/ROADMAP.md §3 "feat/runner"。
背景:TradingAgents 的 CLI 路径只写 reports/*.md,`propagate()` 只写 memory 与 full_states_log;runner 必须按 SPEC §6.1 八步合并两条路径。
交付:
1. `runner/runner.py`(单文件,仅标准库 + 镜像内 tradingagents)
2. `sim/`(假 tradingagents 包 + 8 种 SIM_MODE)+ `scripts/build_sim.sh`
3. `scripts/fetch_vendor.sh`(只读 scp NAS 的 `tradingagents/`、`cli/`、`Dockerfile`、`pyproject.toml`、`requirements.txt` → `vendor/`)、`scripts/verify_vendor.sh`(不挂卷、不传 env、`--network none` 的一次性容器读镜像内包哈希与 vendor/ 比对)
4. `tests/shape/`(8 个属性 `inspect.signature` 对等;cli 常量与 `app/models.py` 对等)
5. `sim/llm_stub/` + `scripts/build_local.sh`(vendor/ + 上游 Dockerfile 本地构建 `tradingagents-local:latest`,不改源码)
6. `tests/unit/test_runner.py`、`tests/integration/test_runner_sim.py`、`tests/integration/test_runner_real.py`(`-m real`)
代理:执行容器 env 用 DESIGN §8 的 `.local/ta.env` 模板,`NO_PROXY` 含 `llm-stub`。
DoD:sim 8 种模式退出码与产物符合 SPEC §6.1/6.2;`tests/shape` 通过;`-m real` 在 local 镜像上 3 与 4 分析师各 succeeded、三产物齐全、取消后 resume 续跑。
汇报必答:真实图在 llm-stub 下能否走完;若对数据工具打了 monkeypatch,列出。

### S3 `feat/executor`

读:SPEC.md §3、§6.3、§6.4 → docs/DESIGN.md §2、§3、§4.5~4.8、§5 → docs/REQUIREMENTS.md M3(R-EXE-01~12)。
交付:`app/executor/{launcher,status_reader,verdict,worker,retention}.py`,签名按 DESIGN §4.5~4.8;替换 S1 的 Worker stub。
硬规则:docker-py 只在 Worker 线程;挂载源只用 `AM_*_HOST`;environment 只允许 `AM_RUN_ID`、`TZ`(本地加 `AM_SIM_ENV`);`network=AM_TA_NETWORK`;终态按 SPEC §6.4 顺序与双重判定;container.log 经 scrub 落档后 remove;worker 异常不退出线程。
测试:单测用 `FakeLauncher`(S1 只给骨架则补全);集成测试 `@pytest.mark.docker` 用 sim 覆盖 ok/fail/hang+cancel/watchdog(缩短阈值)/no_memory/no_report/corrupt_status/resume/host_restarted 模拟。
DoD:单测 + docker 集成测试绿;ruff 零告警。

### S4 `feat/scheduler`

读:SPEC.md §3 约束 1/5、§6.5 → docs/DESIGN.md §4.9 → docs/REQUIREMENTS.md M5。
交付:`app/scheduler.py`(start/shutdown/rebuild_jobs/next_fires),MemoryJobStore,Asia/Shanghai;触发只调 `services.runs.enqueue_scheduled`;同 tick 按 schedule.id 顺序;`services/schedules.py` 写操作后调 `rebuild_jobs()`;替换 S1 的 stub。
测试(freezegun):周六日不触发;周一同标的日频+周频只产生 1 条全量 run;三条同时刻按 id 顺序;`next_fires` 正确。
DoD:单测绿;ruff 零告警。

### S5 `feat/api-runs`

读:SPEC.md §5 → docs/DESIGN.md §4.4、§4.10、§4.11 → docs/REQUIREMENTS.md M6。
交付:`app/web/routes/api_runs.py`,只有 SPEC §5 的 6 个端点;业务全部调 `services.runs`,不写第二套校验;409 busy 体含 current{…} 与 queued;artifacts 只返回路径;actor=api。
测试:409 busy/already_done/force、AM-17 全部非法参数 → 400、401、cancel/resume 状态限制、artifacts 不含内容。
DoD:单测绿;ruff 零告警。

### S6 `feat/web-ui`

读:SPEC.md §7、§8 → docs/DESIGN.md §4.10、§4.11 → docs/REQUIREMENTS.md M7。风格参照 `D:\2.Develop\5.Codex\stocks-alert\app\web`(只读参考)。
交付:`routes/pages.py`、`routes/fragments.py`、全部模板、`app.css`。
硬规则:无外链(模板/静态中不得出现 `http(s)://` 资源引用);`hx-trigger="every 15s"`,无 WebSocket/SSE;忙时手动发起内联显示 409 信息不跳转;调度行显示 `describe()` 文案,周一冲突提示「周一将只跑全量」;详情页 agent 时间线用 `models.agent_sequence`,container.log 只显示路径,取消 `hx-confirm`,续跑仅 cancelled/failed;禁 `|safe`。
测试:三页 200、片段轮询属性、忙时内联、转义、模板无外链扫描、登录跳转。
DoD:单测绿;ruff 零告警。

### S7 `feat/deploy`

读:docs/NAS-ACCESS.md → SPEC.md §9 → docs/DESIGN.md §3、§7.1、§8 → docs/REQUIREMENTS.md M8 → docs/DEPLOY.md 章节要求。
交付:`deploy/Dockerfile`(python:3.12-slim,build-arg APP_UID/DOCKER_GID,非 root,无 node)、`deploy/docker-compose.yml`(SPEC §9,含 healthcheck)、`deploy/docker-compose.local.yml`(管理台 + llm-stub,网络名对应 `AM_TA_NETWORK`)、`deploy/.env.example`、`scripts/sync_to_nas.sh`(只同步到 `/home/chen/docker/agents-manage/`,排除 data/vendor/.local/.git)、`scripts/gen_token.py`、`docs/DEPLOY.md` 全部 7 章。
验证:本地 `docker compose -f deploy/docker-compose.local.yml up` 跑通一次 sim 任务;镜像内 `which node` 为空。
NAS 只允许 `stat`、`docker image inspect` 之类只读命令核对文档数值;**不要创建 `/home/chen/docker/agents-manage/`、不要部署**。
DoD:本地 compose 跑通;DEPLOY.md 完整。

### S8 `feat/acceptance`

读:docs/ACCEPTANCE.md 全文 → docs/ROADMAP.md §3 "P2" → docs/REQUIREMENTS.md M9。
交付:
1. `tests/acceptance/`:ACCEPTANCE.md 标「自动化 ✅」的项逐条实现,`@pytest.mark.acceptance`(AM-03 d 用 `-m real`)。
2. 本地全链路演练:用 compose.local 起管理台,按 ACCEPTANCE.md §1 逐条走 LOCAL 项,产出 `docs/acceptance-records/<date>-local.md`(§3 模板,每条附证据路径)。
3. 缺陷:开 `fix/<AM-ID>-<slug>` 分支修复并合入 develop(fix 分支不需要单独确认,但要在汇报中列出)。
NAS 项(ACCEPTANCE §2)不由你执行,标 N/A。
DoD:`pytest -m acceptance` 全绿;本地项全部 PASS;记录文件已提交。

### S9 移交

develop 打 tag `v1.0.0-rc1` 并 push;STATUS.md 全部 done;汇报中写明:「开发阶段完成,请将 P3(NAS 验收与生产部署)交给最终验收方,入口 docs/ACCEPTANCE.md §2 与 docs/ROADMAP.md §3 P3」。

## 3. 汇报格式(每步完成后,原样输出)

```
## 步骤 S<n> <分支> — 待确认

**状态**:完成 / 部分完成 / 阻塞
**分支与 commit**:<分支> @ <sha>(已 push)
**交付清单**:
- [x] …(对应 R-ID)
- [ ] …(未完成的写原因)
**测试**:
- pytest tests/unit:<通过数/失败数>(贴失败输出)
- pytest -m docker / -m real / -m acceptance:<结果或"未运行:原因">
- ruff:<零告警 / 告警数>
**DoD**:达到 / 未达到(哪条)
**接口变更**:无 / [interface] …(已同步 DESIGN.md)
**假设与自行决定**:
- …
**需要用户决定**:
- …(没有写"无")
**下一步**:S<n+1> <分支>(等待确认后开始)
```

## 4. 确认后动作

用户回复"确认"(或等价表述)后,在同一轮里:
```
git checkout develop && git pull
git merge --no-ff <分支> -m "merge: S<n> <分支>"
pytest tests/unit(合并后再跑一次,失败则不 push,回到修复)
git push origin develop
更新 docs/STATUS.md:S<n> → done(记合并 sha);S<n+1> → todo
git add docs/STATUS.md && git commit -m "status: S<n> done" && git push
输出一行:「S<n> 已合入 develop(<sha>)。下一步 S<n+1> <分支>,回复"继续"即开始。」
```
然后停。用户说"继续"时按 §1 协议执行 S<n+1>。

## 5. 阻塞处理

- 需要 NAS 写操作 / 需要真实 LLM 额度 / 契约有误 → 汇报「阻塞」并写清需要用户做什么,状态置 `blocked`,不要绕过。
- 前置分支缺东西(如 sim 镜像未合入)→ 先用 fake/stub 完成能做的部分,汇报中标明哪些测试待前置合入后补跑。
