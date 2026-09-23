"""Opt-in hourly maintenance on the API service's recovery-export volume."""
from __future__ import annotations

import logging
import os
import threading
import time

from app.opportunity_cleanup import scheduled_sweep
from app.opportunity_fold_recovery import expire_fold_artifacts

logger = logging.getLogger(__name__)


def run_once(engine, *, stopped=lambda: False, sleep=time.sleep):
    # Expiry remains necessary after cleanup is disabled. A failed sweep must
    # not turn finite recovery exports into an unbounded archive.
    try:
        report = scheduled_sweep(engine, stopped=stopped, sleep=sleep)
        logger.info(
            "srs_cleanup_sweep disabled=%s candidates=%s batches=%s rows_deleted=%s "
            "capped=%s deferred=%s alerts=%s errors=%s",
            report.disabled, report.sweep.candidates, report.sweep.batches,
            report.sweep.rows_deleted, len(report.capped), len(report.deferred),
            len(report.alert_users), len(report.sweep.errors),
        )
        return report
    finally:
        if not stopped():
            expire_fold_artifacts(engine)


class CleanupJob:
    def __init__(self, engine):
        self.engine = engine
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="srs-cleanup", daemon=True)

    def _run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                run_once(self.engine, stopped=self.stop.is_set, sleep=self.stop.wait)
            except Exception:
                logger.exception("srs_cleanup_job_failed")
            # Fixed hourly cadence, no overlapping sweeps or catch-up burst.
            self.stop.wait(max(1, 3600 - (time.monotonic() - started)))

    def shutdown(self):
        self.stop.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            logger.error("srs_cleanup_shutdown_timeout")


def start_cleanup_job(engine):
    if os.environ.get("GHOSTREPLAY_SRS_CLEANUP_JOB_ENABLED", "false").lower() != "true":
        return None
    job = CleanupJob(engine)
    job.thread.start()
    return job
