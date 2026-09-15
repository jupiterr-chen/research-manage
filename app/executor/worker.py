"""Worker 线程(DESIGN §4.8 / SPEC §6.4 / R-EXE-01~12)——替换 S1 stub。

单线程消费队列:tick 1s;唯一的 docker-py 使用方(ADR-1)。
监控子循环:wait(5s) + 每 AM_STATUS_POLL_SECONDS 同步 status.json → SQLite;
取消(cancel_requested_at)与看门狗由本线程执行 docker stop;
终态按 SPEC §6.4 双重判定;container.log 经 scrub 落档后 remove;
任何异常记录日志、当前 run 置 failed(worker_exception:<类名>)、线程不退出。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from sqlite3 import Connection
from zoneinfo import ZoneInfo

from app import services
from app.audit import scrub
from app.config import Settings
from app.executor import verdict as verdict_mod
from app.executor.launcher import ContainerSpec, parse_sim_env
from app.executor.status_reader import Status, read_status
from app.models import now_sh

log = logging.getLogger("am.worker")

TICK_SECONDS = 1.0
WAIT_SLICE = 1  # 每 tick 的 wait 切片,保证进度/取消/看门狗的检查粒度 = tick
CONTAINER_LOG_LINES = 200


class Worker(threading.Thread):
    def __init__(self, settings: Settings, launcher, db_factory: Callable[[], Connection]):
        super().__init__(daemon=False, name="am-worker")
        # 注意不能命名为 _stop:会遮蔽 threading.Thread._stop
        self.settings = settings
        self.launcher = launcher
        self.db_factory = db_factory
        self._stop_event = threading.Event()
        self._tz = ZoneInfo(settings.tz or "Asia/Shanghai")
        # 当前任务内存态(仅本线程读写)
        self._current: dict | None = None
        self._cid: str | None = None
        self._cancelled_by_us = False
        self._cancel_error: str | None = None  # 看门狗时预置 watchdog_timeout
        self._last_status: Status | None = None
        self._last_progress_at: float | None = None
        self._docker_ok: bool | None = None
        self._last_tick: datetime | None = None

    # ---------- 生命周期 ----------

    def self_check(self) -> list[str]:
        """R-EXE-03:docker 可达、镜像存在、AM_TA_DATA_DIR 可读、AM_DATA_DIR 可写。"""
        errors: list[str] = []
        if not self.launcher.ping():
            errors.append("docker 不可达(docker.sock / DOCKER_HOST)")
        elif not self.launcher.image_exists(self.settings.ta_image):
            errors.append(f"镜像不存在:{self.settings.ta_image}")
        try:
            if not Path(self.settings.ta_data_dir).is_dir():
                errors.append(f"AM_TA_DATA_DIR 不存在或不可读:{self.settings.ta_data_dir}")
        except OSError as e:
            errors.append(f"AM_TA_DATA_DIR 不可读:{e}")
        try:
            Path(self.settings.data_dir).mkdir(parents=True, exist_ok=True)
            probe = Path(self.settings.data_dir) / ".am_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            errors.append(f"AM_DATA_DIR 不可写:{e}")
        return errors

    def stop(self) -> None:
        """置停止标志;不 stop 执行容器(DESIGN §2,容器由下次启动 recover 接管)。"""
        self._stop_event.set()

    def run(self) -> None:
        errors = self.self_check()
        if errors:
            for line in errors:
                log.error("[worker] 自检失败:%s", line)
            log.error("[worker] 自检失败,工作线程退出(应用应已在 lifespan 拒绝启动)")
            return
        try:
            self._recover_once()
        except Exception:  # noqa: BLE001
            log.exception("[worker] 恢复阶段异常")
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 - R-EXE-12 线程不退出
                log.exception("[worker] tick 异常")
                self._fail_current(f"worker_exception:{type(e).__name__}")
            self._stop_event.wait(TICK_SECONDS)

    # ---------- 恢复与主循环 ----------

    def _recover_once(self) -> None:
        """R-EXE-02:孤儿 running → 容器不在则 failed(host_restarted);在则接管。"""
        conn = self.db_factory()
        try:
            for row in services.runs.orphans_running(conn):
                if self.launcher.exists(row["container_id"] or ""):
                    if self._current is None:
                        log.info("[worker] 接管运行中容器 run=%s cid=%s", row["id"], row["container_id"])
                        self._current = row
                        self._cid = row["container_id"]
                    else:  # pragma: no cover - 并发=1 下不应出现
                        log.warning("[worker] 多个孤儿 running,仅接管一个:%s", row["id"])
                else:
                    services.runs.finalize(
                        conn,
                        row["id"],
                        status="failed",
                        exit_code=None,
                        error="host_restarted",
                        report_ready=False,
                    )
                    log.info("[worker] 孤儿 run=%s 置 failed(host_restarted)", row["id"])
        finally:
            conn.close()

    def _tick(self) -> None:
        self._last_tick = datetime.now(self._tz)
        try:
            self._docker_ok = self.launcher.ping()
        except Exception:  # noqa: BLE001
            self._docker_ok = False
        if self._current is not None:
            self._monitor()
        else:
            self._launch_next()

    def _launch_next(self) -> None:
        conn = self.db_factory()
        try:
            run = services.runs.pick_next_queued(conn)
            if run is None:
                return
            run_dir = Path(self.settings.data_host) / "runs" / run["id"]
            run_dir.mkdir(parents=True, exist_ok=True)
            spec = ContainerSpec(
                image=self.settings.ta_image,
                run_id=run["id"],
                ticker=run["code"],
                date=run["analysis_date"],
                analysts=tuple(run["analysts_csv"].split(",")),
                ta_data_host=self.settings.ta_data_host,
                ta_env_host=self.settings.ta_env_host,
                runner_host=self.settings.runner_host,
                workspace_host=str(run_dir),
                ta_container_data=self.settings.ta_container_data,
                workdir=self.settings.ta_workdir,
                stop_timeout=self.settings.stop_timeout,
                extra_env=parse_sim_env(self.settings.sim_env),
            )
            try:
                cid = self.launcher.start(spec)
            except Exception as e:  # noqa: BLE001
                log.exception("[worker] 启动容器失败 run=%s", run["id"])
                services.runs.finalize(
                    conn,
                    run["id"],
                    status="failed",
                    exit_code=None,
                    error=f"launch_failed: {type(e).__name__}: {str(e)[:150]}",
                    report_ready=False,
                )
                return
            services.runs.mark_running(conn, run["id"], cid)
            self._current = run
            self._cid = cid
            self._cancelled_by_us = False
            self._cancel_error = None
            self._last_status = None
            self._last_progress_at = time.monotonic()
            log.info("[worker] run=%s 启动 cid=%s", run["id"], cid)
        finally:
            conn.close()

    def _monitor(self) -> None:
        run = self._current
        # 每 tick 只等 1s:进度同步、取消、看门狗的检查粒度与 tick 一致
        exit_code = self.launcher.wait(self._cid, timeout=WAIT_SLICE)
        now_mono = time.monotonic()

        # 进度同步(R-EXE-05):每 AM_STATUS_POLL_SECONDS 读一次 status.json
        if (
            self._last_progress_at is None
            or now_mono - self._last_progress_at >= self.settings.status_poll_seconds
        ):
            self._sync_progress(run, now_mono)

        # 取消与看门狗(R-EXE-06/07):docker 调用只在此处
        conn = self.db_factory()
        try:
            fresh = services.runs.get(conn, run["id"])
        finally:
            conn.close()
        if fresh["cancel_requested_at"] and not self._cancelled_by_us:
            log.info("[worker] 收到取消请求 run=%s", run["id"])
            self._cancelled_by_us = True
            self.launcher.stop(self._cid, self.settings.stop_timeout)
        elif not self._cancelled_by_us:
            started_at = fresh.get("started_at")
            if started_at:
                try:
                    elapsed = (now_sh() - datetime.fromisoformat(started_at)).total_seconds()
                except ValueError:
                    elapsed = 0
                if elapsed > self.settings.watchdog_minutes * 60:
                    log.warning("[worker] 看门狗超时 run=%s(%.0fs)", run["id"], elapsed)
                    self._cancelled_by_us = True
                    self._cancel_error = "watchdog_timeout"
                    self.launcher.stop(self._cid, self.settings.stop_timeout)

        if exit_code is None:
            return
        self._finalize(run, exit_code)

    def _sync_progress(self, run: dict, now_mono: float) -> None:
        status_path = Path(self.settings.data_dir) / "runs" / run["id"] / "status.json"
        s = read_status(status_path)
        stale = False
        if s is not None:
            self._last_status = s
            if s.updated_at is not None:
                age = (now_sh() - s.updated_at).total_seconds()
                stale = age > self.settings.stale_minutes * 60
            pushed = {
                "current_agent": s.current_agent,
                "agents_done": s.agents_done,
                "tokens_in": s.tokens_in,
                "tokens_out": s.tokens_out,
            }
        else:
            # 文件缺失/半截/损坏:保持上次值 + stale(AM-08)
            stale = True
            pushed = {"current_agent": None, "agents_done": None, "tokens_in": None, "tokens_out": None}
        conn = self.db_factory()
        try:
            services.runs.update_progress(conn, run["id"], stale=stale, **pushed)
        finally:
            conn.close()
        self._last_progress_at = now_mono

    def _finalize(self, run: dict, exit_code: int) -> None:
        v = verdict_mod.decide(
            exit_code=exit_code,
            cancelled_by_us=self._cancelled_by_us,
            status=self._last_status,
            ta_data_dir=Path(self.settings.ta_data_dir),
            code=run["code"],
            date=run["analysis_date"],
        )
        error = v.error
        if v.status == "cancelled" and self._cancel_error:
            error = self._cancel_error  # 看门狗语义(R-EXE-07)
        # container.log 落档(经 scrub)后 remove;失败不影响终态(R-EXE-10 / AM-18)
        try:
            log_text = scrub(self.launcher.logs_tail(self._cid, CONTAINER_LOG_LINES))
            log_dir = Path(self.settings.data_dir) / "runs" / run["id"]
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "container.log").write_text(log_text, encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.exception("[worker] container.log 落档失败 run=%s", run["id"])
        try:
            self.launcher.remove(self._cid)
        except Exception:  # noqa: BLE001
            log.exception("[worker] remove 容器失败 run=%s", run["id"])
        conn = self.db_factory()
        try:
            services.runs.finalize(
                conn,
                run["id"],
                status=v.status,
                exit_code=exit_code,
                error=error,
                report_ready=v.report_ready,
            )
        finally:
            conn.close()
        log.info("[worker] run=%s 终态 %s exit=%s error=%s", run["id"], v.status, exit_code, error)
        self._current = None
        self._cid = None
        self._cancelled_by_us = False
        self._cancel_error = None
        self._last_status = None

    def _fail_current(self, error: str) -> None:
        """R-EXE-12:worker 异常时当前 run 置 failed,线程继续。"""
        if self._current is None:
            return
        run_id = self._current["id"]
        cid = self._cid
        try:
            conn = self.db_factory()
            try:
                services.runs.finalize(
                    conn, run_id, status="failed", exit_code=None, error=error, report_ready=False
                )
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            log.exception("[worker] _fail_current 失败 run=%s", run_id)
        if cid:
            try:
                self.launcher.stop(cid, self.settings.stop_timeout)
                self.launcher.remove(cid)
            except Exception:  # noqa: BLE001
                log.exception("[worker] 清理失败 run=%s", run_id)
        self._current = None
        self._cid = None
        self._cancelled_by_us = False
        self._cancel_error = None

    # ---------- 观察 ----------

    def health(self) -> dict:
        """{alive, docker_ok, queue_depth, current_run_id, last_tick}(DESIGN §4.8)。"""
        queue_depth, current_run_id = 0, None
        try:
            conn = self.db_factory()
            try:
                current, queued = services.runs.current_and_queue(conn)
                queue_depth = len(queued)
                current_run_id = current["id"] if current else None
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - health 探测不抛
            log.debug("health 读取队列失败", exc_info=True)
        return {
            "alive": self.is_alive(),
            "docker_ok": self._docker_ok,
            "queue_depth": queue_depth,
            "current_run_id": current_run_id,
            "last_tick": self._last_tick.isoformat(timespec="seconds") if self._last_tick else None,
        }
