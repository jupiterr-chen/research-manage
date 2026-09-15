"""app/services/runs.py 单测(R-SVC-04~10 / AM-01/10)。"""

from __future__ import annotations

import pytest

from app import db
from app.services import instruments, profiles, runs, schedules

TODAY = "2026-09-14"


@pytest.fixture
def inst(conn):
    return instruments.create(conn, market="hk", code="1810.HK", name="小米集团", actor="web")


def insert_run(
    conn,
    instrument_id,
    *,
    status="queued",
    date=TODAY,
    csv="market,social,news",
    rid=None,
    profile_id=None,
    created_at="2026-09-14T08:00:00+08:00",
):
    """绕过业务规则的原始插入,构造 busy/running 场景。"""
    rid = rid or f"r-test-{status}-{instrument_id}-{date}"
    with db.tx(conn):
        conn.execute(
            "INSERT INTO run(id, instrument_id, profile_id, analysts_csv, analysis_date,"
            " status, trigger, agents_total, created_at)"
            " VALUES (?,?,?,?,?,?,'web',11,?)",
            (rid, instrument_id, profile_id, csv, date, status, created_at),
        )
    return rid


def counts(conn):
    return (
        conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"],
        conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"],
    )


# ---------- create_run(R-SVC-04 六步) ----------


