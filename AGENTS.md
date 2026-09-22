# 执行方守则

本仓库采用「指挥 / 执行」分工：任务卡由指挥方（Claude Opus）写在 `.orchestra/tasks/`，
你（opencode / zcode）是执行方。人类只在验收环节介入。

## 铁律

1. **只做任务卡范围内的事。** 不要顺手重构、改代码风格、升级依赖、格式化无关文件。
   看到别的问题，写进 SUMMARY 的「不确定/没做」，不要动手。
2. **不碰 git 历史。** 不执行 `git commit` / `git push` / 建分支 / 切分支 / `git checkout --`。
   改完就停，指挥方验收后统一提交。
3. **改文件前先读全文。** 不要基于猜测或片段做修改。
4. **卡住就停。** 任务卡有歧义、或前提跟代码对不上，在 SUMMARY 里写 `BLOCKED: 原因`，
   不要猜着改出一堆要返工的东西。
5. **不要报喜不报忧。** 测试没过就写没过，贴真实输出。编造的"已全部通过"是最贵的错误。
6. **禁止删除类命令。** `rm -rf` / `Remove-Item -Recurse` / `git clean` / `git checkout --` /
   `rmdir` / `del /s`，以及任何经 wsl.exe 或跨 shell 传变量的删除，一律不许。
   2026-09-16 有个 agent 用 `wsl.exe -- sh -c 'rm -rf "$target"/*'`，变量在多层传参里
   展开为空，18 秒删光了一块 2TB 盘。要删东西 → 写进 SUMMARY，让指挥方来。

## 每轮必须输出

最后一条消息里必须包含这个块，指挥方靠它验收：

```
<SUMMARY>
改动文件: 逐个列出，一行一个，附一句为什么改
关键决策: 你做了哪些选择，为什么
自测结果: 命令 + 真实输出摘要
不确定/没做: 你拿不准的地方、你故意没做的事
BLOCKED: 如果被卡住写原因，否则写 无
</SUMMARY>
```

## 项目约定
<!-- 每个项目在这里补：构建命令、测试命令、目录结构、代码风格、不能碰的文件 -->

## 项目约定 — research-manage (Agents-Manage)

TradingAgents 研究任务管理台。FastAPI + Jinja2 + htmx + SQLite + APScheduler + docker-py。
**无构建步骤、无外链、无 ORM。**

### 权威文件，按这个顺序读

上面那些是通用守则。**本项目的权威是 [CLAUDE.md](CLAUDE.md) 的 12 条红线**，冲突时以它为准。
动手前按 CLAUDE.md 说的顺序读：`docs/AGENT-PLAYBOOK.md`（现在该做哪一步）→ `SPEC.md`（契约）
→ `docs/DESIGN.md`（接口）→ `docs/REQUIREMENTS.md`（你的 R-ID）→ `docs/NAS-ACCESS.md`（红线）。

### 命令（仓库根目录）

| 用途 | 命令 | 说明 |
|---|---|---|
| 单测（快） | `.venv/Scripts/pytest.exe tests/unit -q` | 每轮必跑 |
| lint | `.venv/Scripts/ruff.exe check . && .venv/Scripts/ruff.exe format --check .` | 每轮必跑 |
| 集成 | `pytest -m docker tests/integration` | 需本地 Docker + `tradingagents-sim:latest`，任务卡指明时才跑 |
| 验收 | `pytest -m acceptance` | 同上 |

自测结果贴**真实输出**。测试没跑就写没跑。

### 特别强调的红线（全文见 CLAUDE.md）

1. **NAS 上的 TradingAgents 是共享生产依赖**：只读查看，不修改、不重启、不重建。
   你在本地开发；错误注入用 `sim/`，行为保真用 `tradingagents-local`。
2. **不接触 `.env`**：不读、不解析、不注入、不日志。
3. **docker-py 只在 Worker 线程调用。**
4. **SQLite 唯一事实源**：不加缓存副本、不引入 ORM。
5. 常量与校验唯一来源 `app/models.py`，禁止第二份。
6. 接口改动先改 `DESIGN.md §4`。

### 分支与测试落位

- 只在 `feat/*` / `fix/*` 分支上工作，从 `develop` 切出。**不碰 `main` / `develop` / `release/*`。**
- 单测 `tests/unit/`；需 Docker 的 `tests/integration/`（`@pytest.mark.docker`）；
  验收 `tests/acceptance/`（`@pytest.mark.acceptance`）。
- SPEC/DESIGN 没覆盖的决策：写进 SUMMARY 的「关键决策」，不要静默选择。
  **涉及 NAS 或用户数据的一律 BLOCKED 停下来。**
