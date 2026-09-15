"""executor 模块单测:launcher 参数 / status_reader / verdict / retention。"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from app import models
from app.audit import scrub
from app.executor import retention, verdict
from app.executor.launcher import ContainerSpec, DockerLauncher, parse_sim_env
from app.executor.status_reader import Status, read_status

# ---------- launcher ----------


class FakeDockerClient:
    """捕获 containers.run 参数的假 client(不需要 docker daemon)。"""

    def __init__(self):
        self.run_kwargs: dict | None = None
        self._containers: dict[str, FakeContainer] = {}

    @property
    def containers(self):
        return self

    def run(self, **kwargs):
        self.run_kwargs = kwargs
        c = FakeContainer()
        self._containers[c.id] = c
        return c

    def get(self, cid):
        c = self._containers.get(cid)
        if c is None or c.removed:
            raise KeyError(cid)
        return c

    def ping(self):
        return True

    @property
    def images(self):
        return self

    def list(self, **kw):
        return []


class FakeContainer:
    _seq = 0

    def __init__(self):
        FakeContainer._seq += 1
        self.id = f"cid-{FakeContainer._seq:04d}"
        self.wait_result: dict | None = None
        self.stopped = False
        self.removed = False

    def wait(self, timeout=None):
        if self.wait_result is None:
            raise TimeoutError("still running")
        return self.wait_result

    def stop(self, timeout=None):
        self.stopped = True
        self.wait_result = {"StatusCode": 130}

    def logs(self, tail=None):
        return b"[container] log with sk-abcdefgh12345678 secret\n"

    def remove(self, force=False):
        self.removed = True


def make_spec(**overrides) -> ContainerSpec:
    base = dict(
        image="tradingagents-sim:latest",
        run_id="r-x",
        ticker="1810.HK",
        date="2026-09-14",
        analysts=("market", "social", "news"),
        ta_data_host="/host/ta",
        ta_env_host="/host/ta/.env",
        runner_host="/host/runner/runner.py",
        workspace_host="/host/data/runs/r-x",
        ta_container_data="/home/appuser/.tradingagents",
        workdir="/home/appuser/app",
        stop_timeout=20,
    )
    base.update(overrides)
    return ContainerSpec(**base)


class TestLauncher:
    def test_start_params_match_spec_6_3(self):
        client = FakeDockerClient()
        launcher = DockerLauncher(client=client, network="research-manage_default")
        launcher.start(make_spec(extra_env={"SIM_MODE": "fail"}))
        kw = client.run_kwargs
        assert kw["image"] == "tradingagents-sim:latest"
        assert kw["name"] == "am-r-x"
        assert kw["entrypoint"] == ["python", "/runner.py"]
        assert kw["command"] == [
            "--ticker",
            "1810.HK",
            "--date",
            "2026-09-14",
            "--analysts",
            "market,social,news",
            "--workspace",
            "/ws",
            "--run-id",
            "r-x",
        ]
        assert kw["working_dir"] == "/home/appuser/app"
        assert kw["volumes"] == {
            "/host/ta": {"bind": "/home/appuser/.tradingagents", "mode": "rw"},
            "/host/ta/.env": {"bind": "/home/appuser/app/.env", "mode": "ro"},
            "/host/runner/runner.py": {"bind": "/runner.py", "mode": "ro"},
            "/host/data/runs/r-x": {"bind": "/ws", "mode": "rw"},
        }
        # AM-07:environment 白名单(AM_RUN_ID/TZ + 本地 AM_SIM_ENV)
        assert kw["environment"] == {"AM_RUN_ID": "r-x", "TZ": "Asia/Shanghai", "SIM_MODE": "fail"}
        assert kw["stop_signal"] == "SIGINT" and kw["detach"] is True
        assert kw["labels"] == {"am.run_id": "r-x"}
        assert kw["network"] == "research-manage_default"

    def test_network_none_default_bridge(self):
        client = FakeDockerClient()
        DockerLauncher(client=client).start(make_spec())
        assert client.run_kwargs["network"] is None

    def test_wait_running_vs_exited(self):
        client = FakeDockerClient()
        launcher = DockerLauncher(client=client)
        cid = launcher.start(make_spec())
        assert launcher.wait(cid, 0) is None  # 仍在运行
        container = client.get(cid)
        container.wait_result = {"StatusCode": 0}
        assert launcher.wait(cid, 1) == 0

    def test_stop_and_remove(self):
        client = FakeDockerClient()
        launcher = DockerLauncher(client=client)
        cid = launcher.start(make_spec())
        container = client.get(cid)
        launcher.stop(cid, 20)
        assert container.stopped
        launcher.remove(cid)
        assert container.removed
        assert launcher.exists(cid) is False

    def test_logs_tail_scrubbed_downstream(self):
        client = FakeDockerClient()
        launcher = DockerLauncher(client=client)
        cid = launcher.start(make_spec())
        raw = launcher.logs_tail(cid)
        assert "sk-abcdefgh12345678" in raw
        assert "sk-abcdefgh12345678" not in scrub(raw)  # worker 落档前 scrub

    def test_parse_sim_env(self):
        assert parse_sim_env("") == {}
        assert parse_sim_env("SIM_MODE=fail") == {"SIM_MODE": "fail"}
        assert parse_sim_env("SIM_MODE=fail, SIM_STEP_SECONDS=0.1") == {
            "SIM_MODE": "fail",
            "SIM_STEP_SECONDS": "0.1",
        }


# ---------- status_reader ----------


class TestStatusReader:
    def _write(self, path: Path, payload) -> None:
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")

    def test_valid(self, tmp_path):
        p = tmp_path / "status.json"
        self._write(
            p,
            {
                "run_id": "r",
                "ticker": "T",
                "date": "2026-09-14",
                "phase": "running",
                "current_agent": "Bear Researcher",
                "agents_done": 3,
                "agents_total": 11,
                "tokens_in": 90,
                "tokens_out": 45,
                "updated_at": "2026-09-14T08:30:00+08:00",
                "error": None,
            },
        )
        s = read_status(p)
        assert isinstance(s, Status)
        assert s.phase == "running" and s.agents_done == 3
        assert s.updated_at is not None and s.error is None

    def test_missing_file(self, tmp_path):
        assert read_status(tmp_path / "nope.json") is None

    def test_half_json(self, tmp_path):
        p = tmp_path / "status.json"
        self._write(p, '{"run_id": "r-broken", "phase": "run')
        assert read_status(p) is None  # AM-08

    def test_missing_fields(self, tmp_path):
        p = tmp_path / "status.json"
        self._write(p, {"phase": "running"})  # 缺 agents_done 等
        assert read_status(p) is None

    def test_bad_types(self, tmp_path):
        p = tmp_path / "status.json"
        self._write(
            p, {"phase": "running", "agents_done": "3", "agents_total": 11, "tokens_in": 0, "tokens_out": 0}
        )
        assert read_status(p) is None

    def test_naive_updated_at_gets_tz(self, tmp_path):
        p = tmp_path / "status.json"
        self._write(
            p,
            {
                "phase": "running",
                "agents_done": 0,
                "agents_total": 11,
                "tokens_in": 0,
                "tokens_out": 0,
                "updated_at": "2026-09-14T08:30:00",
            },
        )
        s = read_status(p)
        assert s.updated_at is not None and s.updated_at.utcoffset() is not None


# ---------- verdict ----------


def make_ta(tmp_path, code="1810.HK", date="2026-09-14", plan=True, memory=True):
    if plan:
        d = tmp_path / "logs" / code / date / "reports"
        d.mkdir(parents=True)
        (d / "investment_plan.md").write_text("plan", encoding="utf-8")
    if memory:
        m = tmp_path / "memory"
        m.mkdir(parents=True, exist_ok=True)
        (m / "trading_memory.md").write_text(f"[{date} | {code} | HOLD | pending]\n", encoding="utf-8")


def status(error=None):
    return Status("running", "X", 3, 11, 1, 1, None, error)


class TestVerdict:
    def test_order1_cancelled_by_us(self, tmp_path):
        make_ta(tmp_path)
        v = verdict.decide(
            exit_code=137,
            cancelled_by_us=True,
            status=None,
            ta_data_dir=tmp_path,
            code="1810.HK",
            date="2026-09-14",
        )
        assert (v.status, v.error, v.report_ready) == ("cancelled", None, True)

    def test_order1_exit130(self, tmp_path):
        v = verdict.decide(
            exit_code=130,
            cancelled_by_us=False,
            status=None,
            ta_data_dir=tmp_path,
            code="1810.HK",
            date="2026-09-14",
        )
        assert v.status == "cancelled"

    def test_order2_nonzero_failed_with_status_error(self, tmp_path):
        v = verdict.decide(
            exit_code=1,
            cancelled_by_us=False,
            status=status("boom"),
            ta_data_dir=tmp_path,
            code="1810.HK",
            date="2026-09-14",
        )
        assert (v.status, v.error) == ("failed", "boom")

    def test_order2_nonzero_failed_exit_code_fallback(self, tmp_path):
        v = verdict.decide(
            exit_code=2,
            cancelled_by_us=False,
            status=None,
            ta_data_dir=tmp_path,
            code="1810.HK",
            date="2026-09-14",
        )
        assert v.error == "exit_2"

    def test_order3_double_pass(self, tmp_path):
        make_ta(tmp_path)
        v = verdict.decide(
            exit_code=0,
            cancelled_by_us=False,
            status=None,
            ta_data_dir=tmp_path,
            code="1810.HK",
            date="2026-09-14",
        )
        assert (v.status, v.error, v.report_ready) == ("succeeded", None, True)

    def test_order3_missing_plan(self, tmp_path):
        """AM-03:删掉 investment_plan.md 后退出码 0 仍判 failed 并写明缺项。"""
        make_ta(tmp_path, plan=False)
        v = verdict.decide(
            exit_code=0,
            cancelled_by_us=False,
            status=None,
            ta_data_dir=tmp_path,
            code="1810.HK",
            date="2026-09-14",
        )
        assert v.status == "failed"
        assert "investment_plan.md" in v.error

    def test_order3_missing_memory(self, tmp_path):
        make_ta(tmp_path, memory=False)
        v = verdict.decide(
            exit_code=0,
            cancelled_by_us=False,
            status=None,
            ta_data_dir=tmp_path,
            code="1810.HK",
            date="2026-09-14",
        )
        assert v.status == "failed" and "memory" in v.error

    def test_memory_prefix_requires_bracket_format(self, tmp_path):
        m = tmp_path / "memory"
        m.mkdir(parents=True)
        (m / "trading_memory.md").write_text("2026-09-14 | 1810.HK | unrelated line\n", encoding="utf-8")
        assert verdict.memory_has_entry(tmp_path, "1810.HK", "2026-09-14") is False


# ---------- retention ----------


def seed_run(conn, instrument_id, *, status="succeeded", finished=None, rid="r-old"):
    with conn:
        conn.execute(
            "INSERT INTO run(id, instrument_id, analysts_csv, analysis_date, status,"
            " trigger, agents_total, created_at, finished_at)"
            " VALUES (?,?,?,?,?,'web',11,?,?)",
            (
                rid,
                instrument_id,
                "market,social,news",
                "2026-09-14",
                status,
                "2026-08-01T00:00:00+08:00",
                finished,
            ),
        )


class TestRetention:
    def test_purges_old_runs_and_dirs(self, conn, settings, tmp_path):
        conn.execute(
            "INSERT INTO instrument(market, code, name, enabled, created_at, updated_at)"
            " VALUES ('hk','1810.HK','',1,'t','t')"
        )
        old = (models.now_sh() - timedelta(days=settings.retention_days + 5)).isoformat(timespec="seconds")
        seed_run(conn, 1, finished=old, rid="r-old")
        run_dir = Path(settings.data_dir) / "runs" / "r-old"
        run_dir.mkdir(parents=True)
        (run_dir / "status.json").write_text("{}", encoding="utf-8")
        fresh = (models.now_sh() - timedelta(days=1)).isoformat(timespec="seconds")
        seed_run(conn, 1, finished=fresh, rid="r-fresh")
        seed_run(conn, 1, status="queued", finished=None, rid="r-queued")

        purged = retention.purge(conn, settings)
        assert purged == 1
        ids = {r["id"] for r in conn.execute("SELECT id FROM run")}
        assert ids == {"r-fresh", "r-queued"}
        assert not run_dir.exists()  # 目录一并清理
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action='purged'").fetchone()["n"] == 1

    def test_nothing_to_purge(self, conn, settings):
        assert retention.purge(conn, settings) == 0
