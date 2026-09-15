"""app/models.py 单测(R-FND-07 / AM-17 / AM-16)。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app import models


class TestConstants:
    def test_markets_analysts(self):
        assert models.MARKETS == ("us", "hk", "cn")
        assert models.ANALYSTS == ("market", "social", "news", "fundamentals")
        assert len(models.FIXED_AGENTS) == 8

    def test_agents_total(self):
        # AM-16:3 → 11,4 → 12(SPEC §4)
        assert models.agents_total(("market", "social", "news")) == 11
        assert models.agents_total(models.ANALYSTS) == 12
        assert models.agents_total([]) == 8

    def test_agent_sequence(self):
        seq = models.agent_sequence(("news", "market"))
        assert seq[:2] == ["Market Analyst", "News Analyst"]
        assert seq[2:] == list(models.FIXED_AGENTS)
        with pytest.raises(ValueError):
            models.agent_sequence(("news", "bad"))


class TestCode:
    def test_normalize_us(self):
        assert models.normalize_code("us", " nvda ") == "NVDA"
        assert models.normalize_code("us", "brk.b") == "BRK.B"

    def test_normalize_hk_zfill(self):
        assert models.normalize_code("hk", "810.hk") == "0810.HK"
        assert models.normalize_code("hk", " 1810.HK ") == "1810.HK"

    def test_normalize_cn(self):
        assert models.normalize_code("cn", " 600519.ss ") == "600519.SS"
        assert models.normalize_code("cn", "000001.sz") == "000001.SZ"

    def test_normalize_bad_market(self):
        with pytest.raises(ValueError):
            models.normalize_code("jp", "6501.T")

    @pytest.mark.parametrize(
        "market,code",
        [
            ("us", "NVDA"),
            ("us", "BRK.B"),
            ("us", "A"),
            ("us", "AB-CD.EF"),
            ("hk", "1810.HK"),
            ("hk", "0700.HK"),
            ("cn", "600519.SS"),
            ("cn", "000001.SZ"),
        ],
    )
    def test_validate_ok(self, market, code):
        models.validate_code(market, code)

    @pytest.mark.parametrize(
        "market,code",
        [
            ("us", "nvda"),  # 小写
            ("us", "1NVDA"),  # 数字开头
            ("us", ""),  # 空
            ("us", "TOOLONGCODE10"),  # 超长
            ("hk", "1810.hk"),  # 后缀小写
            ("hk", "810.HK"),  # 不足 4 位(须先 normalize)
            ("hk", "1810"),
            ("cn", "600519.ss"),
            ("cn", "60051.SS"),  # 5 位
            ("cn", "600519.SH"),  # 非 SS/SZ
        ],
    )
    def test_validate_bad(self, market, code):
        with pytest.raises(ValueError, match="市场"):
            models.validate_code(market, code)

    def test_validate_bad_market(self):
        with pytest.raises(ValueError):
            models.validate_code("jp", "6501")


class TestAnalysts:
    def test_parse_orders_and_dedup(self):
        assert models.parse_analysts("news,market,market") == ("market", "news")
        assert models.parse_analysts(" fundamentals , social ") == ("social", "fundamentals")

    @pytest.mark.parametrize("csv", ["", "   ", "market,bogus", "bogus"])
    def test_parse_bad(self, csv):
        with pytest.raises(ValueError):
            models.parse_analysts(csv)


class TestDate:
    def test_today_ok(self):
        assert models.validate_date(models.today_sh().isoformat()) == models.today_sh().isoformat()

    def test_past_ok(self):
        assert models.validate_date("2020-01-01") == "2020-01-01"

    def test_future_rejected(self):
        from datetime import timedelta

        future = (models.today_sh() + timedelta(days=1)).isoformat()
        with pytest.raises(ValueError, match="不能晚于今天"):
            models.validate_date(future)

    @pytest.mark.parametrize("bad", ["", "2026-9-1", "2026/09/01", "20260901", "abc"])
    def test_bad_format(self, bad):
        with pytest.raises(ValueError, match="日期格式"):
            models.validate_date(bad)


class TestRunId:
    def test_make_run_id(self):
        now = datetime(2026, 9, 14, 8, 30, 0, tzinfo=models.SH_TZ)
        assert models.make_run_id("1810.HK", now) == "r-20260914-083000-1810HK"
        assert models.make_run_id("BRK.B", now) == "r-20260914-083000-BRKB"

    def test_make_run_id_tz_conversion(self):
        # UTC 时间应换算为 Asia/Shanghai 后取值

        utc = datetime(2026, 9, 14, 0, 30, 0, tzinfo=UTC)
        assert models.make_run_id("NVDA", utc) == "r-20260914-083000-NVDA"
