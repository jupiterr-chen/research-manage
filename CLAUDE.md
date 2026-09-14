# research-manage — 给 AI agent 的项目须知

TradingAgents 研究任务管理台。FastAPI + Jinja2 + htmx + SQLite + APScheduler + docker-py;
每个研究任务 = 一个从 `tradingagents-tradingagents:latest` 镜像启动的兄弟容器,容器内跑本仓库的 `runner/runner.py`。

**先读**:[SPEC.md](SPEC.md)(契约)→ [docs/DESIGN.md](docs/DESIGN.md)(接口)→ [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)(你要做的 R-ID)→ [docs/ROADMAP.md](docs/ROADMAP.md)(你的分支)→ [docs/NAS-ACCESS.md](docs/NAS-ACCESS.md)(红线)。

---

## 🔴 红线

1. **NAS 上的 TradingAgents 是共享生产依赖**:只读查看、不修改、不重启、不重建;真实运行需用户确认。全文见 [docs/NAS-ACCESS.md](docs/NAS-ACCESS.md)。开发与测试在本地:错误注入用 `sim/` 仿真镜像,行为保真用 `tradingagents-local`(真实源码只读拷贝 + llm-stub),见 DESIGN §7.1。
2. **管理台不接触 `.env`**:不读、不解析、不注入、不日志。`.env` 只作为 docker 挂载源路径字符串出现在 `app/executor/launcher.py`。[SPEC 约束 7]
3. **docker-py 只在 Worker 线程调用**。Web/API 取消只置 `cancel_requested_at`。[DESIGN ADR-1]
4. **SQLite 唯一事实源**:不加缓存副本、不用持久化 JobStore、不引入 ORM。[SPEC 约束 1]
5. **无构建步骤、无外链**:htmx 单文件随仓库,手写 CSS;模板/静态里不得出现 `http(s)://` 资源引用。[SPEC 约束 2]
6. **绑定门禁**用 `is_loopback_bind()`,禁止 `bind == "0.0.0.0"` 字面量比较。[SPEC 约束 6]
7. **常量与校验唯一来源** `app/models.py`(MARKETS/ANALYSTS/agents_total/validate_code/parse_analysts);禁止第二份。
8. **传给 docker 的挂载源用 `AM_*_HOST`**,不得用 `AM_*_DIR`。[SPEC 约束 10]
9. **run.analysts_csv 是不可变快照**;resume 必须复制它。
10. **succeeded 判定必须双重**(investment_plan.md + memory 行前缀),退出码 0 不等于成功。[SPEC §6.4]
11. **接口改动先改 DESIGN.md §4**,PR 标题带 `[interface]`。
12. 不提交 `.env*`、`data/`、`.local/`、`vendor/`、`*.db`。

## 工作方式

- 你在**一个分支**上工作(见 ROADMAP §3),只实现该分支列出的交付;别的分支的模块若尚未合入,用 DESIGN §4 的接口写 stub/fake,不要越界实现。
- 每个 PR:标题 `feat(模块): R-ID 摘要`;描述列出 R-ID、AM-ID、测试方式;`pytest` 与 `ruff check .` 必须绿。
- 测试:单测放 `tests/unit/`,需要本地 Docker 的放 `tests/integration/`(标 `@pytest.mark.docker`),验收放 `tests/acceptance/`(标 `@pytest.mark.acceptance`)。
- 本地环境搭建见 DESIGN §8;仿真镜像见 DESIGN §7。
- 遇到 SPEC/DESIGN 没覆盖的决策:在 PR 描述里写明假设,不要静默选择;涉及 NAS 或用户数据的一律停下来问。
- 报告工作结果时如实:测试没跑就说没跑,失败就贴输出。

## 网络

本机与 NAS 访问外网(yfinance 等)都必须经代理 `http://192.168.1.150:7890`。本地 L2 测试的执行容器 env 含 `HTTP(S)_PROXY` 与 `NO_PROXY=llm-stub,...`;管理台自身不需要外网;NAS 生产的代理由 TradingAgents 自己的 `.env` 提供。

## 常用命令

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt -r requirements-dev.txt
scripts/build_sim.sh                       # 构建 tradingagents-sim:latest
scripts/dev.sh                             # source .env.local 后 python -m app.main(应用本身只读 OS 环境变量)
pytest tests/unit                          # 快
pytest -m docker tests/integration         # 需本地 Docker
pytest -m acceptance                       # 验收
ruff check . && ruff format --check .
```
