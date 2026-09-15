#!/usr/bin/env bash
# 用 vendor/(NAS 只读拷贝)+ 上游 Dockerfile 本地构建 tradingagents-local:latest。
# 不修改 vendor/ 任何源码(DESIGN §7.1 L2 层)。
# 依赖外网(pip 安装 langchain 栈)时经代理:AM_PROXY 默认 http://192.168.1.150:7890
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f vendor/pyproject.toml ]; then
  echo "[build_local] 缺少 vendor/,先运行 scripts/fetch_vendor.sh" >&2
  exit 1
fi

PROXY="${AM_PROXY:-http://192.168.1.150:7890}"
NO_PROXY="${NO_PROXY:-localhost,127.0.0.1,192.168.1.150}"

docker build \
  --build-arg "HTTP_PROXY=${PROXY}" \
  --build-arg "HTTPS_PROXY=${PROXY}" \
  --build-arg "NO_PROXY=${NO_PROXY}" \
  -t tradingagents-local:latest vendor/

echo "[build_local] done: tradingagents-local:latest(基于 vendor/,未改源码)"
