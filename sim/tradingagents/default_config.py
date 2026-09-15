"""sim 版 DEFAULT_CONFIG:与真实 default_config.py 同名键,值指向 ~/.tradingagents。"""

from __future__ import annotations

import os

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")


def _coerce_bool(raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in _BOOL_TRUE:
        return True
    if normalized in _BOOL_FALSE:
        return False
    raise ValueError(f"expected a boolean, got {raw!r}")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return _coerce_bool(raw)


DEFAULT_CONFIG = {
    "project_dir": os.path.abspath(os.path.dirname(__file__)),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    "memory_log_path": os.getenv(
        "TRADINGAGENTS_MEMORY_LOG_PATH",
        os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md"),
    ),
    "llm_provider": os.getenv("TRADINGAGENTS_LLM_PROVIDER", "openai"),
    "deep_think_llm": "sim-model",
    "quick_think_llm": "sim-model",
    "backend_url": os.getenv("TRADINGAGENTS_LLM_BACKEND_URL") or None,
    "checkpoint_enabled": _env_bool("TRADINGAGENTS_CHECKPOINT_ENABLED", False),
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    "output_language": "English",
}
