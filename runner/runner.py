#!/usr/bin/env python
"""Agents-Manage 执行容器内 runner(SPEC §6.1 / DESIGN §6)。

合并 TradingAgents 两条执行路径(SPEC §1.3):
- CLI 流式路径:`graph.graph.stream()` 逐 chunk 更新 agent 状态 + 写 `reports/*.md`
- propagate() 收尾路径:`_log_state` + `memory_log.store_decision` + 清断点

仅依赖标准库 + 镜像内已安装的 tradingagents(cli.stats_handler 可选)。
退出码:0 成功 / 1 失败 / 2 自检失败 / 130 SIGINT。

用法:
  python /runner.py --ticker 1810.HK --date 2026-09-14 \
      --analysts market,social,news --workspace /ws --run-id r-xxx
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SH_TZ = ZoneInfo("Asia/Shanghai")

ANALYSTS = ("market", "social", "news", "fundamentals")
# ---- 以下常量照抄上游 cli/main.py(tests/shape 与 vendor 对等校验)----
FIXED_AGENTS = (
    "Bull Researcher",
    "Bear Researcher",
    "Research Manager",
    "Trader",
    "Aggressive Analyst",
    "Neutral Analyst",
    "Conservative Analyst",
    "Portfolio Manager",
)
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
# section -> (analyst_key | None, finalizing_agent)
REPORT_SECTIONS = {
    "market_report": ("market", "Market Analyst"),
    "sentiment_report": ("social", "Sentiment Analyst"),
    "news_report": ("news", "News Analyst"),
    "fundamentals_report": ("fundamentals", "Fundamentals Analyst"),
    "investment_plan": (None, "Research Manager"),
    "trader_investment_plan": (None, "Trader"),
    "final_trade_decision": (None, "Portfolio Manager"),
}

# SPEC §6.1 步骤 1:启动自检的 8 个上游属性
REQUIRED_ATTRS = (
    "_resolve_pending_entries",
    "begin_checkpoint",
    "checkpoint_input",
    "propagator",
    "_log_state",
    "memory_log",  # .store_decision 在运行期二次校验
    "clear_checkpoint_on_success",
    "end_checkpoint",
)

TICKER_RE = re.compile(r"^[A-Za-z0-9.\-^=]{1,16}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class UsageError(Exception):
    """参数非法(退出码 1,status.error=invalid_args)。"""


class SelfCheckError(Exception):
    """上游 API 缺属性(退出码 2,error=upstream_api_changed)。"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TradingAgents headless runner")
    p.add_argument("--ticker", required=True)
    p.add_argument("--date", required=True)
    p.add_argument("--analysts", required=True, help="逗号分隔,⊆ market,social,news,fundamentals")
    p.add_argument("--workspace", required=True)
    p.add_argument("--run-id", required=True, dest="run_id")
    args = p.parse_args(argv)

    if not TICKER_RE.match(args.ticker):
        raise UsageError(f"非法 ticker:{args.ticker!r}")
    if not DATE_RE.match(args.date):
        raise UsageError(f"非法 date:{args.date!r}(应为 YYYY-MM-DD)")
    items = [x.strip() for x in args.analysts.split(",") if x.strip()]
    unknown = [x for x in items if x not in ANALYSTS]
    if unknown or not items:
        raise UsageError(f"非法 analysts:{args.analysts!r}")
    seen: list[str] = []
    for a in ANALYSTS:  # 按 ANALYSTS 顺序归一
        if a in items and a not in seen:
            seen.append(a)
    args.analysts = tuple(seen)
    args.workspace_path = Path(args.workspace)
    args.workspace_path.mkdir(parents=True, exist_ok=True)
    return args


