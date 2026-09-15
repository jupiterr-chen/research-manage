# 部署手册(DEPLOY)

> 适用:`research-manage`(agents-manage 管理台)在 NAS(飞牛 fnOS,`chen@192.168.1.150`)的部署与运维。
> 红线:本系统的任何操作不得触碰 `/home/chen/docker/TradingAgents*`(见 [NAS-ACCESS.md](NAS-ACCESS.md))。
> 以下 uid/gid 数值为 2026-09-15 NAS 实测;每次部署前请按 §1 重新核对。

## 1. 前置检查(只读,SSH 执行)

```bash
# 1) TradingAgents data 的 owner(决定管理台运行的 uid;执行容器写报告需要同 uid)
ssh chen@192.168.1.150 "stat -c %u:%g /home/chen/docker/TradingAgents/data"
# 实测:1000:1000  → APP_UID=1000

# 2) docker.sock 的组(决定管理台进程的 gid,否则无权启停兄弟容器)
ssh chen@192.168.1.150 "stat -c %g /var/run/docker.sock"
# 实测:994  → DOCKER_GID=994

# 3) 执行镜像存在与形态
ssh chen@192.168.1.150 "docker image inspect tradingagents-tradingagents:latest --format '{{.Id}} {{.Config.User}} {{.Config.WorkingDir}}'"
# 实测:sha256:c85a52e… appuser /home/appuser/app

# 4) SSH 免密与磁盘余量(自定)
ssh chen@192.168.1.150 "df -h /home/chen/docker"
```

把 §1 查到的 uid/gid 写进 `deploy/.env` 的 `APP_UID` / `DOCKER_GID`。

## 2. 首次部署

```bash
# 0) 工作机:在仓库根目录
# 1) 生成 token(≥32 字符),填入 deploy/.env(模板 deploy/.env.example)
python scripts/gen_token.py
cp deploy/.env.example deploy/.env   # 然后编辑:AM_TOKEN=…、APP_UID、DOCKER_GID

# 2) 同步仓库到 NAS(排除 data/vendor/.local/.git/.venv/deploy/.env)
bash scripts/sync_to_nas.sh

# 3) NAS 上构建并启动
#    deploy/.env 被排除同步(含密钥,不应落同步通道之外),首次在 NAS 上单独创建:
ssh chen@192.168.1.150
cd /home/chen/docker/agents-manage
vim deploy/.env        # 与工作机同内容:AM_TOKEN / APP_UID / DOCKER_GID
docker compose -f deploy/docker-compose.yml build   # build-arg(APP_UID/DOCKER_GID)经 compose 从 .env 读取
docker compose -f deploy/docker-compose.yml up -d

# 4) 健康检查
curl -H "Authorization: Bearer <AM_TOKEN>" http://192.168.1.150:8090/healthz
#    期望 {"status":"ok","worker_alive":true,"docker_ok":true,...}
```

说明:
- 容器内 `AM_BIND=0.0.0.0`(LAN 暴露由 `ports: "8090:8090"` 端口映射实现;非回环绑定 + token 满足 SPEC §8 绑定门禁)。若要更严,可去掉 ports 改走 SSH 隧道。
- `deploy/.env` 不同步、不提交(gitignore);NAS 上单独保管,泄漏即重新生成 token。

## 3. 升级

```bash
# 工作机
bash scripts/sync_to_nas.sh
ssh chen@192.168.1.150 "cd /home/chen/docker/agents-manage && \
  docker compose -f deploy/docker-compose.yml build && \
  docker compose -f deploy/docker-compose.yml up -d"
```

- 执行中的分析容器是**兄弟容器**,不受管理台重建影响;管理台重启后由 Worker 的 recover 接管(`running` 且容器仍在 → 继续监控;容器已消失 → `failed(host_restarted)`)。
- `queued` 任务保留,重启后继续消费。

## 4. 回滚

```bash
# 工作机:回到上一个可用版本(tag 或 commit),再走同步+构建
git checkout v1.0.0            # 目标版本
bash scripts/sync_to_nas.sh
ssh chen@192.168.1.150 "cd /home/chen/docker/agents-manage && \
  docker compose -f deploy/docker-compose.yml build && \
  docker compose -f deploy/docker-compose.yml up -d"
```

SQLite schema 变更均为幂等迁移(`app/db.py::migrate`),回滚到更早版本时新增列被忽略、不破坏数据;如需完全回退数据,用 §5 备份恢复 `data/agents-manage.db`。

