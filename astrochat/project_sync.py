"""Mirror GitHub discussions; checkpoint only confirmed Zulip operations."""

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event
from typing import Any

import zulip

from astrochat.config import SyncConfig
from astrochat.github import Discussion, GitHubClient, Task
from astrochat.image_sync import ImageSync, ImageSyncCancelled
from astrochat.markdown import convert_github_markdown, escape_text, split_message
from astrochat.sync_state import MessageState, Observation, SyncState

logger = logging.getLogger(__name__)


class ZulipError(RuntimeError):
    pass


def checked(response: dict[str, Any]) -> dict[str, Any]:
    if response["result"] != "success":
        raise ZulipError(f"{response.get('code', response['result'])}: {response['msg']}")
    return response


def topic_name(task: Task, project_id: str | None = None) -> str:
    # The suffix stays stable across title edits and distinguishes tasks/projects.
    identity = f"{project_id}:{task.id}" if project_id is not None else task.id
    suffix = " · " + hashlib.sha256(identity.encode()).hexdigest()[:10]
    prefix = "✔ " if (task.status or "").strip().casefold() == "done" else ""
    title = " ".join(task.title.split()).removeprefix("✔ ")
    return prefix + title[: 60 - len(prefix) - len(suffix)].rstrip() + suffix


def render(task: Task, entry: Discussion, image_urls: dict[str, str] | None = None) -> str:
    heading = f"**{escape_text(entry.kind)}** · {escape_text(entry.author)} · {escape_text(entry.created_at)}"
    if entry.id == task.id:
        heading += f"\n**{escape_text(task.title)}**"
        heading += f"\nProject status: {escape_text(task.status or '(unset)')}"
        heading += f"\nAssignees: {escape_text(', '.join(task.assignees) or '(unassigned)')}"
    if entry.is_event:
        body = escape_text(entry.body)
        if entry.status_change is not None:
            previous, current = entry.status_change
            transition = f"**{escape_text(previous)}** → **{escape_text(current)}**"
            body = transition + (f"\n{body}" if body else "")
    else:
        body = (
            convert_github_markdown(entry.body, entry.url, image_urls)
            if entry.body
            else "*(empty)*"
        )
    return f"{heading}\n[View on GitHub]({entry.url})\n\n{body}"


@dataclass
class SyncResult:
    tasks: int = 0
    sent: int = 0
    updated: int = 0
    deleted: int = 0
    renamed: int = 0
    skipped: int = 0
    failed: int = 0

    def summary(self) -> str:
        return (
            f"tasks={self.tasks}, sent={self.sent}, updated={self.updated}, "
            f"deleted={self.deleted}, renamed={self.renamed}, "
            f"skipped={self.skipped}, failed={self.failed}"
        )


