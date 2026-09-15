import asyncio
from typing import Callable, Coroutine

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

log = structlog.get_logger()


class BotScheduler:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()

    def add_job(
        self,
        func: Callable[..., Coroutine],
        interval_seconds: int,
        job_id: str,
        start_delay_seconds: int = 5,
    ) -> None:
        self._scheduler.add_job(
            func,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id=job_id,
            name=job_id,
            replace_existing=True,
            misfire_grace_time=30,
            max_instances=1,
        )
        log.info("job_added", job_id=job_id, interval=interval_seconds)

    def start(self) -> None:
        self._scheduler.start()
        log.info("scheduler_started")

    def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        log.info("scheduler_stopped")

    def pause_job(self, job_id: str) -> None:
        self._scheduler.pause_job(job_id)
        log.info("job_paused", job_id=job_id)

    def resume_job(self, job_id: str) -> None:
        self._scheduler.resume_job(job_id)
        log.info("job_resumed", job_id=job_id)
