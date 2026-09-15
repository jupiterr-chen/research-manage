"""app/scheduler.py 单测(R-SCH-01~05 / AM-02/10/12;freezegun)。"""

from __future__ import annotations

from datetime import date

import pytest

from app.db import connect
from app.scheduler import RETENTION_JOB_ID, SH_TZ, Scheduler
from app.services import instruments as inst_srv
from app.services import runs as runs_srv
from app.services import schedules as sch_srv


@pytest.fixture
def sched_env(tmp_path, make_settings):
    s = make_settings(
        data_dir=str(tmp_path / "am"),
        data_host=str(tmp_path / "am"),
        ta_data_dir=str(tmp_path / "ta"),
        ta_data_host=str(tmp_path / "ta"),
    )
    from pathlib import Path

    Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    s.db_path = str(Path(s.data_dir) / "test.db")
    Path(s.ta_data_dir).mkdir(parents=True, exist_ok=True)
    conn = connect(s.db_path)
    from app import db as app_db

    app_db.migrate(conn)
    inst = inst_srv.create(conn, market="hk", code="1810.HK", name="小米集团", actor="web")
    sched = Scheduler(s, db_factory=lambda: connect(s.db_path))
    return s, conn, sched, inst


def add_schedule(conn, inst, *, kind="daily_trading", at="08:30", weekday=None, profile_id=1, enabled=True):
    return sch_srv.create(
        conn,
        instrument_id=inst["id"],
        profile_id=profile_id,
        kind=kind,
        at_time=at,
        weekday=weekday,
        enabled=enabled,
        actor="web",
    )


class TestRebuildJobs:
    def test_creates_jobs_for_enabled_only(self, sched_env):
        _, conn, sched, inst = sched_env
        add_schedule(conn, inst)  # id=1 enabled
        add_schedule(conn, inst, at="09:00", enabled=False)  # disabled
        n = sched.rebuild_jobs()
        assert n == 1
        assert sched.jobs_count == 1

    def test_disabled_instrument_excluded(self, sched_env):
        _, conn, sched, inst = sched_env
        add_schedule(conn, inst)
        inst_srv.update(conn, inst["id"], enabled=False, actor="web")
        assert sched.rebuild_jobs() == 0

    def test_jobs_registered_in_id_order(self, sched_env):
        """R-SCH-04:同 tick 触发顺序 = id 顺序(job 注册顺序 + 单线程执行器)。"""
        _, conn, sched, inst = sched_env
        for at in ("08:30", "08:30", "08:30"):
            add_schedule(conn, inst, at=at)
        sched.rebuild_jobs()
        ids = [j.id for j in sched._scheduler.get_jobs() if j.id.startswith("am-sched-")]
        assert ids == ["am-sched-1", "am-sched-2", "am-sched-3"]
        # 单 worker:同一时刻的触发串行按注册顺序执行(APScheduler 包装层 → _pool)
        executor = sched._scheduler._executors["default"]
        assert executor._pool._max_workers == 1

    def test_retention_job_registered(self, sched_env):
        """R-EXE-11:每日 03:30 保留策略任务。"""
        _, conn, sched, inst = sched_env
        add_schedule(conn, inst)
        sched.rebuild_jobs()
        job = sched._scheduler.get_job(RETENTION_JOB_ID)
        assert job is not None
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields.get("hour") == "3" and fields.get("minute") == "30"

    def test_rebuild_is_idempotent(self, sched_env):
        _, conn, sched, inst = sched_env
        add_schedule(conn, inst)
        sched.rebuild_jobs()
        sched.rebuild_jobs()
        assert sched.jobs_count == 1


