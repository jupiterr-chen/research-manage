"""AM-03 d(LOCAL-REAL):tradingagents-local + llm-stub 上 3/4 分析师各一次。

标记 acceptance + real:`pytest -m acceptance` 会连带执行(约 3~6 分钟);
缺 vendor 构建的镜像时 skip。复用 S2 的 RealStack(tests/integration/test_runner_real)。
"""

from __future__ import annotations

import subprocess

import pytest

from tests.integration.test_runner_real import (  # noqa: F401
    LOCAL_IMAGE,
    STUB_IMAGE,
    RealStack,
    assert_three_artifacts,
)


def _image_exists(image: str) -> bool:
    try:
        r = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.real,
    pytest.mark.skipif(
        not _image_exists(LOCAL_IMAGE),
        reason=f"缺少 {LOCAL_IMAGE}(scripts/fetch_vendor.sh + scripts/build_local.sh)",
    ),
    pytest.mark.skipif(not _image_exists(STUB_IMAGE), reason="缺少 llm-stub:latest"),
]


def test_am03d_real_three_and_four_analysts(tmp_path):
    """3 与 4 分析师在真实图 + llm-stub 下各 succeeded,三产物齐全,agents_total 11/12。"""
    s = RealStack(tmp_path)
    try:
        s.up()
        code3, status3, ta3 = s.run("1810.HK", "market,social,news", "r-am03d-3")
        assert code3 == 0, status3
        assert status3["phase"] == "succeeded"
        assert status3["agents_total"] == 11
        assert status3["tokens_in"] > 0
        assert_three_artifacts(ta3, "1810.HK")

        code4, status4, ta4 = s.run("NVDA", "market,social,news,fundamentals", "r-am03d-4")
        assert code4 == 0, status4
        assert status4["agents_total"] == 12
        assert (ta4 / "logs" / "NVDA" / "2026-09-14" / "reports" / "fundamentals_report.md").is_file()
        assert_three_artifacts(ta4, "NVDA")
    finally:
        s.down()