class TestCreateRun:
    def test_happy_with_profile(self, conn, inst, freezer):
        with freezer(f"{TODAY} 08:30:00"):
            r = runs.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        assert r["status"] == "queued" and r["trigger"] == "web"
        assert r["analysts_csv"] == "market,social,news"
        assert r["agents_total"] == 11
        assert r["id"] == "r-20260914-083000-1810HK"
        assert r["code"] == "1810.HK"
        assert r["profile_id"] == 1

    def test_explicit_analysts_no_profile(self, conn, inst):
        r = runs.create_run(conn, code="1810.HK", analysts=("fundamentals",), trigger="api", actor="api")
        assert r["analysts_csv"] == "fundamentals" and r["profile_id"] is None
        assert r["agents_total"] == 9

    def test_date_defaults_today(self, conn, inst):
        r = runs.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        assert r["analysis_date"]  # 当天

    def test_fallback_first_enabled_schedule_profile(self, conn, inst):
        full = profiles.create(conn, name="临时", analysts_csv="news", actor="web")
        schedules.create(
            conn, instrument_id=inst["id"], profile_id=full["id"], kind="daily_trading", actor="web"
        )
        r = runs.create_run(conn, code="1810.HK", trigger="web", actor="web")
        assert r["profile_id"] == full["id"] and r["analysts_csv"] == "news"

    def test_fallback_default_profile(self, conn, inst):
        r = runs.create_run(conn, code="1810.HK", trigger="web", actor="web")
        assert r["profile_id"] == 1  # 种子默认档案「日频三件套」

    def test_no_analysts_available(self, conn):
        instruments.create(conn, market="us", code="NVDA", actor="web")
        conn.execute("DELETE FROM profile")  # 清掉种子
        with pytest.raises(runs.ValidationError, match="分析师"):
            runs.create_run(conn, code="NVDA", trigger="web", actor="web")

    def test_busy_running(self, conn, inst):
        insert_run(conn, inst["id"], status="running")
        with pytest.raises(runs.BusyError) as ei:
            runs.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        assert ei.value.current["status"] == "running"
        assert ei.value.queued == 0

    def test_busy_queued_only_counts_behind(self, conn, inst):
        insert_run(conn, inst["id"], status="queued", rid="r-q1")
        insert_run(conn, inst["id"], status="queued", rid="r-q2", date="2026-09-13")
        with pytest.raises(runs.BusyError) as ei:
            runs.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        assert ei.value.current["id"] == "r-q1"  # 最早排队者视作当前
        assert ei.value.queued == 1  # 其后还有 1 条

    def test_busy_zero_side_effect(self, conn, inst):
        insert_run(conn, inst["id"], status="running")
        before = counts(conn)
        with pytest.raises(runs.BusyError):
            runs.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        assert counts(conn) == before  # AM-01:零副作用(无 run、无 audit)

    def test_already_done(self, conn, inst):
        rid = insert_run(conn, inst["id"], status="succeeded")
        with pytest.raises(runs.AlreadyDoneError) as ei:
            runs.create_run(conn, code="1810.HK", profile_id=1, date=TODAY, trigger="web", actor="web")
        assert ei.value.run_id == rid
        with pytest.raises(runs.AlreadyDoneError):
            runs.create_run(
                conn, code="1810.HK", profile_id=1, date=TODAY, trigger="web", actor="web"
            )  # 幂等报错

    def test_force_overrides_already_done(self, conn, inst, freezer):
        with freezer(f"{TODAY} 08:30:00"):
            insert_run(conn, inst["id"], status="succeeded")
            r = runs.create_run(
                conn, code="1810.HK", profile_id=1, date=TODAY, trigger="web", actor="web", force=True
            )
        assert r["status"] == "queued"

    def test_disabled_instrument(self, conn, inst):
        instruments.update(conn, inst["id"], enabled=False, actor="web")
        with pytest.raises(runs.ConflictError, match="停用"):
            runs.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")

    def test_unknown_code(self, conn):
        with pytest.raises(runs.NotFound):
            runs.create_run(conn, code="NOPE.HK", profile_id=1, trigger="web", actor="web")

    def test_ambiguous_code(self, conn, inst):
        # 正常校验下不可能;构造跨市场同 code 防御分支
        conn.execute(
            "INSERT INTO instrument(market, code, name, enabled, created_at, updated_at)"
            " VALUES ('us','1810.HK','',1,'t','t')"
        )
        with pytest.raises(runs.ConflictError, match="多个市场"):
            runs.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")

    def test_analysts_and_profile_exclusive(self, conn, inst):
        with pytest.raises(runs.ValidationError, match="二选一"):
            runs.create_run(
                conn, code="1810.HK", analysts=("market",), profile_id=1, trigger="web", actor="web"
            )

    def test_bad_analysts(self, conn, inst):
        with pytest.raises(runs.ValidationError):
            runs.create_run(conn, code="1810.HK", analysts=("bogus",), trigger="web", actor="web")

    def test_bad_trigger(self, conn, inst):
        with pytest.raises(runs.ValidationError):
            runs.create_run(conn, code="1810.HK", profile_id=1, trigger="schedule", actor="schedule")

    def test_bad_date(self, conn, inst):
        with pytest.raises(runs.ValidationError):
            runs.create_run(conn, code="1810.HK", profile_id=1, date="2030-01-01", trigger="web", actor="web")

    def test_run_id_collision_suffix(self, conn, inst, freezer):
        with freezer(f"{TODAY} 08:30:00"):
            r1 = runs.create_run(
                conn, code="1810.HK", profile_id=1, date="2026-09-01", trigger="web", actor="web"
            )
            runs.request_cancel(conn, r1["id"], actor="web")  # 释放活动位,保留同秒 run id
            r1b = runs.create_run(
                conn, code="1810.HK", profile_id=1, date="2026-09-02", trigger="web", actor="web"
            )
        assert r1["id"] == "r-20260914-083000-1810HK"
        assert r1b["id"] == "r-20260914-083000-1810HK-2"  # R-SVC-10

    def test_market_filter(self, conn, inst):
        r = runs.create_run(conn, code="1810.hk ", market="hk", profile_id=1, trigger="web", actor="web")
        assert r["code"] == "1810.HK"


# ---------- enqueue_scheduled(R-SVC-05 / AM-10) ----------


