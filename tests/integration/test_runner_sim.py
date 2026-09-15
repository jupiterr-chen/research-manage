"""sim 镜像集成测试(R-RUN-03/08 / AM-11/16):真实 docker + tradingagents-sim:latest。

运行:pytest -m docker tests/integration/test_runner_sim.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "runner" / "runner.py"
IMAGE = "tradingagents-sim:latest"


def _docker_available() -> bool:
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


def _image_exists() -> bool:
    r = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, text=True, timeout=20)
    return r.returncode == 0


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="本地 Docker 不可用"),
    pytest.mark.skipif(not _image_exists(), reason=f"缺少 {IMAGE},先运行 scripts/build_sim.sh"),
]


def _win(p: Path) -> str:
    return str(p).replace("\\", "/")


def run_container(
    tmp: Path,
    *,
    mode: str,
    ticker="1810.HK",
    date="2026-09-14",
    analysts="market,social,news",
    run_id=None,
    env_extra: dict | None = None,
    timeout=120,
) -> tuple[int, dict | None]:
    """一次性执行 runner 容器;返回 (退出码, 最终 status.json)。"""
    ws, ta = tmp / "ws", tmp / "ta"
    ws.mkdir(exist_ok=True)
    ta.mkdir(exist_ok=True)
    cmd = [
        "docker",
        "run",
        "--rm",
        "--stop-signal",
        "SIGINT",
        "--entrypoint",
        "python",
        "-v",
        f"{_win(ws)}:/ws",
        "-v",
        f"{_win(ta)}:/home/appuser/.tradingagents",
        "-v",
        f"{_win(RUNNER)}:/runner.py:ro",
        "-e",
        f"SIM_MODE={mode}",
        "-e",
        "TRADINGAGENTS_CHECKPOINT_ENABLED=true",
    ]
    for k, v in (env_extra or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [
        IMAGE,
        "/runner.py",
        "--ticker",
        ticker,
        "--date",
        date,
        "--analysts",
        analysts,
        "--workspace",
        "/ws",
        "--run-id",
        run_id or f"r-{mode}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    status = None
    status_file = ws / "status.json"
    if status_file.exists():
        status = json.loads(status_file.read_text(encoding="utf-8"))
    return r.returncode, status


class TestSimModes:
    def test_ok_full_artifacts(self, tmp_path):
        """AM-03(sim 侧):三产物齐全 + 报告文件 + 断点清除。"""
        code, status = run_container(tmp_path, mode="ok")
        assert code == 0
        assert status["phase"] == "succeeded"
        assert status["error"] is None
        assert status["agents_done"] == 11 and status["agents_total"] == 11  # AM-16
        assert status["tokens_in"] > 0  # sim 触发了 token 回调
        reports = tmp_path / "ta" / "logs" / "1810.HK" / "2026-09-14" / "reports"
        assert {
            "market_report.md",
            "sentiment_report.md",
            "news_report.md",
            "investment_plan.md",
            "trader_investment_plan.md",
            "final_trade_decision.md",
        } <= {p.name for p in reports.glob("*.md")}
        memory = (tmp_path / "ta" / "memory" / "trading_memory.md").read_text(encoding="utf-8")
        assert memory.startswith("[2026-09-14 | 1810.HK |")
        assert "pending]" in memory.splitlines()[0]
        log = (
            tmp_path
            / "ta"
            / "logs"
            / "1810.HK"
            / "TradingAgentsStrategy_logs"
            / "full_states_log_2026-09-14.json"
        )
        assert json.loads(log.read_text(encoding="utf-8"))["final_trade_decision"]
        assert (
            not (tmp_path / "ta" / "cache" / "checkpoints").exists()
            or list((tmp_path / "ta" / "cache" / "checkpoints").glob("*")) == []
        )

    def test_ok_four_analysts_total_12(self, tmp_path):
        code, status = run_container(tmp_path, mode="ok", analysts="market,social,news,fundamentals")
        assert code == 0
        assert status["agents_total"] == 12  # AM-16

    def test_fail_exit1(self, tmp_path):
        code, status = run_container(tmp_path, mode="fail")
        assert code == 1
        assert status["phase"] == "failed"
        assert "sim injected failure" in status["error"]

    def test_no_report(self, tmp_path):
        """AM-03 前置:investment_plan.md 缺失但 runner 正常退出(终态由管理台判定)。"""
        code, status = run_container(tmp_path, mode="no_report")
        assert code == 0
        assert status["phase"] == "succeeded"
        reports = tmp_path / "ta" / "logs" / "1810.HK" / "2026-09-14" / "reports"
        assert not (reports / "investment_plan.md").exists()

    def test_no_memory(self, tmp_path):
        code, status = run_container(tmp_path, mode="no_memory")
        assert code == 0
        assert not (tmp_path / "ta" / "memory" / "trading_memory.md").exists()

    def test_upstream_changed_exit2(self, tmp_path):
        """AM-11:上游缺属性 → 退出码 2 + error=upstream_api_changed。"""
        code, status = run_container(tmp_path, mode="upstream_changed")
        assert code == 2
        assert status["phase"] == "failed"
        assert status["error"].startswith("upstream_api_changed")

    def test_slow_completes(self, tmp_path):
        code, status = run_container(
            tmp_path, mode="slow", env_extra={"SIM_SLOW_SECONDS": "0.2"}, timeout=180
        )
        assert code == 0
        assert status["phase"] == "succeeded"

    def test_corrupt_status_still_ok(self, tmp_path):
        """corrupt_status 注入半截 JSON 一次;runner 自身的原子写最终恢复完整文件。"""
        code, status = run_container(tmp_path, mode="corrupt_status")
        assert code == 0
        assert status["phase"] == "succeeded"


class TestCancelResume:
    def _start_hang(self, tmp: Path):
        ws, ta = tmp / "ws", tmp / "ta"
        ws.mkdir(exist_ok=True)
        ta.mkdir(exist_ok=True)
        cmd = [
            "docker",
            "run",
            "-d",
            "--stop-signal",
            "SIGINT",
            "--entrypoint",
            "python",
            "-v",
            f"{_win(ws)}:/ws",
            "-v",
            f"{_win(ta)}:/home/appuser/.tradingagents",
            "-v",
            f"{_win(RUNNER)}:/runner.py:ro",
            "-e",
            "SIM_MODE=hang",
            "-e",
            "TRADINGAGENTS_CHECKPOINT_ENABLED=true",
            IMAGE,
            "/runner.py",
            "--ticker",
            "0700.HK",
            "--date",
            "2026-09-14",
            "--analysts",
            "market,social,news",
            "--workspace",
            "/ws",
            "--run-id",
            "r-hang",
        ]
        cid = subprocess.check_output(cmd, text=True, timeout=30).strip()
        return cid, ws, ta

    def test_sigint_exit130_and_resume(self, tmp_path):
        """AM-04(sim 侧):SIGINT → 130、断点保留;同签名重跑跳过已完成节点并清断点。"""
        cid, ws, ta = self._start_hang(tmp_path)
        try:
            time.sleep(3)  # 走完前 2 个节点,第 3 个挂起
            subprocess.run(["docker", "stop", "-t", "15", cid], capture_output=True, text=True, timeout=60)
            subprocess.run(["docker", "wait", cid], capture_output=True, text=True, timeout=30)
            ec = subprocess.check_output(
                ["docker", "inspect", "-f", "{{.State.ExitCode}}", cid], text=True, timeout=30
            ).strip()
        finally:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=30)
        assert ec == "130"
        status = json.loads((ws / "status.json").read_text(encoding="utf-8"))
        assert status["error"] == "interrupted"
        cp_files = list((ta / "cache" / "checkpoints").glob("*.json"))
        assert len(cp_files) == 1, "断点必须保留"

        # resume:相同 ticker+date+analysts(断点签名一致)
        code, status2 = run_container(tmp_path, mode="ok", ticker="0700.HK", run_id="r-resume")
        assert code == 0
        assert status2["phase"] == "succeeded"
        # 续跑只执行剩余 9 个节点 → tokens = 9 × 120(证明跳过了 2 个已完成节点)
        assert status2["tokens_in"] == 9 * 120
        assert not list((ta / "cache" / "checkpoints").glob("*.json")), "成功后断点清除"
        reports = ta / "logs" / "0700.HK" / "2026-09-14" / "reports"
        assert (reports / "investment_plan.md").exists()
        memory = (ta / "memory" / "trading_memory.md").read_text(encoding="utf-8")
        assert memory.startswith("[2026-09-14 | 0700.HK |")

    def test_different_signature_starts_fresh(self, tmp_path):
        """分析师集合不同 = 断点签名不同 = 从头跑(SPEC §1.3)。"""
        cid, ws, ta = self._start_hang(tmp_path)
        try:
            time.sleep(3)
            subprocess.run(["docker", "stop", "-t", "15", cid], capture_output=True, timeout=60)
        finally:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=30)
        code, status = run_container(
            tmp_path,
            mode="ok",
            ticker="0700.HK",
            analysts="market,social,news,fundamentals",
            run_id="r-fresh",
        )
        assert code == 0
        # 全量 12 节点全部执行 → tokens = 12 × 120,证明未复用旧断点
        assert status["tokens_in"] == 12 * 120


@pytest.mark.skipif(shutil.which("docker") is None, reason="无 docker CLI")
class TestEnvMount:
    def test_env_marker(self, tmp_path):
        """sim __init__ 写 .env-present marker:未挂载 .env 时为 0。"""
        code, _ = run_container(tmp_path, mode="ok", run_id="r-env")
        assert code == 0
        marker = tmp_path / "ta" / "logs" / ".env-present"
        assert marker.read_text(encoding="utf-8").strip() == "0"
