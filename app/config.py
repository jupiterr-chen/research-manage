"""配置加载与 fail-closed 校验(DESIGN §3 / R-FND-01/02)。

全部配置来自 `AM_*` 环境变量;`Settings.load()` 顺序:
读 env → 类型转换 → validate()(绑定门禁、必填、数值范围)
→ 失败 SystemExit(2) 并打印原因。
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass

MIN_TOKEN_LEN = 32


def is_loopback_bind(bind: str) -> bool:
    """绑定地址是否回环;无法判定的一律 False(fail-closed,禁止字面量比较)。"""
    s = bind.strip()
    if not s:
        return False
    if s.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(s).is_loopback
    except ValueError:
        return False


@dataclass
class Settings:
    bind: str = "127.0.0.1"
    port: int = 8090
    token: str = ""
    data_dir: str = "./data"
    data_host: str = ""
    db_path: str = ""
    ta_data_dir: str = ""
    ta_data_host: str = ""
    ta_env_host: str = ""
    runner_host: str = ""
    ta_image: str = "tradingagents-tradingagents:latest"
    ta_container_data: str = "/home/appuser/.tradingagents"
    ta_workdir: str = "/home/appuser/app"
    smb_prefix: str = ""
    watchdog_minutes: int = 120
    stale_minutes: int = 20
    retention_days: int = 90
    status_poll_seconds: int = 5
    stop_timeout: int = 20
    ta_network: str = ""
    sim_env: str = ""
    tz: str = "Asia/Shanghai"

    @classmethod
    def load(cls, env: dict[str, str] | None = None) -> Settings:
        get = (env if env is not None else os.environ).get
        parse_errors: list[str] = []

        def to_int(name: str, default: int) -> int:
            raw = get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except ValueError:
                parse_errors.append(f"{name}={raw!r} 不是整数")
                return default

        s = cls(
            bind=get("AM_BIND", "127.0.0.1"),
            port=to_int("AM_PORT", 8090),
            token=get("AM_TOKEN", ""),
            data_dir=get("AM_DATA_DIR", "./data"),
            data_host=get("AM_DATA_HOST", "") or get("AM_DATA_DIR", "./data"),
            db_path=get("AM_DB_PATH", ""),
            ta_data_dir=get("AM_TA_DATA_DIR", ""),
            ta_data_host=get("AM_TA_DATA_HOST", ""),
            ta_env_host=get("AM_TA_ENV_HOST", ""),
            runner_host=get("AM_RUNNER_HOST", ""),
            ta_image=get("AM_TA_IMAGE", "tradingagents-tradingagents:latest"),
            ta_container_data=get("AM_TA_CONTAINER_DATA", "/home/appuser/.tradingagents"),
            ta_workdir=get("AM_TA_WORKDIR", "/home/appuser/app"),
            smb_prefix=get("AM_SMB_PREFIX", ""),
            watchdog_minutes=to_int("AM_WATCHDOG_MINUTES", 120),
            stale_minutes=to_int("AM_STALE_MINUTES", 20),
            retention_days=to_int("AM_RETENTION_DAYS", 90),
            status_poll_seconds=to_int("AM_STATUS_POLL_SECONDS", 5),
            stop_timeout=to_int("AM_STOP_TIMEOUT", 20),
            ta_network=get("AM_TA_NETWORK", ""),
            sim_env=get("AM_SIM_ENV", ""),
            tz=get("TZ", "Asia/Shanghai"),
        )
        if parse_errors:
            for line in parse_errors:
                print(f"[config] 校验失败:{line}")
            raise SystemExit(2)
        if not s.db_path:
            s.db_path = os.path.join(s.data_dir, "agents-manage.db")
        s.validate()
        return s

    def validate(self) -> None:
        errors: list[str] = []
        # 绑定门禁(R-FND-02 / AM-06):非回环必须配 token
        if not is_loopback_bind(self.bind):
            if not self.token:
                errors.append(f"AM_BIND={self.bind!r} 非回环地址,必须配置 AM_TOKEN(≥{MIN_TOKEN_LEN} 字符)")
        if self.token and len(self.token) < MIN_TOKEN_LEN:
            errors.append(f"AM_TOKEN 长度 {len(self.token)} < {MIN_TOKEN_LEN},拒绝启动")
        # 必填项
        for name, val in (
            ("AM_TA_DATA_DIR", self.ta_data_dir),
            ("AM_TA_DATA_HOST", self.ta_data_host),
            ("AM_TA_ENV_HOST", self.ta_env_host),
            ("AM_RUNNER_HOST", self.runner_host),
        ):
            if not val:
                errors.append(f"缺少必填配置 {name}")
        # 数值范围
        if not 1 <= self.port <= 65535:
            errors.append(f"AM_PORT={self.port} 超出 1..65535")
        for name, val in (
            ("AM_WATCHDOG_MINUTES", self.watchdog_minutes),
            ("AM_STALE_MINUTES", self.stale_minutes),
            ("AM_RETENTION_DAYS", self.retention_days),
            ("AM_STATUS_POLL_SECONDS", self.status_poll_seconds),
            ("AM_STOP_TIMEOUT", self.stop_timeout),
        ):
            if val < 1:
                errors.append(f"{name}={val} 必须 ≥1")
        if errors:
            for line in errors:
                print(f"[config] 校验失败:{line}")
            raise SystemExit(2)

    def summary(self) -> dict:
        """不含 AM_TOKEN 的生效配置摘要(R-FND-01)。"""
        return {
            "bind": self.bind,
            "port": self.port,
            "data_dir": self.data_dir,
            "data_host": self.data_host,
            "db_path": self.db_path,
            "ta_data_dir": self.ta_data_dir,
            "ta_data_host": self.ta_data_host,
            "ta_env_host": self.ta_env_host,
            "runner_host": self.runner_host,
            "ta_image": self.ta_image,
            "ta_container_data": self.ta_container_data,
            "ta_workdir": self.ta_workdir,
            "smb_prefix": self.smb_prefix,
            "watchdog_minutes": self.watchdog_minutes,
            "stale_minutes": self.stale_minutes,
            "retention_days": self.retention_days,
            "status_poll_seconds": self.status_poll_seconds,
            "stop_timeout": self.stop_timeout,
            "ta_network": self.ta_network or "(默认 bridge)",
            "sim_env": self.sim_env or "(无)",
            "tz": self.tz,
            "token": "(已配置)" if self.token else "(未配置,仅限回环绑定)",
        }
