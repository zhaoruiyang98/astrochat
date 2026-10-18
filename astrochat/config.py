"""Runtime settings for one sync job, built from the user's saved project."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SyncConfig:
    owner: str
    project_number: int
    channel: str
    state_file: Path
    owner_type: str = "user"
    interval_seconds: int = 900
    project_id: str | None = None
    channel_id: int | None = None
