"""契约对等测试(R-RUN-09 / AM-11):vendor(真实源码)vs sim vs runner vs app.models。

vendor 的 tradingagents 依赖 langgraph/langchain/yfinance,本地 venv 未安装,
故对 vendor 一侧用 AST 静态解析;sim 一侧可直接 import(纯标准库)。
缺 vendor/(gitignore)时整体 skip:`scripts/fetch_vendor.sh` 后重跑。
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VENDOR = REPO / "vendor"
SIM = REPO / "sim"
RUNNER = REPO / "runner" / "runner.py"

pytestmark = [
    pytest.mark.shape,
    pytest.mark.skipif(
        not (VENDOR / "tradingagents").is_dir(),
        reason="缺少 vendor/(从 NAS 只读拷贝:scripts/fetch_vendor.sh)",
    ),
]


# ---------- AST 工具(解析 vendor,不执行其代码) ----------


def _ast_methods(tree: ast.AST, class_name: str) -> dict[str, ast.FunctionDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {n.name: n for n in node.body if isinstance(n, ast.FunctionDef)}
    raise AssertionError(f"vendor 中找不到类 {class_name}")


def _ast_signature(method: ast.FunctionDef) -> list[tuple[str, str | None]]:
    """(参数名, 默认值字面量);忽略注解,只比形状。"""
    params: list[tuple[str, str | None]] = []
    args = method.args
    posargs = args.posonlyargs + args.args
    defaults: list[ast.expr] = args.defaults
    offset = len(posargs) - len(defaults)
    for i, arg in enumerate(posargs):
        default = None
        if i >= offset:
            default = ast.unparse(defaults[i - offset])
        params.append((arg.arg, default))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        params.append((arg.arg, ast.unparse(default) if default is not None else None))
    return params


def _ast_constants(tree: ast.AST, names: set[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in names
        ):
            out[node.targets[0].id] = ast.literal_eval(node.value)
    return out


def _inspect_signature(func) -> list[tuple[str, str | None]]:
    params: list[tuple[str, str | None]] = []
    sig = inspect.signature(func)
    for p in sig.parameters.values():
        if p.name == "self":
            continue
        default = None
        if p.default is not inspect.Parameter.empty:
            default = repr(p.default)
            if default.startswith("'") or default.startswith('"'):
                default = f"'{p.default}'"  # 统一字符串字面量引号风格
        params.append((p.name, default))
    return params


# ---------- 被测对象加载 ----------


@pytest.fixture(scope="module")
def vendor_graph_ast():
    return ast.parse((VENDOR / "tradingagents" / "graph" / "trading_graph.py").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def vendor_memory_ast():
    return ast.parse(
        (VENDOR / "tradingagents" / "agents" / "utils" / "memory.py").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def vendor_propagation_ast():
    return ast.parse((VENDOR / "tradingagents" / "graph" / "propagation.py").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def vendor_cli_ast():
    return ast.parse((VENDOR / "cli" / "main.py").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sim_modules(tmp_path_factory):
    import os

    os.environ["TRADINGAGENTS_RESULTS_DIR"] = str(tmp_path_factory.mktemp("sim-results"))
    if str(SIM) not in sys.path:
        sys.path.insert(0, str(SIM))
    tg = importlib.import_module("tradingagents.graph.trading_graph")
    mem = importlib.import_module("tradingagents.agents.utils.memory")
    prop = importlib.import_module("tradingagents.graph.propagation")
    return tg, mem, prop


@pytest.fixture(scope="module")
def runner_module():
    spec = importlib.util.spec_from_file_location("shape_runner", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- SPEC §6.1 的 8 个属性:vendor vs sim ----------

GRAPH_METHODS = [
    "__init__",
    "_resolve_pending_entries",
    "begin_checkpoint",
    "checkpoint_input",
    "end_checkpoint",
    "clear_checkpoint_on_success",
    "_log_state",
]


class TestGraphShape:
    @pytest.mark.parametrize("method", GRAPH_METHODS)
    def test_method_shape(self, vendor_graph_ast, sim_modules, method):
        vendor_sig = _ast_signature(_ast_methods(vendor_graph_ast, "TradingAgentsGraph")[method])
        vendor_sig = vendor_sig[1:]  # 去 self
        sim_cls = sim_modules[0].TradingAgentsGraph
        sim_sig = _inspect_signature(getattr(sim_cls, method))
        assert vendor_sig == sim_sig, f"{method} 签名不一致:\nvendor={vendor_sig}\nsim={sim_sig}"

    def test_memory_store_decision(self, vendor_memory_ast, sim_modules):
        vendor_sig = _ast_signature(_ast_methods(vendor_memory_ast, "TradingMemoryLog")["store_decision"])[1:]
        sim_sig = _inspect_signature(sim_modules[1].TradingMemoryLog.store_decision)
        assert vendor_sig == sim_sig

    def test_memory_get_past_context(self, vendor_memory_ast, sim_modules):
        vendor_sig = _ast_signature(_ast_methods(vendor_memory_ast, "TradingMemoryLog")["get_past_context"])[
            1:
        ]
        sim_sig = _inspect_signature(sim_modules[1].TradingMemoryLog.get_past_context)
        assert vendor_sig == sim_sig

    @pytest.mark.parametrize("method", ["create_initial_state", "get_graph_args"])
    def test_propagator(self, vendor_propagation_ast, sim_modules, method):
        vendor_sig = _ast_signature(_ast_methods(vendor_propagation_ast, "Propagator")[method])[1:]
        sim_sig = _inspect_signature(getattr(sim_modules[2].Propagator, method))
        assert vendor_sig == sim_sig


# ---------- CLI 常量对等 ----------


class TestCliConstants:
    def test_models_constants_match_cli(self, vendor_cli_ast):
        from app import models

        consts = _ast_constants(vendor_cli_ast, {"FIXED_AGENTS", "ANALYST_MAPPING", "ANALYST_ORDER"})
        cli_fixed_flat = [a for team in consts["FIXED_AGENTS"].values() for a in team]
        assert list(models.FIXED_AGENTS) == cli_fixed_flat
        assert models.ANALYST_AGENT == consts["ANALYST_MAPPING"]
        assert list(models.ANALYSTS) == consts["ANALYST_ORDER"]

    def test_runner_constants_match_cli(self, vendor_cli_ast, runner_module):
        consts = _ast_constants(
            vendor_cli_ast,
            {
                "FIXED_AGENTS",
                "ANALYST_MAPPING",
                "REPORT_SECTIONS",
                "ANALYST_AGENT_NAMES",
                "ANALYST_REPORT_MAP",
            },
        )
        assert list(runner_module.FIXED_AGENTS) == [
            a for team in consts["FIXED_AGENTS"].values() for a in team
        ]
        assert runner_module.ANALYST_AGENT == consts["ANALYST_MAPPING"] == consts["ANALYST_AGENT_NAMES"]
        assert runner_module.REPORT_SECTIONS == consts["REPORT_SECTIONS"]
        assert runner_module.ANALYST_REPORT == consts["ANALYST_REPORT_MAP"]
