"""SQLite 连接、幂等迁移与事务(DESIGN §4.2 / R-FND-03)。

DDL 与 SPEC §4 逐字一致;迁移用 PRAGMA table_info 检查后 CREATE/ALTER,可重复执行。
"""

from __future__ import annotations

import itertools
import sqlite3
from contextlib import contextmanager

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS instrument (
      id       INTEGER PRIMARY KEY,
      market   TEXT NOT NULL CHECK(market IN ('us','hk','cn')),
      code     TEXT NOT NULL,
      name     TEXT NOT NULL DEFAULT '',
      enabled  INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(market, code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profile (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      analysts_csv TEXT NOT NULL,
      is_default INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schedule (
      id INTEGER PRIMARY KEY,
      instrument_id INTEGER NOT NULL REFERENCES instrument(id),
      profile_id    INTEGER NOT NULL REFERENCES profile(id),
      kind     TEXT NOT NULL CHECK(kind IN ('daily_trading','weekly')),
      at_time  TEXT NOT NULL DEFAULT '08:30',
      weekday  INTEGER,
      enabled  INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS run (
      id TEXT PRIMARY KEY,
      instrument_id INTEGER NOT NULL REFERENCES instrument(id),
      profile_id    INTEGER REFERENCES profile(id),
      analysts_csv  TEXT NOT NULL,
      analysis_date TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN
        ('queued','running','succeeded','failed','cancelled')),
      trigger TEXT NOT NULL CHECK(trigger IN ('web','api','schedule')),
      resumed_from TEXT REFERENCES run(id),
      container_id TEXT, exit_code INTEGER,
      current_agent TEXT, agents_done INTEGER DEFAULT 0, agents_total INTEGER NOT NULL,
      tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
      report_ready INTEGER DEFAULT 0,
      status_stale INTEGER DEFAULT 0,
      cancel_requested_at TEXT,
      error TEXT, started_at TEXT, finished_at TEXT,
      created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_run_status ON run(status)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_run_active
      ON run(instrument_id, analysis_date) WHERE status IN ('queued','running')
    """,
    """
    CREATE TABLE IF NOT EXISTS report_job (        -- 财报原文获取任务(reports-fetcher 对接;本地跟踪记录)
      id TEXT PRIMARY KEY,                          -- rj-YYYYMMDD-HHMMSS-<slug>
      instrument_id INTEGER REFERENCES instrument(id),
      market TEXT NOT NULL CHECK(market IN ('us','hk','cn')),
      code TEXT NOT NULL,                           -- 本系统代码(1810.HK)
      symbol TEXT NOT NULL,                         -- 提交给 reports-fetcher 的 symbol
      last_n INTEGER NOT NULL DEFAULT 4,
      refresh INTEGER NOT NULL DEFAULT 0,
      idempotency_key TEXT NOT NULL UNIQUE,         -- 创建时生成,所有重试复用
      remote_job_id TEXT,                           -- 服务端 job_id(提交成功后)
      status TEXT NOT NULL CHECK(status IN
        ('pending','queued','running','succeeded','partial','failed','timeout','error')),
      trigger TEXT NOT NULL CHECK(trigger IN ('web','api')),
      submit_attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt_at TEXT,                         -- 429 Retry-After 后的下次提交时间
      progress_json TEXT, summary_json TEXT, results_json TEXT,
      warnings_json TEXT,                           -- 汇总的证券级 warnings
      report_ids_json TEXT,                         -- 可用报告 id 列表(partial 也保留)
      error TEXT, error_code TEXT, error_retryable INTEGER,
      created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_report_job_status ON report_job(status)",
    """
    CREATE TABLE IF NOT EXISTS audit_log (
      id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
      actor TEXT NOT NULL CHECK(actor IN ('web','api','schedule')),
      action TEXT NOT NULL, entity TEXT NOT NULL, entity_id TEXT,
      detail_json TEXT
    )
    """,
]

# 表名 → SPEC §4 期望列(用于已有库的幂等 ALTER 补列)
_EXPECTED_COLUMNS: dict[str, list[str]] = {
    "instrument": ["id", "market", "code", "name", "enabled", "created_at", "updated_at"],
    "profile": ["id", "name", "analysts_csv", "is_default"],
    "schedule": ["id", "instrument_id", "profile_id", "kind", "at_time", "weekday", "enabled"],
    "report_job": [
        "id",
        "instrument_id",
        "market",
        "code",
        "symbol",
        "last_n",
        "refresh",
        "idempotency_key",
        "remote_job_id",
        "status",
        "trigger",
        "submit_attempts",
        "next_attempt_at",
        "progress_json",
        "summary_json",
        "results_json",
        "warnings_json",
        "report_ids_json",
        "error",
        "error_code",
        "error_retryable",
        "created_at",
        "started_at",
        "finished_at",
        "updated_at",
    ],
    "run": [
        "id",
        "instrument_id",
        "profile_id",
        "analysts_csv",
        "analysis_date",
        "status",
        "trigger",
        "resumed_from",
        "container_id",
        "exit_code",
        "current_agent",
        "agents_done",
        "agents_total",
        "tokens_in",
        "tokens_out",
        "report_ready",
        "status_stale",
        "cancel_requested_at",
        "error",
        "started_at",
        "finished_at",
        "created_at",
    ],
    "audit_log": ["id", "ts", "actor", "action", "entity", "entity_id", "detail_json"],
}

SEED_PROFILES = [
    ("日频三件套", "market,social,news", 1),
    ("全量", "market,social,news,fundamentals", 0),
]


def connect(path: str) -> sqlite3.Connection:
    """row_factory=Row,WAL,busy_timeout=5000,foreign_keys=ON。"""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r["name"] for r in rows}


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def migrate(conn: sqlite3.Connection) -> None:
    """幂等迁移:CREATE IF NOT EXISTS + 缺列 ALTER + 首次种子档案(R-SCH-06)。"""
    with tx(conn):
        for stmt in _DDL:
            conn.execute(stmt)
        # 老库补列(SPEC §4 演进;新库此步为空操作)
        for table, columns in _EXPECTED_COLUMNS.items():
            existing = set(_table_columns(conn, table))
            for col in columns:
                if col not in existing:  # pragma: no cover - 仅升级路径触发
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN "{col}"')
        # 种子档案:仅首次(表空)写入;不种标的
        count = conn.execute("SELECT COUNT(*) AS n FROM profile").fetchone()["n"]
        if count == 0:
            for name, analysts_csv, is_default in SEED_PROFILES:
                # 种子数据属于系统初始化,不走 audit(actor 无用户语义)
                conn.execute(
                    "INSERT INTO profile(name, analysts_csv, is_default) VALUES (?,?,?)",
                    (name, analysts_csv, is_default),
                )


@contextmanager
def tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE / COMMIT / ROLLBACK。嵌套调用内层退化为 SAVEPOINT。"""
    if conn.in_transaction:
        savepoint = f"am_sp_{next(_savepoint_seq)}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            raise
        else:
            conn.execute(f"RELEASE {savepoint}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


_savepoint_seq = itertools.count(1)


def seed_profile_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM profile ORDER BY id").fetchall()