class TestEnqueueScheduled:
    def _mk_schedules(self, conn, inst):
        daily = schedules.create(
            conn, instrument_id=inst["id"], profile_id=1, kind="daily_trading", actor="web"
        )
        weekly = schedules.create(
            conn, instrument_id=inst["id"], profile_id=2, kind="weekly", weekday=1, actor="web"
        )
        return daily, weekly

    def test_enqueue_ok(self, conn, inst):
        sched, _ = self._mk_schedules(conn, inst)
        r = runs.enqueue_scheduled(conn, schedule_id=sched["id"], date=TODAY)
        assert r and r["status"] == "queued" and r["trigger"] == "schedule"
        assert r["analysts_csv"] == "market,social,news"  # 档案快照
        assert r["profile_id"] == 1

    def test_not_busy_limited(self, conn, inst):
        """调度入队不受忙判定:已有 running 仍可继续排队。"""
        insert_run(conn, inst["id"], status="running")
        sched, _ = self._mk_schedules(conn, inst)
        r = runs.enqueue_scheduled(conn, schedule_id=sched["id"], date="2026-09-13")
        assert r is not None

    def test_dedup_bigger_replaces_queued(self, conn, inst):
        """AM-10:周一两条调度,只产生 1 条 run 且为全量。"""
        daily, weekly = self._mk_schedules(conn, inst)
        assert runs.enqueue_scheduled(conn, schedule_id=daily["id"], date=TODAY)
        r2 = runs.enqueue_scheduled(conn, schedule_id=weekly["id"], date=TODAY)
        active = conn.execute("SELECT id, analysts_csv FROM run WHERE status='queued'").fetchall()
        assert len(active) == 1
        assert active[0]["analysts_csv"] == "market,social,news,fundamentals"
        assert r2["id"] == active[0]["id"]
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action='deduped'").fetchone()["n"] == 1

    def test_dedup_smaller_keeps_existing(self, conn, inst):
        daily, weekly = self._mk_schedules(conn, inst)
        assert runs.enqueue_scheduled(conn, schedule_id=weekly["id"], date=TODAY)
        assert runs.enqueue_scheduled(conn, schedule_id=daily["id"], date=TODAY) is None
        active = conn.execute("SELECT analysts_csv FROM run WHERE status='queued'").fetchall()
        assert len(active) == 1
        assert active[0]["analysts_csv"] == "market,social,news,fundamentals"

    def test_dedup_equal_keeps_existing(self, conn, inst):
        sched, _ = self._mk_schedules(conn, inst)
        runs.enqueue_scheduled(conn, schedule_id=sched["id"], date=TODAY)
        assert runs.enqueue_scheduled(conn, schedule_id=sched["id"], date=TODAY) is None
        assert conn.execute("SELECT COUNT(*) AS n FROM run").fetchone()["n"] == 1

    def test_dedup_never_replaces_running(self, conn, inst):
        insert_run(conn, inst["id"], status="running", csv="market")
        _, weekly = self._mk_schedules(conn, inst)
        assert runs.enqueue_scheduled(conn, schedule_id=weekly["id"], date=TODAY) is None
        kept = conn.execute("SELECT analysts_csv FROM run WHERE status='running'").fetchone()
        assert kept["analysts_csv"] == "market"

    def test_disabled_schedule_noop(self, conn, inst):
        sched, _ = self._mk_schedules(conn, inst)
        schedules.toggle(conn, sched["id"], actor="web")
        assert runs.enqueue_scheduled(conn, schedule_id=sched["id"], date=TODAY) is None

    def test_disabled_instrument_noop(self, conn, inst):
        sched, _ = self._mk_schedules(conn, inst)
        instruments.update(conn, inst["id"], enabled=False, actor="web")
        assert runs.enqueue_scheduled(conn, schedule_id=sched["id"], date=TODAY) is None

    def test_unknown_schedule_noop(self, conn):
        assert runs.enqueue_scheduled(conn, schedule_id=99, date=TODAY) is None


# ---------- cancel / resume ----------


class TestCancel:
    def test_queued_cancelled_immediately(self, conn, inst):
        r = runs.create_run(conn, code="1810.HK", profile_id=1, trigger="web", actor="web")
        out = runs.request_cancel(conn, r["id"], actor="web")
        assert out["status"] == "cancelled" and out["finished_at"]

    def test_running_sets_flag(self, conn, inst):
        rid = insert_run(conn, inst["id"], status="running")
        out = runs.request_cancel(conn, rid, actor="api")
        assert out["status"] == "running" and out["cancel_requested_at"]

    def test_running_idempotent(self, conn, inst):
        rid = insert_run(conn, inst["id"], status="running")
        first = runs.request_cancel(conn, rid, actor="api")["cancel_requested_at"]
        second = runs.request_cancel(conn, rid, actor="api")["cancel_requested_at"]
        assert first == second  # 不覆盖首次时间

    def test_finished_conflict(self, conn, inst):
        rid = insert_run(conn, inst["id"], status="succeeded")
        with pytest.raises(runs.ConflictError, match="不能取消"):
            runs.request_cancel(conn, rid, actor="web")

    def test_not_found(self, conn):
        with pytest.raises(runs.NotFound):
            runs.request_cancel(conn, "r-missing", actor="web")


