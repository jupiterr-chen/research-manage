"""seq 顺序 + 时间线视图 单测(方案:统一开始时间 + (seq,id) 顺序)。"""

from __future__ import annotations

import pytest

from app.db import connect
from app.scheduler import Scheduler
from app.services import instruments as inst_srv
from app.services import schedules as sch_srv


@pytest.fixture
def env(tmp_path, make_settings):
    s = make_settings(
        data_dir=str(tmp_path / "am"),
        data_host=str(tmp_path / "am"),
        ta_data_dir=str(tmp_path / "ta"),
        ta_data_host=str(tmp_path / "ta"),
    )
    from pathlib import Path

    Path(s.data_dir).mkdir(parents=True, exist_ok=True)
    s.db_path = str(Path(s.data_dir) / "t.db")
    conn = connect(s.db_path)
    from app import db as app_db

    app_db.migrate(conn)
    i1 = inst_srv.create(conn, market="hk", code="0700.HK", name="腾讯", actor="web")
    i2 = inst_srv.create(conn, market="us", code="NVDA", name="英伟达", actor="web")
    i3 = inst_srv.create(conn, market="cn", code="600519.SS", name="贵州茅台", actor="web")
    yield s, conn, (i1, i2, i3)
    conn.close()


class TestMigration:
    def test_fresh_db_has_seq(self, env):
        _, conn, _ = env
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(schedule)")]
        assert "seq" in cols

    def test_old_db_alter_adds_seq(self, tmp_path, make_settings):
        """老库(无 seq 列)经 migrate 幂等补列,默认 0。"""
        import sqlite3

        s = make_settings()
        from pathlib import Path

        Path(s.db_path).parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(s.db_path)
        raw.execute("PRAGMA foreign_keys=ON")
        raw.executescript("""
        CREATE TABLE instrument (id INTEGER PRIMARY KEY, market TEXT, code TEXT, name TEXT,
          enabled INTEGER, created_at TEXT, updated_at TEXT, UNIQUE(market, code));
        CREATE TABLE profile (id INTEGER PRIMARY KEY, name TEXT UNIQUE,
          analysts_csv TEXT, is_default INTEGER);
        CREATE TABLE schedule (id INTEGER PRIMARY KEY, instrument_id INTEGER, profile_id INTEGER,
          kind TEXT, at_time TEXT, weekday INTEGER, enabled INTEGER);
        INSERT INTO instrument VALUES (1,'hk','0700.HK','',1,'t','t');
        INSERT INTO profile VALUES (1,'p','market,social,news',1);
        INSERT INTO schedule VALUES (1,1,1,'daily_trading','08:30',NULL,1);
        """)
        raw.commit()
        raw.close()
        conn = connect(s.db_path)
        from app import db as app_db

        app_db.migrate(conn)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(schedule)")]
        assert "seq" in cols
        assert conn.execute("SELECT seq FROM schedule WHERE id=1").fetchone()["seq"] == 0
        conn.close()


class TestServiceSeq:
    def test_create_with_seq_and_default(self, env):
        _, conn, (i1, i2, _) = env
        a = sch_srv.create(
            conn,
            instrument_id=i1["id"],
            profile_id=1,
            kind="daily_trading",
            at_time="04:00",
            seq=5,
            actor="web",
        )
        b = sch_srv.create(
            conn, instrument_id=i2["id"], profile_id=1, kind="daily_trading", at_time="04:00", actor="web"
        )
        assert a["seq"] == 5 and b["seq"] == 0

    def test_update_seq(self, env):
        _, conn, (i1, _, _) = env
        a = sch_srv.create(conn, instrument_id=i1["id"], profile_id=1, kind="daily_trading", actor="web")
        b = sch_srv.update(conn, a["id"], seq=7, actor="web")
        assert b["seq"] == 7
        c = sch_srv.update(conn, a["id"], at_time="05:00", actor="web")  # 未给 seq 保持
        assert c["seq"] == 7

    def test_bad_seq_rejected(self, env):
        _, conn, (i1, _, _) = env
        with pytest.raises(sch_srv.ValidationError):
            sch_srv.create(
                conn, instrument_id=i1["id"], profile_id=1, kind="daily_trading", seq="x", actor="web"
            )  # type: ignore[arg-type]

    def test_timeline_rows_sorted(self, env):
        _, conn, (i1, i2, i3) = env
        sch_srv.create(
            conn,
            instrument_id=i1["id"],
            profile_id=1,
            kind="daily_trading",
            at_time="04:00",
            seq=3,
            actor="web",
        )  # id=1 seq=3
        sch_srv.create(
            conn,
            instrument_id=i2["id"],
            profile_id=1,
            kind="daily_trading",
            at_time="04:00",
            seq=1,
            actor="web",
        )  # id=2 seq=1
        sch_srv.create(
            conn,
            instrument_id=i3["id"],
            profile_id=1,
            kind="daily_trading",
            at_time="03:30",
            seq=9,
            actor="web",
        )  # id=3 更早时间
        sch_srv.create(
            conn,
            instrument_id=i1["id"],
            profile_id=2,
            kind="weekly",
            weekday=1,
            at_time="04:00",
            seq=0,
            actor="web",
        )  # id=4 周频
        rows = sch_srv.timeline(conn)
        dailies = [r for r in rows if r["kind"] == "daily_trading"]
        weeklies = [r for r in rows if r["kind"] == "weekly"]
        assert [r["id"] for r in dailies] == [3, 2, 1]  # 时间优先,同刻按 seq
        assert len(weeklies) == 1
        assert dailies[0]["code"] == "600519.SS"
        assert dailies[0]["describe"].startswith("贵州茅台:")
        assert rows.index(weeklies[0]) > rows.index(dailies[-1])  # 周频排在日频后


