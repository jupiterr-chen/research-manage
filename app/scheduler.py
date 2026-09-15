"""APScheduler 封装(DESIGN §4.9 / SPEC §6.5 / R-SCH-01~06)——替换 S1 stub。

- BackgroundScheduler + MemoryJobStore(SQLite 唯一事实源,约束 1;启动与
  schedule 变更时从 SQLite 全量重建)
- 时区 Asia/Shanghai;`analysis_date` = 触发时刻的 Asia/Shanghai 日期
- 触发动作只调 `services.runs.enqueue_scheduled`(去重/审计在服务层)
- 单工作线程执行器 + 按 schedule.id 升序注册:同 tick 触发顺序 = id 顺序(R-SCH-04)
- 附带每日 03:30 保留策略任务(R-EXE-11 的调度接线)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime
from sqlite3 import Connection
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import models, services
from app.config import Settings
from app.executor import retention

log = logging.getLogger("am.scheduler")

SH_TZ = ZoneInfo("Asia/Shanghai")
JOB_PREFIX = "am-sched-"
RETENTION_JOB_ID = "am-retention"
RETENTION_AT = (3, 30)  # 每日 03:30(R-EXE-11)


def _parse_at(at_time: str) -> tuple[int, int]:
    hour, minute = at_time.split(":")
    return int(hour), int(minute)


class Scheduler:
    def __init__(self, settings: Settings, db_factory: Callable[[], Connection]):
        self.settings = settings
        self.db_factory = db_factory
        # 单 worker:同一时刻多条调度按注册顺序(id 升序)串行入队
        self._scheduler = BackgroundScheduler(
            jobstores={"default": MemoryJobStore()},
            executors={"default": ThreadPoolExecutor(1)},
            timezone=SH_TZ,
            job_defaults={"coalesce": True, "misfire_grace_time": 300},
        )
        self._started = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._started:
            return
        self.rebuild_jobs()
        self._scheduler.start()
        self._started = True
        log.info("[scheduler] 启动,jobs=%d", self.jobs_count)

    def shutdown(self) -> None:
        if self._started:
            self._scheduler.shutdown(wait=False)
            self._started = False

    # ---------- 作业管理 ----------

    def _enabled_schedules(self) -> list[dict]:
        conn = self.db_factory()
        try:
            rows = conn.execute(
                "SELECT s.id, s.profile_id, s.kind, s.at_time, s.weekday, s.enabled,"
                "       i.code, i.enabled AS inst_enabled"
                " FROM schedule s JOIN instrument i ON i.id = s.instrument_id"
                " WHERE s.enabled=1 AND i.enabled=1 ORDER BY s.id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def rebuild_jobs(self) -> int:
        """从 schedule 表全量重建,返回 schedule job 数(不含保留策略任务)。"""
        schedules = self._enabled_schedules()
        for job in self._scheduler.get_jobs():
            if job.id == RETENTION_JOB_ID or job.id.startswith(JOB_PREFIX):
                self._scheduler.remove_job(job.id)
        for s in schedules:  # id 升序注册 → 同 tick 执行顺序 = id 顺序
            self._scheduler.add_job(
                self._fire,
                trigger=self._trigger_for(s),
                id=f"{JOB_PREFIX}{s['id']}",
                args=[s["id"]],
                replace_existing=True,
            )
        self._scheduler.add_job(
            self._fire_retention,
            trigger=CronTrigger(hour=RETENTION_AT[0], minute=RETENTION_AT[1], timezone=SH_TZ),
            id=RETENTION_JOB_ID,
            replace_existing=True,
        )
        return len(schedules)

    @staticmethod
    def _trigger_for(s: dict) -> CronTrigger:
        hour, minute = _parse_at(s["at_time"])
        if s["kind"] == "weekly":
            # 本系统 weekday 1=周一..7=周日;APScheduler 0=mon..6=sun
            return CronTrigger(day_of_week=s["weekday"] - 1, hour=hour, minute=minute, timezone=SH_TZ)
        return CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=SH_TZ)

    # ---------- 触发动作 ----------

    def _fire(self, schedule_id: int) -> None:
        conn = self.db_factory()
        try:
            analysis_date = models.today_sh().isoformat()
            run = services.runs.enqueue_scheduled(conn, schedule_id=schedule_id, date=analysis_date)
            if run is None:
                log.info("[scheduler] 调度 %s 在 %s 去重,未入队", schedule_id, analysis_date)
            else:
                log.info("[scheduler] 调度 %s 入队 run=%s", schedule_id, run["id"])
        except Exception:  # noqa: BLE001 - 单个调度失败不影响调度器
            log.exception("[scheduler] 调度 %s 入队失败", schedule_id)
        finally:
            conn.close()

    def _fire_retention(self) -> None:
        conn = self.db_factory()
        try:
            purged = retention.purge(conn, self.settings)
            if purged:
                log.info("[scheduler] 保留策略清理 %d 条", purged)
        except Exception:  # noqa: BLE001
            log.exception("[scheduler] 保留策略清理失败")
        finally:
            conn.close()

    # ---------- 预告 ----------

    def next_fires(self, today: date) -> list[dict]:
        """今日剩余触发(总览页用):[{schedule_id, code, at, analysts}]。"""
        now = models.now_sh()
        out: list[dict] = []
        conn = self.db_factory()
        try:
            profiles = {
                r["id"]: r["analysts_csv"] for r in conn.execute("SELECT id, analysts_csv FROM profile")
            }
        finally:
            conn.close()
        for s in self._enabled_schedules():
            nxt = self._trigger_for(s).get_next_fire_time(None, now)
            if nxt is None:
                continue
            nxt = nxt.astimezone(SH_TZ)
            if nxt.date() != today:
                continue
            out.append(
                {
                    "schedule_id": s["id"],
                    "code": s["code"],
                    "at": nxt.strftime("%H:%M"),
                    "analysts": profiles.get(s["profile_id"], ""),
                }
            )
        return out

    @property
    def jobs_count(self) -> int:
        return sum(1 for j in self._scheduler.get_jobs() if j.id.startswith(JOB_PREFIX))

    @property
    def running(self) -> bool:
        return self._started

    def job_next_fire(self, job_id: str) -> datetime | None:
        """测试/诊断辅助:某 job 的下次触发时间(SH 时区)。

        未启动的 scheduler 不会计算 next_run_time,此时退回 trigger 直接求值。
        """
        job = self._scheduler.get_job(job_id)
        if job is None:
            return None
        # APScheduler 3.11:未调度的 Job 访问 next_run_time 会 AttributeError
        nrt = getattr(job, "next_run_time", None)
        if nrt is not None:
            return nrt.astimezone(SH_TZ)
        nxt = job.trigger.get_next_fire_time(None, models.now_sh())
        return None if nxt is None else nxt.astimezone(SH_TZ)
