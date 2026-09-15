"""真实代码本地镜像测试(R-RUN-11,`-m real`):tradingagents-local + llm-stub。

L2 保真:runner 跑的是真实 LangGraph/checkpointer/memory_log/报告目录,
只有 LLM 是假的(stub)。无 vendor 构建的镜像或无法起 stub 时 skip 并打印原因。

运行:pytest -m real tests/integration/test_runner_real.py
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "runner" / "runner.py"
LOCAL_IMAGE = "tradingagents-local:latest"
STUB_IMAGE = "llm-stub:latest"
PROXY = "http://192.168.1.150:7890"
NO_PROXY = "llm-stub,localhost,127.0.0.1,192.168.1.150"


def _image_exists(image: str) -> bool:
    try:
        r = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.real,
    pytest.mark.skipif(
        not _image_exists(LOCAL_IMAGE),
        reason=f"缺少 {LOCAL_IMAGE}(scripts/fetch_vendor.sh + scripts/build_local.sh)",
    ),
    pytest.mark.skipif(not _image_exists(STUB_IMAGE), reason="缺少 llm-stub:latest(sim/llm_stub)"),
]


def _win(p: Path) -> str:
    return str(p).replace("\\", "/")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 60), **kw)


class RealStack:
    """每测试一套:docker 网络 + llm-stub 容器。"""

    def __init__(self, tmp: Path, delay: str = "0"):
        self.tmp = tmp
        self.name = f"am-real-{int(time.time() * 1000) % 10**9:x}"
        self.stub_cid: str | None = None
        self.delay = delay

    def up(self):
        _run(["docker", "network", "create", self.name])
        self.stub_cid = _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                f"llm-stub-{self.name}",
                "--network",
                self.name,
                "--network-alias",
                "llm-stub",  # 执行容器经 backend_url 按 llm-stub 解析
                "-e",
                f"STUB_DELAY_SECONDS={self.delay}",
                STUB_IMAGE,
            ]
        ).stdout.strip()
        for _ in range(30):
            r = _run(
                [
                    "docker",
                    "exec",
                    f"llm-stub-{self.name}",
                    "python",
                    "-c",
                    "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/v1/models',timeout=2)",
                ]
            )
            if r.returncode == 0:
                return
            time.sleep(0.5)
        raise RuntimeError("llm-stub 未就绪")

    def stub_requests(self) -> int:
        logs = _run(["docker", "logs", f"llm-stub-{self.name}"]).stdout
        return sum(1 for line in logs.splitlines() if line.startswith("REQ "))

    def env_file(self) -> Path:
        p = self.tmp / "ta.env"
        p.write_text(
            "TRADINGAGENTS_LLM_PROVIDER=openai\n"
            "TRADINGAGENTS_LLM_BACKEND_URL=http://llm-stub:8000/v1\n"
            "OPENAI_API_KEY=FAKEKEY_local\n"
            "TRADINGAGENTS_CHECKPOINT_ENABLED=true\n"
            f"HTTP_PROXY={PROXY}\n"
            f"HTTPS_PROXY={PROXY}\n"
            f"NO_PROXY={NO_PROXY}\n",
            encoding="utf-8",
        )
        return p

    def run(
        self, ticker: str, analysts: str, run_id: str, *, env_file: Path | None = None, timeout: int = 900
    ) -> tuple[int, dict | None, Path]:
        ws, ta = self.tmp / "ws", self.tmp / "ta"
        ws.mkdir(exist_ok=True)
        ta.mkdir(exist_ok=True)
        cmd = [
            "docker",
            "run",
            "--rm",
            "--stop-signal",
            "SIGINT",
            "--network",
            self.name,
            "--entrypoint",
            "python",
            "-v",
            f"{_win(ws)}:/ws",
            "-v",
            f"{_win(ta)}:/home/appuser/.tradingagents",
            "-v",
            f"{_win(RUNNER)}:/runner.py:ro",
            "--env-file",
            _win(env_file or self.env_file()),
            LOCAL_IMAGE,
            "/runner.py",
            "--ticker",
            ticker,
            "--date",
            "2026-09-14",
            "--analysts",
            analysts,
            "--workspace",
            "/ws",
            "--run-id",
            run_id,
        ]
        r = _run(cmd, timeout=timeout)
        status = None
        sf = ws / "status.json"
        if sf.exists():
            status = json.loads(sf.read_text(encoding="utf-8"))
        return r.returncode, status, ta

    def start_detached(self, ticker: str, analysts: str, run_id: str) -> str:
        ws, ta = self.tmp / "ws", self.tmp / "ta"
        ws.mkdir(exist_ok=True)
        ta.mkdir(exist_ok=True)
        cmd = [
            "docker",
            "run",
            "-d",
            "--stop-signal",
            "SIGINT",
            "--network",
            self.name,
            "--entrypoint",
            "python",
            "-v",
            f"{_win(ws)}:/ws",
            "-v",
            f"{_win(ta)}:/home/appuser/.tradingagents",
            "-v",
            f"{_win(RUNNER)}:/runner.py:ro",
            "--env-file",
            _win(self.env_file()),
            LOCAL_IMAGE,
            "/runner.py",
            "--ticker",
            ticker,
            "--date",
            "2026-09-14",
            "--analysts",
            analysts,
            "--workspace",
            "/ws",
            "--run-id",
            run_id,
        ]
        return _run(cmd, timeout=60).stdout.strip()

    def down(self):
        if self.stub_cid:
            _run(["docker", "rm", "-f", f"llm-stub-{self.name}"])
        _run(["docker", "network", "rm", self.name])


@pytest.fixture
def stack(tmp_path):
    s = RealStack(tmp_path)
    try:
        s.up()
        yield s
    finally:
        s.down()


def assert_three_artifacts(ta: Path, ticker: str, date: str = "2026-09-14"):
    """AM-03:reports/*.md 与 memory 条目与 full_states_log 三者齐全。"""
    reports = ta / "logs" / ticker / date / "reports"
    names = {p.name for p in reports.glob("*.md")}
    assert {
        "market_report.md",
        "sentiment_report.md",
        "news_report.md",
        "investment_plan.md",
        "trader_investment_plan.md",
        "final_trade_decision.md",
    } <= names, names
    memory = (ta / "memory" / "trading_memory.md").read_text(encoding="utf-8")
    assert any(line.startswith(f"[{date} | {ticker} |") for line in memory.splitlines())
    log = ta / "logs" / ticker / "TradingAgentsStrategy_logs" / f"full_states_log_{date}.json"
    data = json.loads(log.read_text(encoding="utf-8"))
    assert data["final_trade_decision"] and data["investment_plan"]


class TestRealGraph:
    def test_three_analysts_succeeded(self, stack, tmp_path):
        code, status, ta = stack.run("1810.HK", "market,social,news", "r-real-3")
        assert code == 0, status
        assert status["phase"] == "succeeded"
        assert status["error"] is None
        assert status["agents_total"] == 11
        assert status["tokens_in"] > 0 and status["tokens_out"] > 0  # 真实 StatsCallbackHandler
        assert_three_artifacts(ta, "1810.HK")
        assert (
            not list((ta / "cache" / "checkpoints").glob("*.json"))
            or list((ta / "cache" / "checkpoints").glob("*.db")) == []
        )

    def test_four_analysts_succeeded(self, stack, tmp_path):
        code, status, ta = stack.run("NVDA", "market,social,news,fundamentals", "r-real-4")
        assert code == 0, status
        assert status["agents_total"] == 12
        assert_three_artifacts(ta, "NVDA")
        reports = ta / "logs" / "NVDA" / "2026-09-14" / "reports"
        assert (reports / "fundamentals_report.md").exists()

    def test_cancel_then_resume_from_checkpoint(self, tmp_path):
        """AM-04(真实栈):SIGINT 中断 → 130 + 断点保留;resume 跳过已完成节点。"""
        s = RealStack(tmp_path, delay="0.6")  # 每 LLM 请求略延迟,保证可中断窗口
        try:
            s.up()
            cid = s.start_detached("0700.HK", "market,social,news", "r-real-cancel")
            ws = tmp_path / "ws"
            try:
                # 真实图启动慢(import + yfinance),轮询 status.json 直到 ≥2 节点完成
                deadline = time.time() + 420
                done = -1
                while time.time() < deadline:
                    sf = ws / "status.json"
                    if sf.exists():
                        try:
                            done = json.loads(sf.read_text(encoding="utf-8"))["agents_done"]
                        except (ValueError, KeyError):
                            done = -1
                        if done >= 2:
                            break
                    time.sleep(3)
                assert done >= 2, f"等待节点完成超时(agents_done={done})"
                subprocess.run(
                    ["docker", "stop", "-t", "25", cid], capture_output=True, text=True, timeout=60
                )
                subprocess.run(["docker", "wait", cid], capture_output=True, timeout=30)
                ec = subprocess.check_output(
                    ["docker", "inspect", "-f", "{{.State.ExitCode}}", cid], text=True, timeout=30
                ).strip()
            finally:
                subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=30)
            assert ec == "130"
            status = json.loads((ws / "status.json").read_text(encoding="utf-8"))
            assert status["error"] == "interrupted"
            cps = list((tmp_path / "ta" / "cache" / "checkpoints").glob("*.db"))
            assert cps, "LangGraph SqliteSaver 断点必须保留"
            baseline_before = s.stub_requests()

            code, status2, ta = s.run("0700.HK", "market,social,news", "r-real-resume", timeout=1200)
            assert code == 0, status2
            assert status2["phase"] == "succeeded"
            assert_three_artifacts(ta, "0700.HK")
            resume_calls = s.stub_requests() - baseline_before
            # 全量 3 分析师一次跑 ≥ 15 次 LLM 调用;续跑必须显著少于全量
            assert resume_calls < 15, f"续跑 LLM 调用 {resume_calls} 次,疑似从头重跑"
        finally:
            s.down()