class TestResume:
    def test_from_cancelled(self, conn, inst):
        r = runs.create_run(conn, code="1810.HK", profile_id=1, date="2026-09-01", trigger="web", actor="web")
        runs.request_cancel(conn, r["id"], actor="web")
        r2 = runs.resume(conn, r["id"], actor="web")
        assert r2["resumed_from"] == r["id"]
        assert r2["analysts_csv"] == r["analysts_csv"]  # 逐字复制(断点签名)
        assert r2["analysis_date"] == r["analysis_date"]
        assert r2["instrument_id"] == r["instrument_id"]
        assert r2["status"] == "queued" and r2["trigger"] == "web"

    def test_from_failed(self, conn, inst):
        rid = insert_run(conn, inst["id"], status="failed", date="2026-09-01")
        r2 = runs.resume(conn, rid, actor="api")
        assert r2["trigger"] == "api" and r2["resumed_from"] == rid

    def test_status_not_allowed(self, conn, inst):
        rid = insert_run(conn, inst["id"], status="succeeded")
        with pytest.raises(runs.ConflictError, match="可续跑"):
            runs.resume(conn, rid, actor="web")

    def test_busy(self, conn, inst):
        rid = insert_run(conn, inst["id"], status="failed", date="2026-09-01")
        insert_run(conn, inst["id"], status="running")
        with pytest.raises(runs.BusyError):
            runs.resume(conn, rid, actor="web")

    def test_already_done_same_date(self, conn, inst):
        insert_run(conn, inst["id"], status="failed", date="2026-09-01", rid="r-f1")
        insert_run(conn, inst["id"], status="succeeded", date="2026-09-01", rid="r-ok")
        with pytest.raises(runs.AlreadyDoneError):
            runs.resume(conn, "r-f1", actor="web")

    def test_disabled_instrument(self, conn, inst):
        rid = insert_run(conn, inst["id"], status="failed")
        instruments.update(conn, inst["id"], enabled=False, actor="web")
        with pytest.raises(runs.ConflictError, match="停用"):
            runs.resume(conn, rid, actor="web")

    def test_not_found(self, conn):
        with pytest.raises(runs.NotFound):
            runs.resume(conn, "r-missing", actor="web")


# ---------- 查询与 Worker 接口 ----------


class TestQueries:
    def test_get_not_found(self, conn):
        with pytest.raises(runs.NotFound):
            runs.get(conn, "nope")

    def test_list_filters(self, conn, inst):
        insert_run(conn, inst["id"], status="succeeded", rid="a", date="2026-09-01")
        insert_run(conn, inst["id"], status="failed", rid="b", date="2026-09-02")
        assert [r["id"] for r in runs.list_runs(conn)] == ["b", "a"]  # created_at 同刻按 id 倒序兜底
        assert len(runs.list_runs(conn, status="failed")) == 1
        assert len(runs.list_runs(conn, code="1810.HK")) == 2
        assert len(runs.list_runs(conn, limit=1)) == 1
        with pytest.raises(runs.ValidationError):
            runs.list_runs(conn, status="bogus")

    def test_current_and_queue(self, conn, inst):
        insert_run(conn, inst["id"], status="running", rid="r-run")
        insert_run(conn, inst["id"], status="queued", rid="r-q1", date="2026-09-13")
        current, queued = runs.current_and_queue(conn)
        assert current["id"] == "r-run"
        assert [q["id"] for q in queued] == ["r-q1"]

    def test_artifacts(self, conn, inst, settings):
        from pathlib import Path

        (Path(settings.ta_data_dir) / "logs" / "1810.HK" / TODAY / "reports").mkdir(parents=True)
        (Path(settings.ta_data_dir) / "logs" / "1810.HK" / TODAY / "reports" / "investment_plan.md").touch()
        (Path(settings.ta_data_dir) / "logs" / "1810.HK" / TODAY / "reports" / "market_report.md").touch()
        rid = insert_run(conn, inst["id"], status="succeeded")
        out = runs.artifacts(conn, settings, rid)
        rel = f"logs/1810.HK/{TODAY}/reports/investment_plan.md"
        assert rel in out and len(out) == 2
        settings.smb_prefix = "\\\\192.168.1.150\\docker\\ta"
        out2 = runs.artifacts(conn, settings, rid)
        assert out2[0] == f"\\\\192.168.1.150\\docker\\ta\\{rel.replace('/', chr(92))}"

    def test_artifacts_empty(self, conn, inst, settings):
        rid = insert_run(conn, inst["id"], status="failed")
        assert runs.artifacts(conn, settings, rid) == []


