import threading
import time
import logging
from datetime import datetime, timedelta, timezone

from src.control_plane.application.services.control_plane_service import ControlPlaneService

logger = logging.getLogger(__name__)


def _parse_cron_field(expr: str, min_val: int, max_val: int) -> set:
    result = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            step = int(step_str)
        else:
            base = part
        if base == "*":
            lo, hi = min_val, max_val
        elif "-" in base:
            lo_str, hi_str = base.split("-", 1)
            lo, hi = int(lo_str), int(hi_str)
        else:
            lo = hi = int(base)
        for v in range(lo, hi + 1, step):
            result.add(v)
    return result


def cron_matches(expr: str, dt: datetime) -> bool:
    fields = expr.split()
    if len(fields) != 5:
        return False
    minute = _parse_cron_field(fields[0], 0, 59)
    hour = _parse_cron_field(fields[1], 0, 23)
    dom = _parse_cron_field(fields[2], 1, 31)
    month = _parse_cron_field(fields[3], 1, 12)
    dow = _parse_cron_field(fields[4], 0, 6)
    cron_dow = {d % 7 for d in dow}
    return (
        dt.minute in minute
        and dt.hour in hour
        and dt.day in dom
        and dt.month in month
        and dt.weekday() in cron_dow
    )


class SchedulerService:
    def __init__(self, control_plane_service: ControlPlaneService, interval_seconds: int = 60):
        self._service = control_plane_service
        self._interval = interval_seconds
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_check: dict[str, datetime] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="cp-scheduler")
        self._thread.start()
        logger.info("Scheduler started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Scheduler stopped")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("Scheduler tick failed")
            self._stop_event.wait(self._interval)

    def _tick(self) -> None:
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        settings = self._service.get_settings()
        global_cron = settings.global_cron_expression if settings else None
        targets = self._service.list_targets()
        logger.debug("Scheduler tick: now=%s, targets=%d, global_cron=%s", now, len(targets), global_cron or "none")
        for target in targets:
            if not target.enabled:
                continue
            cron = target.cron_expression or global_cron
            if not cron:
                continue
            try:
                if cron_matches(cron, now):
                    last = self._last_check.get(target.id)
                    if last and (now - last) < timedelta(minutes=1):
                        continue
                    self._last_check[target.id] = now
                    source = "target cron" if target.cron_expression else "global cron"
                    logger.info("Dispatching scheduled backup for target %s (%s=%s)", target.id, source, cron)
                    self._service.dispatch_backup_for_target(
                        target.id,
                        requested_by="scheduler",
                        trigger="schedule",
                    )
            except Exception:
                logger.exception("Error evaluating cron for target %s", target.id)