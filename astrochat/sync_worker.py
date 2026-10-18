"""Schedule independent, user-owned projects without blocking the chat listener."""

import logging
import time
from datetime import datetime, timezone
from threading import Event, Lock, Thread

from astrochat.project_store import ProjectStore

logger = logging.getLogger(__name__)


class SyncWorker:
    def __init__(self, store: ProjectStore, run_project) -> None:
        self.store = store
        self.run_project = run_project
        self.stop = Event()
        self.wake = Event()
        self.lock = Lock()
        self.active_id: str | None = None
        self.active_stop: Event | None = None
        self.thread = Thread(target=self.run, name="github-project-sync", daemon=True)

    def changed(self, project_id: str) -> None:
        with self.lock:
            if self.active_id == project_id and self.active_stop is not None:
                self.active_stop.set()
        self.wake.set()

    def run(self) -> None:
        while not self.stop.is_set():
            self.wake.clear()
            # Claim and publish the active job together, so a concurrent configuration
            # change can always cancel it. Database locks are released before API work.
            with self.lock:
                if self.stop.is_set():
                    break
                project = self.store.claim_due(time.time())
                job_stop = Event()
                self.active_id = project.id if project else None
                self.active_stop = job_stop
            if project is None:
                self.wake.wait(1)
                continue
            try:
                result = self.run_project(project, job_stop)
                timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                status = f"Last sync ({timestamp}): {result.summary()}"
            except Exception as error:
                # Credentials and upstream response bodies must not reach shared logs
                # or another user's status. Keep the project ID and exception type.
                logger.error("Project %s sync failed (%s)", project.id, type(error).__name__)
                status = (
                    "Sync failed. Check authorization and channel membership; polling will retry."
                )
            with self.lock:
                self.store.finish(project, status, time.time(), cancelled=job_stop.is_set())
                self.active_id = None
                self.active_stop = None

    def run_saved_once(self, dry_run: bool = False) -> int:
        failures = 0
        for project in self.store.enabled_projects():
            try:
                result = self.run_project(project, self.stop, dry_run=dry_run)
                failures += result.failed
                logger.info(
                    "Project %s%s: %s",
                    project.name,
                    " preview" if dry_run else "",
                    result.summary(),
                )
            except Exception as error:
                logger.error("Project %s sync failed (%s)", project.id, type(error).__name__)
                failures += 1
        return int(failures > 0)

    def close(self) -> None:
        self.stop.set()
        with self.lock:
            if self.active_stop is not None:
                self.active_stop.set()
        self.wake.set()
        # Finish the in-flight API operation and checkpoint before closing the registry.
        self.thread.join()
