"""测试夹具(R-FND-10):临时 SQLite、Settings 工厂、TestClient、FakeLauncher、冻结时间。"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db as app_db
from app.config import Settings
from app.web.server import create_app

TOKEN = "t" * 40  # ≥32 字符的测试 token


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
    return create_app(settings)


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


# ---------- FakeLauncher(DESIGN §9;骨架,S3 集成时补全行为) ----------


@dataclass
class _FakeContainer:
    id: str
    spec: object
    running: bool = True
    exit_code: int | None = None
    started_at: float = field(default_factory=time.monotonic)
    stopped_by_us: bool = False


class FakeLauncher:
    """DockerLauncher 同接口的内存实现(stub 级):容器常驻直到 stop/kill。

    S3(feat/executor)将扩展为按 SIM_MODE 推进生命周期与写 status.json。
    """

    def __init__(self):
        self.containers: dict[str, _FakeContainer] = {}
        self._lock = threading.Lock()

    # -- DockerLauncher 接口 --
    def ping(self) -> bool:
        return True

    def image_exists(self, image: str) -> bool:
        return True

    def start(self, spec) -> str:
        cid = "fake-" + uuid.uuid4().hex[:12]
        with self._lock:
            self.containers[cid] = _FakeContainer(id=cid, spec=spec)
        return cid

    def wait(self, container_id: str, timeout: int) -> int | None:
        with self._lock:
            c = self.containers.get(container_id)
        if c is None:
            return 0  # 已消失视为退出 0(真实语义由 S3 细化)
        return None if c.running else c.exit_code

    def exists(self, container_id: str) -> bool:
        with self._lock:
            return container_id in self.containers

    def stop(self, container_id: str, timeout: int) -> None:
        with self._lock:
            c = self.containers.get(container_id)
            if c:
                c.running = False
                c.exit_code = 130
                c.stopped_by_us = True

    def logs_tail(self, container_id: str, n: int = 200) -> str:
        return "fake container log line\n"

    def remove(self, container_id: str) -> None:
        with self._lock:
            self.containers.pop(container_id, None)

    # -- 测试辅助 --
    def finish(self, container_id: str, exit_code: int) -> None:
        with self._lock:
            c = self.containers.get(container_id)
            if c:
                c.running = False
                c.exit_code = exit_code


@pytest.fixture
def fake_launcher() -> FakeLauncher:
    return FakeLauncher()
