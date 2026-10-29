# ─────────────────────────────────────────────────────────────────────────────
# Rausch PT — Tebra calendar sync (Docker service: scheduler-tebra-sync)
#
# Keeps the Supabase copy of the Tebra calendar (table tebra_appointments) fresh for
# multi-provider availability:
#   - days 0-14  every TEBRA_SYNC_FAST_MINUTES (default 10)
#   - days 15-60 every TEBRA_SYNC_SLOW_MINUTES (default 60)
#   - removes rows that ended before today
# Respects Tebra's 1 GetAppointments call/second (see app/services/tebra_service.py).
# Logs: /code/logs/scheduler_tebra_sync.log (+ SOAP detail in logs/tebra_debug.log)
#
# One-off run (local check):  python scheduler_tebra_sync.py --once
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

from app.core.config import AVAILABILITY_WINDOW_DAYS  # noqa: E402
from app.services import tebra_sync  # noqa: E402

FAST_MINUTES = int(os.getenv("TEBRA_SYNC_FAST_MINUTES", "10"))
SLOW_MINUTES = int(os.getenv("TEBRA_SYNC_SLOW_MINUTES", "60"))


def _setup_logger() -> logging.Logger:
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger("scheduler_tebra_sync")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = logging.FileHandler("logs/scheduler_tebra_sync.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


logger = _setup_logger()

# Fast and slow runs never overlap (they share the Tebra rate limit anyway)
_RUN_LOCK = asyncio.Lock()


def _summary(res: dict) -> str:
    top = ", ".join(f"{p}:{n}" for p, n in sorted(res["per_provider"].items(), key=lambda x: (x[0] is None, x[0] or 0)))
    return (f"window={res['window'][0]}..{res['window'][1]} rows={res['rows']} ok={res['ok']} "
            f"chunks_ok={res['chunks_ok']} failed={res['chunks_failed']} per_provider={{{top}}}")


async def job_fast():
    async with _RUN_LOCK:
        rid = "sf-" + datetime.now(timezone.utc).strftime("%H%M%S")
        try:
            res = await tebra_sync.sync_fast(rid)
            logger.info("[%s] fast sync %s", rid, _summary(res))
            removed = await tebra_sync.cleanup_past(rid)
            logger.info("[%s] cleanup removed=%s", rid, removed)
        except Exception as e:
            logger.exception("[%s] fast sync crashed: %s", rid, e)


async def job_slow():
    async with _RUN_LOCK:
        rid = "ss-" + datetime.now(timezone.utc).strftime("%H%M%S")
        try:
            res = await tebra_sync.sync_slow(rid)
            logger.info("[%s] slow sync %s", rid, _summary(res))
        except Exception as e:
            logger.exception("[%s] slow sync crashed: %s", rid, e)


async def run_once() -> int:
    await job_fast()
    await job_slow()
    return 0


async def main():
    missing = [k for k in ("SUPABASE_URL", "SUPABASE_API_KEY", "CUSTOMER_KEY", "TEBRA_USER", "TEBRA_PASSWORD")
               if not os.getenv(k)]
    if missing:
        logger.error("STARTUP ERROR missing env vars: %s", missing)
        return

    logger.info("[scheduler_tebra_sync] starting fast=every %smin (days 0-14) slow=every %smin (days 15-%s)",
                FAST_MINUTES, SLOW_MINUTES, AVAILABILITY_WINDOW_DAYS)

    now = datetime.now(timezone.utc)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(job_fast, "interval", minutes=FAST_MINUTES, id="tebra_sync_fast",
                      next_run_time=now, max_instances=1, coalesce=True)
    scheduler.add_job(job_slow, "interval", minutes=SLOW_MINUTES, id="tebra_sync_slow",
                      next_run_time=now + timedelta(seconds=30), max_instances=1, coalesce=True)
    scheduler.start()

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        scheduler.shutdown()
        logger.info("[scheduler_tebra_sync] stopped")


if __name__ == "__main__":
    if "--once" in sys.argv:
        sys.exit(asyncio.run(run_once()))
    asyncio.run(main())
