"""sim 版 TradingMemoryLog:与真实 memory.py 同形(追加 [date | ticker | rating | pending])。"""

from __future__ import annotations

import os
import re
from pathlib import Path

_SEPARATOR = "\n\n<!-- ENTRY_END -->\n\n"


class TradingMemoryLog:
    def __init__(self, config: dict):
        self.config = config
        self._log_path = config.get("memory_log_path")

    def store_decision(self, ticker: str, trade_date: str, final_trade_decision: str) -> None:
        """追加 pending 条目。SIM_MODE=no_memory 时跳过。"""
        if os.environ.get("SIM_MODE") == "no_memory" or not self._log_path:
            return
        if self._has_entry(trade_date, ticker):
            return
        rating = self._parse_rating(final_trade_decision)
        tag = f"[{trade_date} | {ticker} | {rating} | pending]"
        entry = f"{tag}\n\nDECISION:\n{final_trade_decision}{_SEPARATOR}"
        path = Path(self._log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)

    def _has_entry(self, trade_date: str, ticker: str) -> bool:
        p = Path(self._log_path)  # type: ignore[arg-type]
        if not p.exists():
            return False
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"[{trade_date} | {ticker} |") and line.endswith("| pending]"):
                return True
        return False

    @staticmethod
    def _parse_rating(decision: str) -> str:
        m = re.search(r"\b(BUY|BUY_MARKER|STRONG_BUY|SELL|SHORT|HOLD|WAIT)\b", (decision or "").upper())
        return m.group(1) if m else "HOLD"

    # ---- 供 runner 调用的读取接口(与真实包同形)----
    def load_entries(self) -> list[dict]:
        p = Path(str(self._log_path))
        if not self._log_path or not p.exists():
            return []
        text = p.read_text(encoding="utf-8")
        entries = []
        for raw in text.split(_SEPARATOR):
            raw = raw.strip()
            if not raw:
                continue
            first = raw.splitlines()[0]
            m = re.match(r"\[([^|]+) \| ([^|]+) \| ([^|]+) \| (.+)\]$", first.strip())
            if not m:
                continue
            entries.append(
                {
                    "date": m.group(1).strip(),
                    "ticker": m.group(2).strip(),
                    "rating": m.group(3).strip(),
                    "pending": "pending" in m.group(4),
                    "decision": raw,
                }
            )
        return entries

    def get_pending_entries(self) -> list[dict]:
        return [e for e in self.load_entries() if e.get("pending")]

    def get_past_context(
        self, ticker: str, n_same: int = 5, n_cross: int = 3, as_of: str | None = None
    ) -> str:
        entries = [e for e in self.load_entries() if not e.get("pending")]
        if as_of is not None:
            entries = [e for e in entries if e.get("resolved") and e["resolved"] <= as_of]
        same = [e for e in entries if e["ticker"] == ticker][:n_same]
        cross = [e for e in entries if e["ticker"] != ticker][:n_cross]
        if not same and not cross:
            return ""
        return "\n".join(f"[{e['date']} {e['ticker']}] {e['rating']}" for e in same + cross)
