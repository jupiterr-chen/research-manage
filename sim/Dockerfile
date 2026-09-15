# sim 仿真镜像:python:3.12-slim + 假 tradingagents 包(DESIGN §7)
FROM python:3.12-slim

RUN useradd --create-home --uid 1000 appuser

COPY tradingagents /usr/local/lib/python3.12/site-packages/tradingagents
COPY cli /usr/local/lib/python3.12/site-packages/cli

# 占位 ENTRYPOINT:与真实镜像同形(真实为 tradingagents CLI);实际执行走 runner.py
RUN printf '#!/bin/sh\necho "tradingagents-sim placeholder entrypoint; use runner.py"\nexit 0\n' \
      > /usr/local/bin/tradingagents && chmod +x /usr/local/bin/tradingagents

USER appuser
WORKDIR /home/appuser/app
ENTRYPOINT ["tradingagents"]
