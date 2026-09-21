"""Phase 1 + coordination — load config, start/stop, fan out runners.

Maps to:
  Phase 1  Start Tasks / arm schedule
  Phase 2  MonitorService (stock → trigger)
  Phase 3–8 TaskRunner per activated task (parallel threads)
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime
from typing import Optional

from raindance.captcha.service import CaptchaService
from raindance.evasion.proxy_groups import ProxyGroupManager
from raindance.profiles.store import ProfileStore
from raindance.tasks import models as M
from raindance.tasks.monitor import MonitorService
from raindance.tasks.runner import TaskRunner
from raindance.tasks.store import TaskStore


def _parse_start_at(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        if "-" in value or "T" in value:
            return datetime.fromisoformat(value.replace("T", " "))
        parts = [int(x) for x in value.split(":")]
        parts += [0] * (3 - len(parts))
        h, m, s = parts[:3]
        return datetime.now().replace(hour=h, minute=m, second=s, microsecond=0)
    except (ValueError, IndexError):
        return None


class Orchestrator:
    """Central bot controller exposed to the GUI as ctx.orchestrator."""

    def __init__(self, *, settings, bus, hub, max_workers: int = 8):
        self.settings = settings
        self.bus = bus
        self.hub = hub
        self.max_workers = max_workers

        self.task_store = TaskStore(settings)
        self.profile_store = ProfileStore()
        self.proxy_groups = ProxyGroupManager()
        self.captcha = CaptchaService(settings.data, bus=bus)

        self._stop = threading.Event()
        self._schedule_thread: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()
        self.state = {
            "running": False,
            "phase": "idle",
            "monitoring": False,
            "active_tasks": 0,
            "started_at": "",
        }

        self.monitor = MonitorService(
            bus=bus,
            hub=hub,
            task_store=self.task_store,
            proxy_groups=self.proxy_groups,
            settings=settings,
            on_stock=self._on_stock,
        )
        self.reload_managers()

    # -- Phase 0/1 load ---------------------------------------------------- #
    def reload_managers(self) -> None:
        self.proxy_groups.load_from_settings(self.settings.data)
        self.captcha.configure(self.settings.data)
        # ensure at least one profile exists for new users
        self.profile_store.ensure_default()
        self.bus.log(
            f"[orch] managers loaded — proxies={self.proxy_groups.total()} "
            f"groups={self.proxy_groups.group_names()} "
            f"profiles={len(self.profile_store.list())} "
            f"tasks={len(self.task_store.list())}",
            "info",
        )

    @property
    def is_running(self) -> bool:
        return bool(self.state.get("running"))

    # -- Phase 1: Start / Stop --------------------------------------------- #
    def start(self, *, force_checkout: bool = False) -> bool:
        """Arm the bot. force_checkout=True skips monitor and runs enabled tasks now."""
        if self.is_running:
            self.bus.log("[orch] already running", "warn")
            return False

        self.reload_managers()
        self._stop.clear()
        self.state.update(
            running=True,
            phase="starting",
            started_at=datetime.now().isoformat(timespec="seconds"),
            active_tasks=0,
        )
        self.bus.log("[orch] Phase 1 — Start Tasks", "ok")

        # Mark enabled tasks armed
        for t in self.task_store.list(enabled_only=True):
            self.task_store.set_runtime(
                t["id"], status=M.STATUS_ARMED, message="armed", last_error="",
                denied=False
            )

        workers = int(
            (self.settings.data.get("orchestrator") or {}).get("max_workers")
            or self.max_workers
        )
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="task")

        # Schedule gate
        start_at = (self.settings.data.get("schedule") or {}).get("start_at") or ""
        target = _parse_start_at(start_at)
        if target and target > datetime.now() and not force_checkout:
            self.state["phase"] = "scheduled"
            self.bus.log(
                f"[orch] scheduled — waiting until {target:%Y-%m-%d %H:%M:%S}",
                "info",
            )

            def _wait():
                while not self._stop.is_set():
                    if datetime.now() >= target:
                        break
                    time.sleep(0.4)
                if self._stop.is_set():
                    return
                self._begin_work(force_checkout=force_checkout)

            self._schedule_thread = threading.Thread(
                target=_wait, daemon=True, name="orch-schedule"
            )
            self._schedule_thread.start()
            return True

        self._begin_work(force_checkout=force_checkout)
        return True

    def _begin_work(self, *, force_checkout: bool) -> None:
        if self._stop.is_set() or not self.state.get("running"):
            return
        tasks = self.task_store.list(enabled_only=True)
        if not tasks:
            self.bus.log("[orch] no enabled tasks — idle", "warn")
            self.state["phase"] = "idle_no_tasks"
            return

        monitor_tasks = [t for t in tasks if t.get("monitor", True) and not force_checkout]
        immediate = [t for t in tasks if (not t.get("monitor", True)) or force_checkout]

        if monitor_tasks:
            self.state["phase"] = "monitoring"
            self.state["monitoring"] = True
            self.monitor.start()
            self.bus.log(
                f"[orch] Phase 2 — monitoring {len(monitor_tasks)} task(s)",
                "info",
            )

        for t in immediate:
            self.activate_task(t)

        if not monitor_tasks and not immediate:
            self.bus.log("[orch] nothing to run", "warn")

    def stop(self) -> None:
        self.bus.log("[orch] stop requested", "warn")
        self._stop.set()
        self.monitor.stop()
        self.state["monitoring"] = False
        self.state["phase"] = "stopping"
        # Cancel pending futures (running ones finish cooperatively)
        with self._lock:
            for fut in self._futures.values():
                fut.cancel()
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
        for t in self.task_store.list():
            st = t.get("status")
            if st not in (M.STATUS_SUCCESS, M.STATUS_FAILED, M.STATUS_IDLE):
                self.task_store.set_runtime(
                    t["id"], status=M.STATUS_STOPPED, message="stopped"
                )
        self.state.update(running=False, phase="idle", active_tasks=0)
        self.bus.log("[orch] stopped", "info")

    # -- stock trigger → Phase 3 ------------------------------------------- #
    def _on_stock(self, task: dict) -> None:
        if self._stop.is_set() or not self.state.get("running"):
            return
        # Activate all enabled tasks sharing the same URL (multi-profile parallel)
        url = (task.get("url") or "").strip()
        siblings = [
            t for t in self.task_store.list(enabled_only=True)
            if (t.get("url") or "").strip() == url
            # Terminal states are deliberately absent. A sibling's stock hit
            # used to drag FAILED and STOPPED tasks back into a live run, so a
            # task that failed checkout was re-fired on every subsequent stock
            # event with no bound. start() re-arms every enabled task to ARMED
            # and clears last_error, so recovery stays an explicit user action.
            and t.get("status") in (
                M.STATUS_MONITORING, M.STATUS_ARMED, M.STATUS_TRIGGERED,
                M.STATUS_IDLE,
            )
        ]
        if not siblings:
            siblings = [task]
        self.bus.log(
            f"[orch] activating {len(siblings)} task(s) for stock URL",
            "hit",
        )
        for t in siblings:
            self.activate_task(t)

    def activate_task(self, task: dict) -> None:
        """Phase 3 entry — submit TaskRunner to the pool."""
        if self._stop.is_set():
            return
        tid = task["id"]
        with self._lock:
            fut = self._futures.get(tid)
            if fut and not fut.done():
                self.bus.log(f"[orch] task {tid} already running", "warn")
                return
            if not self._pool:
                workers = int(
                    (self.settings.data.get("orchestrator") or {}).get("max_workers")
                    or self.max_workers
                )
                self._pool = ThreadPoolExecutor(
                    max_workers=max(1, workers), thread_name_prefix="task"
                )
            self.state["phase"] = "checkout"
            self.state["active_tasks"] = self.state.get("active_tasks", 0) + 1
            future = self._pool.submit(self._run_with_retries, dict(task))
            self._futures[tid] = future
            future.add_done_callback(lambda f, i=tid: self._done(i, f))

    def _run_with_retries(self, task: dict) -> dict:
        tid = task["id"]
        max_retries = int(task.get("max_retries") or 0)
        attempt = 0
        last: dict = {"ok": False, "error": "not started", "task_id": tid}
        while attempt <= max_retries:
            if self._stop.is_set():
                self.task_store.set_runtime(
                    tid, status=M.STATUS_STOPPED, message="stopped"
                )
                return {"ok": False, "error": "stopped", "task_id": tid}
            if attempt > 0:
                self.task_store.set_runtime(
                    tid, status=M.STATUS_RETRYING,
                    message=f"retry {attempt}/{max_retries}",
                    attempt=attempt,
                )
                self.bus.log(f"[{tid}] retry {attempt}/{max_retries}", "warn")
                # fresh sticky key on retry
                time.sleep(min(2 * attempt, 8))
            runner = TaskRunner(
                bus=self.bus,
                hub=self.hub,
                task_store=self.task_store,
                profile_store=self.profile_store,
                proxy_groups=self.proxy_groups,
                captcha=self.captcha,
                settings=self.settings,
                should_stop=self._stop.is_set,
            )
            task["attempt"] = attempt
            last = runner.run(task)
            if last.get("ok"):
                return last
            if last.get("aborted_gate"):
                return last   # seller/MSRP gate rejected it — a skip, not a retry
            attempt += 1
        return last

    def _done(self, tid: str, fut: Future) -> None:
        with self._lock:
            self.state["active_tasks"] = max(0, self.state.get("active_tasks", 1) - 1)
            self._futures.pop(tid, None)
        try:
            fut.result()
        except Exception as e:
            self.bus.log(f"[orch] task {tid} crashed: {e}", "err")
            self.task_store.set_runtime(
                tid, status=M.STATUS_FAILED, last_error=str(e), message=str(e)
            )

    # -- manual single-task run (GUI button) ------------------------------- #
    def run_task_now(self, tid: str) -> bool:
        t = self.task_store.get(tid)
        if not t:
            self.bus.log(f"[orch] unknown task {tid}", "warn")
            return False
        if not self.is_running:
            # light start without full monitor for ad-hoc run
            self.reload_managers()
            self._stop.clear()
            self.state.update(running=True, phase="checkout", active_tasks=0)
            workers = int(
                (self.settings.data.get("orchestrator") or {}).get("max_workers")
                or self.max_workers
            )
            self._pool = ThreadPoolExecutor(
                max_workers=max(1, workers), thread_name_prefix="task"
            )
        self.activate_task(t)
        return True

    def snapshot(self) -> dict:
        return {
            **self.state,
            "proxy_groups": self.proxy_groups.summary(),
            "profiles": len(self.profile_store.list()),
            "tasks": len(self.task_store.list()),
            "enabled_tasks": len(self.task_store.list(enabled_only=True)),
            "monitor_alive": self.monitor.is_running,
        }
