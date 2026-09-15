"""常量与校验的唯一来源(SPEC §5 / DESIGN §4.1)。

Web/API/调度器/服务层共用本模块;禁止在别处出现第二份拷贝
(见 CLAUDE.md 红线 7)。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

SH_TZ = ZoneInfo("Asia/Shanghai")

MARKETS = ("us", "hk", "cn")
ANALYSTS = ("market", "social", "news", "fundamentals")
FIXED_AGENTS = (
    "Bull Researcher",
    "Bear Researcher",
    "Research Manager",
    "Trader",
    "Aggressive Analyst",
    "Conservative Analyst",
    "Neutral Analyst",
    "Portfolio Manager",
)
ANALYST_AGENT = {
    "market": "Market Analyst",
    "social": "Social Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}
ANALYST_LABEL = {"market": "技术", "social": "舆情", "news": "新闻", "fundamentals": "基本面"}
RUN_STATUS = ("queued", "running", "succeeded", "failed", "cancelled")
TRIGGERS = ("web", "api", "schedule")

# 代码格式与 yfinance 约定一致:us 裸代码、hk ####.HK、cn ######.SS/.SZ
_CODE_RE = {
    "us": re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$"),
    "hk": re.compile(r"^\d{4}\.HK$"),
    "cn": re.compile(r"^\d{6}\.(SS|SZ)$"),
}

_MARKET_MSG = {
    "us": "us 市场代码应为裸代码,如 NVDA、BRK.B",
    "hk": "hk 市场代码应为 4 位数字加 .HK,如 1810.HK",
    "cn": "cn 市场代码应为 6 位数字加 .SS 或 .SZ,如 600519.SS、000001.SZ",
}


def now_sh() -> datetime:
    """当前 Asia/Shanghai 时间(带时区)。"""
    return datetime.now(SH_TZ)


def today_sh() -> date:
    return now_sh().date()


def agents_total(analysts: Sequence[str]) -> int:
    """len(analysts) + 8(见 SPEC §4)。"""
    return len(analysts) + len(FIXED_AGENTS)


def agent_sequence(analysts: Sequence[str]) -> list[str]:
    """时间线顺序:分析师(按 ANALYSTS 顺序)+ FIXED_AGENTS。"""
    items = tuple(analysts)
    missing = [a for a in items if a not in ANALYSTS]
    if missing:
        raise ValueError(f"非法分析师: {','.join(missing)};合法值 {','.join(ANALYSTS)}")
    ordered = [a for a in ANALYSTS if a in set(items)]
    return [ANALYST_AGENT[a] for a in ordered] + list(FIXED_AGENTS)


def normalize_code(market: str, raw: str) -> str:
    """去空白、大写;hk 数字部分补零到 4 位。"""
    if market not in MARKETS:
        raise ValueError(f"未知市场 {market!r};合法值 {','.join(MARKETS)}")
    code = raw.strip().upper()
    if market == "hk":
        head, sep, tail = code.partition(".")
        if sep and tail in ("HK", "") and head.isdigit():
            code = f"{head.zfill(4)}.HK"
    return code


def validate_code(market: str, code: str) -> None:
    if market not in MARKETS:
        raise ValueError(f"未知市场 {market!r};合法值 {','.join(MARKETS)}")
    if not _CODE_RE[market].match(code):
        raise ValueError(_MARKET_MSG[market])


def parse_analysts(csv: str) -> tuple[str, ...]:
    """去重、按 ANALYSTS 顺序;非法或空 raise ValueError。"""
    items = [x.strip() for x in csv.split(",") if x.strip()]
    unknown = [x for x in items if x not in ANALYSTS]
    if unknown:
        raise ValueError(f"非法分析师 {','.join(unknown)};合法值 {','.join(ANALYSTS)}")
    chosen = {x for x in items}
    if not chosen:
        raise ValueError("分析师集合不能为空")
    return tuple(a for a in ANALYSTS if a in chosen)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_date(s: str) -> str:
    """YYYY-MM-DD 且不晚于今天(Asia/Shanghai);返回校验后的字符串。"""
    if not _DATE_RE.match(s or ""):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    try:
        d = date.fromisoformat(s)
    except ValueError as e:
        raise ValueError("日期格式应为 YYYY-MM-DD") from e
    if d > today_sh():
        raise ValueError("分析日期不能晚于今天")
    return s


def make_run_id(code: str, now: datetime) -> str:
    """r-YYYYMMDD-HHMMSS-<code 去掉非字母数字>(R-SVC-10)。"""
    stamp = now.astimezone(SH_TZ).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9]", "", code)
    return f"r-{stamp}-{slug}"
