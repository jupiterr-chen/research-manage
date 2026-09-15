"""sim 版 cli.stats_handler:与真实镜像内同名模块同形(runner 可选依赖)。"""

from __future__ import annotations

import threading
from typing import Any


class StatsCallbackHandler:
    """计数器桩:LLM 调用与 token 用量(无真实 LLM,数值仅自增演示)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.llm_calls = 0
        self.tool_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def on_llm_start(self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any) -> None:
        with self._lock:
            self.llm_calls += 1

    def on_chat_model_start(self, serialized: dict[str, Any], messages: list, **kwargs: Any) -> None:
        with self._lock:
            self.llm_calls += 1

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        with self._lock:
            self.tokens_in += 120
            self.tokens_out += 60

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, **kwargs: Any) -> None:
        with self._lock:
            self.tool_calls += 1

    def get_stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
            }
