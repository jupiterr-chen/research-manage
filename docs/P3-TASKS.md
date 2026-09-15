# P3 验收任务书(P3-TASKS)

> 最终验收(P3)过程中发现的问题,统一登记在此,由开发 agent 领取修复。
> 修复分支 `fix/<T-ID>-<slug>`,PR 到 `release/v1.0`(P3 期间只接受 fix),合入后回填 develop。
> 严重度:**S1** 阻塞上线 / **S2** 上线前应修 / **S3** 上线后可修。
> 状态:open / in_progress / fixed(sha) / wontfix(原因)。

| ID | 严重度 | 状态 | 发现于 | 问题 | 期望 | 位置 |
|---|---|---|---|---|---|---|
| T-01 | S3 | open | P3 代码复核 | 执行容器在运行中被外部删除(`docker rm -f`)时,`DockerLauncher.wait()` 把 NotFound 与超时一并返回 `None`,Worker 持续等待直到看门狗(默认 120 min)才收尾 | `wait()` 区分 NotFound → 立即判 `failed(error=container_vanished)`;补单测 | `app/executor/launcher.py::wait`、`worker.py::_monitor` |
| T-02 | S2 | fixed(见下) | P3 部署复核 | compose `ports: "8090:8090"` 发布在宿主全部接口(0.0.0.0),SPEC §8 要求局域网部署绑 `192.168.1.150` | `ports: "192.168.1.150:8090:8090"` | `deploy/docker-compose.yml` |
| T-03 | S3 | wontfix(用户 2026-09-15 决定) | S8 | worker 内部转移(看门狗/host_restarted)无审计留痕:SPEC 约束 8 的 actor 枚举无系统角色 | 若将来需要:SPEC actor 增加 `system`,属契约变更 | `app/executor/worker.py` |

| T-04 | S3 | fixed(见下) | P3 部署 | compose 未设 `name:`,项目名取自目录 → 镜像/容器名为 `deploy-agents-manage*`,与 DEPLOY.md §5 的 `docker ps -qf name=agents-manage` 仍可匹配但易混淆 | compose 顶层 `name: agents-manage` | `deploy/docker-compose.yml` |
| T-05 | S1 | fixed(部署脚本/文档;代码部分 open) | P3 首次部署 | NAS 首次 `up -d` 失败:`data/` 不存在时 docker 以 root 创建 bind 源目录,管理台(uid 1000)`unable to open database file`,容器反复重启 | ① `sync_to_nas.sh` 预建 `data/runs`(已修);DEPLOY §2/§7 补说明(已修);② **代码**:lifespan 应在连接 SQLite 前检查 `AM_DATA_DIR` 可写并给出中文原因后退出码 2(R-EXE-03 已要求,但 DB 连接发生在自检之前) | `scripts/sync_to_nas.sh`、`docs/DEPLOY.md`、`app/web/server.py::lifespan` |
| T-06 | S1 | fixed(fix/T-06-workspace-host-path) | P3 第 4 步首次发起 | run 停在 queued:Worker 用 `AM_DATA_HOST`(宿主路径)在**容器内** mkdir 工作区 → `PermissionError: /home/chen`,每 tick 重抛。本地开发 HOST=DIR 故未被任何测试覆盖 | 工作区在 `AM_DATA_DIR/runs/<id>` 创建,`spec.workspace_host` 用 `AM_DATA_HOST` 拼接;新增单测强制 HOST≠DIR | `app/executor/worker.py::_launch_next` |
| T-07 | S1 | fixed(fix/T-07-scrub-url-secrets) | P3 第 4 步失败落档 | **密钥泄漏到 `container.log`**:上游 TradingAgents 把 FRED 请求 URL(含 `api_key=<真实值>`)打进 stderr,`scrub()` 只处理字典键名与 `sk-*`,URL 查询串/键值对/Bearer 形式未遮蔽 → 违反 SPEC 约束 7 / AM-07 | `scrub_text()` 新增 `key=value`、`key: value`、JSON `"key": "v"`、`Bearer xxx` 遮蔽;回归测试用真实日志行形态;NAS 上已存在的 container.log 现场重新脱敏 | `app/audit.py` |
| T-08 | S3 | open | P3 第 4 步 | 成功终态时 `current_agent` 停留在 `Aggressive Analyst`(12/12 已完成),不是最后一个节点 `Portfolio Manager` 或空;推测 runner 的 risk 团队三节点合并计数时未更新 current_agent | 终态 `phase=succeeded` 时 `current_agent` 置 null 或最后完成的 agent;页面显示"已完成" | `runner/runner.py` 状态映射 |

## 记录

- 2026-09-15 T-02、T-04:由验收方在 `release/v1.0` 直接修正(单行 compose 改动,不经开发 agent)。
- 2026-09-15 T-05:现场处置 `down → rmdir data → mkdir -p data/runs → up`,管理台 healthy;脚本/文档已修,代码部分留给开发 agent。
- 2026-09-15 T-06:由验收方修复(3 行 + 回归测试),理由:阻塞真实运行且改动边界清晰;`tests/conftest.py::make_settings` 默认 HOST=DIR 是测试盲区,建议开发 agent 在 T-05 代码项一并把 conftest 默认改为 HOST≠DIR。
- 2026-09-15 T-07:由验收方修复;AM-07 的本地验收(FAKEKEY 假值)之所以没抓到,是因为 sim 与 llm-stub 不会像真实上游那样把带 key 的 URL 打进日志——建议开发 agent 在 sim 的 `fail` 模式里加一行含 `api_key=` 的 stderr 输出,让 AM-07 本地用例覆盖该形态。
