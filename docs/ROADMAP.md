# 路线图与分支计划(Roadmap)

## 1. 分支模型

```
main      ── 只接收 develop 的合并;每次合并 = 一个可部署版本(打 tag v1.x.y)
develop   ── 集成分支;所有 feat/* 经 PR 合入;CI(pytest + ruff)绿才可合
feat/*    ── 功能分支,从 develop 切出,PR 回 develop
fix/*     ── 缺陷分支(集成/验收阶段),同上
```

规则:
- **禁止直接 push `main`/`develop`**;一律 PR。PR 标题格式 `feat(EXE): R-EXE-05 status.json stale 判定`,描述列出覆盖的 R-ID/AM-ID 与测试方式
- 合并方式:`--no-ff` 保留分支轨迹;PR 内自行 rebase 到最新 develop
- 接口改动(DESIGN §4)MUST 先改 DESIGN.md,PR 标题带 `[interface]`,并通知其他分支
- 提交信息 Conventional Commits;禁止提交 `.env*`、`data/`、`vendor/`、`.local/`

## 2. 阶段总览

| 阶段 | 目标 | 分支 | 前置 | 退出标准 |
|---|---|---|---|---|
| **P0 基础** | 骨架 + 领域服务 + 契约固化,让 P1 能并行 | `feat/p0-foundation` | — | 单测绿;`python -m app.main` 能起(空页面 + /healthz);services 100% 接口按 DESIGN §4.4 实现 |
| **P1 并行开发** | 六条功能线同时推进 | `feat/runner` `feat/executor` `feat/scheduler` `feat/api-runs` `feat/web-ui` `feat/deploy` | P0 合入 develop | 各分支单测/集成测试绿并合入 develop |
| **P2 集成** | 本地端到端(sim 镜像)+ 自动化验收 | `feat/acceptance` + `fix/*` | P1 全部合入 | `pytest -m acceptance` 全绿;ACCEPTANCE.md 本地项全部 PASS 并有记录 |
| **P3 验收与上线** | NAS 真实环境验收、生产部署 | `release/v1.0` → `main` | P2 | ACCEPTANCE.md NAS 项 PASS;生产运行 ≥3 个交易日无 P0 缺陷 |

依赖图(P1 内):

```
p0-foundation ──┬── runner ─────────────┐
                ├── executor ───────────┤(集成测试需要 sim 镜像:runner 分支交付)
                ├── scheduler ──────────┤
                ├── api-runs ───────────┤(只依赖 services)
                ├── web-ui ─────────────┤(只依赖 services + scheduler.next_fires 接口)
                └── deploy ─────────────┘(Dockerfile/compose 可先行;local compose 需要 sim)
```

## 3. 各阶段交付明细

### P0 `feat/p0-foundation`(串行,一个 agent)

| 交付 | 对应需求 |
|---|---|
| `app/config.py` + 绑定门禁测试矩阵 | R-FND-01/02 |
| `app/db.py` 连接/迁移/事务 + SPEC §4 DDL + 种子档案 | R-FND-03, R-SCH-06 |
| `app/models.py` 常量与校验 | R-FND-07 |
| `app/audit.py` scrub/audit | R-FND-06 |
| `app/services/{instruments,profiles,schedules,runs}.py` 全接口(DESIGN §4.4) | R-SVC-01~10 |
| `app/web/server.py` create_app + lifespan(worker/scheduler 以 stub 注入)+ 异常边界 + `/healthz` | R-FND-08/09 |
| `app/web/auth.py` + `/login` | R-FND-04/05, R-WEB-07 |
| `app/web/templates/base.html`、`static/htmx.min.js`(随仓库)、`app.css` 骨架 | R-WEB-01 |
| `tests/conftest.py`(临时库、TestClient、FakeLauncher 骨架、冻结时间) | R-FND-10 |
| `pyproject.toml`(ruff 配置)、`requirements*.txt`、`pytest.ini` markers | R-NFR-03/04 |
| **DoD**:`pytest tests/unit` 绿;ruff 零告警;services 单测覆盖 ≥90% | |

### P1 并行分支

**`feat/runner`**(可最先启动,与 P0 只共享 SPEC §6 契约)

| 交付 | 对应需求 |
|---|---|
| `runner/runner.py` | R-RUN-01~07 |
| `sim/`(Dockerfile + 假包 + 8 种 SIM_MODE)+ `scripts/build_sim.sh` | R-RUN-08 |
| `tests/unit/test_runner.py`(mock graph:成功/异常/SIGINT/自检失败/原子写) | R-RUN-02/04/06 |
| `tests/integration/test_runner_sim.py`(在 sim 容器内跑 runner,断言 status.json/reports/memory/退出码) | R-RUN-03/08 |
| `tests/shape/test_upstream_shape.py`(需 `vendor/`,无则 skip) + `scripts/fetch_vendor.sh`(只读 scp) | R-RUN-09 |
| DoD:sim 8 种模式退出码与产物符合 SPEC §6.1/6.2 | |

