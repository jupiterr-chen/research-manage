# 对 NAS(192.168.1.150)的操作边界

> TradingAgents 只有一套部署,在 NAS 上,是**共享的生产依赖**。开发在本地完成;NAS 只用于只读核对与最终验收。违反本文任何「禁止」项都视为事故。

## 允许(无需确认)

| 操作 | 示例 |
|---|---|
| SSH 只读查看 TradingAgents 源码与数据 | `ssh chen@192.168.1.150 'cat /home/chen/docker/TradingAgents/tradingagents/graph/trading_graph.py'`、`ls data/logs`、`tail data/memory/trading_memory.md` |
| 从 NAS 拷贝文件到本地 | `scp -r chen@192.168.1.150:/home/chen/docker/TradingAgents/tradingagents vendor/`(`vendor/` 已 gitignore) |
| 查看 docker 状态 | `docker ps -a`、`docker image inspect tradingagents-tradingagents:latest`、`docker logs <本系统启动的容器>` |
| 只读探查镜像内容 | `docker run --rm --network none --entrypoint python tradingagents-tradingagents:latest -c '…'`:**不挂任何卷、不传任何 env**,只打印包文件哈希/版本(`scripts/verify_vendor.sh`) |
| 读写本系统自己的目录 | `/home/chen/docker/agents-manage/` 下任何操作(P3 阶段) |

## 禁止(任何情况下)

- 修改 `/home/chen/docker/TradingAgents/` 与 `/home/chen/docker/TradingAgents-custom/` 下的**任何文件**(含 `.env`、`data/`、源码、compose、补丁)
- `docker compose up/down/restart/build`、`docker stop/rm/restart` 作用于 TradingAgents 的容器或镜像;`docker rmi`;`docker system prune`
- 在 NAS 上安装/升级任何软件(`apt`、`pip`、`docker pull`)
- 使用 `sudo`
- 读取 `.env` 的内容并出现在对话、日志、文档、代码中(验收方需要密钥扫描时,由用户在 NAS 上执行 grep 并只反馈"有/无")

## 需用户确认后才能做

- 从 TradingAgents 镜像启动执行容器跑真实分析(会向 `data/` 写入报告、记忆、断点;会消耗 LLM 额度)—— 确认内容:标的、日期、分析师集合
- 首次创建 `/home/chen/docker/agents-manage/` 并部署管理台
- 启用生产调度(会自动、周期性地跑真实分析)
- 任何超出上表的操作

## 发现问题时

若核对过程中发现 TradingAgents 侧异常(容器挂死、数据目录权限、镜像缺失等):**不要处理**,把现象、命令与输出整理后交给用户决定。
