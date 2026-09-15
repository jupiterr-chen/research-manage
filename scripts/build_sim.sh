#!/usr/bin/env bash
# 构建本地仿真镜像 tradingagents-sim:latest(DESIGN §7 / R-RUN-08)
set -euo pipefail
cd "$(dirname "$0")/.."
docker build -t tradingagents-sim:latest sim/
echo "[build_sim] done: tradingagents-sim:latest"
