# 部署手册(DEPLOY)

> 由 `feat/deploy` 分支交付完整内容。必须覆盖以下章节:

1. 前置检查:`stat -c %u:%g /home/chen/docker/TradingAgents/data`、`stat -c %g /var/run/docker.sock`、`docker image inspect tradingagents-tradingagents:latest`
2. 首次部署:`scripts/sync_to_nas.sh` → `.env`(`scripts/gen_token.py` 生成 `AM_TOKEN`)→ `docker compose build --build-arg APP_UID=… --build-arg DOCKER_GID=…` → `up -d` → `/healthz`
3. 升级:同步 → build → `up -d`(执行中的容器不受影响,重启后由 recover 接管)
4. 回滚:checkout 上一个 tag → 同步 → build → up
5. 备份:`data/agents-manage.db`(WAL 模式下用 `sqlite3 .backup`)
6. 可选:docker-socket-proxy 方案
7. 故障排查:healthz 各字段含义、常见错误(镜像不存在、挂载权限、绑定门禁)
