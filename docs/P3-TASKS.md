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

## 记录

- 2026-09-15 T-02、T-04:由验收方在 `release/v1.0` 直接修正(单行 compose 改动,不经开发 agent)。
- 2026-09-15 T-05:现场处置 `down → rmdir data → mkdir -p data/runs → up`,管理台 healthy;脚本/文档已修,代码部分留给开发 agent。
