"""终态判定(DESIGN §4.7 / SPEC §6.4 / R-EXE-08/09 / AM-03)。

判定顺序(不得调整):
1. 退出码 130 或本系统主动 stop → cancelled
2. 退出码 ≠ 0 → failed(error 取 status.json.error,缺省 exit_<code>)
3. 退出码 0 → succeeded 双重判定:investment_plan.md 存在 且 memory 行前缀
   `[<date> | <code> |`;不满足 → failed(error 写明缺哪个)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.executor.status_reader import Status

INVESTMENT_PLAN_REL = "logs/{code}/{date}/reports/investment_plan.md"
MEMORY_REL = "memory/trading_memory.md"


@dataclass
class Verdict:
    status: str
    error: str | None
    report_ready: bool


def report_ready(ta_data_dir: Path | str, code: str, date: str) -> bool:
    path = Path(ta_data_dir) / INVESTMENT_PLAN_REL.format(code=code, date=date)
    return path.is_file()


def memory_has_entry(ta_data_dir: Path | str, code: str, date: str) -> bool:
    memory = Path(ta_data_dir) / MEMORY_REL
    if not memory.is_file():
        return False
    prefix = f"[{date} | {code} |"
    try:
        with open(memory, encoding="utf-8") as f:
            return any(line.startswith(prefix) for line in f)
    except OSError:
        return False


def decide(
    *,
    exit_code: int | None,
    cancelled_by_us: bool,
    status: Status | None,
    ta_data_dir: Path | str,
    code: str,
    date: str,
) -> Verdict:
    # 1. 主动取消(含看门狗)优先
    if cancelled_by_us or exit_code == 130:
        return Verdict("cancelled", None, report_ready(ta_data_dir, code, date))
    # 2. 非零退出 → failed
    if exit_code is None:
        exit_code = -1
    if exit_code != 0:
        error = status.error if status and status.error else f"exit_{exit_code}"
        return Verdict("failed", error, report_ready(ta_data_dir, code, date))
    # 3. 退出码 0 → 双重判定
    has_plan = report_ready(ta_data_dir, code, date)
    has_memory = memory_has_entry(ta_data_dir, code, date)
    if has_plan and has_memory:
        return Verdict("succeeded", None, True)
    missing = []
    if not has_plan:
        missing.append("investment_plan.md")
    if not has_memory:
        missing.append("memory 条目(trading_memory.md)")
    return Verdict("failed", f"退出码 0 但产物缺失: 缺少 {' 与 '.join(missing)}", has_plan)
