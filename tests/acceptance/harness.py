"""验收测试公用骨架:真实 Worker + DockerLauncher + sim 镜像 + TestClient。"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db as app_db
from app.db import connect
from app.executor.launcher import DockerLauncher
from app.executor.worker import Worker
from app.scheduler import Scheduler
from app.services import instruments as inst_srv
from app.services import runs as runs_srv
from app.web.server import create_app

REPO = Path(__file__).resolve().parents[2]
SIM_IMAGE = "tradingagents-sim:latest"


def docker_ok() -> bool:
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def sim_image_ok() -> bool:
    r = subprocess.run(["docker", "image", "inspect", SIM_IMAGE], capture_output=True, text=True, timeout=20)
    return r.returncode == 0


requires_sim = pytest.mark.skipif(
    not (docker_ok() and sim_image_ok()),
    reason="需要本地 Docker + tradingagents-sim:latest(scripts/build_sim.sh)",
)


class AcceptanceEnv:
    """一套隔离环境:settings + db + 真实 Worker(容器由 sim 镜像执行)。"""

    def __init__(self, tmp_path, make_settings, **setting_overrides):
        base = dict(
            data_dir=str(tmp_path / "am"),
            data_host=str(tmp_path / "am"),
            ta_data_dir=str(tmp_path / "ta"),
            ta_data_host=str(tmp_path / "ta"),
            ta_env_host=str(tmp_path / "ta" / ".env"),
            runner_host=str(REPO / "runner" / "runner.py"),
            ta_image=SIM_IMAGE,
            status_poll_seconds=1,
            stale_minutes=1,
        )
        base.update(setting_overrides)
        s = make_settings(**base)
        Path(s.data_dir).mkdir(parents=True, exist_ok=True)
        s.db_path = str(Path(s.data_dir) / "test.db")
        Path(s.ta_data_dir).mkdir(parents=True, exist_ok=True)
        Path(s.ta_env_host).write_text(
            "OPENAI_COMPATIBLE_API_KEY=FAKEKEY_abc123\n"
            "TRADINGAGENTS_LLM_PROVIDER=openai_compatible\n"
            "TRADINGAGENTS_CHECKPOINT_ENABLED=true\n",
            encoding="utf-8",
        )
        self.settings = s
        self.tmp = tmp_path
        self.db_path = s.db_path
        conn = connect(self.db_path)
        app_db.migrate(conn)
        inst_srv.create(conn, market="hk", code="1810.HK", name="小米集团", actor="web")
        inst_srv.create(conn, market="us", code="NVDA", name="NVIDIA", actor="web")
        inst_srv.create(conn, market="cn", code="600519.SS", name="贵州茅台", actor="web")
        conn.close()

    def set_sim(self, mode: str | None, step: str | None = None) -> None:
        parts = ["TRADINGAGENTS_CHECKPOINT_ENABLED=true"]
        if mode:
            parts.append(f"SIM_MODE={mode}")
        if step:
            # slow 模式读 SIM_SLOW_SECONDS,其余读 SIM_STEP_SECONDS(见 sim 包)
            key = "SIM_SLOW_SECONDS" if mode == "slow" else "SIM_STEP_SECONDS"
            parts.append(f"{key}={step}")
        self.settings.sim_env = ",".join(parts)

    def worker(self) -> Worker:
        return Worker(self.settings, launcher=DockerLauncher(), db_factory=lambda: connect(self.db_path))

    def client(self, worker: Worker) -> TestClient:
        app = create_app(
            self.settings,
            worker=worker,
            scheduler=Scheduler(self.settings, db_factory=lambda: connect(self.db_path)),
        )
        c = TestClient(app)
        c.headers.update({"Authorization": f"Bearer {self.settings.token}"})
        return c

    def conn(self):
        return connect(self.db_path)


def _fetch(env, run_id):
    conn = env.conn()
    try:
        return runs_srv.get(conn, run_id)
    finally:
        conn.close()


def wait_status(env, run_id, terminal=True, timeout=180):
    """轮询 DB 至目标;返回最终 run 行。terminal=False 时等到 status 即返回。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = _fetch(env, run_id)
        if not terminal or row["status"] in ("succeeded", "failed", "cancelled"):
            return row
        time.sleep(0.5)
    raise TimeoutError(f"run {run_id} 未到终态")


def wait_agents_done(env, run_id, minimum: int, timeout=180) -> int:
    deadline = time.time() + timeout
    seen = -1
    while time.time() < deadline:
        row = _fetch(env, run_id)
        seen = max(seen, row["agents_done"])
        if seen >= minimum:
            return seen
        if row["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.4)
    return seen


def exec_containers(run_id: str | None = None) -> list[str]:
    cmd = ["docker", "ps", "-aq", "--filter", "label=am.run_id"]
    if run_id:
        cmd[-1] = f"label=am.run_id={run_id}"
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    return [x for x in r.stdout.split() if x]
