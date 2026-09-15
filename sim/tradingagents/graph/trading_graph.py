"""sim 版 TradingAgentsGraph:8 个上游属性与真实包同形,行为由 SIM_MODE 控制。

SIM_MODE ∈ ok | fail | hang | slow | no_memory | no_report | upstream_changed | corrupt_status
SIM_STEP_SECONDS:每节点 sleep 秒数(slow 默认 30,其余默认 0)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.propagation import Propagator

ANALYST_ORDER = ["market", "social", "news", "fundamentals"]
ANALYST_AGENT = {
    "market": "Market Analyst",
    "social": "Sentiment Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}
ANALYST_REPORT = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}


def safe_ticker_component(value: str, *, max_len: int = 32) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("ticker component must be a non-empty string")
    if not re.fullmatch(r"[A-Za-z0-9._\-^=]{1,16}", value) or len(value) > max_len:
        raise ValueError(f"unsafe ticker component: {value!r}")
    return value


def _sim_mode() -> str:
    return os.environ.get("SIM_MODE", "ok")


def _step_seconds() -> float:
    if _sim_mode() == "slow":
        return float(os.environ.get("SIM_SLOW_SECONDS", "30"))
    return float(os.environ.get("SIM_STEP_SECONDS", "0"))


def _corrupt_workspace_status_once() -> None:
    """corrupt_status 模式:向 /ws/status.json 写一次半截 JSON(AM-08 注入)。"""
    if _sim_mode() != "corrupt_status":
        return
    ws = Path("/ws")
    if not ws.is_dir():
        return
    try:
        with open(ws / "status.json", "w", encoding="utf-8") as f:
            f.write('{"run_id": "r-broken", "phase": "run')  # 半截,无闭合
    except OSError:
        pass
    time.sleep(2)  # 给管理台一个可轮询到的损坏窗口


def thread_id(ticker: str, date: str, signature: str = "") -> str:
    base = f"{ticker.upper()}:{date}"
    if signature:
        base = f"{base}:{signature}"
    return hashlib.sha256(base.encode()).hexdigest()[:16]


class _SimCheckpointer:
    """用单个 json 文件模拟 LangGraph SqliteSaver 的断点续跑。"""

    def __init__(self, data_cache_dir: str, ticker: str):
        safe = safe_ticker_component(ticker).upper()
        d = Path(data_cache_dir) / "checkpoints"
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"{safe}.json"

    def load(self, tid: str) -> int | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if data.get("thread_id") != tid:
            return None
        return int(data.get("step", 0))

    def save(self, tid: str, step: int) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"thread_id": tid, "step": step}), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self, tid: str) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("thread_id") == tid:
                    self.path.unlink()
            except (OSError, ValueError):
                self.path.unlink()


class _CompiledGraph:
    """stream(stream_mode='values') 逐节点 yield 累积状态,与真实图同形。"""

    def __init__(self, outer: TradingAgentsGraph):
        self._outer = outer

    def stream(self, graph_input, **kwargs):
        return self._outer._stream(graph_input, **kwargs)


class TradingAgentsGraph:
    def __init__(
        self,
        selected_analysts=("market", "social", "news", "fundamentals"),
        debug=False,
        config: dict[str, Any] | None = None,
        callbacks: list | None = None,
    ):
        self.debug = debug
        self.config = config or DEFAULT_CONFIG
        if self.config is DEFAULT_CONFIG:
            self.config = DEFAULT_CONFIG.copy()
        self.callbacks = callbacks or []
        os.makedirs(self.config["data_cache_dir"], exist_ok=True)
        os.makedirs(self.config["results_dir"], exist_ok=True)
        self.memory_log = TradingMemoryLog(self.config)
        self.propagator = Propagator(max_recur_limit=self.config.get("max_recur_limit", 100))
        self.curr_state = None
        self.ticker = None
        self.log_states_dict: dict[str, dict] = {}
        self.selected_analysts = tuple(selected_analysts)
        self._checkpointer_ctx = None
        self._resuming = False
        self._cp = None
        self._cp_tid: str | None = None
        self.graph = _CompiledGraph(self)
        if _sim_mode() == "upstream_changed" and hasattr(TradingAgentsGraph, "_log_state"):
            # 响亮失败注入:让 runner 的 8 属性自检发现缺口
            delattr(TradingAgentsGraph, "_log_state")

    # ---- 8 个契约属性 ----

    def _resolve_pending_entries(self, ticker: str) -> None:
        """记忆反思(轻量模拟):把同标的 pending 条目改写为 resolved。"""
        pending = [e for e in self.memory_log.get_pending_entries() if e["ticker"] == ticker]
        if not pending or not self.memory_log._log_path:
            return
        p = Path(str(self.memory_log._log_path))
        text = p.read_text(encoding="utf-8")
        for entry in pending:
            text = text.replace(
                f"[{entry['date']} | {entry['ticker']} | {entry['rating']} | pending]",
                f"[{entry['date']} | {entry['ticker']} | {entry['rating']} |"
                f" resolved:raw=+1.2%,alpha=+0.4%,holding_days=5,resolved={entry['date']}]",
                1,
            )
        p.write_text(text, encoding="utf-8")

    def resolve_instrument_context(self, ticker: str, asset_type: str = "stock") -> str:
        return f"[SIM] {ticker} ({asset_type}) instrument context"

    def _memory_as_of(self, trade_date) -> str | None:
        td = str(trade_date)
        return td if td < datetime.now().strftime("%Y-%m-%d") else None

    def _run_signature(self, asset_type: str) -> str:
        return "|".join(
            [
                "analysts=" + ",".join(self.selected_analysts),
                f"debate={self.config['max_debate_rounds']}",
                f"risk={self.config['max_risk_discuss_rounds']}",
                f"asset={asset_type}",
            ]
        )

    def begin_checkpoint(self, company_name, trade_date, asset_type: str = "stock") -> str | None:
        self._resuming = False
        if not self.config.get("checkpoint_enabled"):
            return None
        signature = self._run_signature(asset_type)
        self._cp = _SimCheckpointer(self.config["data_cache_dir"], company_name)
        self._cp_tid = thread_id(company_name, str(trade_date), signature)
        step = self._cp.load(self._cp_tid)
        self._resuming = step is not None
        return self._cp_tid

    def checkpoint_input(self, init_state):
        return None if self._resuming else init_state

    def end_checkpoint(self):
        self._cp = None
        self._cp_tid = None
        self._resuming = False

    def clear_checkpoint_on_success(self, company_name, trade_date, asset_type: str = "stock"):
        if self.config.get("checkpoint_enabled") and self._cp is not None and self._cp_tid:
            self._cp.clear(self._cp_tid)

    def _log_state(self, trade_date, final_state) -> None:
        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state.get("company_of_interest"),
            "trade_date": final_state.get("trade_date"),
            "market_report": final_state.get("market_report", ""),
            "sentiment_report": final_state.get("sentiment_report", ""),
            "news_report": final_state.get("news_report", ""),
            "fundamentals_report": final_state.get("fundamentals_report", ""),
            "investment_debate_state": final_state.get("investment_debate_state", {}),
            "trader_investment_decision": final_state.get("trader_investment_plan", ""),
            "risk_debate_state": final_state.get("risk_debate_state", {}),
            "investment_plan": final_state.get("investment_plan", ""),
            "final_trade_decision": final_state.get("final_trade_decision", ""),
        }
        safe = safe_ticker_component(self.ticker or "")
        directory = Path(self.config["results_dir"]) / safe / "TradingAgentsStrategy_logs"
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / f"full_states_log_{trade_date}.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(self.log_states_dict[str(trade_date)], f, indent=4)

    def process_signal(self, full_signal: str) -> str:
        m = re.search(r"\b(BUY|SELL|HOLD|WAIT)\b", (full_signal or "").upper())
        return m.group(1) if m else "HOLD"

    # ---- 仿真执行 ----

    def _plan(self) -> list[tuple[str, dict]]:
        """节点计划:每项 (tag, delta);yield 时与累积状态合并。"""
        ticker = self.ticker or "SIM"
        date = str(self.curr_state.get("trade_date", "")) if self.curr_state else ""
        plan: list[tuple[str, dict]] = []
        for key in ANALYST_ORDER:
            if key not in self.selected_analysts:
                continue
            plan.append(
                (
                    f"analyst:{key}",
                    {
                        ANALYST_REPORT[key]: f"[SIM] {ANALYST_AGENT[key]} report for {ticker} on {date}",
                    },
                )
            )
        if _sim_mode() != "no_report":
            plan.append(
                (
                    "debate:bull",
                    {
                        "investment_debate_state": {
                            "bull_history": f"[SIM] Bull case for {ticker}",
                            "count": 1,
                        }
                    },
                )
            )
            plan.append(
                (
                    "debate:bear",
                    {
                        "investment_debate_state": {
                            "bull_history": f"[SIM] Bull case for {ticker}",
                            "bear_history": f"[SIM] Bear case for {ticker}",
                            "count": 2,
                        }
                    },
                )
            )
            plan.append(
                (
                    "debate:judge",
                    {
                        "investment_debate_state": {
                            "bull_history": f"[SIM] Bull case for {ticker}",
                            "bear_history": f"[SIM] Bear case for {ticker}",
                            "judge_decision": f"[SIM] Research Manager decision for {ticker}",
                            "count": 2,
                        },
                        "investment_plan": f"[SIM] Research team investment plan for {ticker}",
                    },
                )
            )
        plan.append(
            (
                "trader",
                {
                    "trader_investment_plan": f"[SIM] Trader plan for {ticker}",
                },
            )
        )
        plan.append(
            (
                "risk:aggressive",
                {"risk_debate_state": {"aggressive_history": f"[SIM] Aggressive view {ticker}"}},
            )
        )
        plan.append(
            (
                "risk:conservative",
                {
                    "risk_debate_state": {
                        "aggressive_history": f"[SIM] Aggressive view {ticker}",
                        "conservative_history": f"[SIM] Conservative view {ticker}",
                    }
                },
            )
        )
        plan.append(
            (
                "risk:neutral",
                {
                    "risk_debate_state": {
                        "aggressive_history": f"[SIM] Aggressive view {ticker}",
                        "conservative_history": f"[SIM] Conservative view {ticker}",
                        "neutral_history": f"[SIM] Neutral view {ticker}",
                    }
                },
            )
        )
        plan.append(
            (
                "risk:judge",
                {
                    "risk_debate_state": {
                        "aggressive_history": f"[SIM] Aggressive view {ticker}",
                        "conservative_history": f"[SIM] Conservative view {ticker}",
                        "neutral_history": f"[SIM] Neutral view {ticker}",
                        "judge_decision": f"[SIM] Portfolio Manager decision for {ticker}",
                    },
                    "final_trade_decision": f"[SIM] FINAL DECISION: HOLD {ticker} on {date}",
                },
            )
        )
        return plan

    def _stream(self, graph_input, **kwargs):
        mode = _sim_mode()
        step_seconds = _step_seconds()
        if graph_input is not None:
            self.curr_state = dict(graph_input)
        state: dict[str, Any] = dict(self.curr_state or {})
        _corrupt_workspace_status_once()
        plan = self._plan()
        start_at = 0
        if self._cp is not None and self._cp_tid:
            done = self._cp.load(self._cp_tid)
            if done is not None:
                start_at = done  # 断点续跑:跳过已完成节点
        for index, (tag, delta) in enumerate(plan):
            if index < start_at:
                continue
            if step_seconds > 0:
                time.sleep(step_seconds)
            if mode == "fail" and index == 2:
                raise RuntimeError(f"sim injected failure at node {index + 1} ({tag})")
            if mode == "hang" and index == 2:
                time.sleep(3600)  # SIGINT 可中断
            for cb in self.callbacks:  # 模拟真实 LLM 调用的 token 统计回调
                try:
                    cb.on_llm_end(None)
                except Exception:
                    pass
            state = {**state, **delta}
            if self._cp is not None and self._cp_tid:
                self._cp.save(self._cp_tid, index + 1)  # 节点完成后持久化断点
            yield dict(state)
