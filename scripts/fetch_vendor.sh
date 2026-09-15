#!/usr/bin/env bash
# 只读拷贝 NAS 上 TradingAgents 源码到 vendor/(gitignore;R-RUN-09)。
# 遵循 NAS-ACCESS.md:仅 scp 读取,不做任何写操作。
set -euo pipefail
cd "$(dirname "$0")/.."

NAS=chen@192.168.1.150
SRC=/home/chen/docker/TradingAgents

mkdir -p vendor
for item in tradingagents cli Dockerfile pyproject.toml requirements.txt; do
  echo "[fetch_vendor] scp $NAS:$SRC/$item -> vendor/"
  scp -r -q "$NAS:$SRC/$item" "vendor/"
done
echo "[fetch_vendor] done. 对等校验:scripts/verify_vendor.sh"