class StatusWriter:
    """原子写 <workspace>/status.json(SPEC §6.2 schema 逐字段一致)。"""

    def __init__(self, path: Path, run_id: str, ticker: str, date: str, analysts: tuple[str, ...]):
        self.path = path
        self.base = {
            "run_id": run_id,
            "ticker": ticker,
            "date": date,
            "phase": "starting",
            "current_agent": None,
            "agents_done": 0,
            "agents_total": len(analysts) + len(FIXED_AGENTS),
            "tokens_in": 0,
            "tokens_out": 0,
            "updated_at": None,
            "error": None,
        }

    def write(self, **overrides) -> None:
        self.base.update(overrides)
        self.base["updated_at"] = datetime.now(SH_TZ).isoformat(timespec="seconds")
        tmp = self.path.with_suffix(".json.tmp")
        data = json.dumps(self.base, ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # Windows:目标正被并发读取(未带 FILE_SHARE_DELETE)时 replace 抛
        # PermissionError,短暂重试;POSIX 上 rename 原子且不受影响
        for attempt in range(20):
            try:
                os.replace(tmp, self.path)  # 原子替换:并发读只见完整文件(AM-16)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.005)

    @property
    def state(self) -> dict:
        return dict(self.base)


class StatsLoader:
    """token 统计 callback:优先镜像内 cli.stats_handler,缺失则静默为 0(R-RUN-05)。"""

    def __init__(self):
        self.handler = None
        try:
            from cli.stats_handler import StatsCallbackHandler  # type: ignore

            self.handler = StatsCallbackHandler()
        except Exception:
            self.handler = None

    def tokens(self) -> tuple[int, int]:
        if self.handler is None:
            return 0, 0
        try:
            stats = self.handler.get_stats()
            return int(stats.get("tokens_in", 0)), int(stats.get("tokens_out", 0))
        except Exception:
            return 0, 0


class Tracker:
    """chunk → agent 状态 / reports/*.md 映射,照抄 cli/main.py run_analysis。"""

    def __init__(self, graph, analysts: tuple[str, ...], reports_dir: Path):
        self.graph = graph
        self.selected = list(analysts)
        self.reports_dir = reports_dir
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.agent_status: dict[str, str] = {}
        for key in self.selected:
            self.agent_status[ANALYST_AGENT[key]] = "pending"
        for agent in FIXED_AGENTS:
            self.agent_status[agent] = "pending"
        self.sections: dict[str, str | None] = {}
        for section, (analyst_key, _) in REPORT_SECTIONS.items():
            if analyst_key is None or analyst_key in self.selected:
                self.sections[section] = None

    # -- 状态与产物 --
    @property
    def current_agent(self) -> str | None:
        for agent, status in self.agent_status.items():
            if status == "in_progress":
                return agent
        for agent, status in self.agent_status.items():
            if status == "pending":
                return agent
        return None

    @property
    def agents_done(self) -> int:
        return sum(1 for s in self.agent_status.values() if s == "completed")

    def _update_status(self, agent: str, status: str) -> None:
        if agent in self.agent_status:
            self.agent_status[agent] = status

    def _write_section(self, section: str, content) -> None:
        if section not in self.sections:
            return
        if content is None:
            return
        text = "\n".join(str(x) for x in content) if isinstance(content, list) else str(content)
        if not text.strip():
            return
        self.sections[section] = text
        # 与 CLI save_report_section_decorator 一致:每次更新覆盖写该 section 文件
        # (investment_plan 在辩论期间是增量内容,最终由 final_state 覆盖)
        with open(self.reports_dir / f"{section}.md", "w", encoding="utf-8") as f:
            f.write(text)

    def consume(self, chunk: dict) -> None:
        # 1) 分析师报告 → 分析师状态(cli update_analyst_statuses)
        found_active = False
        for key in ANALYSTS:
            if key not in self.selected:
                continue
            agent = ANALYST_AGENT[key]
            report_key = ANALYST_REPORT[key]
            if chunk.get(report_key):
                self._write_section(report_key, chunk[report_key])
            has_report = bool(self.sections.get(report_key))
            if has_report:
                self._update_status(agent, "completed")
            elif not found_active:
                self._update_status(agent, "in_progress")
                found_active = True
            else:
                self._update_status(agent, "pending")
        if not found_active and self.selected and self.agent_status.get("Bull Researcher") == "pending":
            self._update_status("Bull Researcher", "in_progress")

        # 2) Research Team 辩论
        debate = chunk.get("investment_debate_state")
        if debate:
            bull = str(debate.get("bull_history", "")).strip()
            bear = str(debate.get("bear_history", "")).strip()
            judge = str(debate.get("judge_decision", "")).strip()
            if bull or bear:
                for agent in ("Bull Researcher", "Bear Researcher", "Research Manager"):
                    if self.agent_status.get(agent) == "pending":
                        self._update_status(agent, "in_progress")
            if bull:
                self._write_section("investment_plan", f"### Bull Researcher Analysis\n{bull}")
            if bear:
                self._write_section("investment_plan", f"### Bear Researcher Analysis\n{bear}")
            if judge:
                self._write_section("investment_plan", f"### Research Manager Decision\n{judge}")
                for agent in ("Bull Researcher", "Bear Researcher", "Research Manager"):
                    self._update_status(agent, "completed")
                self._update_status("Trader", "in_progress")

        # 3) Trading Team
        if chunk.get("trader_investment_plan"):
            self._write_section("trader_investment_plan", chunk["trader_investment_plan"])
            if self.agent_status.get("Trader") != "completed":
                self._update_status("Trader", "completed")
                self._update_status("Aggressive Analyst", "in_progress")

        # 4) Risk Management 辩论
        risk = chunk.get("risk_debate_state")
        if risk:
            agg = str(risk.get("aggressive_history", "")).strip()
            con = str(risk.get("conservative_history", "")).strip()
            neu = str(risk.get("neutral_history", "")).strip()
            judge = str(risk.get("judge_decision", "")).strip()
            if agg and self.agent_status.get("Aggressive Analyst") != "completed":
                self._update_status("Aggressive Analyst", "in_progress")
            if con and self.agent_status.get("Conservative Analyst") != "completed":
                self._update_status("Conservative Analyst", "in_progress")
            if neu and self.agent_status.get("Neutral Analyst") != "completed":
                self._update_status("Neutral Analyst", "in_progress")
            if agg:
                self._write_section("final_trade_decision", f"### Aggressive Analyst Analysis\n{agg}")
            if con:
                self._write_section("final_trade_decision", f"### Conservative Analyst Analysis\n{con}")
            if neu:
                self._write_section("final_trade_decision", f"### Neutral Analyst Analysis\n{neu}")
            if judge and self.agent_status.get("Portfolio Manager") != "completed":
                self._update_status("Portfolio Manager", "in_progress")
                self._write_section("final_trade_decision", f"### Portfolio Manager Decision\n{judge}")
                for agent in (
                    "Aggressive Analyst",
                    "Neutral Analyst",
                    "Conservative Analyst",
                    "Portfolio Manager",
                ):
                    self._update_status(agent, "completed")


def self_check(graph) -> None:
    missing = [attr for attr in REQUIRED_ATTRS if not hasattr(graph, attr)]
    if missing:
        raise SelfCheckError(f"missing upstream attrs: {','.join(missing)}")
    if not hasattr(graph.memory_log, "store_decision"):
        raise SelfCheckError("missing upstream attrs: memory_log.store_decision")


def build_graph(analysts: tuple[str, ...], stats):
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = DEFAULT_CONFIG.copy()  # 继承容器 env;不传任何模型参数
    callbacks = [stats.handler] if stats.handler is not None else None
    return TradingAgentsGraph(selected_analysts=analysts, debug=False, config=config, callbacks=callbacks)


def run(args: argparse.Namespace) -> int:
    status = StatusWriter(
        args.workspace_path / "status.json", args.run_id, args.ticker, args.date, args.analysts
    )
    stats = StatsLoader()
    analysts = args.analysts

    status.write(phase="starting")
    graph = None
    try:
        graph = build_graph(analysts, stats)
        self_check(graph)  # 步骤 1
        graph.ticker = args.ticker  # propagate() 的副作用,_log_state 依赖

        results_dir = Path(str(graph.config["results_dir"]))
        reports_dir = results_dir / args.ticker / args.date / "reports"
        tracker = Tracker(graph, analysts, reports_dir)

        ticker, date = args.ticker, args.date

        # 步骤 2~4:保持 propagate() 的记忆反思语义
        graph._resolve_pending_entries(ticker)
        past_context = graph.memory_log.get_past_context(ticker, as_of=graph._memory_as_of(date))
        instrument_context = graph.resolve_instrument_context(ticker, "stock")
        state0 = graph.propagator.create_initial_state(
            ticker,
            date,
            asset_type="stock",
            past_context=past_context,
            instrument_context=instrument_context,
        )
        # CLI:构造器挂 LLM 统计;graph_args 挂工具执行统计
        args_d = graph.propagator.get_graph_args(
            callbacks=[stats.handler] if stats.handler is not None else None
        )
        tid = graph.begin_checkpoint(ticker, date, "stock")
        if tid is not None:
            args_d.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = tid

        status.write(phase="running")
        trace: list[dict] = []
        for chunk in graph.graph.stream(graph.checkpoint_input(state0), **args_d):
            if not isinstance(chunk, dict):
                continue
            tracker.consume(chunk)
            trace.append(chunk)
            tokens_in, tokens_out = stats.tokens()
            status.write(
                current_agent=tracker.current_agent,
                agents_done=tracker.agents_done,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )

        # 与 CLI 一致:values 模式下逐 chunk 合并(等价取并集)
        final_state: dict = {}
        for chunk in trace:
            final_state.update(chunk)

        # propagate() 收尾三步(SPEC §6.1 步骤 6)
        graph._log_state(date, final_state)
        graph.memory_log.store_decision(
            ticker=ticker,
            trade_date=date,
            final_trade_decision=final_state["final_trade_decision"],
        )
        graph.clear_checkpoint_on_success(ticker, date, "stock")

        # 终版报告以 final_state 为准(CLI 的 final report sections 更新)
        for section in tracker.sections:
            if section in final_state and final_state[section]:
                tracker._write_section(section, final_state[section])
        for agent in tracker.agent_status:
            tracker._update_status(agent, "completed")
        tokens_in, tokens_out = stats.tokens()
        status.write(
            phase="succeeded",
            current_agent=None,
            agents_done=tracker.agents_done,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        return 0
    except SelfCheckError as e:
        status.write(phase="failed", error=f"upstream_api_changed: {e}")
        print(f"[runner] self-check failed: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # 不吞:落盘状态后以 130 退出;断点已由上游 persist,finally 收尾
        status.write(phase="running", error="interrupted")
        return 130
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {str(e)[:200]}"
        status.write(phase="failed", error=msg)
        print(f"[runner] failed: {msg}", file=sys.stderr)
        return 1
    finally:
        if graph is not None:
            try:
                graph.end_checkpoint()  # SPEC §6.1 步骤 7
            except Exception as e:  # noqa: BLE001
                print(f"[runner] end_checkpoint error: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except UsageError as e:
        print(f"[runner] {e}", file=sys.stderr)
        return 1
    t0 = time.monotonic()
    code = run(args)
    print(f"[runner] exit={code} elapsed={time.monotonic() - t0:.1f}s", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
