# 管理台镜像(R-DEP-01):python:3.12-slim、非 root、无 node/npm
# build-arg:
#   APP_UID    宿主 chen 的 uid(NAS 实测 1000,与 TradingAgents data 同 owner)
#   DOCKER_GID 宿主 docker 组 gid(NAS 实测 994,与 /var/run/docker.sock 同组)
FROM python:3.12-slim

ARG APP_UID=1000
ARG DOCKER_GID=994

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /srv/agents-manage

COPY requirements.txt ./
# --trusted-host:本环境出口代理对 pypi 做证书拦截(MITM),镜像内无其 CA;
# 仅对 pypi 官方两域跳过校验。若你的网络无此问题可去掉这两个参数。
RUN pip install --no-cache-dir \
      --trusted-host pypi.org --trusted-host files.pythonhosted.org \
      -r requirements.txt

COPY app ./app
COPY runner ./runner
COPY scripts ./scripts

RUN groupadd --gid "${DOCKER_GID}" dockerg \
 && useradd --uid "${APP_UID}" --gid "${DOCKER_GID}" --create-home appuser \
 && mkdir -p /srv/agents-manage/data \
 && chown -R appuser:dockerg /srv/agents-manage

USER appuser
EXPOSE 8090
CMD ["python", "-m", "app.main"]
