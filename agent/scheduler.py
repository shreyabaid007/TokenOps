"""APScheduler entry point for the optimizer agent.

Runs once on startup (so the demo sees immediate output without waiting
for the first tick) then every AGENT_RUN_INTERVAL_MINUTES via a
BlockingScheduler. Logs the summary returned by run_optimizer at INFO on
each completion — the durable record of what the agent decided lives in
the agent_decisions Postgres table.

Run as its own process:
    python agent/scheduler.py

Stops cleanly on Ctrl+C.
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from agent.graph import run_optimizer
from proxy.config import settings


logger = logging.getLogger("agent.scheduler")


def _setup_logging() -> None:
    """Plain-text logging for the scheduler process. The proxy emits JSON
    to stdout for log aggregators; the agent runs in a terminal for the
    demo, where plain text is more readable."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _safe_run() -> None:
    """Wrap run_optimizer so an exception in one tick does not kill the
    scheduler. The next tick starts fresh."""
    try:
        summary = run_optimizer()
        logger.info("scheduled run summary: %s", summary)
    except Exception:
        logger.exception("scheduled optimizer run failed")


def main() -> None:
    _setup_logging()
    interval_min = settings.agent_run_interval_minutes
    logger.info("agent scheduler starting (interval=%d min)", interval_min)

    # Immediate startup run — same wrapper so the same error handling applies.
    _safe_run()

    scheduler = BlockingScheduler()
    scheduler.add_job(
        _safe_run,
        trigger="interval",
        minutes=interval_min,
        id="optimizer",
        # max_instances=1 + coalesce keeps overlapping ticks from piling up
        # if a single run takes longer than the interval.
        max_instances=1,
        coalesce=True,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler stopped")


if __name__ == "__main__":
    main()
