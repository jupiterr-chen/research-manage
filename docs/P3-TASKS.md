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

## 记录

- 2026-09-15 T-02:由验收方在 `release/v1.0` 直接修正(单行 compose 改动,不经开发 agent)。
