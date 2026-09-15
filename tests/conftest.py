"""测试夹具(R-FND-10):临时 SQLite、Settings 工厂、TestClient、FakeLauncher、冻结时间。"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import db as app_db
from app.config import Settings
from app.web.server import create_app

TOKEN = "t" * 40  # ≥32 字符的测试 token
SH_TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def conn(db_file: Path) -> Iterator:
    c = app_db.connect(str(db_file))
    app_db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def make_settings(tmp_path: Path):
    def _make(**overrides) -> Settings:
        base = dict(
            bind="127.0.0.1",
            port=8090,
            token=TOKEN,
            data_dir=str(tmp_path / "am-data"),
            data_host=str(tmp_path / "am-data"),
            ta_data_dir=str(tmp_path / "ta-data"),
            ta_data_host=str(tmp_path / "ta-data"),
            ta_env_host=str(tmp_path / "ta.env"),
            runner_host=str(tmp_path / "runner.py"),
        )
        base.update(overrides)
        s = Settings(**base)
        s.db_path = str(tmp_path / "am-data" / "test.db")
        return s

    return _make


@pytest.fixture
def settings(make_settings) -> Settings:
    return make_settings()


@pytest.fixture
def app(settings):
    """注入 FakeLauncher 的真实 Worker:web 单测不依赖 docker(自检必过)。"""
    from pathlib import Path as _P

    _P(settings.ta_data_dir).mkdir(parents=True, exist_ok=True)
    from app.db import connect as _connect
    from app.executor.worker import Worker

    worker = Worker(settings, launcher=FakeLauncher(), db_factory=lambda: _connect(settings.db_path))
    return create_app(settings, worker=worker)


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client(client: TestClient, settings) -> TestClient:
    client.headers.update({"Authorization": f"Bearer {settings.token}"})
    return client


@pytest.fixture(autouse=True)
def _clean_login_attempts():
    from app.web import auth

    auth._login_attempts.clear()
    yield
    auth._login_attempts.clear()


@contextmanager
def _freeze(ts: str):
    """按 Asia/Shanghai(+08:00)冻结时间,如 freezer("2026-09-14 08:30:00")。

    freezegun 的 tz_offset 正向会再叠加时区差,实测 -8 才能让 now(SH_TZ) 等于给定值。
    """
    from freezegun import freeze_time

    with freeze_time(ts, tz_offset=-8) as frozen:
        yield frozen


@pytest.fixture
def freezer():
    """freeze_time 上下文工厂,如 freezer("2026-09-14 08:30:00")。"""
    return _freeze


# ---------- FakeLauncher(DESIGN §9):内存模拟容器生命周期与 status.json ----------


@dataclass
class _FakeContainer:
    id: str
    spec: object
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    exit_code: int | None = None
    removed: bool = False


class FakeLauncher:
    """DockerLauncher 同接口的内存实现,供 Worker 单测(无需 docker)。

    start() 起线程模拟「执行容器 + runner」:
    - 向 spec.workspace_host 写 status.json(与 runner 相同 schema)
    - 按位推进 agents_done;SIM_MODE 经 spec.extra_env 注入:
      ok(默认)| fail(第 3 节点异常退出 1)| hang(第 3 节点阻塞,SIGINT 可中断)
      | no_report | no_memory
    - 完成时向 spec.ta_data_host 写 investment_plan.md 与 memory 条件(除 no_* 模式)
    stop() 模拟 SIGINT:置 stop_event → 线程以 130 退出。
    """

    def __init__(self, step_seconds: float = 0.05):
        self.step_seconds = step_seconds
        self.containers: dict[str, _FakeContainer] = {}
        self._lock = threading.Lock()
        self.ping_ok = True  # 自检用:模拟 docker 不可达
        self.fail_on_start = False  # 启动用:模拟 docker run 失败

    # -- DockerLauncher 接口 --
    def ping(self) -> bool:
        return self.ping_ok

    def image_exists(self, image: str) -> bool:
        return True

    def start(self, spec) -> str:
        if self.fail_on_start:
            raise RuntimeError("simulated docker failure")
        cid = "fake-" + uuid.uuid4().hex[:12]
        c = _FakeContainer(id=cid, spec=spec)
        c.thread = threading.Thread(target=self._simulate, args=(c,), daemon=True)
        with self._lock:
            self.containers[cid] = c
        c.thread.start()
        return cid

    def wait(self, container_id: str, timeout: int) -> int | None:
        with self._lock:
            c = self.containers.get(container_id)
        if c is None:
            return 0
        if c.done_event.wait(timeout=timeout if isinstance(timeout, int) else 5):
            return c.exit_code
        return None

    def exists(self, container_id: str) -> bool:
        with self._lock:
            return container_id in self.containers

    def stop(self, container_id: str, timeout: int) -> None:
        with self._lock:
            c = self.containers.get(container_id)
        if c is None:
            return
        c.stop_event.set()  # 模拟 SIGINT
        if c.thread:
            c.thread.join(timeout=timeout + 5)

    def logs_tail(self, container_id: str, n: int = 200) -> str:
        return "[fake] container log line\n" * 5

    def remove(self, container_id: str) -> None:
        with self._lock:
            c = self.containers.pop(container_id, None)
            if c:
                c.removed = True

    # -- 模拟执行 --
    def _simulate(self, c: _FakeContainer) -> None:
        spec = c.spec
        mode = (spec.extra_env or {}).get("SIM_MODE", "ok")
        ws = Path(spec.workspace_host)
        ws.mkdir(parents=True, exist_ok=True)
        status_file = ws / "status.json"
        total = len(spec.analysts) + 8
        run_id = spec.run_id
        try:
            self._write_status(status_file, run_id, spec, "starting", None, 0, total)
            done = 0
            for i in range(total):
                if c.stop_event.wait(self.step_seconds):
                    self._write_status(
                        status_file, run_id, spec, "running", f"agent-{i}", done, total, error="interrupted"
                    )
                    c.exit_code = 130
                    return
                if mode == "fail" and i == 2:
                    self._write_status(
                        status_file,
                        run_id,
                        spec,
                        "failed",
                        f"agent-{i}",
                        done,
                        total,
                        error="RuntimeError: sim injected failure",
                    )
                    c.exit_code = 1
                    return
                if mode == "hang" and i == 2:
                    if c.stop_event.wait(3600):
                        self._write_status(
                            status_file,
                            run_id,
                            spec,
                            "running",
                            f"agent-{i}",
                            done,
                            total,
                            error="interrupted",
                        )
                        c.exit_code = 130
                        return
                done = i + 1
                self._write_status(status_file, run_id, spec, "running", f"agent-{i}", done, total)
            if mode == "corrupt_status":
                status_file.write_text('{"run_id": "r-broken", "phase": "run', encoding="utf-8")
                c.stop_event.wait(1.5)
            self._write_status(status_file, run_id, spec, "succeeded", None, total, total)
            if mode != "no_report":
                reports = Path(spec.ta_data_host) / "logs" / spec.ticker / spec.date / "reports"
                reports.mkdir(parents=True, exist_ok=True)
                (reports / "investment_plan.md").write_text(
                    f"[fake] plan for {spec.ticker}", encoding="utf-8"
                )
            if mode != "no_memory":
                memory = Path(spec.ta_data_host) / "memory" / "trading_memory.md"
                memory.parent.mkdir(parents=True, exist_ok=True)
                with open(memory, "a", encoding="utf-8") as f:
                    f.write(f"[{spec.date} | {spec.ticker} | HOLD | pending]\n\nDECISION:\nfake\n")
            c.exit_code = 0
        except Exception:  # noqa: BLE001 - 模拟器异常按容器崩溃处理
            c.exit_code = 1
        finally:
            c.done_event.set()

    @staticmethod
    def _write_status(path: Path, run_id, spec, phase, agent, done, total, error=None):
        payload = {
            "run_id": run_id,
            "ticker": spec.ticker,
            "date": spec.date,
            "phase": phase,
            "current_agent": agent,
            "agents_done": done,
            "agents_total": total,
            "tokens_in": done * 120,
            "tokens_out": done * 60,
            "updated_at": datetime.now(SH_TZ).isoformat(timespec="seconds"),
            "error": error,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


@pytest.fixture
def fake_launcher() -> FakeLauncher:
    return FakeLauncher()


@pytest.fixture
def fake_worker():
    """工厂:给定 settings 构造带 FakeLauncher 的真实 Worker(自检必过)。"""

    def _make(settings):
        from pathlib import Path as _P

        _P(settings.ta_data_dir).mkdir(parents=True, exist_ok=True)
        from app.db import connect as _connect
        from app.executor.worker import Worker

        return Worker(settings, launcher=FakeLauncher(), db_factory=lambda: _connect(settings.db_path))

    return _make
