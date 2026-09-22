"""本系统标的代码 ↔ reports-fetcher 证券代码的映射。

本系统(app.models):us `NVDA`、hk `1810.HK`、cn `600519.SS/.SZ`。
reports-fetcher:us `NVDA`、hk `1810.HK`(服务端规范化为 5 位 `01810`)、cn `600519`(6 位数字)。
代码格式校验仍由 app.models.validate_code 承担(唯一来源);本模块只做形态转换。
"""

from __future__ import annotations

from app import models

FETCHER_MARKET = {"us": "US", "hk": "HK", "cn": "CN"}
LOCAL_MARKET = {v: k for k, v in FETCHER_MARKET.items()}


def to_fetcher_symbol(market: str, code: str) -> str:
    """提交任务用的 symbol。"""
    code = models.normalize_code(market, code)
    models.validate_code(market, code)
    if market == "cn":
        return code.split(".", 1)[0]
    return code


def archive_symbol(market: str, code: str) -> str:
    """归档查询(`GET /reports?symbol=`)用的 symbol:与服务端规范化结果一致。"""
    sym = to_fetcher_symbol(market, code)
    if market == "hk":
        return sym.split(".", 1)[0].zfill(5)
    return sym


def resolve(code: str, market: str | None = None) -> tuple[str, str]:
    """(market, normalized_code);market 缺省按代码形态推断。非法 raise ValueError。"""
    if not market:
        market = models.code_market(code)
        if market is None:
            raise ValueError("代码格式无法识别:美股裸代码 / 港股 ####.HK / A股 ######.SS|.SZ")
    normalized = models.normalize_code(market, code)
    models.validate_code(market, normalized)
    return market, normalized
