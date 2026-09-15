#!/usr/bin/env bash
# 本地开发启动:source .env.local 后 exec python -m app.main。
# 应用本身只读 OS 环境变量(CLAUDE.md 常用命令)。
set -euo pipefail
cd "$(dirname "$0")/.."

# Git Bash 下取 Windows 形式路径(D:/...),供 SQLite 与后续 docker 挂载使用
ROOT="$(pwd -W 2>/dev/null || pwd)"

if [ ! -f .env.local ]; then
  mkdir -p .local/ta-data .local/am-data
  cat > .env.local <<EOF
# 本地开发默认值(自动生成;可按需修改,不入库)
AM_BIND=127.0.0.1
AM_PORT=8090
AM_TA_IMAGE=tradingagents-sim:latest
AM_TA_DATA_DIR=${ROOT}/.local/ta-data
AM_TA_DATA_HOST=${ROOT}/.local/ta-data
AM_DATA_DIR=${ROOT}/.local/am-data
AM_DATA_HOST=${ROOT}/.local/am-data
AM_TA_ENV_HOST=${ROOT}/.local/ta.env
AM_RUNNER_HOST=${ROOT}/runner/runner.py
EOF
  echo "[dev] 已生成 .env.local(本地默认值)"
fi
[ -f .local/ta.env ] || touch .local/ta.env

set -a
# shellcheck disable=SC1091
source .env.local
set +a

if [ -x .venv/Scripts/python ]; then
  PY=.venv/Scripts/python
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python
fi
echo "[dev] 启动 http://${AM_BIND:-127.0.0.1}:${AM_PORT:-8090}(healthz/login)"
exec "${PY}" -m app.main