class TestSchedulerSeqOrder:
    def test_jobs_registered_by_seq_then_id(self, env, freezer):
        """同刻三条调度,注册与入队顺序 = (seq, id) 而非纯 id。"""
        s, conn, (i1, i2, i3) = env
        sch_srv.create(
            conn,
            instrument_id=i1["id"],
            profile_id=1,
            kind="daily_trading",
            at_time="04:00",
            seq=9,
            actor="web",
        )  # id=1, seq=9 → 最后
        sch_srv.create(
            conn,
            instrument_id=i2["id"],
            profile_id=1,
            kind="daily_trading",
            at_time="04:00",
            seq=1,
            actor="web",
        )  # id=2, seq=1 → 最先
        sch_srv.create(
            conn,
            instrument_id=i3["id"],
            profile_id=1,
            kind="daily_trading",
            at_time="04:00",
            seq=1,
            actor="web",
        )  # id=3, seq=1 → 第二
        from app.services import runs as runs_srv

        sched = Scheduler(s, db_factory=lambda: connect(s.db_path))
        with freezer("2026-09-29 04:00:00") as frozen:
            sched.rebuild_jobs()
            ids = [j.id for j in sched._scheduler.get_jobs() if j.id.startswith("am-sched-")]
            assert ids == ["am-sched-2", "am-sched-3", "am-sched-1"]  # (seq, id)
            for jid in ids:
                sched._scheduler.get_job(jid).func(int(jid.split("-")[-1]))
                frozen.tick(1)
        rows = sorted(runs_srv.list_runs(conn), key=lambda r: r["created_at"])
        assert [r["code"] for r in rows] == ["NVDA", "600519.SS", "0700.HK"]

    def test_next_fires_sorted_by_time_then_seq(self, env, freezer):
        from datetime import date as d

        s, conn, (i1, i2, i3) = env
        sch_srv.create(
            conn,
            instrument_id=i1["id"],
            profile_id=1,
            kind="daily_trading",
            at_time="09:00",
            seq=9,
            actor="web",
        )
        sch_srv.create(
            conn,
            instrument_id=i2["id"],
            profile_id=1,
            kind="daily_trading",
            at_time="09:00",
            seq=1,
            actor="web",
        )
        sch_srv.create(
            conn,
            instrument_id=i3["id"],
            profile_id=1,
            kind="daily_trading",
            at_time="08:00",
            seq=5,
            actor="web",
        )
        sched = Scheduler(s, db_factory=lambda: connect(s.db_path))
        with freezer("2026-09-29 07:00:00"):
            fires = sched.next_fires(d(2026, 9, 29))
        assert [(f["code"], f["seq"]) for f in fires] == [("600519.SS", 5), ("NVDA", 1), ("0700.HK", 9)]


class TestTimelineView:
    def test_page_renders_both_views(self, env):
        from fastapi.testclient import TestClient

        from app.scheduler import Scheduler
        from app.web.server import create_app

        class NoopWorker:
            def self_check(self):
                return []

            def start(self):
                pass

            def stop(self):
                pass

            def health(self):
                return {
                    "alive": True,
                    "docker_ok": None,
                    "queue_depth": 0,
                    "current_run_id": None,
                    "last_tick": None,
                }

        s, conn, (i1, _, _) = env
        sch_srv.create(
            conn,
            instrument_id=i1["id"],
            profile_id=1,
            kind="daily_trading",
            at_time="04:00",
            seq=2,
            actor="web",
        )
        app = create_app(
            s, worker=NoopWorker(), scheduler=Scheduler(s, db_factory=lambda: connect(s.db_path))
        )
        with TestClient(app) as c:
            c.headers.update({"Authorization": f"Bearer {s.token}"})
            r1 = c.get("/instruments?view=timeline")
            assert r1.status_code == 200
            assert "时间线" in r1.text and "每日交易日" in r1.text
            assert "0700.HK" in r1.text and ">2<" in r1.text  # seq 列
            assert "每周" in r1.text
            r2 = c.get("/instruments")
            assert "时间线" in r2.text and 'href="/instruments?view=timeline"' in r2.text
            assert 'name="seq"' in r2.text  # 编辑表单含 seq 输入
            # 无外链约束延续
            assert "http://" not in r1.text and "https://" not in r1.text
