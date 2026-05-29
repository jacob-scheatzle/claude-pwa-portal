"""In-process scheduler for recurring app-tool runs.

A ``ScheduledRun`` row names an app, one of its declared ``tools``, preset
arguments, and a cadence. The portal runs a lightweight asyncio ticker (started
in the FastAPI lifespan) that wakes every ``TICK_SECONDS`` and fires any
schedule whose ``next_run_at`` has passed, then advances it to the next
occurrence. Tools are executed by the same declarative executor the MCP server
uses (``portal.app_tools.run_tool``), so their output is delivered through the
tool's own ``deliver`` action (email / store / share) — no uploaded code runs
server-side.

Design notes:

- **No external scheduler/cron dependency.** Cadence is a small structured set
  (daily / weekly / monthly at a fixed UTC hour:minute); ``compute_next_run``
  derives the next fire time arithmetically.
- **No backfill.** A schedule that came due while the process was down fires
  once on the next tick and is then advanced to the next *future* occurrence —
  it never replays a storm of missed runs.
- **Per-process.** Like the rate-limit counters in ``portal/api.py``, the ticker
  runs in each process; the shipped single-uvicorn-process deployment runs
  exactly one. Running multiple workers would double-fire — see
  docs/deploying.md.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import anyio
from sqlmodel import Session, select

from portal.app_tools import AppToolError, run_tool
from portal.config import settings
from portal.db import engine
from portal.models import App, ScheduledRun

logger = logging.getLogger("portal.scheduler")

TICK_SECONDS = 60
FREQUENCIES = ("daily", "weekly", "monthly")
_RESULT_MAX = 300  # truncate last_result so a verbose error can't bloat the row


def _aware(dt: datetime) -> datetime:
    """Treat naive datetimes (SQLite round-trips drop tzinfo) as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def compute_next_run(
    *,
    frequency: str,
    hour: int,
    minute: int,
    day_of_week: int = 0,
    day_of_month: int = 1,
    after: datetime,
) -> datetime:
    """Return the first UTC datetime strictly after ``after`` matching the cadence."""
    after = _aware(after)
    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))
    base = after.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if frequency == "weekly":
        dow = max(0, min(6, day_of_week))
        candidate = base + timedelta(days=(dow - base.weekday()) % 7)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate

    if frequency == "monthly":
        dom = max(1, min(28, day_of_month))
        candidate = base.replace(day=dom)
        if candidate <= after:
            month = 1 if candidate.month == 12 else candidate.month + 1
            year = candidate.year + (1 if candidate.month == 12 else 0)
            candidate = candidate.replace(year=year, month=month, day=dom)
        return candidate

    # daily (default / unknown frequency)
    candidate = base
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def _find_tool(app_row: App, tool_name: str) -> Optional[dict]:
    for decl in (app_row.tools or []):
        if decl.get("name") == tool_name:
            return decl
    return None


def _run_one(
    db: Session, sched: ScheduledRun, now: datetime, *, advance: bool = True
) -> None:
    """Fire one schedule and record the outcome.

    ``advance=True`` (the ticker) rolls ``next_run_at`` forward to the next
    occurrence; ``advance=False`` (an admin/MCP "run now") leaves the cadence
    untouched so a manual fire doesn't skip the normally-scheduled run.
    """
    status, result = "ok", ""
    try:
        app_row = db.exec(select(App).where(App.slug == sched.app_slug)).first()
        if app_row is None or not app_row.enabled:
            raise AppToolError(f"app '{sched.app_slug}' not found or disabled")
        decl = _find_tool(app_row, sched.tool_name)
        if decl is None:
            raise AppToolError(
                f"tool '{sched.tool_name}' no longer exists on '{sched.app_slug}'"
            )
        out = run_tool(
            slug=sched.app_slug,
            tool=decl,
            args=dict(sched.args or {}),
            user_id=sched.user_id,
            host=settings.site_url,
        )
        result = f"delivered={out.get('delivered', '?')}"
    except AppToolError as e:
        status, result = "error", str(e)
    except Exception as e:  # never let one bad schedule wedge the ticker
        logger.exception("scheduled run failed (id=%s)", sched.id)
        status, result = "error", f"{type(e).__name__}: {e}"

    sched.last_run_at = now
    sched.last_status = status
    sched.last_result = result[:_RESULT_MAX]
    if advance:
        sched.next_run_at = compute_next_run(
            frequency=sched.frequency,
            hour=sched.hour,
            minute=sched.minute,
            day_of_week=sched.day_of_week,
            day_of_month=sched.day_of_month,
            after=now,
        )
    db.add(sched)
    db.commit()


def fire_schedule(sched_id: int, *, advance: bool = False) -> dict:
    """Run one schedule by id immediately (admin "run now" / MCP run_schedule).

    Blocking — call from a worker thread / threadpooled handler. ``advance``
    defaults False so a manual trigger records ``last_*`` without disturbing the
    cadence. Returns ``{found, status, result}``.
    """
    now = _aware(datetime.now(timezone.utc))
    with Session(engine) as db:
        sched = db.get(ScheduledRun, sched_id)
        if sched is None:
            return {"found": False, "status": "", "result": ""}
        _run_one(db, sched, now, advance=advance)
        return {"found": True, "status": sched.last_status, "result": sched.last_result}


def run_due_schedules(now: Optional[datetime] = None) -> int:
    """Fire every enabled schedule whose ``next_run_at`` has passed.

    Synchronous and blocking (WeasyPrint / SMTP / disk) — call it from a worker
    thread, never on the event loop. Each schedule commits independently so one
    failure can't lose the others' advanced state. Returns the number fired.
    """
    now = _aware(now or datetime.now(timezone.utc))
    ran = 0
    with Session(engine) as db:
        rows = db.exec(
            select(ScheduledRun).where(ScheduledRun.enabled == True)  # noqa: E712
        ).all()
        for sched in rows:
            if _aware(sched.next_run_at) > now:
                continue
            ran += 1
            _run_one(db, sched, now)
    return ran


async def scheduler_loop(tick_seconds: int = TICK_SECONDS) -> None:
    """Background ticker; started in the FastAPI lifespan, cancelled on shutdown."""
    logger.info("scheduler started (tick=%ss)", tick_seconds)
    try:
        while True:
            try:
                fired = await anyio.to_thread.run_sync(run_due_schedules)
                if fired:
                    logger.info("scheduler fired %d run(s)", fired)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(tick_seconds)
    except asyncio.CancelledError:
        logger.info("scheduler stopped")
        raise