class ProjectSync:
    def __init__(
        self, config: SyncConfig, github: GitHubClient, client: zulip.Client, state: SyncState
    ) -> None:
        self.config = config
        self.github = github
        self.client = client
        self.state = state

    def run_once(self, stop: Event | None = None) -> SyncResult:
        project = self.github.project(
            self.config.owner, self.config.project_number, self.config.owner_type
        )
        if self.config.project_id is not None and project["id"] != self.config.project_id:
            raise ValueError("GitHub Project identity changed; authorize the project again")
        channel_id = self.config.channel_id
        if channel_id is None:
            channel_id = checked(self.client.get_stream_id(self.config.channel))["stream_id"]
        self.state.bind(
            {
                "project": project["id"],
                "site": self.client.base_url,
                "bot": self.client.email,
                "channel": channel_id,
            }
        )
        tasks, skipped = self.github.tasks(project)
        result = SyncResult(skipped=skipped)
        for task in tasks:
            if stop is not None and stop.is_set():
                break
            try:
                # Fetch all comment/review/thread pages before any changes to this task.
                entries = self.github.discussions(task)
                self.sync_task(task, entries, channel_id, result, stop)
                if stop is not None and stop.is_set():
                    break
                result.tasks += 1
            except ImageSyncCancelled:
                break
            except Exception:
                # Isolate a failed task; preserve its mappings and the original traceback.
                logger.exception("Could not sync GitHub task %s", task.id)
                result.failed += 1
        logger.info("GitHub sync%s: %s", " preview" if self.state.dry_run else "", result.summary())
        return result

    def sync_task(
        self,
        task: Task,
        entries: list[Discussion],
        channel_id: int,
        result: SyncResult,
        stop: Event | None = None,
    ) -> None:
        if stop is not None and stop.is_set():
            return
        # Prepare every image before changing this task's discussion. A failed
        # download/upload preserves its existing messages, including deletions.
        image_urls = ImageSync(self.github, self.client, self.state).prepare(
            [entry for entry in entries if not entry.is_event], task.reference, stop
        )
        if stop is not None and stop.is_set():
            return
        messages = self.state.messages_for(task.id)
        topic = self.sync_topic(task, messages, channel_id, result)
        current_keys = set()
        for entry in entries:
            for part, content in enumerate(split_message(render(task, entry, image_urls))):
                if stop is not None and stop.is_set():
                    return
                key = f"{'event:' if entry.is_event else ''}{entry.id}:{part}"
                current_keys.add(key)
                self.sync_message(task.id, messages, key, content, channel_id, topic, result)
        for key in list(messages):
            if stop is not None and stop.is_set():
                return
            # Timeline entries form an audit history, not deletable comments.
            if key not in current_keys and not key.startswith("event:"):
                if not self.state.dry_run:
                    checked(self.client.delete_message(messages[key].id))
                self.state.delete_message(task.id, key)
                del messages[key]
                result.deleted += 1
        if task.type == "DraftIssue" and (stop is None or not stop.is_set()):
            self.sync_observation(task, messages, channel_id, topic, result)

    def sync_observation(
        self,
        task: Task,
        messages: dict[str, MessageState],
        channel_id: int,
        topic: str,
        result: SyncResult,
    ) -> None:
        # DraftIssue has no timeline API: report only changes observed by polling.
        previous = self.state.observation_for(task.id)
        assignees = tuple(sorted(task.assignees))
        if previous is None:
            self.state.save_observation(task.id, Observation(task.status, assignees, 0))
            return
        changes = []
        status_change = None
        if previous.status != task.status:
            status_change = (previous.status or "(unset)", task.status or "(unset)")
        if previous.assignees != assignees:
            changes.append(
                f"Assignees: {', '.join(previous.assignees) or '(unassigned)'} → "
                f"{', '.join(assignees) or '(unassigned)'}"
            )
        if not changes and status_change is None:
            return
        sequence = previous.sequence + 1
        entry = Discussion(
            f"observation:{sequence}",
            "\n".join(changes),
            task.description.url,
            "AstroChat",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "Observed draft change (polling time; actor unavailable)",
            True,
            status_change=status_change,
        )
        for part, content in enumerate(split_message(render(task, entry))):
            self.sync_message(
                task.id, messages, f"event:{entry.id}:{part}", content, channel_id, topic, result
            )
        self.state.save_observation(task.id, Observation(task.status, assignees, sequence))

    def sync_topic(
        self, task: Task, messages: dict[str, MessageState], channel_id: int, result: SyncResult
    ) -> str:
        desired = topic_name(task, self.config.project_id)
        previous = self.state.topic_for(task.id, desired)
        if previous == desired:
            return previous
        if messages:
            if not self.state.dry_run:
                anchor_id = min(message.id for message in messages.values())
                self.rename_topic(anchor_id, channel_id, previous, desired)
            result.renamed += 1
        # With no saved messages, there is no existing discussion to move. This
        # also handles a changed title after the very first send failed.
        self.state.save_topic(task.id, desired)
        return desired

    def rename_topic(self, anchor_id: int, channel_id: int, previous: str, desired: str) -> None:
        anchor = checked(self.client.call_endpoint(f"messages/{anchor_id}", method="GET"))[
            "message"
        ]
        if anchor["type"] != "stream" or anchor["stream_id"] != channel_id:
            raise ZulipError("Mirrored message moved to another channel; cannot rename its topic.")
        if anchor["subject"] == desired:
            # The previous request may have succeeded before its response or
            # SQLite checkpoint was lost. Reconcile without another edit.
            return
        if anchor["subject"] != previous:
            raise ZulipError("Mirrored message moved to another topic; cannot rename its topic.")
        if previous.casefold() != desired.casefold():
            destination = checked(
                self.client.get_messages(
                    {
                        "narrow": [
                            {"operator": "stream", "operand": channel_id},
                            {"operator": "topic", "operand": desired},
                        ],
                        "anchor": "newest",
                        "num_before": 1,
                        "num_after": 0,
                        "apply_markdown": False,
                    }
                )
            )["messages"]
            if destination:
                raise ZulipError("Target topic already exists; refusing to merge discussions.")
        checked(
            self.client.update_message(
                {
                    "message_id": anchor_id,
                    "topic": desired,
                    "propagate_mode": "change_all",
                    "send_notification_to_old_thread": False,
                    "send_notification_to_new_thread": False,
                }
            )
        )

    def sync_message(
        self,
        task_id: str,
        messages: dict[str, MessageState],
        key: str,
        content: str,
        channel_id: int,
        topic: str,
        result: SyncResult,
    ) -> None:
        digest = hashlib.sha256(content.encode()).hexdigest()
        previous = messages.get(key)
        if previous is not None and previous.digest == digest:
            return
        if previous is None:
            message_id = None
            if not self.state.dry_run:
                response = checked(
                    self.client.send_message(
                        {
                            "type": "stream",
                            "to": channel_id,
                            "topic": topic,
                            "content": content,
                        }
                    )
                )
                message_id = response["id"]
            result.sent += 1
        else:
            message_id = previous.id
            if not self.state.dry_run:
                checked(self.client.update_message({"message_id": message_id, "content": content}))
            result.updated += 1
        message = MessageState(message_id, digest)
        self.state.save_message(task_id, key, message)
        messages[key] = message
