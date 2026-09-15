# 进度台账(STATUS)

> 由执行 agent 维护,协议见 [AGENT-PLAYBOOK.md](AGENT-PLAYBOOK.md)。状态:`todo` / `in_progress` / `awaiting_confirmation` / `blocked` / `done`。与 git 不一致时以 `git branch -r --merged origin/develop` 为准。

| 步 | 分支 | 状态 | 分支最新 commit | 合入 develop 的 sha | 备注 |
|---|---|---|---|---|---|
| S1 | feat/p0-foundation | awaiting_confirmation | (见下) | | 待用户确认后合并 |
| S2 | feat/runner | todo | | | |
| S3 | feat/executor | todo | | | |
| S4 | feat/scheduler | todo | | | |
| S5 | feat/api-runs | todo | | | |
| S6 | feat/web-ui | todo | | | |
| S7 | feat/deploy | todo | | | |
| S8 | feat/acceptance | todo | | | |
| S9 | 移交(tag v1.0.0-rc1) | todo | | | |

## 汇报摘要(每步一条,最新在上)

### S1 feat/p0-foundation — awaiting_confirmation(2026-09-15)

- 交付:config(含 is_loopback_bind 门禁)、db(SPEC §4 DDL+种子档案)、models、audit、services 四模块(DESIGN §4.4 全接口)、web 骨架(create_app/lifespan+worker/scheduler stub/auth+login/base.html+htmx 本地文件)、conftest(含 FakeLauncher 骨架)、scripts/dev.sh。
- 测试:pytest tests/unit 234 通过;ruff check/format 零告警;services 覆盖 98%;dev.sh 实跑 /healthz、/login 均 200。
- 待确认事项:本地模式(AM_TOKEN 未配置且回环绑定时放行)等 4 项假设,见汇报「假设与自行决定」。