class TestWorkerApi:
    def test_pick_next_queued_fifo(self, conn, inst):
        # 同 (标的,日期) 活动唯一,故用不同日期构造队列顺序
        for rid, date in [("r-late", "2026-09-12"), ("r-early", "2026-09-11")]:
            with db.tx(conn):
                conn.execute(
                    "INSERT INTO run(id, instrument_id, analysts_csv, analysis_date, status,"
                    " trigger, agents_total, created_at) VALUES (?,?,?,?,'queued','web',11,?)",
                    (
                        rid,
                        inst["id"],
                        "market,social,news",
                        date,
                        f"2026-09-14T08:00:{5 if rid == 'r-late' else 0:02d}+08:00",
                    ),
                )
        assert runs.pick_next_queued(conn)["id"] == "r-early"

    def test_pick_none(self, conn):
        assert runs.pick_next_queued(conn) is None

    def test_mark_running(self, conn, inst):
        rid = insert_run(conn, inst["id"], status="queued")
        runs.mark_running(conn, rid, "cid-1")
        row = runs.get(conn, rid)
        assert row["status"] == "running" and row["container_id"] == "cid-1"
        assert row["started_at"]
        with pytest.raises(runs.ConflictError):
            runs.mark_running(conn, rid, "cid-2")  # 已 running

    def test_update_progress(self, conn, inst):
        rid = insert_run(conn, inst["id"], status="running")
        runs.update_progress(
            conn,
            rid,
            current_agent="Bear Researcher",
            agents_done=3,
            tokens_in=100,
            tokens_out=50,
            stale=False,
        )
        row = runs.get(conn, rid)
        assert (
            row["current_agent"],
            row["agents_done"],
            row["tokens_in"],
            row["tokens_out"],
            row["status_stale"],
        ) == ("Bear Researcher", 3, 100, 50, False)
        runs.update_progress(
            conn, rid, current_agent=None, agents_done=None, tokens_in=None, tokens_out=None, stale=True
        )
        row = runs.get(conn, rid)
        assert (row["current_agent"], row["agents_done"], row["status_stale"]) == ("Bear Researcher", 3, True)

    def test_update_progress_ignored_when_not_running(self, conn, inst):
        rid = insert_run(conn, inst["id"], status="queued")
        runs.update_progress(
            conn, rid, current_agent="X", agents_done=1, tokens_in=1, tokens_out=1, stale=False
        )
        assert runs.get(conn, rid)["current_agent"] is None

    def test_finalize(self, conn, inst):
        rid = insert_run(conn, inst["id"], status="running")
        runs.finalize(conn, rid, status="succeeded", exit_code=0, report_ready=True)
        row = runs.get(conn, rid)
        assert row["status"] == "succeeded" and row["exit_code"] == 0
        assert row["report_ready"] is True and row["finished_at"]

    def test_finalize_bad_status(self, conn, inst):
        rid = insert_run(conn, inst["id"], status="running")
        with pytest.raises(runs.ValidationError):
            runs.finalize(conn, rid, status="running")

    def test_orphans_running(self, conn, inst):
        insert_run(conn, inst["id"], status="running", rid="r-o1")
        insert_run(conn, inst["id"], status="queued", rid="r-o2", date="2026-09-13")
        assert [r["id"] for r in runs.orphans_running(conn)] == ["r-o1"]
