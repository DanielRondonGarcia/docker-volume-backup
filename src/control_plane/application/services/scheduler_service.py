import threading
import logging
from datetime import datetime, timedelta, timezone

from src.control_plane.application.services.control_plane_service import ControlPlaneService

logger = logging.getLogger(__name__)


def _parse_cron_field(expr: str, min_val: int, max_val: int) -> set[int]:
    result: set[int] = set()
    for raw_part in expr.split(","):
        part = raw_part.strip()
        if not part or part.count("/") > 1:
            raise ValueError("invalid cron field")
        if "/" in part:
            base, step_text = part.split("/", 1)
            step = int(step_text)
            if step <= 0:
                raise ValueError("cron step must be positive")
        else:
            base, step = part, 1

        if base == "*":
            lo, hi = min_val, max_val
        elif base.count("-") == 1:
            lo_text, hi_text = base.split("-", 1)
            lo, hi = int(lo_text), int(hi_text)
        else:
            lo = int(base)
            hi = max_val if "/" in part else lo
        if lo < min_val or hi > max_val or lo > hi:
            raise ValueError("cron field value out of range")
        result.update(range(lo, hi + 1, step))

    if not result:
        raise ValueError("empty cron field")
    return result


def cron_matches(expr: str, dt: datetime) -> bool:
    if not isinstance(expr, str):
        return False
    try:
        fields = expr.split()
        if len(fields) != 5:
            return False
        minute = _parse_cron_field(fields[0], 0, 59)
        hour = _parse_cron_field(fields[1], 0, 23)
        dom = _parse_cron_field(fields[2], 1, 31)
        month = _parse_cron_field(fields[3], 1, 12)
        cron_dow = {day % 7 for day in _parse_cron_field(fields[4], 0, 7)}
        dom_matches = dt.day in dom
        dow_matches = ((dt.weekday() + 1) % 7) in cron_dow
        dom_unrestricted = dom == set(range(1, 32))
        dow_unrestricted = cron_dow == set(range(0, 7))
        if not dom_unrestricted and not dow_unrestricted:
            day_matches = dom_matches or dow_matches
        elif not dom_unrestricted:
            day_matches = dom_matches
        elif not dow_unrestricted:
            day_matches = dow_matches
        else:
            day_matches = True
        return dt.minute in minute and dt.hour in hour and dt.month in month and day_matches
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False


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
                    if not self._service.is_worker_eligible(target.worker_id):
                        logger.info(
                            "Skipping scheduled backup for target %s: worker %s is not eligible",
                            target.id,
                            target.worker_id,
                        )
                        self._last_check[target.id] = now
                        continue
                    if self._service.has_active_backup_for_target(target.id):
                        logger.info("Skipping scheduled backup for target %s: backup already pending or active", target.id)
                        self._last_check[target.id] = now
                        continue
                    self._last_check[target.id] = now
                    source = "target cron" if target.cron_expression else "global cron"
                    logger.info("Dispatching scheduled backup for target %s (%s=%s)", target.id, source, cron)
                    self._service.dispatch_backup_for_target(
                        target.id,
                        requested_by="scheduler",
                        trigger="schedule",
                    )
            except ValueError as exc:
                logger.info("Skipping scheduled backup for target %s: %s", target.id, exc)
            except Exception:
                logger.exception("Error evaluating cron for target %s", target.id)
