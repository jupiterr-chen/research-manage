"""app/db.py 单测(R-FND-03 / R-SCH-06)。"""

from __future__ import annotations

import sqlite3

import pytest

from app import db


class TestConnect:
    def test_pragmas(self, db_file):
        conn = db.connect(str(db_file))
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.close()


class TestMigrate:
    def test_idempotent(self, db_file):
        conn = db.connect(str(db_file))
        db.migrate(conn)
        db.migrate(conn)  # 重复执行不报错、不重复种子
        n = conn.execute("SELECT COUNT(*) AS n FROM profile").fetchone()["n"]
        assert n == 2
        conn.close()

    def test_seed_profiles(self, conn):
        rows = conn.execute("SELECT name, analysts_csv, is_default FROM profile ORDER BY id").fetchall()
        assert rows[0]["name"] == "日频三件套"
        assert rows[0]["analysts_csv"] == "market,social,news"
        assert rows[0]["is_default"] == 1
        assert rows[1]["name"] == "全量"
        assert rows[1]["analysts_csv"] == "market,social,news,fundamentals"
        assert rows[1]["is_default"] == 0

    def test_no_seed_instruments(self, conn):
        assert conn.execute("SELECT COUNT(*) AS n FROM instrument").fetchone()["n"] == 0

    @pytest.mark.parametrize(
        "table,cols",
        [
            ("instrument", ["id", "market", "code", "name", "enabled", "created_at", "updated_at"]),
            ("profile", ["id", "name", "analysts_csv", "is_default"]),
            ("schedule", ["id", "instrument_id", "profile_id", "kind", "at_time", "weekday", "enabled"]),
            (
                "run",
                [
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
            ),
            ("audit_log", ["id", "ts", "actor", "action", "entity", "entity_id", "detail_json"]),
        ],
    )
    def test_ddl_columns(self, conn, table, cols):
        existing = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert set(cols) <= set(existing)

    def test_run_status_check(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO run(id) VALUES ('x')")  # NOT NULL 约束先行失败

    def test_uq_run_active(self, conn):
        conn.execute(
            "INSERT INTO instrument(market, code, name, enabled, created_at, updated_at)"
            " VALUES ('hk','1810.HK','',1,'t','t')"
        )
        base = (
            "INSERT INTO run(id, instrument_id, analysts_csv, analysis_date, status,"
            " trigger, agents_total, created_at) VALUES (?,?,?,?,?,'web',11,'t')"
        )
        conn.execute(base, ("r1", 1, "market,social,news", "2026-09-14", "queued"))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            conn.execute(base, ("r2", 1, "market,social,news", "2026-09-14", "running"))
        # 不同日期不受限
        conn.execute(base, ("r3", 1, "market,social,news", "2026-09-15", "queued"))
        # 终态后可再入队
        conn.execute("UPDATE run SET status='cancelled' WHERE id='r1'")
        conn.execute(base, ("r4", 1, "market,social,news", "2026-09-14", "queued"))


class TestTx:
    def test_commit_and_rollback(self, conn):
        with pytest.raises(RuntimeError):
            with db.tx(conn):
                conn.execute(
                    "INSERT INTO instrument(market, code, name, enabled, created_at, updated_at)"
                    " VALUES ('us','NVDA','',1,'t','t')"
                )
                raise RuntimeError("boom")
        assert conn.execute("SELECT COUNT(*) AS n FROM instrument").fetchone()["n"] == 0

        with db.tx(conn):
            conn.execute(
                "INSERT INTO instrument(market, code, name, enabled, created_at, updated_at)"
                " VALUES ('us','NVDA','',1,'t','t')"
            )
        assert conn.execute("SELECT COUNT(*) AS n FROM instrument").fetchone()["n"] == 1

    def test_nested_savepoint_rollback_inner_only(self, conn):
        with db.tx(conn):
            conn.execute(
                "INSERT INTO instrument(market, code, name, enabled, created_at, updated_at)"
                " VALUES ('us','NVDA','',1,'t','t')"
            )
            with pytest.raises(ValueError):
                with db.tx(conn):
                    conn.execute(
                        "INSERT INTO instrument(market, code, name, enabled, created_at,"
                        " updated_at) VALUES ('us','AAPL','',1,'t','t')"
                    )
                    raise ValueError("inner")
        codes = [r["code"] for r in conn.execute("SELECT code FROM instrument")]
        assert codes == ["NVDA"]  # 内层回滚,外层保留