class TestTriggerSemantics:
    def test_daily_never_fires_weekend(self, sched_env, freezer):
        """AM-12:周六日不触发(daily → mon-fri)。"""
        _, conn, sched, inst = sched_env
        add_schedule(conn, inst, at="08:30")
        sched.rebuild_jobs()
        with freezer("2026-09-12 09:00:00"):  # 周六上午
            nxt = sched.job_next_fire("am-sched-1")
            assert nxt is not None and nxt.astimezone(SH_TZ).weekday() == 0  # 下周一
            assert nxt.astimezone(SH_TZ).date() == date(2026, 9, 14)
            assert sched.next_fires(date(2026, 9, 12)) == []  # 今日(周六)无触发
        with freezer("2026-09-13 20:00:00"):  # 周日
            assert sched.next_fires(date(2026, 9, 13)) == []

    def test_weekly_weekday_mapping(self, sched_env, freezer):
        """weekly weekday=1(周一)→ APScheduler day_of_week=0。"""
        _, conn, sched, inst = sched_env
        add_schedule(conn, inst, kind="weekly", weekday=1, at="09:00")
        sched.rebuild_jobs()
        job = sched._scheduler.get_job("am-sched-1")
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields.get("day_of_week") == "0"
        with freezer("2026-09-15 10:00:00"):  # 周二
            nxt = sched.job_next_fire("am-sched-1")
            assert nxt.astimezone(SH_TZ).date() == date(2026, 9, 21)  # 下周一

    def test_daily_fires_today_before_at_time(self, sched_env, freezer):
        _, conn, sched, inst = sched_env
        add_schedule(conn, inst, at="08:30")
        with freezer("2026-09-14 08:00:00"):  # 周一 08:00
            fires = sched.next_fires(date(2026, 9, 14))
            assert fires == [
                {"schedule_id": 1, "code": "1810.HK", "at": "08:30", "analysts": "market,social,news"}
            ]
        with freezer("2026-09-14 08:31:00"):  # 已过点 → 今日无剩余
            assert sched.next_fires(date(2026, 9, 14)) == []

    def test_next_fires_orders_and_lists(self, sched_env, freezer):
        _, conn, sched, inst = sched_env
        add_schedule(conn, inst, at="08:30", profile_id=2)  # id=1 全量
        add_schedule(conn, inst, at="08:30")  # id=2 三件套
        with freezer("2026-09-14 07:00:00"):
            fires = sched.next_fires(date(2026, 9, 14))
        assert [f["schedule_id"] for f in fires] == [1, 2]
        assert fires[0]["analysts"] == "market,social,news,fundamentals"


