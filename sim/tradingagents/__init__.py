"""sim 假 tradingagents 包(DESIGN §7)。

仅标准库;API 表面与 NAS 真实包一致(tests/shape 对等校验)。
import 时检查工作目录 .env 挂载并写 marker,验证 SPEC §6.3 的 ro 挂载生效。
"""

from __future__ import annotations

import os
from pathlib import Path

_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")


def _write_env_marker() -> None:
    try:
        marker_dir = Path(os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_HOME, "logs")))
        marker_dir.mkdir(parents=True, exist_ok=True)
        env_path = Path.cwd() / ".env"
        (marker_dir / ".env-present").write_text("1" if env_path.exists() else "0", encoding="utf-8")
    except OSError:
        pass  # marker 仅为测试辅助,失败不影响仿真


_write_env_marker()
