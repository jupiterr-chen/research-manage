"""runner/runner.py 单测(R-RUN-01~07,AM-11/16):mock graph,不依赖 docker。"""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def load_runner():
    spec = importlib.util.spec_from_file_location("runner_mod", _REPO / "runner" / "runner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def runner():
    return load_runner()


class FakeMemoryLog:
    def __init__(self):
        self.decisions: list[tuple] = []
        self.context = "past lesson"

    def get_past_context(self, ticker, n_same=5, n_cross=3, as_of=None):
        return self.context

    def store_decision(self, ticker, trade_date, final_trade_decision):
        self.decisions.append((ticker, trade_date, final_trade_decision))


class FakeGraph:
    """具备 runner 依赖的 8 个属性与同形方法的假图。"""

    def __init__(self, results_dir: Path, chunks: list[dict], error: Exception | None = None):
        self.config = {
            "results_dir": str(results_dir),
            "data_cache_dir": str(results_dir / "cache"),
            "checkpoint_enabled": True,
        }
        self.memory_log = FakeMemoryLog()
        self.ticker = None
        self.calls: list[str] = []
        self.chunks = chunks
        self.error = error
        inner = self

        class _P:
            @staticmethod
            def create_initial_state(
                company_name, trade_date, asset_type="stock", past_context="", instrument_context=""
            ):
                return {"company_of_interest": company_name, "trade_date": str(trade_date), "messages": []}

            @staticmethod
            def get_graph_args(callbacks=None):
                cfg = {"recursion_limit": 100}
                if callbacks:
                    cfg["callbacks"] = callbacks
                return {"stream_mode": "values", "config": cfg}

        class _G:
            @staticmethod
            def stream(graph_input, **kwargs):
                yield from inner._emit()

        self.propagator = _P()
        self.graph = _G()

    def _emit(self):
        for i, chunk in enumerate(self.chunks):
            if self.error is not None and i == 2:
                raise self.error
            yield chunk

    def _resolve_pending_entries(self, ticker):
        self.calls.append("resolve_pending")

    def resolve_instrument_context(self, ticker, asset_type="stock"):
        return "[fake] instrument context"

    def _memory_as_of(self, trade_date):
        return None

    def begin_checkpoint(self, company_name, trade_date, asset_type="stock"):
        self.calls.append("begin_checkpoint")
        return "tid-123"

    def checkpoint_input(self, init_state):
        return init_state

    def end_checkpoint(self):
        self.calls.append("end_checkpoint")

    def clear_checkpoint_on_success(self, company_name, trade_date, asset_type="stock"):
        self.calls.append("clear_checkpoint")

    def _log_state(self, trade_date, final_state):
        self.calls.append("log_state")


def scenario_chunks() -> list[dict]:
    """3 分析师完整场景(values 模式:每 chunk 为累积状态)。"""
    return [
        {"market_report": "market analysis v1"},
        {"market_report": "market analysis v1", "sentiment_report": "sentiment v1"},
        {"market_report": "market analysis v1", "sentiment_report": "sentiment v1", "news_report": "news v1"},
        {
            "news_report": "news v1",
            "investment_debate_state": {
                "bull_history": "bull case",
                "bear_history": "",
                "judge_decision": "",
                "count": 1,
            },
        },
        {
            "investment_debate_state": {
                "bull_history": "bull case",
                "bear_history": "bear case",
                "judge_decision": "judge says hold",
                "count": 2,
            },
            "investment_plan": "research team plan",
        },
        {"trader_investment_plan": "trader plan"},
        {
            "risk_debate_state": {
                "aggressive_history": "agg",
                "conservative_history": "",
                "neutral_history": "",
                "judge_decision": "",
            }
        },
        {
            "risk_debate_state": {
                "aggressive_history": "agg",
                "conservative_history": "con",
                "neutral_history": "neu",
                "judge_decision": "pm decision",
            },
            "final_trade_decision": "FINAL: HOLD",
        },
        {"final_trade_decision": "FINAL: HOLD", "investment_plan": "final plan"},
    ]


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def invoke(runner, monkeypatch, graph_factory, workspace, analysts="market,social,news"):
    """把 runner.build_graph 替换为假图工厂后执行 run()。"""
    captured = {}

    def fake_build(analysts_tuple, stats):
        g = graph_factory()
        captured["graph"] = g
        return g

    monkeypatch.setattr(runner, "build_graph", fake_build)
    args = runner.parse_args(
        [
            "--ticker",
            "1810.HK",
            "--date",
            "2026-09-14",
            "--analysts",
            analysts,
            "--workspace",
            str(workspace),
            "--run-id",
            "r-unit-1",
        ]
    )
    code = runner.run(args)
    return code, captured.get("graph")


class TestSuccess:
    def test_exit0_and_status(self, runner, monkeypatch, workspace, tmp_path):
        g = FakeGraph(tmp_path / "results", scenario_chunks())
        code, graph = invoke(runner, monkeypatch, lambda: g, workspace)
        assert code == 0
        status = json.loads((workspace / "status.json").read_text(encoding="utf-8"))
        assert status["phase"] == "succeeded"
        assert status["run_id"] == "r-unit-1"
        assert status["ticker"] == "1810.HK" and status["date"] == "2026-09-14"
        assert status["error"] is None
        assert status["agents_done"] == 11 and status["agents_total"] == 11  # 3+8(AM-16)

    def test_agents_total_4_analysts(self, runner, monkeypatch, workspace, tmp_path):
        g = FakeGraph(tmp_path / "results", scenario_chunks())
        code, _ = invoke(
            runner, monkeypatch, lambda: g, workspace, analysts="market,social,news,fundamentals"
        )
        assert code == 0
        status = json.loads((workspace / "status.json").read_text(encoding="utf-8"))
        assert status["agents_total"] == 12

    def test_reports_written(self, runner, monkeypatch, workspace, tmp_path):
        results = tmp_path / "results"
        g = FakeGraph(results, scenario_chunks())
        invoke(runner, monkeypatch, lambda: g, workspace)
        reports = results / "1810.HK" / "2026-09-14" / "reports"
        names = {p.name for p in reports.glob("*.md")}
        assert {
            "market_report.md",
            "sentiment_report.md",
            "news_report.md",
            "investment_plan.md",
            "trader_investment_plan.md",
            "final_trade_decision.md",
        } <= names
        # 辩论期间的增量写入以 final_state 覆盖为终版
        assert "final plan" in (reports / "investment_plan.md").read_text(encoding="utf-8")

    def test_propagate_finishing_steps(self, runner, monkeypatch, workspace, tmp_path):
        g = FakeGraph(tmp_path / "results", scenario_chunks())
        code, graph = invoke(runner, monkeypatch, lambda: g, workspace)
        assert code == 0
        assert "resolve_pending" in graph.calls
        assert "log_state" in graph.calls
        assert "clear_checkpoint" in graph.calls
        assert graph.calls[-1] == "end_checkpoint"  # finally 收尾
        assert graph.memory_log.decisions == [("1810.HK", "2026-09-14", "FINAL: HOLD")]
        assert graph.ticker == "1810.HK"

    def test_ticker_set_before_log_state(self, runner, monkeypatch, workspace, tmp_path):
        g = FakeGraph(tmp_path / "results", scenario_chunks())
        invoke(runner, monkeypatch, lambda: g, workspace)
        assert "resolve_pending" in g.calls and g.ticker == "1810.HK"


class TestFailures:
    def test_stream_exception_exit1(self, runner, monkeypatch, workspace, tmp_path):
        g = FakeGraph(tmp_path / "results", scenario_chunks(), error=RuntimeError("boom-模拟"))
        code, _ = invoke(runner, monkeypatch, lambda: g, workspace)
        assert code == 1
        status = json.loads((workspace / "status.json").read_text(encoding="utf-8"))
        assert status["phase"] == "failed"
        assert "RuntimeError" in status["error"]
        assert "end_checkpoint" in g.calls  # finally 仍执行

    def test_keyboard_interrupt_exit130(self, runner, monkeypatch, workspace, tmp_path):
        g = FakeGraph(tmp_path / "results", scenario_chunks(), error=KeyboardInterrupt())
        code, _ = invoke(runner, monkeypatch, lambda: g, workspace)
        assert code == 130
        status = json.loads((workspace / "status.json").read_text(encoding="utf-8"))
        assert status["phase"] == "running" and status["error"] == "interrupted"
        assert "end_checkpoint" in g.calls

    def test_self_check_missing_attr_exit2(self, runner, monkeypatch, workspace, tmp_path):
        g = FakeGraph(tmp_path / "results", scenario_chunks())
        del FakeGraph._log_state  # 制造上游缺口(AM-11)
        try:
            code, _ = invoke(runner, monkeypatch, lambda: g, workspace)
        finally:
            FakeGraph._log_state = lambda self, trade_date, final_state: None
        assert code == 2
        status = json.loads((workspace / "status.json").read_text(encoding="utf-8"))
        assert status["phase"] == "failed"
        assert status["error"].startswith("upstream_api_changed")
        assert "log_state" not in g.calls


class TestArgs:
    @pytest.mark.parametrize(
        "argv",
        [
            [
                "--ticker",
                "bad ticker!",
                "--date",
                "2026-09-14",
                "--analysts",
                "market",
                "--workspace",
                "/tmp",
                "--run-id",
                "r",
            ],
            [
                "--ticker",
                "NVDA",
                "--date",
                "2026/09/14",
                "--analysts",
                "market",
                "--workspace",
                "/tmp",
                "--run-id",
                "r",
            ],
            [
                "--ticker",
                "NVDA",
                "--date",
                "2026-09-14",
                "--analysts",
                "market,bogus",
                "--workspace",
                "/tmp",
                "--run-id",
                "r",
            ],
            [
                "--ticker",
                "NVDA",
                "--date",
                "2026-09-14",
                "--analysts",
                "",
                "--workspace",
                "/tmp",
                "--run-id",
                "r",
            ],
        ],
    )
    def test_invalid(self, runner, argv):
        with pytest.raises(runner.UsageError):
            runner.parse_args(argv)

    def test_analysts_normalized_order(self, runner, tmp_path):
        args = runner.parse_args(
            [
                "--ticker",
                "NVDA",
                "--date",
                "2026-09-14",
                "--analysts",
                "news,market,market",
                "--workspace",
                str(tmp_path),
                "--run-id",
                "r",
            ]
        )
        assert args.analysts == ("market", "news")


class TestAtomicWrite:
    def test_concurrent_reader_never_sees_partial(self, runner, tmp_path):
        """AM-16:并发读 status.json 不解析失败。"""
        writer = runner.StatusWriter(
            tmp_path / "status.json", "r-x", "NVDA", "2026-09-14", ("market", "social", "news")
        )
        stop = threading.Event()
        errors: list[Exception] = []

        def reader():
            while not stop.is_set():
                try:
                    data = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
                    assert data["run_id"] == "r-x"
                except (FileNotFoundError, PermissionError):
                    # Windows 下 replace 瞬间的文件锁:管理台 read_status 同样按
                    # "读不到 → 保持上次值"处理(DESIGN §4.6),不属于半截解析
                    pass
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            for i in range(300):
                writer.write(phase="running", agents_done=i % 11)
                time.sleep(0.001)
        finally:
            stop.set()
        t.join(timeout=5)
        assert errors == []


class TestStatusSchema:
    def test_fields_exactly_spec_6_2(self, runner, tmp_path):
        writer = runner.StatusWriter(
            tmp_path / "status.json", "r-1", "1810.HK", "2026-09-14", ("market", "social")
        )
        writer.write(phase="running", current_agent="Bear Researcher")
        data = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
        assert set(data) == {
            "run_id",
            "ticker",
            "date",
            "phase",
            "current_agent",
            "agents_done",
            "agents_total",
            "tokens_in",
            "tokens_out",
            "updated_at",
            "error",
        }
        assert data["updated_at"].endswith("+08:00")
        assert data["agents_total"] == 10
