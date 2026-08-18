import threading
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.control_plane.application.services.control_plane_service import ControlPlaneService

logger = logging.getLogger(__name__)
DEFAULT_SCHEDULER_TIMEZONE = "America/Bogota"
SCHEDULER_TIMEZONE_ENV = "CONTROL_PLANE_TIMEZONE"
CRON_SEARCH_YEARS = 8


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


def _parse_cron_expression(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    if not isinstance(expr, str):
        raise ValueError("cron expression must be text")
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError("cron expression must contain five fields")
    minute = _parse_cron_field(fields[0], 0, 59)
    hour = _parse_cron_field(fields[1], 0, 23)
    dom = _parse_cron_field(fields[2], 1, 31)
    month = _parse_cron_field(fields[3], 1, 12)
    cron_dow = {day % 7 for day in _parse_cron_field(fields[4], 0, 7)}
    return minute, hour, dom, month, cron_dow


def _cron_matches_fields(
    fields: tuple[set[int], set[int], set[int], set[int], set[int]],
    dt: datetime,
) -> bool:
    minute, hour, dom, month, cron_dow = fields
    return dt.minute in minute and dt.hour in hour and dt.month in month and _cron_day_matches(fields, dt)


def _cron_day_matches(
    fields: tuple[set[int], set[int], set[int], set[int], set[int]],
    value,
) -> bool:
    _, _, dom, _, cron_dow = fields
    dom_matches = value.day in dom
    dow_matches = ((value.weekday() + 1) % 7) in cron_dow
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
    return day_matches


def cron_matches(expr: str, dt: datetime) -> bool:
    try:
        return _cron_matches_fields(_parse_cron_expression(expr), dt)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False


def cron_next_run(expr: str, from_dt: datetime, scheduler_timezone: ZoneInfo) -> datetime | None:
    """Return the next matching instant in UTC, evaluated in scheduler_timezone."""
    try:
        fields = _parse_cron_expression(expr)
        base = from_dt
        if base.tzinfo is None:
            base = base.replace(tzinfo=scheduler_timezone)
        base_utc = base.astimezone(timezone.utc)
        local_start = base_utc.astimezone(scheduler_timezone)
        minute_values = sorted(fields[0])
        hour_values = sorted(fields[1])
        month = fields[3]

        def valid_candidate(day, hour, minute):
            candidates = []
            for fold in (0, 1):
                local_candidate = datetime(
                    day.year,
                    day.month,
                    day.day,
                    hour,
                    minute,
                    tzinfo=scheduler_timezone,
                    fold=fold,
                )
                candidate_utc = local_candidate.astimezone(timezone.utc)
                round_trip = candidate_utc.astimezone(scheduler_timezone)
                if (
                    round_trip.year,
                    round_trip.month,
                    round_trip.day,
                    round_trip.hour,
                    round_trip.minute,
                ) != (day.year, day.month, day.day, hour, minute):
                    continue
                if candidate_utc > base_utc:
                    candidates.append(candidate_utc)
            return min(candidates) if candidates else None

        # Gregorian month/day combinations can have an eight-year gap around
        # the non-leap century boundary; use a conservative fixed horizon.
        max_days = CRON_SEARCH_YEARS * 366 + 1
        current_day = local_start.date()
        for day_offset in range(max_days):
            day = current_day + timedelta(days=day_offset)
            if day.month not in month:
                continue
            if not _cron_day_matches(fields, day):
                continue
            for hour in hour_values:
                for minute in minute_values:
                    candidate = valid_candidate(day, hour, minute)
                    if candidate is not None:
                        return candidate
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return None


def resolve_scheduler_timezone(timezone_name: str | None = None) -> tuple[str, ZoneInfo]:
    configured = (timezone_name or DEFAULT_SCHEDULER_TIMEZONE).strip() or DEFAULT_SCHEDULER_TIMEZONE
    try:
        return configured, ZoneInfo(configured)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            f"Invalid {SCHEDULER_TIMEZONE_ENV} value {configured!r}; use a valid IANA timezone such as {DEFAULT_SCHEDULER_TIMEZONE}"
        ) from None


class SchedulerService:
    def __init__(
        self,
        control_plane_service: ControlPlaneService,
        interval_seconds: int = 60,
        timezone_name: str = DEFAULT_SCHEDULER_TIMEZONE,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._service = control_plane_service
        self._interval = interval_seconds
        self._timezone_name, self._timezone = resolve_scheduler_timezone(timezone_name)
        self._now_fn = now_fn or (lambda: datetime.now(self._timezone))
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_check: dict[str, datetime] = {}

    @property
    def timezone_name(self) -> str:
        return self._timezone_name

    def preview(
        self,
        target=None,
        cron_expression: str | None = None,
        settings=None,
        now: datetime | None = None,
        target_context: bool = False,
    ) -> dict:
        settings = settings if settings is not None else self._service.get_settings()
        global_cron = getattr(settings, "global_cron_expression", None) if settings else None
        if cron_expression is None:
            effective_cron = getattr(target, "cron_expression", None) if target else None
            source = "target" if effective_cron else "global" if global_cron else "manual"
        else:
            effective_cron = cron_expression.strip() or None
            source = "target" if effective_cron and (target is not None or target_context) else "global" if effective_cron else "manual"
        if not effective_cron and (cron_expression is None or target is not None or target_context):
            effective_cron = global_cron.strip() if isinstance(global_cron, str) and global_cron.strip() else None
            source = "global" if effective_cron else "manual"

        valid = effective_cron is None or self._is_valid_cron(effective_cron)
        next_run = None
        if valid and effective_cron and (target is None or getattr(target, "enabled", True)):
            current = now or self._now()
            next_run = cron_next_run(effective_cron, current, self._timezone)
        return {
            "effective_cron_expression": effective_cron,
            "cron_source": source,
            "scheduler_timezone": self._timezone_name,
            "cron_valid": valid,
            "next_scheduled_at": self._serialize_datetime(next_run),
        }

    @staticmethod
    def _is_valid_cron(expr: str) -> bool:
        try:
            _parse_cron_expression(expr)
            return True
        except (TypeError, ValueError, OverflowError):
            return False

    @staticmethod
    def _serialize_datetime(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _now(self) -> datetime:
        current = self._now_fn()
        if current.tzinfo is None:
            current = current.replace(tzinfo=self._timezone)
        else:
            current = current.astimezone(self._timezone)
        return current.replace(second=0, microsecond=0)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="cp-scheduler")
        self._thread.start()
        logger.info("Scheduler started (interval=%ss, timezone=%s)", self._interval, self._timezone_name)

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
        now = self._now()
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
