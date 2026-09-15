"""app/config.py 单测(R-FND-01/02 / AM-06 完整绑定门禁矩阵)。"""

from __future__ import annotations

import json

import pytest

from app.config import Settings, is_loopback_bind

REQUIRED = {
    "AM_TA_DATA_DIR": "/ta/data",
    "AM_TA_DATA_HOST": "/ta/data",
    "AM_TA_ENV_HOST": "/ta/.env",
    "AM_RUNNER_HOST": "/runner/runner.py",
}
TOKEN = "a" * 32


class TestIsLoopback:
    @pytest.mark.parametrize(
        "bind",
        [
            "127.0.0.1",
            "::1",
            "localhost",
            "127.8.8.8",
            " 127.0.0.1 ",
            "LOCALHOST",
        ],
    )
    def test_loopback(self, bind):
        assert is_loopback_bind(bind) is True

    @pytest.mark.parametrize(
        "bind",
        [
            "0.0.0.0",
            "::",
            "192.168.1.150",
            "0.0.0.0 ",
            " 0.0.0.0",
            "10.0.0.1",
            "not-an-ip",
            "",
            "0:0:0:0:0:0:0:0",
        ],
    )
    def test_not_loopback(self, bind):
        assert is_loopback_bind(bind) is False


class TestBindGate:
    """AM-06 矩阵:非回环绑定无 token(或 <32)→ 拒绝启动。"""

    @pytest.mark.parametrize("bind", ["0.0.0.0", "::", "192.168.1.150", "0.0.0.0 "])
    def test_non_loopback_no_token_refused(self, bind):
        with pytest.raises(SystemExit) as ei:
            Settings.load(env={**REQUIRED, "AM_BIND": bind})
        assert ei.value.code == 2

    def test_non_loopback_short_token_refused(self):
        with pytest.raises(SystemExit):
            Settings.load(env={**REQUIRED, "AM_BIND": "192.168.1.150", "AM_TOKEN": "short"})

    def test_non_loopback_valid_token_ok(self):
        s = Settings.load(env={**REQUIRED, "AM_BIND": "192.168.1.150", "AM_TOKEN": TOKEN})
        assert s.bind == "192.168.1.150"

    @pytest.mark.parametrize("bind", ["127.0.0.1", "127.0.0.1 ", "::1", "localhost"])
    def test_loopback_no_token_ok(self, bind):
        s = Settings.load(env={**REQUIRED, "AM_BIND": bind})
        assert is_loopback_bind(s.bind) is True

    def test_loopback_short_token_refused(self):
        # token 一旦提供就必须 ≥32( fail-closed)
        with pytest.raises(SystemExit):
            Settings.load(env={**REQUIRED, "AM_BIND": "127.0.0.1", "AM_TOKEN": "short"})


class TestRequiredAndRanges:
    def test_missing_required(self):
        with pytest.raises(SystemExit):
            Settings.load(env={})  # 四个必填全缺

    @pytest.mark.parametrize("key", list(REQUIRED))
    def test_each_required(self, key):
        env = dict(REQUIRED)
        env.pop(key)
        with pytest.raises(SystemExit):
            Settings.load(env=env)

    @pytest.mark.parametrize(
        "key,bad",
        [
            ("AM_PORT", "0"),
            ("AM_PORT", "70000"),
            ("AM_PORT", "abc"),
            ("AM_WATCHDOG_MINUTES", "0"),
            ("AM_STALE_MINUTES", "0"),
            ("AM_RETENTION_DAYS", "-1"),
            ("AM_STATUS_POLL_SECONDS", "0"),
            ("AM_STOP_TIMEOUT", "0"),
        ],
    )
    def test_bad_ranges(self, key, bad):
        with pytest.raises(SystemExit):
            Settings.load(env={**REQUIRED, key: bad})

    def test_good_ranges(self):
        s = Settings.load(
            env={
                **REQUIRED,
                "AM_PORT": "9000",
                "AM_WATCHDOG_MINUTES": "30",
                "AM_STALE_MINUTES": "5",
                "AM_RETENTION_DAYS": "7",
                "AM_STATUS_POLL_SECONDS": "2",
                "AM_STOP_TIMEOUT": "10",
            }
        )
        assert (
            s.port,
            s.watchdog_minutes,
            s.stale_minutes,
            s.retention_days,
            s.status_poll_seconds,
            s.stop_timeout,
        ) == (9000, 30, 5, 7, 2, 10)


class TestLoad:
    def test_defaults(self):
        s = Settings.load(env=dict(REQUIRED))
        assert s.bind == "127.0.0.1" and s.port == 8090
        assert s.ta_image == "tradingagents-tradingagents:latest"
        assert s.data_host == "./data"
        assert s.db_path.replace("\\", "/").endswith("data/agents-manage.db")

    def test_data_host_defaults_to_dir(self):
        s = Settings.load(env={**REQUIRED, "AM_DATA_DIR": "/x/y"})
        assert s.data_host == "/x/y"

    def test_summary_has_no_token(self):
        s = Settings.load(env={**REQUIRED, "AM_TOKEN": TOKEN})
        dumped = json.dumps(s.summary(), ensure_ascii=False)
        assert TOKEN not in dumped
        assert s.summary()["token"] == "(已配置)"

    def test_summary_unconfigured_token(self):
        s = Settings.load(env=dict(REQUIRED))
        assert s.summary()["token"] == "(未配置,仅限回环绑定)"