**`feat/executor`**

| 交付 | 对应需求 |
|---|---|
| `app/executor/launcher.py` ContainerSpec/DockerLauncher | R-EXE-04 |
| `app/executor/status_reader.py`、`verdict.py` | R-EXE-05/08/09 |
| `app/executor/worker.py`(自检、恢复、监控、取消、看门狗、container.log、异常隔离) | R-EXE-01/02/03/06/07/10/12 |
| `app/executor/retention.py` | R-EXE-11 |
| 单测(FakeLauncher)+ 集成测试(sim:ok/fail/hang+cancel/watchdog/no_memory/no_report/corrupt_status/resume/host_restarted 模拟) | AM-03/04/05/08/09 |

**`feat/scheduler`**

| 交付 | 对应需求 |
|---|---|
| `app/scheduler.py`(rebuild_jobs/next_fires/触发入队) | R-SCH-01~05 |
| 单测:冻结时间验证周六日不触发、周一去重只剩全量、同 tick 顺序 | AM-02/10/12 |

**`feat/api-runs`**

| 交付 | 对应需求 |
|---|---|
| `app/web/routes/api_runs.py` + pydantic schema + 错误映射 | R-API-01~05 |
| 单测:409 busy 体、already_done、400 校验、401、artifacts 不含内容 | AM-01/10/17 |

**`feat/web-ui`**

| 交付 | 对应需求 |
|---|---|
| `routes/pages.py`、`routes/fragments.py`、全部模板与 CSS | R-WEB-02~06/08 |
| 单测:页面 200、片段轮询属性存在、忙时内联、转义、无外链(扫描模板中的 `http(s)://`) | AM-01/14/15 |

**`feat/deploy`**

| 交付 | 对应需求 |
|---|---|
| `deploy/Dockerfile`(build-arg uid/gid,无 node)、`docker-compose.yml`、`docker-compose.local.yml`、`.env.example` | R-DEP-01~03 |
| `scripts/sync_to_nas.sh`、`scripts/gen_token.py` | R-DEP-04 |
| `docs/DEPLOY.md`(首次部署/升级/回滚/备份/socket-proxy 可选) | R-DEP-05 |
| 验证:本地 `docker compose -f deploy/docker-compose.local.yml up` 能跑通一次 sim 任务 | |

### P2 集成 `feat/acceptance`(+ `fix/*`)

| 交付 |
|---|
| `tests/acceptance/`:ACCEPTANCE.md 标「自动化」的用例逐条实现,`pytest -m acceptance` |
| 本地全链路演练:管理台容器 + sim,按 ACCEPTANCE.md 走一遍本地项,产出 `docs/acceptance-records/<date>-local.md` |
| 缺陷以 `fix/<AM-ID>-<slug>` 分支修复 |
| 退出:本地项全部 PASS;develop 打 tag `v1.0.0-rc1` |

### P3 验收与上线(由最终验收方执行)

1. `release/v1.0` 从 develop 切出,只接受 `fix/*`
2. **NAS 形状验收**:`scripts/fetch_vendor.sh` 拉真实包 → `tests/shape` 通过
3. **NAS 真实运行验收**(需用户确认,见 NAS-ACCESS.md):同步仓库到 `/home/chen/docker/agents-manage/`,以 `AM_BIND=127.0.0.1` 起管理台,手动发起一个用户指定的标的/日期,观察全程,核对 AM-03/04/07/13/18
4. 生产切换:`AM_BIND=192.168.1.150` + token;运行 ≥3 个交易日;每日核对调度产出
5. 合并 `release/v1.0` → `main`,tag `v1.0.0`;回填 develop
6. 产出 `docs/acceptance-records/<date>-nas.md` 与上线记录

## 4. 关键里程碑

| 里程碑 | 判据 |
|---|---|
| M-A 契约冻结 | SPEC v1.1 + DESIGN §4 接口无 open 问题 |
| M-B P0 合入 | develop 上 `python -m app.main` 可启动、/healthz 200 |
| M-C 首次本地端到端 | develop 上用 sim 镜像手动发起 → succeeded,report ✓ |
| M-D rc1 | 自动化验收全绿 |
| M-E NAS 首跑 | 真实镜像一次 succeeded(双重判定通过) |
| M-F 上线 | 3 个交易日调度稳定 |
