"""docker-py 封装(DESIGN §4.5 / SPEC §6.3 / R-EXE-04)。

docker-py 只允许在 Worker 线程内经由本模块调用(ADR-1)。
挂载源全部来自 AM_*_HOST(宿主路径,约束 10);environment 只允许
AM_RUN_ID、TZ(本地开发另加 AM_SIM_ENV 解析出的 extra_env)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import docker

log = logging.getLogger("am.launcher")

RUNNER_CONTAINER_PATH = "/runner.py"
WORKSPACE_CONTAINER_PATH = "/ws"


@dataclass(frozen=True)
class ContainerSpec:
    image: str
    run_id: str
    ticker: str
    date: str
    analysts: tuple[str, ...]
    ta_data_host: str
    ta_env_host: str
    runner_host: str
    workspace_host: str
    ta_container_data: str
    workdir: str
    stop_timeout: int
    extra_env: dict[str, str] = field(default_factory=dict)


def parse_sim_env(raw: str) -> dict[str, str]:
    """解析 AM_SIM_ENV(仅本地开发):`SIM_MODE=fail,SIM_STEP_SECONDS=0.1` → dict。"""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


class DockerLauncher:
    def __init__(self, client: docker.DockerClient | None = None, network: str | None = None):
        self._client = client  # None → 延迟 from_env(),避免构造期连接
        self._own_client = client is None
        self._network = network or None  # AM_TA_NETWORK;空 = 默认 bridge

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:  # noqa: BLE001 - 探测不抛
            return False

    def image_exists(self, image: str) -> bool:
        try:
            self.client.images.get(image)
            return True
        except Exception:  # noqa: BLE001
            return False

    def start(self, spec: ContainerSpec) -> str:
        """按 SPEC §6.3 启动执行容器,返回 container id。"""
        container = self.client.containers.run(
            image=spec.image,
            name=f"am-{spec.run_id}",
            entrypoint=["python", RUNNER_CONTAINER_PATH],
            command=[
                "--ticker",
                spec.ticker,
                "--date",
                spec.date,
                "--analysts",
                ",".join(spec.analysts),
                "--workspace",
                WORKSPACE_CONTAINER_PATH,
                "--run-id",
                spec.run_id,
            ],
            working_dir=spec.workdir,
            volumes={
                spec.ta_data_host: {"bind": spec.ta_container_data, "mode": "rw"},
                spec.ta_env_host: {"bind": f"{spec.workdir}/.env", "mode": "ro"},
                spec.runner_host: {"bind": RUNNER_CONTAINER_PATH, "mode": "ro"},
                spec.workspace_host: {"bind": WORKSPACE_CONTAINER_PATH, "mode": "rw"},
            },
            environment={"AM_RUN_ID": spec.run_id, "TZ": "Asia/Shanghai", **spec.extra_env},
            stop_signal="SIGINT",
            detach=True,
            labels={"am.run_id": spec.run_id},
            network=self._network,  # AM_TA_NETWORK(空 = 默认 bridge)
        )
        return container.id

    def wait(self, container_id: str, timeout: int) -> int | None:
        """等待最多 timeout 秒;仍在运行返回 None,退出返回退出码。"""
        try:
            result = self.client.containers.get(container_id).wait(timeout=timeout)
            return int(result.get("StatusCode", -1))
        except Exception:  # noqa: BLE001 - ReadTimeout 等价于仍在运行
            return None

    def exists(self, container_id: str) -> bool:
        try:
            self.client.containers.get(container_id)
            return True
        except Exception:  # noqa: BLE001
            return False

    def stop(self, container_id: str, timeout: int) -> None:
        """SIGINT(镜像 stop_signal)+ timeout 秒宽限(SPEC §6.3)。"""
        try:
            self.client.containers.get(container_id).stop(timeout=timeout)
        except Exception as e:  # noqa: BLE001
            log.warning("stop %s 失败: %s", container_id, e)

    def logs_tail(self, container_id: str, n: int = 200) -> str:
        try:
            raw = self.client.containers.get(container_id).logs(tail=n)
            return raw.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            return f"[launcher] 读取容器日志失败: {e}\n"

    def remove(self, container_id: str) -> None:
        try:
            self.client.containers.get(container_id).remove(force=True)
        except Exception as e:  # noqa: BLE001
            log.warning("remove %s 失败: %s", container_id, e)
