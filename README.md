# research-manage(Agents-Manage)

TradingAgents 研究任务管理台:在内网 NAS 上可视化配置「市场-标的」的研究调度,驱动已部署的
TradingAgents 多智能体框架执行,跟踪进度与状态,展示「报告是否已生成」。纯任务管理台,不渲染报告内容,不涉及交易。

## 文档

| 文档 | 用途 |
|---|---|
| [SPEC.md](SPEC.md) | 实现契约(MUST/SHOULD、DDL、API、判定规则)——唯一权威 |
| [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) | 需求条目 R-*,按模块划分,映射验收项 |
| [docs/DESIGN.md](docs/DESIGN.md) | 设计基线:布局、线程模型、配置、分支间接口、runner/sim 设计 |
| [docs/ROADMAP.md](docs/ROADMAP.md) | 阶段、分支、交付与退出标准 |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | 验收手册 AM-01~18 与 NAS 验收流程 |
| [docs/NAS-ACCESS.md](docs/NAS-ACCESS.md) | 对 NAS 的操作边界(红线) |
| [docs/DEPLOY.md](docs/DEPLOY.md) | 部署手册 |
| [docs/AGENT-PLAYBOOK.md](docs/AGENT-PLAYBOOK.md) | 执行 agent 的自驱协议:判断当前步骤 → 实现 → 汇报 → 等确认 |
| [docs/STATUS.md](docs/STATUS.md) | 进度台账(agent 维护) |
| [CLAUDE.md](CLAUDE.md) | 给 AI agent 的项目须知 |

## 技术栈

Python 3.12 · FastAPI · Jinja2 · htmx(单文件)· SQLite(纯 sqlite3)· APScheduler · docker-py。无构建步骤。

## 快速开始(本地开发)

见 [docs/DESIGN.md §8](docs/DESIGN.md)。本地全程使用 `sim/` 仿真镜像,不依赖 NAS。

## 分支

`main`(发布)← `develop`(集成)← `feat/*`。详见 [docs/ROADMAP.md §1](docs/ROADMAP.md)。
