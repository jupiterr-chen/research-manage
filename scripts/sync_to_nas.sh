#!/usr/bin/env bash
# 同步本仓库到 NAS /home/chen/docker/agents-manage/(R-DEP-04)。
# 只同步本仓库;绝不触碰 /home/chen/docker/TradingAgents*。
# 排除:data/ vendor/ .local/ .git/ .venv/ deploy/.env 等本地产物与敏感文件。
# 仅在部署阶段(P3)由用户执行;开发阶段不得运行。
set -euo pipefail
cd "$(dirname "$0")/.."

NAS=chen@192.168.1.150
DEST=/home/chen/docker/agents-manage

EXCLUDES=(
  --exclude=data
  --exclude=vendor
  --exclude=.local
  --exclude=.git
  --exclude=.venv
  --exclude=.pytest_cache
  --exclude=.ruff_cache
  --exclude=__pycache__
  --exclude="*.pyc"
  --exclude=.env
  --exclude=.env.local
  --exclude="*.db"
  --exclude="*.db-wal"
  --exclude="*.db-shm"
)

echo "[sync_to_nas] 目标:$NAS:$DEST(排除 data vendor .local .git .venv 等)"
if command -v rsync >/dev/null 2>&1; then
  rsync -az --delete "${EXCLUDES[@]}" ./ "$NAS:$DEST/"
else
  # 无 rsync(Git Bash)时用 tar 管道;不带 --delete,旧文件靠构建覆盖
  tar czf - "${EXCLUDES[@]}" . | ssh "$NAS" "mkdir -p '$DEST' && tar xzf - -C '$DEST'"
fi
echo "[sync_to_nas] 完成。下一步见 docs/DEPLOY.md §2(build + up)。"