class TestFireAction:
    def test_fire_enqueues_with_profile_snapshot(self, sched_env, freezer):
        with freezer("2026-09-14 08:30:00"):  # 周一
            _, conn, sched, inst = sched_env
            add_schedule(conn, inst, profile_id=2)  # 全量
            sched._fire(1)
            rows = runs_srv.list_runs(conn)
            assert len(rows) == 1
            assert rows[0]["trigger"] == "schedule"
            assert rows[0]["analysts_csv"] == "market,social,news,fundamentals"
            assert rows[0]["analysis_date"] == "2026-09-14"  # 触发时刻 SH 日期

    def test_fire_same_day_twice_deduped(self, sched_env, freezer):
        with freezer("2026-09-14 08:30:00"):
            _, conn, sched, inst = sched_env
            add_schedule(conn, inst)
            sched._fire(1)
            sched._fire(1)  # 同日重触发 → 去重
            assert len(runs_srv.list_runs(conn)) == 1

    def test_monday_daily_and_weekly_single_full_run(self, sched_env, freezer):
        """AM-10:周一同时命中日频+周频 → 只产生 1 条全量 run(超集覆盖)。"""
        with freezer("2026-09-14 08:30:00"):
            _, conn, sched, inst = sched_env
            add_schedule(conn, inst, profile_id=1)  # id=1 日频三件套
            add_schedule(conn, inst, kind="weekly", weekday=1, profile_id=2)  # id=2 周一全量
            sched.rebuild_jobs()
            for job_id in ("am-sched-1", "am-sched-2"):  # 模拟同 tick 按 id 顺序触发
                sched._scheduler.get_job(job_id).func(int(job_id.split("-")[-1]))
            rows = runs_srv.list_runs(conn)
            assert len(rows) == 1
            assert rows[0]["analysts_csv"] == "market,social,news,fundamentals"
            assert (
                conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action='deduped'").fetchone()["n"]
                == 1
            )

    def test_same_tick_three_schedules_id_order(self, sched_env, freezer):
        """R-SCH-04:同一 tick 三条调度按 id 顺序入队(FIFO 可预测)。"""
        with freezer("2026-09-14 08:30:00") as frozen:
            _, conn, sched, inst = sched_env
            other = inst_srv.create(conn, market="us", code="NVDA", actor="web")
            add_schedule(conn, inst, at="08:30")  # id=1
            add_schedule(conn, inst, kind="weekly", weekday=1, at="08:30")  # id=2 同标的
            sch_srv.create(
                conn,
                instrument_id=other["id"],
                profile_id=1,
                kind="daily_trading",
                at_time="08:30",
                actor="web",
            )  # id=3
            sched.rebuild_jobs()
            for job_id in ("am-sched-1", "am-sched-2", "am-sched-3"):
                sched._scheduler.get_job(job_id).func(int(job_id.split("-")[-1]))
                frozen.tick(1)  # 同 tick 内递增 1s,使 created_at 可区分入队顺序
            rows = runs_srv.list_runs(conn)  # created_at desc
            assert len(rows) == 2  # 1、2 去重为全量 + 3
            ordered = sorted(rows, key=lambda r: r["created_at"])
            assert ordered[0]["instrument_id"] == inst["id"]  # 先 id=1/2
            assert ordered[1]["instrument_id"] == other["id"]  # 后 id=3

    def test_fire_disabled_schedule_noop(self, sched_env, freezer):
        with freezer("2026-09-14 08:30:00"):
            _, conn, sched, inst = sched_env
            add_schedule(conn, inst, enabled=False)
            sched._fire(1)
            assert runs_srv.list_runs(conn) == []

    def test_fire_unknown_schedule_no_raise(self, sched_env, freezer):
        with freezer("2026-09-14 08:30:00"):
            _, conn, sched, _inst = sched_env
            sched._fire(999)  # 不抛(单个调度失败不影响调度器)


class TestLifecycle:
    def test_start_shutdown_idempotent(self, sched_env):
        _, conn, sched, inst = sched_env
        add_schedule(conn, inst)
        sched.start()
        assert sched.running is True and sched.jobs_count == 1
        sched.start()  # 二次 start 安全
        assert sched.running is True
        sched.shutdown()
        sched.shutdown()  # 二次 shutdown 安全
        assert sched.running is False

    def test_on_change_hook_called(self, sched_env, monkeypatch):
        """R-SCH-01:写操作后触发 rebuild(经 server 接线的钩子)。"""
        _, conn, sched, inst = sched_env
        calls = []
        monkeypatch.setattr(sch_srv, "on_change", lambda: calls.append(1))
        add_schedule(conn, inst)
        sch_srv.toggle(conn, 1, actor="web")
        sch_srv.update(conn, 1, at_time="09:00", actor="web")
        sch_srv.delete(conn, 1, actor="web")
        assert len(calls) == 4

    def test_on_change_hook_exception_swallowed(self, sched_env, monkeypatch):
        _, conn, sched, inst = sched_env

        def boom():
            raise RuntimeError("rebuild failed")

        monkeypatch.setattr(sch_srv, "on_change", boom)
        d = add_schedule(conn, inst)  # 不因钩子异常失败
        assert d["id"] == 1

    def test_executor_submits_fire_serially(self, sched_env, freezer):
        """同 tick 三条调度经单线程执行器串行:直接验证 executor 提交语义的替代——
        rebuild 注册顺序 = id 升序(上方已测)+ 本用例验证 _fire 可重入无并发问题。"""
        with freezer("2026-09-14 08:30:00"):
            _, conn, sched, inst = sched_env
            add_schedule(conn, inst)
            add_schedule(conn, inst, kind="weekly", weekday=1, profile_id=2)
            sched.rebuild_jobs()
            sched._fire(1)
            sched._fire(2)  # 同 tick 顺序触发
            rows = runs_srv.list_runs(conn)
            assert len(rows) == 1  # 去重为全量
            assert rows[0]["analysts_csv"] == "market,social,news,fundamentals"