## 5. 备份(SQLite)

```bash
# WAL 模式下必须用 sqlite3 .backup(直接 cp 正在写的文件会得到不一致副本)
ssh chen@192.168.1.150 "docker exec \$(docker ps -qf name=agents-manage) \
  python -c \"import sqlite3; c=sqlite3.connect('/app-data/agents-manage.db'); d=sqlite3.connect('/app-data/backup.db'); c.backup(d); d.close()\""
ssh chen@192.168.1.150 "cp /home/chen/docker/agents-manage/data/backup.db \
  /home/chen/docker/agents-manage/data/backup-\$(date +%F).db"
# 恢复:docker compose down → 用备份覆盖 data/agents-manage.db → up -d
```

建议同时周期性快照整个 `data/`(含 `runs/<id>/` 的 status.json 与 container.log)。

## 6. 可选:docker-socket-proxy(SPEC §2 SHOULD 项)

直挂 `/var/run/docker.sock` 使管理台进程获得等效 root 的 docker 权限(内网 + token 缓解,已获认可)。要收紧时改经 proxy 只放行所需端点:

```yaml
# deploy/docker-compose.yml 追加:
  socket-proxy:
    image: tecnativa/docker-socket-proxy
    restart: unless-stopped
    environment:
      - CONTAINERS=1   # 启停 / inspect / logs 执行容器
      - IMAGES=1       # 启动自检 image_exists
      - POST=1         # containers.run / stop / remove(写操作)
      - NETWORKS=1     # 执行容器加入 AM_TA_NETWORK
      - ALLOW_RESTARTS=0
      - VOLUMES=0
      - EXEC=0
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
# 并修改 agents-manage 服务:
#   volumes: 去掉 /var/run/docker.sock 直挂
#   environment: 追加 DOCKER_HOST=tcp://socket-proxy:2375
```

## 7. 故障排查

`GET /healthz`(带 Bearer)字段:

| 字段 | 含义 | 异常时 |
|---|---|---|
| `worker_alive` | 工作线程在跑 | false → 看容器日志 `[worker]` 行;启动自检失败会直接拒绝启动 |
| `docker_ok` | docker 可达(每 tick 刷新) | false → sock 挂载 / DOCKER_GID / 权限 |
| `queue_depth` | 排队任务数 | 持续增长 → 执行容器起不来,看对应 run 的 error |
| `scheduler_jobs` | 已注册调度作业数 | 少于启用调度数 → 页面改一次任意调度触发 rebuild_jobs |
| `current_run_id` | 当前执行 run | 长期不变 → 结合 `status_stale` 判断;看门狗会兜底取消 |

常见错误:

1. **启动即退出:「镜像不存在」**:`AM_TA_IMAGE` 指向的镜像不在本机(NAS 生产为 `tradingagents-tradingagents:latest`,本地为 `tradingagents-sim:latest`)。
2. **启动即退出:「AM_TA_DATA_DIR 不可读」**:compose 的 `/ta-data:ro` 源路径写错,或 uid 无权穿透目录(`APP_UID` 与 §1 查询不一致)。
3. **绑定门禁退出(退出码 2)**:`AM_BIND` 非回环而 `AM_TOKEN` 未设/短于 32 字符;确认 `deploy/.env` 位于 compose 文件同目录且被读取。
4. **run 详情 `launch_failed:`**:多为挂载源(`AM_*_HOST`)在宿主上不存在;对照 SPEC §6.3 逐项检查。
5. **退出码 0 但报告 ✗**:双重判定缺产物(investment_plan.md / memory 条目);run 详情 error 写明缺哪个,细节看 `container.log` 路径所指文件(经 SMB)。
6. **`status_stale=1`**:status.json 超 `AM_STALE_MINUTES` 未更新或损坏;任务仍在跑等看门狗,进程已消失由 recover 判 `host_restarted`。
7. **升级后 502 / 连不上**:`docker compose ps` + `docker compose logs agents-manage`;healthcheck 连续 3 次失败会标 unhealthy。

## 附:本地全链路验证(compose.local)

`deploy/docker-compose.local.yml`(管理台 + llm-stub,网络 `research-manage_default`)。步骤见文件头注释;验收要点:本地用 sim 镜像手动发起一次 → `succeeded`、报告 ✓,管理台镜像内 `which node` 为空(无构建链)。
